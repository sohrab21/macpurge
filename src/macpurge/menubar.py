"""Menu bar GUI wrapping the macpurge CLI.

Run during development:
    pip install -e ".[gui]"
    python -m macpurge.menubar

Or, when packaged as Macpurge.app, just launch the app from /Applications.

Design notes:
- All destructive actions show a confirmation alert before running. Files
  and apps are only ever moved to the Trash (recoverable); caches/logs are
  deleted by the cleaner as before.
- `purge` needs root. The CLI uses sudo + TTY, which doesn't work inside a
  .app bundle, so the GUI routes it through `osascript ... with
  administrator privileges` (native password prompt).
- Long-running work runs on daemon threads. Anything that touches the menu
  (titles, submenus, the icon) or shows an alert is queued with `_on_main`
  and applied by a 0.5s timer on the main thread — Cocoa requires it.
- __init__ does NOT call any subprocess. If status reads fail in a bundle,
  we still want the menu bar item to register; the first timer tick fills
  in real values and every read is wrapped in try/except.
"""

import fcntl
import logging
import plistlib
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from . import __version__
from . import insights
from .cleaner import clean as run_clean
from .memory import get_memory_info
from .scanner import scan_all
from .schedule import (
    INTERVALS, PLIST_PATH, current_interval, remove_schedule, setup_schedule,
)
from .utils import format_size, shorten_path

LOG_DIR = Path.home() / ".macpurge" / "logs"
LOCK_PATH = Path.home() / ".macpurge" / "menubar.lock"
LOGIN_LABEL = "com.macpurge.login"
LOGIN_PLIST = Path.home() / "Library/LaunchAgents" / f"{LOGIN_LABEL}.plist"
APP_PATH = "/Applications/Macpurge.app"

