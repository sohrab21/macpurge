"""Menu bar GUI wrapping the macpurge CLI.

Run during development:
    pip install -e ".[gui]"
    python -m macpurge.menubar

Or, when packaged as Macpurge.app, just launch the app from /Applications.

Design notes:
- All destructive actions show a confirmation alert before running.
- `purge` and any cleanup that touches /Library paths need root. The CLI
  uses sudo + TTY, which doesn't work inside a .app bundle. The GUI routes
  these through `osascript ... with administrator privileges`, which surfaces
  the standard macOS password prompt instead.
- Long-running work runs on a daemon thread. UI calls (alerts, notifications,
  menu mutations) stay on the main thread — Cocoa requires it.
- __init__ does NOT call any subprocess. If status reads fail in a bundle,
  we still want the menu bar item to register; the first timer tick (1s
  later) fills in real values and is wrapped in try/except.
"""

import logging
import subprocess
import sys
import threading
from pathlib import Path

import rumps

from . import __version__
from .cleaner import clean as run_clean
from .memory import get_memory_info
from .scanner import scan_all
from .schedule import (
    INTERVALS, PLIST_PATH, current_interval, remove_schedule, setup_schedule,
)
from .utils import format_size

LOG_DIR = Path.home() / ".macpurge" / "logs"
log = logging.getLogger("macpurge.menubar")

_DEV_ICON = Path(__file__).resolve().parent.parent.parent / "resources" / "menubar_icon.png"


