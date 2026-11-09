"""'Uninstall apps' window: pick several apps, delete them permanently and
watch each one go.

Plain AppKit through PyObjC (rumps can't do windows). Cell-based table:
checkbox · icon · name · size (app + leftovers) · last used / status.
Work runs on worker threads; every UI change goes through
`AppHelper.callAfter` (main thread).
"""

import threading
import time

import objc
from AppKit import (
    NSAlert, NSAlertFirstButtonReturn, NSApp, NSBackingStoreBuffered,
    NSBezelBorder, NSButton, NSButtonCell, NSColor, NSFont, NSImageCell,
    NSMakeRect, NSProgressIndicator, NSScrollView, NSSearchField,
    NSSortDescriptor, NSTableColumn, NSTableView, NSTextAlignmentRight, NSTextField,
    NSTextFieldCell,
    NSViewHeightSizable, NSViewMaxYMargin, NSViewMinXMargin, NSViewMinYMargin,
    NSViewWidthSizable, NSWindow, NSWorkspace,
)
from Foundation import NSAttributedString, NSDictionary, NSObject
from PyObjCTools import AppHelper

from . import app_removal, insights
from .utils import format_size

W, H = 760, 560
_STYLE = 1 | 2 | 4 | 8          # titled, closable, miniaturizable, resizable

_STATE_TEXT = {
    app_removal.QUITTING: "Quitting…",
    app_removal.DELETING: "Deleting…",
    app_removal.NEEDS_PASSWORD: "Waiting for your password…",
    app_removal.WAITING: "Waiting…",
}


def _last_used_text(app) -> str:
    if app.running:
        return "Open now"
    if not app.last_used:
        return "Unknown"
    days = int((time.time() - app.last_used) / 86400)
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


def _colored(text: str, color) -> NSAttributedString:
    attrs = NSDictionary.dictionaryWithObject_forKey_(color, "NSColor")
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


class _CenteredCell(NSTextFieldCell):
    """Text cell that sits in the vertical middle of a tall row."""

    def drawingRectForBounds_(self, rect):
        r = objc.super(_CenteredCell, self).drawingRectForBounds_(rect)
        h = self.cellSizeForBounds_(rect).height
        if h < r.size.height:
            return NSMakeRect(r.origin.x, r.origin.y + (r.size.height - h) / 2, r.size.width, h)
        return r


class _Row:
    def __init__(self, app):
        self.app = app
        self.checked = False
        self.state = None          # None until the uninstall touches it
        self.detail = ""
        self.icon = NSWorkspace.sharedWorkspace().iconForFile_(str(app.path))
        self.icon.setSize_((22, 22))