STATUS_EVERY = 30                 # seconds: RAM, pressure, disk, top apps
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

        self._ui: queue.Queue = queue.Queue()
        self._busy = threading.Lock()          # one scan/clean/uninstall at a time
        self._scan_results = []
        self._own_clean = False                # True while the GUI itself cleans
        self._seen_log = self._newest_clean_log()
        self._last_disk_warning = 0.0
        self._last_scan = 0.0
        self._pressure = insights.PRESSURE_NORMAL
        self._fda = True
        self.fda_item = rumps.MenuItem(FDA_MISSING, callback=self.fix_fda)

        # --- status lines (greyed, informational) ---
        self.ram_item = _placeholder("RAM: …")
        self.disk_item = _placeholder("Disk: …")
        self.last_clean_item = _placeholder("Last cleanup: …")

        # --- memory ---
        self.top_apps_menu = rumps.MenuItem("Apps using the most memory")
        self.top_apps_menu.add(_placeholder("Loading…"))

        # --- disk submenus, filled by a scan ---
        self.reclaim_menu = rumps.MenuItem("Reclaimable space")
        self.large_menu = rumps.MenuItem("Large files")
        self.old_menu = rumps.MenuItem("Old files (6+ months unopened)")
        self.unused_menu = rumps.MenuItem("Unused apps (60+ days)")
        for m in (self.reclaim_menu, self.large_menu, self.old_menu, self.unused_menu):
            m.add(_placeholder("Not scanned yet"))
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

        self.menu = [
            self.ram_item,
            self.disk_item,
            self.last_clean_item,
            None,
            self.top_apps_menu,
            rumps.MenuItem("Free RAM now…", callback=self.free_ram, key="r"),
            None,
            self.scan_item,
            self.reclaim_menu,
            self.large_menu,
            self.old_menu,
            self.unused_menu,
            rumps.MenuItem("Clean caches now…", callback=self.clean_all, key="k"),
            None,
            schedule_menu,
            self.login_item,
            rumps.MenuItem("Open log folder", callback=self.open_logs, key="l"),
            None,
            rumps.MenuItem("About Macpurge…", callback=self.about),
            rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application(), key="q"),
        ]

        rumps.Timer(self._pump, 0.5).start()
        self.timer = rumps.Timer(self._tick, STATUS_EVERY)
        self.timer.start()
        rumps.Timer(self._first_tick, 1).start()

    # ------------------------------------------------------------------ plumbing

    def _on_main(self, fn, *args) -> None:
        """Run fn(*args) on the main thread (menu/icon/alert changes)."""
        self._ui.put((fn, args))

    def _pump(self, _) -> None:
        while True:
            try:
                fn, args = self._ui.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:
                log.exception("UI update failed")

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

    def _first_tick(self, sender) -> None:
        sender.stop()
        self._tick(None)

    def _tick(self, _) -> None:
        """Cheap status refresh on the main thread, every STATUS_EVERY s."""
        self._refresh_fda()
        self._refresh_ram()
        self._refresh_top_apps()
        self._refresh_disk()
        self._refresh_cleanup_status()
        try:
            self._refresh_schedule_marks()
            self.login_item.state = LOGIN_PLIST.exists()
        except Exception as exc:
            log.warning("schedule read failed: %s", exc)
        # Without FDA a scan blocks forever on a macOS privacy prompt, so the
        # automatic scan only runs once access is granted.
        if (self._fda and time.time() - self._last_scan > RESCAN_EVERY
                and not self._busy.locked()):
            self._last_scan = time.time()
            self._background("Scan", lambda: self._do_scan(notify=False))

    def _refresh_fda(self) -> None:
        has = insights.has_full_disk_access()
        if has == self._fda:
            return
        self._fda = has
        if has:
            del self.menu[FDA_MISSING]
            log.info("Full Disk Access granted")
        else:
            self.menu.insert_before("RAM: …", self.fda_item)  # rumps keys = title at insert time
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

    def _refresh_ram(self) -> None:
        try:
            info = get_memory_info()
            used = info.total_bytes - info.free_bytes - info.speculative_bytes
            pct = used / info.total_bytes if info.total_bytes else 0
            self._pressure = insights.memory_pressure()
            label = {
                insights.PRESSURE_NORMAL: "",
                insights.PRESSURE_WARN: " · memory is tight",
                insights.PRESSURE_CRITICAL: " · memory is critically low",
            }[self._pressure]
            self.ram_item.title = f"RAM: {pct:.0%} used{label}"
            alert = self._pressure != insights.PRESSURE_NORMAL
            wanted = self._icon_alert if alert else self._icon_normal
            if wanted and self.icon != wanted:
                self.icon = wanted
        except Exception as exc:
            log.warning("RAM read failed: %s", exc)
            self.ram_item.title = "RAM: unavailable"

    def _refresh_top_apps(self) -> None:
        try:
            apps = insights.top_apps_by_memory(5)
        except Exception as exc:
            log.warning("top apps failed: %s", exc)
            return
        items = [
            rumps.MenuItem(f"{a.name}  ·  {format_size(a.rss_bytes)}",
                           callback=lambda _, a=a: self._quit_app(a))
            for a in apps
        ] or [_placeholder("No apps found")]
        items += [None, _placeholder("Click an app to quit it")]
        self._fill(self.top_apps_menu, items)

    def _refresh_disk(self) -> None:
        try:
            free = insights.disk_free_bytes()
        except Exception as exc:
            log.warning("disk read failed: %s", exc)
            self.disk_item.title = "Disk: unavailable"
            return
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

    def _quit_app(self, app: insights.AppMemory) -> None:
        if not _confirm(f"Quit {app.name}?",
                        f"{app.name} is using {format_size(app.rss_bytes)} of memory.\n\n"
                        "It will be asked to quit normally, so you can still save "
                        "unsaved work."):
            return

        def run():
            if insights.quit_app(app):
                rumps.notification("Macpurge", f"{app.name} quit",
                                   f"About {format_size(app.rss_bytes)} of memory freed.")
            else:
                rumps.notification("Macpurge", f"{app.name} did not quit",
                                   "It may be waiting for you (unsaved changes?).")
            time.sleep(2)
            self._on_main(self._tick, None)

        threading.Thread(target=run, daemon=True).start()

    def free_ram(self, _) -> None:
        if not _confirm("Free RAM",
                        "Run `purge` to reclaim inactive memory?\n\n"
                        "macOS will ask for your password. Tip: quitting a big app from "
                        "'Apps using the most memory' usually frees more."):
            return
        threading.Thread(target=self._do_free_ram, daemon=True).start()

    def _do_free_ram(self) -> None:
        before = get_memory_info().free_bytes
        try:
            subprocess.run(
                ["osascript", "-e", 'do shell script "/usr/sbin/purge" with administrator privileges'],
                check=True, capture_output=True, text=True, timeout=180,
            )
        except subprocess.CalledProcessError:
            rumps.notification("Macpurge", "Could not free RAM", "Password cancelled or purge failed.")
            return
        except Exception as exc:
            rumps.notification("Macpurge", "Error", str(exc)[:120])
            return
        freed = max(get_memory_info().free_bytes - before, 0)
        rumps.notification("Macpurge", "RAM freed", f"~{format_size(freed)} reclaimed.")
        self._on_main(self._tick, None)

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
                           callback=lambda _, a=a: self._remove_app(a))
            for a in apps
        ]
        if items:
            items += [None, _placeholder("Click to move an app to the Trash")]
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
            self._on_main(self._tick, None)
        finally:
            # Queued after the tick, so the tick marks our own log as seen
            # before this flag flips back (no "automatic cleanup" notice).
            self._on_main(setattr, self, "_own_clean", False)

    # ------------------------------------------------------------------ apps

    def _remove_app(self, app: insights.UnusedApp) -> None:
        leftovers = insights.app_leftovers(app)
        listed = "\n".join(f"• {shorten_path(p)}" for p in leftovers[:8])
        more = f"\n• …and {len(leftovers) - 8} more" if len(leftovers) > 8 else ""
        if not _confirm(
            f"Move {app.name} to the Trash?",
            f"Not used for {app.days_unused} days, takes {format_size(app.size)}.\n\n"
            f"Also moves its leftover files:\n{listed or '• none found'}{more}\n\n"
            "Everything goes to the Trash, so you can put it back until you empty it. "
            "Finder may ask for your password.",
        ):
            return

        def run():
            moved, errors = insights.trash_paths([app.path, *leftovers])
            if errors:
                rumps.notification("Macpurge", f"{app.name}: some items stayed", errors[0][:120])
            else:
                rumps.notification("Macpurge", f"{app.name} moved to the Trash",
                                   f"{format_size(moved)} will be freed when you empty the Trash.")
            self._on_main(self._show_unused, insights.unused_apps())

        self._background("Remove app", run)

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
