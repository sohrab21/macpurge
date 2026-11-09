"""Menu bar GUI wrapping the macpurge CLI.

Run during development:
    pip install -e ".[gui]"
    python -m macpurge.menubar

Or, when packaged as Macpurge.app, just launch the app from /Applications.

Design notes:
- All destructive actions show a confirmation alert before running. Large
  and old files are only revealed in Finder. Apps are deleted for good from
  the Uninstall apps window (after a confirmation that says so);
  caches/logs are deleted by the cleaner as before.
- `purge` needs root. The CLI uses sudo + TTY, which doesn't work inside a
  .app bundle, so the GUI routes it through `osascript ... with
  administrator privileges` (native password prompt).
- Long-running work runs on daemon threads. Anything that touches the menu
  (titles, submenus, the icon) or shows an alert goes through `_on_main`
  (AppHelper.callAfter → main thread), as Cocoa requires.
- Status comes from one background sampler thread (livemem: sysctl +
  libproc, no subprocesses), every 60 s, every 15 s under memory pressure.
- __init__ does NOT call any subprocess. If status reads fail in a bundle,
  we still want the menu bar item to register; the first timer tick fills
  in real values and every read is wrapped in try/except.
"""

import fcntl
import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import objc
import rumps
from AppKit import NSWorkspace
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import __version__
from . import insights, livemem, privileged
from .cleaner import clean as run_clean
from .scanner import scan_all
from .schedule import (
    INTERVALS, PLIST_PATH, current_interval, remove_schedule, setup_schedule,
)
from .utils import format_size

LOG_DIR = Path.home() / ".macpurge" / "logs"
LOCK_PATH = Path.home() / ".macpurge" / "menubar.lock"
LOGIN_LABEL = "com.macpurge.login"
LOGIN_PLIST = Path.home() / "Library/LaunchAgents" / f"{LOGIN_LABEL}.plist"
APP_PATH = "/Applications/Macpurge.app"

STATUS_SLOW = 60                  # seconds between samples while pressure is normal
STATUS_FAST = 15                  # … while memory is tight (catch runaways early)
TOP_N = 8                         # rows in "Using the most memory"
IDLE_MIN_BYTES = 1024**3          # idle apps: only those holding 1 GB+
IDLE_AFTER = 2 * 3600             # … and not in front for 2 hours
RESCAN_EVERY = 12 * 3600          # seconds: background disk/apps scan
LOW_DISK_BYTES = 20 * 1024**3     # warn below 20 GB free
LOW_DISK_NOTIFY_EVERY = 24 * 3600
FDA_MISSING = "Full Disk Access missing · click to fix"

log = logging.getLogger("macpurge.menubar")

_DEV_RES = Path(__file__).resolve().parent.parent.parent / "resources"


def _resource(name: str) -> str | None:
    """Find a resource file in dev or bundled (Contents/Resources) layouts."""
    for base in (_DEV_RES, Path(sys.executable).resolve().parent.parent / "Resources"):
        p = base / name
        if p.exists():
            return str(p)
    return None


def _placeholder(text: str) -> rumps.MenuItem:
    return rumps.MenuItem(text)  # no callback → shown greyed out