class UninstallWindow(NSObject):

    @objc.python_method
    def setup(self, preselect=None, on_close=None):
        """Use as UninstallWindow.alloc().init().setup(paths, callback)."""
        self.preselect = set(preselect or [])
        self.on_close = on_close
        self.rows, self.visible = [], []
        self.sort_key, self.ascending = "name", True
        self.running_job = False
        self.finished = False
        self._build()
        self._load()
        return self

    # ---------------------------------------------------------------- layout

    @objc.python_method
    def _build(self):
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), _STYLE, NSBackingStoreBuffered, False)
        win.setTitle_("Uninstall apps")
        win.setMinSize_((620, 420))
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        win.center()
        cv = win.contentView()

        intro = NSTextField.labelWithString_(
            "Select the apps you want to remove. Each app is deleted for good, "
            "together with its settings, caches and other leftovers.")
        intro.setFrame_(NSMakeRect(20, H - 46, W - 280, 32))
        intro.setTextColor_(NSColor.secondaryLabelColor())
        intro.cell().setWraps_(True)
        intro.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        cv.addSubview_(intro)

        search = NSSearchField.alloc().initWithFrame_(NSMakeRect(W - 240, H - 42, 220, 24))
        search.setPlaceholderString_("Search apps")
        search.setTarget_(self)
        search.setAction_("searchChanged:")
        search.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        cv.addSubview_(search)
        self.search = search

        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 40, H - 140))
        table.setRowHeight_(30)
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsColumnReordering_(False)
        table.setAllowsEmptySelection_(True)
        table.setIntercellSpacing_((8, 2))

        def col(ident, title, width, cell=None, sort=None, align=0):
            c = NSTableColumn.alloc().initWithIdentifier_(ident)
            c.headerCell().setStringValue_(title)
            c.setWidth_(width)
            if cell is None:
                cell = _CenteredCell.alloc().init()
                cell.setFont_(NSFont.systemFontOfSize_(13))
                cell.setAlignment_(align)
                cell.setLineBreakMode_(4)           # truncate tail
            c.setDataCell_(cell)
            c.setEditable_(ident == "check")
            if sort:
                c.setSortDescriptorPrototype_(
                    NSSortDescriptor.sortDescriptorWithKey_ascending_(sort, sort == "name"))
            table.addTableColumn_(c)
            return c

        check = NSButtonCell.alloc().init()
        check.setButtonType_(3)                     # switch (checkbox)
        check.setTitle_("")
        col("check", "", 24, cell=check).setResizingMask_(0)
        col("icon", "", 26, cell=NSImageCell.alloc().init()).setResizingMask_(0)
        col("name", "App", 300, sort="name")
        col("size", "Size", 100, sort="size", align=NSTextAlignmentRight)
        col("last", "Last used", 210, sort="last")
        table.setDataSource_(self)
        table.setDelegate_(self)
        table.setDoubleAction_("rowDoubleClicked:")
        table.setTarget_(self)
        self.table = table

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 70, W - 40, H - 128))
        scroll.setDocumentView_(table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        cv.addSubview_(scroll)

        summary = NSTextField.labelWithString_("Loading apps…")
        summary.setFrame_(NSMakeRect(20, 30, W - 240, 20))
        summary.setFont_(NSFont.systemFontOfSize_(13))
        summary.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        cv.addSubview_(summary)
        self.summary = summary

        bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(20, 14, W - 240, 10))
        bar.setStyle_(0)                            # bar
        bar.setIndeterminate_(True)
        bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        bar.startAnimation_(None)
        cv.addSubview_(bar)
        self.bar = bar

        button = NSButton.alloc().initWithFrame_(NSMakeRect(W - 200, 16, 180, 32))
        button.setBezelStyle_(1)                    # rounded push button
        button.setTitle_("Uninstall")
        button.setKeyEquivalent_("\r")
        button.setTarget_(self)
        button.setAction_("uninstallClicked:")
        button.setEnabled_(False)
        if hasattr(button, "setHasDestructiveAction_"):
            button.setHasDestructiveAction_(True)
        button.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        cv.addSubview_(button)
        self.button = button

        self.window = win

    @objc.python_method
    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    # ---------------------------------------------------------------- data

    @objc.python_method
    def _load(self):
        def work():
            apps = insights.installed_apps()
            AppHelper.callAfter(self._loaded, apps)
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _loaded(self, apps):
        self.rows = [_Row(a) for a in apps]
        for r in self.rows:
            r.checked = str(r.app.path) in self.preselect
        self.bar.stopAnimation_(None)
        self.bar.setHidden_(True)
        self._refilter()

    @objc.python_method
    def _refilter(self):
        q = str(self.search.stringValue() or "").strip().lower()
        rows = [r for r in self.rows if not q or q in r.app.name.lower()]
        key = {
            "name": lambda r: r.app.name.lower(),
            "size": lambda r: r.app.total_size,
            "last": lambda r: r.app.last_used or 0,
        }[self.sort_key]
        rows.sort(key=key, reverse=not self.ascending)
        self.visible = rows
        self.table.reloadData()
        self._update_summary()

    @objc.python_method
    def _selected(self):
        return [r for r in self.rows if r.checked]

    @objc.python_method
    def _update_summary(self):
        if self.running_job or self.finished:
            return
        sel = self._selected()
        if not sel:
            self.summary.setStringValue_(f"{len(self.rows)} apps · none selected")
            self.button.setTitle_("Uninstall")
            self.button.setEnabled_(False)
            return
        total = sum(r.app.total_size for r in sel)
        n = len(sel)
        self.summary.setStringValue_(
            f"{n} app{'s' if n != 1 else ''} selected · {format_size(total)} will be freed")
        self.button.setTitle_(f"Uninstall {n} app{'s' if n != 1 else ''}")
        self.button.setEnabled_(True)

    # ---------------------------------------------------------------- table

    def numberOfRowsInTableView_(self, table):
        return len(self.visible)

    def tableView_objectValueForTableColumn_row_(self, table, column, i):
        r = self.visible[i]
        ident = column.identifier()
        if ident == "check":
            return 1 if r.checked else 0
        if ident == "icon":
            return r.icon
        if ident == "name":
            return r.app.name
        if ident == "size":
            return format_size(r.app.total_size)
        if ident == "last":
            return self._status(r)
        return ""

    @objc.python_method
    def _status(self, r):
        if r.state is None:
            text = _last_used_text(r.app)
            color = NSColor.systemOrangeColor() if r.app.running else NSColor.secondaryLabelColor()
            return _colored(text, color)
        if r.state == app_removal.DONE:
            return _colored("✓ Deleted" + (f" · {r.detail}" if r.detail else ""),
                            NSColor.systemGreenColor())
        if r.state == app_removal.IN_TRASH:
            return _colored(r.detail, NSColor.systemOrangeColor())
        if r.state == app_removal.FAILED:
            return _colored("✕ " + r.detail, NSColor.systemRedColor())
        return _colored(_STATE_TEXT.get(r.state, r.detail), NSColor.labelColor())

    def tableView_setObjectValue_forTableColumn_row_(self, table, value, column, i):
        if column.identifier() == "check" and not (self.running_job or self.finished):
            self.visible[i].checked = bool(value)
            self._update_summary()

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, i):
        if column.identifier() == "check":
            cell.setEnabled_(not (self.running_job or self.finished))

    def tableView_sortDescriptorsDidChange_(self, table, old):
        descs = table.sortDescriptors()
        if descs:
            self.sort_key = str(descs[0].key())
            self.ascending = bool(descs[0].ascending())
            self._refilter()

    def rowDoubleClicked_(self, sender):
        i = self.table.clickedRow()
        if 0 <= i < len(self.visible) and not (self.running_job or self.finished):
            self.visible[i].checked = not self.visible[i].checked
            self.table.reloadData()
            self._update_summary()

    def searchChanged_(self, sender):
        self._refilter()

    # ---------------------------------------------------------------- uninstall

    def uninstallClicked_(self, sender):
        if self.finished:
            self.window.close()
            return
        sel = self._selected()
        if not sel or self.running_job:
            return
        names = [r.app.name for r in sel]
        listed = "\n".join(f"• {n}" for n in names[:12])
        if len(names) > 12:
            listed += f"\n• …and {len(names) - 12} more"
        total = sum(r.app.total_size for r in sel)
        running = [r.app.name for r in sel if r.app.running]

        alert = NSAlert.alloc().init()
        alert.setAlertStyle_(2)                     # critical
        alert.setMessageText_(
            f"Delete {len(sel)} app{'s' if len(sel) != 1 else ''} for good?")
        info = (f"{listed}\n\nThe apps and their leftovers ({format_size(total)}) are "
                "deleted right away. They don't go to the Trash, so this can't be undone.")
        if running:
            info += f"\n\nOpen apps are quit first: {', '.join(running)}."
        if any(r.app.path.stat().st_uid == 0 for r in sel if r.app.path.exists()):
            info += "\n\nSome apps were installed by an administrator; macOS will ask for your password once."
        alert.setInformativeText_(info)
        delete = alert.addButtonWithTitle_("Delete")
        if hasattr(delete, "setHasDestructiveAction_"):
            delete.setHasDestructiveAction_(True)
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        self._start(sel)

    @objc.python_method
    def _start(self, sel):
        self.running_job = True
        self.search.setEnabled_(False)
        self.button.setEnabled_(False)
        self.button.setTitle_("Deleting…")
        # Show only what's being removed, in a stable order.
        self.visible = list(sel)
        for r in sel:
            r.state = app_removal.WAITING
        self.table.reloadData()
        self.bar.setHidden_(False)
        self.bar.setIndeterminate_(False)
        self.bar.setMinValue_(0)
        self.bar.setMaxValue_(len(sel))
        self.bar.setDoubleValue_(0)
        self.summary.setStringValue_(f"Deleting 1 of {len(sel)}…")

        by_path = {str(r.app.path): r for r in sel}
        finals = {app_removal.DONE, app_removal.IN_TRASH, app_removal.FAILED}

        def progress(app, state, detail):
            AppHelper.callAfter(self._progress, by_path[str(app.path)], state, detail, finals)

        def work():
            try:
                results = app_removal.uninstall([r.app for r in sel], progress)
            except Exception as exc:          # never leave the window stuck
                import logging
                logging.getLogger("macpurge.uninstall").exception("uninstall failed")
                results = []
                for r in sel:
                    if r.state not in finals:
                        AppHelper.callAfter(self._progress, r, app_removal.FAILED,
                                            f"Error: {str(exc)[:60]}", finals)
                    results.append(app_removal.Result(
                        r.app, r.state if r.state in finals else app_removal.FAILED))
            AppHelper.callAfter(self._done, results)

        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _progress(self, row, state, detail, finals):
        row.state, row.detail = state, detail
        done = sum(1 for r in self.visible if r.state in finals)
        self.bar.setDoubleValue_(done)
        if done < len(self.visible):
            current = next((r for r in self.visible if r.state not in finals
                            and r.state != app_removal.WAITING), None)
            label = f"Deleting {done + 1} of {len(self.visible)}"
            if current is not None:
                label += f": {current.app.name}"
            self.summary.setStringValue_(label + "…")
        self.table.reloadData()
        i = self.visible.index(row)
        self.table.scrollRowToVisible_(i)

    @objc.python_method
    def _done(self, results):
        self.running_job = False
        self.finished = True
        freed = sum(r.freed for r in results)
        ok = sum(1 for r in results if r.state == app_removal.DONE)
        bad = len(results) - ok
        text = f"Done. {ok} app{'s' if ok != 1 else ''} deleted, {format_size(freed)} freed."
        if bad:
            text += f" {bad} need{'s' if bad == 1 else ''} attention, see the list."
        self.summary.setStringValue_(text)
        self.bar.setDoubleValue_(self.bar.maxValue())
        self.button.setTitle_("Done")
        self.button.setEnabled_(True)
        self.table.reloadData()

    # ---------------------------------------------------------------- window

    def windowShouldClose_(self, sender):
        return not self.running_job          # never close mid-deletion

    def windowWillClose_(self, note):
        if self.on_close:
            self.on_close(self.finished)