def _resolve_icon() -> str | None:
    """Find the menu bar icon in dev or bundled layouts. None if missing."""
    candidates = [
        _DEV_ICON,
        # py2app puts data_files at Contents/Resources/
        Path(sys.executable).resolve().parent.parent / "Resources" / "menubar_icon.png",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


class MacpurgeApp(rumps.App):
    def __init__(self) -> None:
        icon = _resolve_icon()
        # template=True: macOS auto-tints the icon for light/dark menu bar.
        # No title text: a wide title (~100px) gets the status item placed
        # underneath the notch on a busy MacBook menu bar, where it's
        # silently invisible. Icon-only is ~36px and fits.
        super().__init__(
            "Macpurge",
            icon=icon,
            template=True,
            quit_button=None,
        )
        log.info("rumps.App initialized; icon=%s", icon)

        self.status_item = rumps.MenuItem("Loading…")

        # Schedule submenu with radio-style checkmarks. The "Off" entry
        # disables the schedule; the others set the interval. Checkmarks
        # are managed by _refresh_schedule_marks() based on the live plist.
        self._sched_off = rumps.MenuItem(
            "Off", callback=lambda _: self._set_schedule(None)
        )
        self._sched_options = {
            name: rumps.MenuItem(
                name.capitalize(),
                callback=lambda _, n=name: self._set_schedule(n),
            )
            for name in INTERVALS
        }
        schedule_menu = rumps.MenuItem("Automatic cleanup")
        schedule_menu.add(self._sched_off)
        for name in INTERVALS:
            schedule_menu.add(self._sched_options[name])

        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("Free RAM now…", callback=self.free_ram),
            rumps.MenuItem("Scan for reclaimable space", callback=self.scan),
            rumps.MenuItem("Clean caches now…", callback=self.clean),
            None,
            schedule_menu,
            rumps.MenuItem("Open log folder", callback=self.open_logs),
            None,
            rumps.MenuItem(f"About macpurge {__version__}"),
            rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application()),
        ]

        # First refresh runs ~1s after the runloop starts, not in __init__.
        # Keeps init fast and ensures the menu bar item registers even if
        # the first status read fails.
        self.timer = rumps.Timer(self.refresh_status, 60)
        self.timer.start()
        rumps.Timer(self._first_refresh, 1).start()

    # --- status ---

    def _first_refresh(self, sender) -> None:
        sender.stop()
        self.refresh_status(None)

    def refresh_status(self, _) -> None:
        try:
            info = get_memory_info()
            used = info.total_bytes - info.free_bytes - info.speculative_bytes
            ram_pct = used / info.total_bytes if info.total_bytes else 0
            under_pressure = ram_pct >= 0.85 or info.swap_used_bytes > 0
            if under_pressure:
                self.status_item.title = f"RAM: {ram_pct:.0%} used (under pressure)"
            else:
                self.status_item.title = f"RAM: {ram_pct:.0%} used"
        except Exception as exc:
            log.warning("status read failed: %s", exc)
            self.status_item.title = "Status unavailable"

        try:
            self._refresh_schedule_marks()
        except Exception as exc:
            log.warning("schedule read failed: %s", exc)

    # --- actions ---

    def free_ram(self, _) -> None:
        if not _confirm(
            "Free RAM",
            "Run `purge` to reclaim inactive memory?\n\n"
            "macOS will ask for your password.",
        ):
            return
        threading.Thread(target=self._do_free_ram, daemon=True).start()

    def _do_free_ram(self) -> None:
        before = get_memory_info().free_bytes
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'do shell script "/usr/sbin/purge" with administrator privileges',
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.CalledProcessError:
            rumps.notification(
                "Macpurge", "Could not free RAM", "Password cancelled or purge failed."
            )
            return
        except Exception as exc:
            rumps.notification("Macpurge", "Error", str(exc)[:120])
            return

        after = get_memory_info().free_bytes
        freed = max(after - before, 0)
        rumps.notification(
            "Macpurge", "RAM freed", f"~{format_size(freed)} reclaimed."
        )
        self.refresh_status(None)

    def scan(self, _) -> None:
        rumps.notification("Macpurge", "Scanning…", "Estimating reclaimable space.")
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self) -> None:
        try:
            results = scan_all(progress=False)
        except Exception as exc:
            rumps.notification("Macpurge", "Scan failed", str(exc)[:120])
            return
        total = sum(r.total_bytes for r in results)
        if total == 0:
            rumps.notification("Macpurge", "Nothing to clean", "No reclaimable space found.")
        else:
            rumps.notification(
                "Macpurge",
                "Scan complete",
                f"~{format_size(total)} reclaimable. Use 'Clean caches now' to remove.",
            )

    def clean(self, _) -> None:
        if not _confirm(
            "Clean caches",
            "Delete cached files now?\n\n"
            "Removes user caches, logs, browser caches, Homebrew/npm/pip "
            "caches, .DS_Store, and /var temp files. Protected paths "
            "(~/Documents, ~/Photos, system directories) are never touched.\n\n"
            "Tip: run 'Scan for reclaimable space' first to see how much "
            "would be freed.",
        ):
            return
        rumps.notification("Macpurge", "Cleaning…", "Running macpurge clean --all.")
        threading.Thread(target=self._do_clean, daemon=True).start()

    def _do_clean(self) -> None:
        # Call cleanup in-process. We can't shell out to a `macpurge` CLI
        # binary because inside the bundle Contents/MacOS/macpurge is the
        # same file as the menu bar launcher (case-insensitive APFS), so
        # spawning it would create a second menu bar app.
        try:
            results = scan_all(progress=False)
            run_clean(results, force=True)
        except Exception as exc:
            log.exception("cleanup failed")
            rumps.notification("Macpurge", "Cleanup failed", str(exc)[:120])
            return
        rumps.notification(
            "Macpurge", "Cleanup complete", "See log folder for what was removed."
        )
        self.refresh_status(None)

    def _refresh_schedule_marks(self) -> None:
        """Sync the schedule submenu checkmarks with the on-disk plist."""
        active = current_interval() if PLIST_PATH.exists() else None
        self._sched_off.state = (active is None)
        for name, item in self._sched_options.items():
            item.state = (active == name)

    def _set_schedule(self, interval: str | None) -> None:
        """Disable the schedule (interval=None) or set it to a named interval."""
        if interval is None:
            if not _confirm(
                "Disable automatic cleanup",
                "Stop running cleanup automatically?",
            ):
                self._refresh_schedule_marks()
                return
            try:
                remove_schedule()
                rumps.notification(
                    "Macpurge", "Schedule off", "Automatic cleanup disabled."
                )
            except Exception as exc:
                rumps.notification("Macpurge", "Error", str(exc)[:120])
        else:
            if not _confirm(
                f"Enable {interval} cleanup",
                f"Run cleanup automatically on a {interval} schedule?\n\n"
                "Logs go to ~/.macpurge/logs/. You can change or disable "
                "this any time from this menu.",
            ):
                self._refresh_schedule_marks()
                return
            try:
                setup_schedule(interval)
                rumps.notification(
                    "Macpurge", "Schedule on", f"{interval.capitalize()} cleanup enabled."
                )
            except Exception as exc:
                rumps.notification("Macpurge", "Error", str(exc)[:120])
        self._refresh_schedule_marks()

    def open_logs(self, _) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(LOG_DIR)])


def _confirm(title: str, message: str) -> bool:
    return rumps.alert(title=title, message=message, ok="OK", cancel="Cancel") == 1


def main() -> None:
    MacpurgeApp().run()


if __name__ == "__main__":
    main()