class MacpurgeApp(rumps.App):
    def __init__(self) -> None:
        self._icon_normal = _resource("menubar_icon.png")
        self._icon_alert = _resource("menubar_icon_alert.png") or self._icon_normal
        # template=True: macOS auto-tints the icon for light/dark menu bar.
        # No title text: a wide title gets the status item placed under the
        # notch on a busy MacBook menu bar, where it's silently invisible.
        super().__init__("Macpurge", icon=self._icon_normal, template=True, quit_button=None)
        log.info("rumps.App initialized; icon=%s", self._icon_normal)

        self._busy = threading.Lock()          # one scan/clean/uninstall at a time
        self._scan_results = []
        self._own_clean = False                # True while the GUI itself cleans
        self._seen_log = self._newest_clean_log()
        self._last_disk_warning = 0.0
        self._last_scan = time.time()          # no scan at login; first auto scan after RESCAN_EVERY
        self._pressure = livemem.NORMAL
        self._consumers: list[livemem.Consumer] = []
        self._wake = threading.Event()         # set → sampler runs now
        self._watch = livemem.RunawayWatch()
        self._activity = livemem.ActivityLog()
        self._observer = _ActivationObserver.alloc().init().setup(self._activity)
        self._fda = True
        self.fda_item = rumps.MenuItem(FDA_MISSING, callback=self.fix_fda)

        # --- status lines (greyed, informational) ---
        self.ram_item = _placeholder("Memory pressure: …")
        self.swap_item = _placeholder("Swap: …")
        self.disk_item = _placeholder("Disk: …")
        self.last_clean_item = _placeholder("Last cleanup: …")

        # --- memory ---
        self.top_apps_menu = rumps.MenuItem("Using the most memory")
        self.top_apps_menu.add(_placeholder("Loading…"))
        self.idle_menu = rumps.MenuItem("Idle apps holding memory")
        self.idle_menu.add(_placeholder("Loading…"))

        # --- disk submenus, filled by a scan ---
        self.reclaim_menu = rumps.MenuItem("Reclaimable space")
        self.large_menu = rumps.MenuItem("Large files")
        self.old_menu = rumps.MenuItem("Old files (6+ months unopened)")
        self.unused_menu = rumps.MenuItem("Unused apps (60+ days)")
        for m in (self.reclaim_menu, self.large_menu, self.old_menu, self.unused_menu):
            m.add(_placeholder("Not scanned yet"))
        self._uninstall_window = None
        self.scan_item = rumps.MenuItem("Scan now", callback=self.scan, key="s")

        # --- schedule submenu with radio-style checkmarks ---
        self._sched_off = rumps.MenuItem("Off", callback=lambda _: self._set_schedule(None))
        self._sched_options = {
            name: rumps.MenuItem(name.capitalize(), callback=lambda _, n=name: self._set_schedule(n))
            for name in INTERVALS
        }
        schedule_menu = rumps.MenuItem("Automatic cleanup")
        schedule_menu.add(self._sched_off)
        for name in INTERVALS:
            schedule_menu.add(self._sched_options[name])
        self.login_item = rumps.MenuItem("Open at login", callback=self.toggle_login)
        self.nopass_item = rumps.MenuItem("Free RAM without password",
                                          callback=self.toggle_passwordless)

        self.menu = [
            self.ram_item,
            self.swap_item,
            self.disk_item,
            self.last_clean_item,
            None,
            self.top_apps_menu,
            self.idle_menu,
            rumps.MenuItem("Free RAM now…", callback=self.free_ram, key="r"),
            None,
            self.scan_item,
            self.reclaim_menu,
            self.large_menu,
            self.old_menu,
            self.unused_menu,
            rumps.MenuItem("Uninstall apps…", callback=lambda _: self.open_uninstaller(), key="u"),
            rumps.MenuItem("Clean caches now…", callback=self.clean_all, key="k"),
            None,
            schedule_menu,
            self.login_item,
            self.nopass_item,
            rumps.MenuItem("Open log folder", callback=self.open_logs, key="l"),
            None,
            rumps.MenuItem("About Macpurge…", callback=self.about),
            rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application(), key="q"),
        ]

        rumps.Timer(self._start_sampler, 1).start()

    # ------------------------------------------------------------------ plumbing

    def _on_main(self, fn, *args) -> None:
        """Run fn(*args) on the main thread (menu/icon/alert changes)."""
        AppHelper.callAfter(self._guarded, fn, args)

    @staticmethod
    def _guarded(fn, args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("UI update failed")

    def refresh_soon(self) -> None:
        """Ask the sampler for a fresh reading now (after quit, clean, …)."""
        self._wake.set()

    def _background(self, label: str, fn) -> bool:
        """Run fn on a worker thread unless another long job is running."""
        if not self._busy.acquire(blocking=False):
            rumps.notification("Macpurge", "Busy", "Another task is still running. Try again in a moment.")
            return False

        def run():
            try:
                fn()
            except Exception as exc:
                log.exception("%s failed", label)
                rumps.notification("Macpurge", f"{label} failed", str(exc)[:120])
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True).start()
        return True

    @staticmethod
    def _fill(menu: rumps.MenuItem, items: list) -> None:
        if menu._menu is not None:   # rumps' clear() fails on an empty submenu
            menu.clear()
        for it in items:
            menu.add(it)

    # ------------------------------------------------------------------ status

    def _start_sampler(self, sender) -> None:
        sender.stop()
        threading.Thread(target=self._sampler, name="sampler", daemon=True).start()

    def _sampler(self) -> None:
        """Background loop: read everything (no subprocesses), then hand the
        result to the main thread. Slower while memory pressure is normal."""
        while True:
            interval = STATUS_SLOW
            try:
                sample = self._collect()
                if sample["mem"].pressure != livemem.NORMAL:
                    interval = STATUS_FAST
                self._on_main(self._apply, sample)
            except Exception:
                log.exception("sampling failed")
            self._wake.wait(interval)
            self._wake.clear()

    def _collect(self) -> dict:
        cons = livemem.consumers()
        return {
            "mem": livemem.system_memory(),
            "consumers": cons,
            "alerts": self._watch.update(cons),
            "fda": insights.has_full_disk_access(),
            "disk": insights.disk_free_bytes(),
            "login": LOGIN_PLIST.exists(),
            "nopass": privileged.passwordless_enabled(),
        }

    def _apply(self, s: dict) -> None:
        """Main thread: show a sample."""
        self._refresh_fda(s["fda"])
        self._show_memory(s["mem"], s["consumers"])
        for c in s["alerts"]:
            rumps.notification(
                "Macpurge", f"{c.name} is using {livemem.format_gb(c.footprint)}",
                f"{c.growing.capitalize()}. Open the Macpurge menu if you want to quit it.")
        self._refresh_disk(s["disk"])
        self._refresh_cleanup_status()
        try:
            self._refresh_schedule_marks()
            self.login_item.state = s["login"]
            self.nopass_item.state = s["nopass"]
        except Exception as exc:
            log.warning("schedule read failed: %s", exc)
        # Background scan: never at login, never while memory is tight, and
        # only with Full Disk Access (else it blocks on a privacy prompt).
        if (self._fda and self._pressure == livemem.NORMAL
                and time.time() - self._last_scan > RESCAN_EVERY and not self._busy.locked()):
            self._last_scan = time.time()
            self._background("Scan", lambda: self._do_scan(notify=False))

    def _refresh_fda(self, has: bool) -> None:
        if has == self._fda:
            return
        self._fda = has
        if has:
            del self.menu[FDA_MISSING]
            log.info("Full Disk Access granted")
        else:
            self.menu.insert_before(self.ram_item.title, self.fda_item)  # rumps keys = title at insert time
            log.warning("Full Disk Access missing")

    def fix_fda(self, _) -> None:
        rumps.alert(
            title="Give Macpurge Full Disk Access",
            message="Macpurge needs Full Disk Access to scan caches and app data.\n\n"
                    "In the window that opens: switch Macpurge on (or add it with + from "
                    "/Applications), then quit and reopen Macpurge.",
            ok="Open System Settings",
        )
        insights.open_fda_settings()

    def _show_memory(self, mem: "livemem.SystemMemory", cons: list) -> None:
        self._pressure = mem.pressure
        self._consumers = cons
        self.ram_item.title = "Memory pressure: " + {
            livemem.NORMAL: "normal", livemem.WARN: "high", livemem.CRITICAL: "critical",
        }[mem.pressure]
        self.swap_item.title = (f"Swap: {livemem.format_gb(mem.swap)} in use"
                                if mem.swap >= 100 * 1024**2 else "Swap: not in use")
        growing = any(c.growing for c in cons)
        wanted = self._icon_alert if (mem.pressure != livemem.NORMAL or growing) else self._icon_normal
        if wanted and self.icon != wanted:
            self.icon = wanted

        items = []
        for c in cons[:TOP_N]:
            label = f"{c.name}  ·  {livemem.format_gb(c.footprint)}"
            if c.growing:
                label += f"  ·  {c.growing}"
            quittable = c.kind == "app" or (c.kind == "cli" and not c.system) or \
                (c.kind == "vm" and c.app_path)
            items.append(rumps.MenuItem(label, callback=(lambda _, c=c: self._ask_quit(c))
                                        if quittable else None))
        items += [None, _placeholder("Includes GPU memory · click to quit")]
        self._fill(self.top_apps_menu, items)
        self._show_idle(cons)

    def _show_idle(self, cons: list) -> None:
        """Regular apps that hold 1 GB+ and haven't been in front for 2 h."""
        by_path = {c.app_path: c for c in cons if c.kind == "app"}
        rows = []
        for ra in NSWorkspace.sharedWorkspace().runningApplications():
            url = ra.bundleURL()
            if ra.activationPolicy() != 0 or ra.isActive() or url is None:
                continue
            c = by_path.get(str(url.path()))
            if c is None or c.footprint < IDLE_MIN_BYTES:
                continue
            launched = ra.launchDate().timeIntervalSince1970() if ra.launchDate() else None
            idle = self._activity.idle_seconds(c.app_path, launched)
            if idle >= IDLE_AFTER:
                rows.append((c, idle))
        rows.sort(key=lambda r: r[0].footprint, reverse=True)
        items = [
            rumps.MenuItem(f"{c.name}  ·  {livemem.format_gb(c.footprint)}  ·  idle {_hours(idle)}",
                           callback=lambda _, c=c, idle=idle: self._ask_quit(c, idle))
            for c, idle in rows
        ]
        if items:
            items += [None, _placeholder("Not used for 2+ hours · click to quit")]
        self._fill(self.idle_menu, items or [_placeholder("No idle apps holding 1 GB or more")])

    def _refresh_disk(self, free: int) -> None:
        low = free < LOW_DISK_BYTES
        self.disk_item.title = f"Disk: {format_size(free)} free" + (" · running low" if low else "")
        if low and time.time() - self._last_disk_warning > LOW_DISK_NOTIFY_EVERY:
            self._last_disk_warning = time.time()
            rumps.notification(
                "Macpurge", "Disk space is running low",
                f"Only {format_size(free)} free. Use 'Clean caches now' or check 'Large files'.",
            )

    def _refresh_cleanup_status(self) -> None:
        try:
            hist = insights.cleanup_history(1)
            if hist:
                h = hist[0]
                text = f"Last cleanup: {insights.ago(h.when)}, freed {h.freed}"
            else:
                text = "Last cleanup: never"
            interval = current_interval()
            if interval:
                limit = INTERVALS[interval] + 86400   # one day of grace
                last = hist[0].when.timestamp() if hist else PLIST_PATH.stat().st_mtime
                if time.time() - last > limit:
                    text += " · automatic cleanup overdue"
            self.last_clean_item.title = text

            # A clean log we didn't write ourselves = the schedule just ran.
            newest = self._newest_clean_log()
            if newest and newest != self._seen_log:
                self._seen_log = newest
                if not self._own_clean and hist and hist[0].log_path == newest:
                    rumps.notification("Macpurge", "Automatic cleanup done",
                                       f"Freed {hist[0].freed}.")
        except Exception as exc:
            log.warning("cleanup status failed: %s", exc)

    @staticmethod
    def _newest_clean_log() -> Path | None:
        logs = sorted(LOG_DIR.glob("clean-*.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None

    # ------------------------------------------------------------------ memory

    def _ask_quit(self, c: "livemem.Consumer", idle: float | None = None) -> None:
        """Always asks first. Apps get a normal quit (they can save); CLI
        programs get SIGTERM; a VM is stopped by quitting the app that owns it."""
        size = livemem.format_gb(c.footprint)
        if c.kind == "cli":
            n = len(c.pids)
            title = f"Stop {c.name}?"
            msg = (f"{c.name} is using {size} ({n} process{'es' if n != 1 else ''}, "
                   f"PID {', '.join(map(str, c.pids[:6]))}{'…' if n > 6 else ''}).\n\n"
                   "It is asked to stop (SIGTERM), like closing it in Terminal. "
                   "Unsaved work in that program may be lost.")
        else:
            app_name = Path(c.app_path).stem if c.app_path else c.name
            title = f"Quit {app_name}?"
            what = f"Its virtual machine is using {size}" if c.kind == "vm" else f"It is using {size}"
            if idle is not None:
                what += f" and hasn't been used for {_hours(idle)}"
            msg = (f"{what}.\n\n{app_name} is asked to quit normally, "
                   "so you can still save unsaved work.")
        if c.growing:
            msg = f"{c.name} has been {c.growing}.\n\n" + msg
        if not _confirm(title, msg):
            return
        if c.kind == "cli":
            stopped = 0
            for pid in c.pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    stopped += 1
                except OSError:
                    pass
            ok = stopped > 0
        else:
            ok = False
            for ra in NSWorkspace.sharedWorkspace().runningApplications():
                url = ra.bundleURL()
                if url is not None and str(url.path()) == c.app_path:
                    ok = bool(ra.terminate()) or ok
        if not ok:
            rumps.notification("Macpurge", f"{c.name} did not stop",
                               "It may be waiting for you (unsaved changes?).")
        threading.Timer(3, self.refresh_soon).start()

    def free_ram(self, _) -> None:
        if not privileged.passwordless_enabled() and not _confirm(
            "Free RAM",
            "Run `purge` to reclaim inactive memory?\n\n"
            "macOS will ask for your password. To skip the password next time, "
            "turn on 'Free RAM without password' in this menu.\n\n"
            "Tip: quitting a big app from 'Apps using the most memory' usually frees more.",
        ):
            return
        threading.Thread(target=self._do_free_ram, daemon=True).start()

    def _do_free_ram(self) -> None:
        before = livemem.system_memory().used
        try:
            ok, err = privileged.run_purge()
        except Exception as exc:
            rumps.notification("Macpurge", "Error", str(exc)[:120])
            return
        if not ok:
            rumps.notification("Macpurge", "Could not free RAM", err or "purge failed.")
            return
        freed = max(before - livemem.system_memory().used, 0)
        rumps.notification("Macpurge", "File cache cleared",
                           f"~{format_size(freed)} freed. Memory apps hold is not affected.")
        self.refresh_soon()

    def toggle_passwordless(self, _) -> None:
        if privileged.passwordless_enabled():
            if not _confirm("Ask for the password again?",
                            "Free RAM will ask for your password again. "
                            "macOS asks for it once now to remove the setting."):
                return
            ok, err = privileged.disable_passwordless()
        else:
            if not _confirm(
                "Free RAM without password?",
                "Lets Macpurge run macOS's `purge` command (it frees inactive "
                "memory) without asking for your password. It allows only this "
                "one command, nothing else.\n\n"
                "macOS asks for your password once now to set this up.",
            ):
                return
            ok, err = privileged.enable_passwordless()
        if not ok:
            rumps.notification("Macpurge", "Setting not changed", err or "Unknown error.")
        self.nopass_item.state = privileged.passwordless_enabled()

    # ------------------------------------------------------------------ scan

    def scan(self, _) -> None:
        if not insights.has_full_disk_access():
            self.fix_fda(None)
            return
        if self._background("Scan", lambda: self._do_scan(notify=True)):
            self._last_scan = time.time()
            self.scan_item.title = "Scanning…"
            for m in (self.reclaim_menu, self.large_menu, self.old_menu, self.unused_menu):
                self._fill(m, [_placeholder("Scanning…")])

    def _do_scan(self, notify: bool) -> None:
        """Full read-only scan: reclaimable space, large/old files, unused apps."""
        try:
            results = scan_all(progress=False)
            self._scan_results = results
            self._on_main(self._show_reclaimable, results)
            large = insights.large_files()
            self._on_main(self._show_files, self.large_menu, large, "No files over 200 MB", None)
            old = insights.old_files()
            self._on_main(self._show_files, self.old_menu, old,
                          "Nothing old and large in Downloads/Desktop", "age")
            apps = insights.unused_apps()
            self._on_main(self._show_unused, apps)
        finally:
            self._on_main(setattr, self.scan_item, "title", "Scan now")
        if notify:
            total = sum(r.total_bytes for r in results if r.deletable)
            rumps.notification(
                "Macpurge", "Scan complete",
                f"{format_size(total)} of caches can go. "
                f"{len(large)} large files, {len(apps)} unused apps found.",
            )

    def _show_reclaimable(self, results) -> None:
        deletable = [r for r in results if r.deletable and r.total_bytes > 0]
        items = [
            rumps.MenuItem(f"{r.category_name}  ·  {format_size(r.total_bytes)}",
                           callback=lambda _, r=r: self._clean_category(r))
            for r in sorted(deletable, key=lambda r: r.total_bytes, reverse=True)
        ]
        for r in results:
            if not r.deletable and r.total_bytes > 0:
                items.append(_placeholder(
                    f"{r.category_name}  ·  {format_size(r.total_bytes)} (manage in its app)"))
        if not items:
            items = [_placeholder("Nothing to clean")]
        else:
            total = sum(r.total_bytes for r in deletable)
            items += [None,
                      rumps.MenuItem(f"Clean all ({format_size(total)})…", callback=self.clean_all),
                      _placeholder("Click a category to clean only that")]
        self._fill(self.reclaim_menu, items)

    def _show_files(self, menu, files, empty_text, mode) -> None:
        items = []
        for f in files:
            extra = f"{f.age_days // 30} months" if mode == "age" else ""
            label = f"{format_size(f.size)}  ·  {_short_name(f.path)}" + (f"  ·  {extra}" if extra else "")
            items.append(rumps.MenuItem(label, callback=lambda _, p=f.path: insights.reveal_in_finder(p)))
        if items:
            items += [None, _placeholder("Click to show in Finder (nothing is deleted)")]
        self._fill(menu, items or [_placeholder(empty_text)])

    def _show_unused(self, apps) -> None:
        items = [
            rumps.MenuItem(f"{a.name}  ·  {format_size(a.size)}  ·  {a.days_unused} days",
                           callback=lambda _, a=a: self.open_uninstaller([a.path]))
            for a in apps
        ]
        if items:
            items += [None, _placeholder("Click to uninstall (opens the app list)")]
        self._fill(self.unused_menu, items or [_placeholder("No unused apps found")])

    # ------------------------------------------------------------------ clean

    def clean_all(self, _) -> None:
        if not _confirm(
            "Clean caches",
            "Delete cached files now?\n\n"
            "Removes user caches, logs, browser caches, Homebrew/npm/pip "
            "caches, .DS_Store, and /var temp files. Protected paths "
            "(~/Documents, ~/Photos, system directories) are never touched.",
        ):
            return
        if self._background("Cleanup", lambda: self._do_clean(None)):
            rumps.notification("Macpurge", "Cleaning…", "Removing caches and temp files.")

    def _clean_category(self, result) -> None:
        if not _confirm(f"Clean {result.category_name}?",
                        f"Delete {format_size(result.total_bytes)} of "
                        f"{result.category_name.lower()}?\n\n"
                        "Apps rebuild these files when they need them."):
            return
        self._background("Cleanup", lambda: self._do_clean(result.category_key))

    def _do_clean(self, category_key: str | None) -> None:
        # In-process, never a subprocess: inside the bundle
        # Contents/MacOS/macpurge is the same file as the menu bar launcher
        # (case-insensitive APFS), so spawning it would start a second app.
        self._own_clean = True
        try:
            results = scan_all(progress=False)
            if category_key:
                results = [r for r in results if r.category_key == category_key]
            run_clean(results, force=True)
            hist = insights.cleanup_history(1)
            freed = hist[0].freed if hist else "some space"
            rumps.notification("Macpurge", "Cleanup complete", f"Freed {freed}.")
            self._scan_results = scan_all(progress=False)
            self._on_main(self._show_reclaimable, self._scan_results)
            self.refresh_soon()
        finally:
            self._on_main(self._own_clean_done)

    def _own_clean_done(self) -> None:
        # Our own log counts as seen, so no "automatic cleanup done" notice.
        self._seen_log = self._newest_clean_log()
        self._own_clean = False

    # ------------------------------------------------------------------ apps

    def open_uninstaller(self, preselect=None) -> None:
        """Open the 'Uninstall apps' window (or bring it to the front)."""
        from .uninstall_window import UninstallWindow   # AppKit-heavy, load lazily
        w = self._uninstall_window
        if w is not None and w.window.isVisible():
            w.show()
            return
        self._uninstall_window = UninstallWindow.alloc().init().setup(
            [str(p) for p in preselect or []], self._uninstaller_closed)
        self._uninstall_window.show()

    def _uninstaller_closed(self, deleted_something: bool) -> None:
        self._uninstall_window = None
        if deleted_something:
            self.refresh_soon()
            if not self._busy.locked():
                self._background("Scan", lambda: self._do_scan(notify=False))

    # ------------------------------------------------------------------ settings

    def _refresh_schedule_marks(self) -> None:
        active = current_interval() if PLIST_PATH.exists() else None
        self._sched_off.state = (active is None)
        for name, item in self._sched_options.items():
            item.state = (active == name)

    def _set_schedule(self, interval: str | None) -> None:
        if interval is None:
            if not _confirm("Disable automatic cleanup", "Stop running cleanup automatically?"):
                self._refresh_schedule_marks()
                return
            try:
                remove_schedule()
                rumps.notification("Macpurge", "Schedule off", "Automatic cleanup disabled.")
            except Exception as exc:
                rumps.notification("Macpurge", "Error", str(exc)[:120])
        else:
            if not _confirm(
                f"Enable {interval} cleanup",
                f"Run cleanup automatically on a {interval} schedule?\n\n"
                "While Macpurge is open you'll get a notification after each run. "
                "Logs go to ~/.macpurge/logs/.",
            ):
                self._refresh_schedule_marks()
                return
            try:
                setup_schedule(interval)
                rumps.notification("Macpurge", "Schedule on", f"{interval.capitalize()} cleanup enabled.")
            except Exception as exc:
                rumps.notification("Macpurge", "Error", str(exc)[:120])
        self._refresh_schedule_marks()

    def toggle_login(self, _) -> None:
        """Start Macpurge automatically when you log in (LaunchAgent)."""
        try:
            if LOGIN_PLIST.exists():
                LOGIN_PLIST.unlink()
            else:
                LOGIN_PLIST.parent.mkdir(parents=True, exist_ok=True)
                with open(LOGIN_PLIST, "wb") as f:
                    plistlib.dump({"Label": LOGIN_LABEL,
                                   "ProgramArguments": ["/usr/bin/open", "-a", APP_PATH],
                                   "RunAtLoad": True}, f)
        except Exception as exc:
            rumps.notification("Macpurge", "Error", str(exc)[:120])
        self.login_item.state = LOGIN_PLIST.exists()

    def open_logs(self, _) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(LOG_DIR)])

    def about(self, _) -> None:
        hist = insights.cleanup_history(5)
        lines = "\n".join(f"{h.when:%d.%m.%Y %H:%M}  ·  freed {h.freed}" for h in hist) or "No cleanups yet."
        interval = current_interval()
        sched = f"Automatic cleanup: {interval}" if interval else "Automatic cleanup: off"
        rumps.alert(title=f"Macpurge {__version__}",
                    message=f"{sched}\n\nRecent cleanups:\n{lines}", ok="Close")


class _ActivationObserver(NSObject):
    """Records when apps come to the front or leave it (for idle apps)."""

    @objc.python_method
    def setup(self, activity: "livemem.ActivityLog"):
        self.activity = activity
        ws = NSWorkspace.sharedWorkspace()
        nc = ws.notificationCenter()
        for name in ("NSWorkspaceDidActivateApplicationNotification",
                     "NSWorkspaceDidDeactivateApplicationNotification"):
            nc.addObserver_selector_name_object_(self, "appChanged:", name, None)
        front = ws.frontmostApplication()
        if front is not None and front.bundleURL() is not None:
            activity.activated(str(front.bundleURL().path()))
        return self

    def appChanged_(self, note):
        app = note.userInfo().get("NSWorkspaceApplicationKey")
        if app is not None and app.bundleURL() is not None:
            self.activity.activated(str(app.bundleURL().path()))


def _hours(seconds: float) -> str:
    h = int(seconds // 3600)
    return f"{h} h" if h < 48 else f"{h // 24} days"


def _short_name(path: Path, limit: int = 48) -> str:
    name = path.name
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _confirm(title: str, message: str) -> bool:
    return rumps.alert(title=title, message=message, ok="OK", cancel="Cancel") == 1


def _acquire_single_instance_lock():
    """Return a held lock file, or None if another menu bar app is running.
    The OS releases the lock when the process exits, so no stale locks."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main() -> None:
    lock = _acquire_single_instance_lock()
    if lock is None:
        log.info("another Macpurge menu bar app is already running; exiting")
        return
    MacpurgeApp().run()


if __name__ == "__main__":
    main()
