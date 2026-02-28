"""Application uninstall logic: full removal with bundle ID lookup."""

import os
import plistlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .logger import AuditLogger
from .paths import UNINSTALL_SEARCH_DIRS
from .utils import (
    confirm,
    dir_size,
    file_size_safe,
    format_size,
    is_protected_path,
    shorten_path,
)

APP_DIRS = [Path("/Applications"), Path.home() / "Applications"]


@dataclass
class AppInfo:
    name: str
    path: Path
    bundle_id: str
    is_running: bool
    pids: list[int]


@dataclass
class AppRemnant:
    path: Path
    size: int
    location_label: str


def find_app(app_name: str) -> AppInfo | None:
    """Find an app by name in /Applications or ~/Applications."""
    # Normalize: strip .app suffix
    search_name = app_name.removesuffix(".app").lower()

    for app_dir in APP_DIRS:
        if not app_dir.exists():
            continue
        try:
            for entry in os.scandir(app_dir):
                if not entry.name.endswith(".app"):
                    continue
                entry_name = entry.name.removesuffix(".app").lower()
                if entry_name == search_name or search_name in entry_name:
                    app_path = Path(entry.path)
                    bundle_id = _read_bundle_id(app_path)
                    if not bundle_id:
                        bundle_id = entry.name.removesuffix(".app")
                    is_running, pids = _is_app_running(entry.name.removesuffix(".app"))
                    return AppInfo(
                        name=entry.name.removesuffix(".app"),
                        path=app_path,
                        bundle_id=bundle_id,
                        is_running=is_running,
                        pids=pids,
                    )
        except (PermissionError, OSError):
            continue

    return None


def find_remnants(app_info: AppInfo) -> list[AppRemnant]:
    """Search common macOS directories for app-related files."""
    remnants = []
    bundle_id = app_info.bundle_id.lower()
    app_name = app_info.name.lower()

    for label, search_dir in UNINSTALL_SEARCH_DIRS:
        if not search_dir.exists():
            continue
        try:
            with os.scandir(search_dir) as entries:
                for entry in entries:
                    name_lower = entry.name.lower()
                    if bundle_id in name_lower or app_name in name_lower:
                        entry_path = Path(entry.path)
                        if entry.is_file(follow_symlinks=False):
                            size = file_size_safe(entry_path)
                        elif entry.is_dir(follow_symlinks=False):
                            size = dir_size(entry_path)
                        else:
                            continue
                        remnants.append(AppRemnant(
                            path=entry_path,
                            size=size,
                            location_label=label,
                        ))
        except (PermissionError, OSError):
            continue

    return remnants


def uninstall(app_name: str, force: bool = False) -> None:
    """Full uninstall flow."""
    # 1. Find app
    app = find_app(app_name)
    if not app:
        print(f"App not found: {app_name!r}")
        print("Check the name and try again. Look in /Applications/ for the exact name.")
        return

    print(f"Found: {app.name} ({app.bundle_id})")
    print(f"  Location: {app.path}")

    # 2. Check if running
    if app.is_running:
        print(f"\n  Warning: {app.name} is currently running (PID: {', '.join(map(str, app.pids))})")
        if confirm(f"  Quit {app.name} first?"):
            if _quit_app(app.name):
                print(f"  {app.name} quit successfully.")
            else:
                print(f"  Could not quit {app.name}. You may need to force-quit manually.")
                if not force and not confirm("  Continue with uninstall anyway?"):
                    return

    # 3. Find remnants
    remnants = find_remnants(app)

    # 4. Show what will be deleted
    print(f"\nFiles to remove:")
    app_size = dir_size(app.path)
    total_size = app_size

    print(f"  {format_size(app_size):>10}  {shorten_path(app.path)}  (app bundle)")
    for r in remnants:
        print(f"  {format_size(r.size):>10}  {shorten_path(r.path)}  ({r.location_label})")
        total_size += r.size

    print(f"\n  Total: {format_size(total_size)} across {1 + len(remnants)} items")

    # 5. Confirm
    if not force:
        print()
        if not confirm(f"Remove {app.name} and all associated files?"):
            print("Cancelled.")
            return

    # 6. Delete
    print()
    with AuditLogger(operation="uninstall") as logger:
        # Delete remnants first, then the app bundle
        for r in remnants:
            _safe_delete(r.path, r.size, logger)

        _safe_delete(app.path, app_size, logger)

        print(f"\n{app.name} uninstalled. Freed {format_size(logger.freed_bytes)}.")
        if logger.log_path:
            print(f"Log: {shorten_path(logger.log_path)}")


def _read_bundle_id(app_path: Path) -> str | None:
    """Read CFBundleIdentifier from an app's Info.plist."""
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.exists():
        return None
    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        return plist.get("CFBundleIdentifier")
    except Exception:
        return None


def _is_app_running(app_name: str) -> tuple[bool, list[int]]:
    """Check if an app is running via pgrep."""
    try:
        result = subprocess.run(
            ["pgrep", "-fi", app_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip()]
            return True, pids
    except (subprocess.CalledProcessError, OSError, ValueError):
        pass
    return False, []


def _quit_app(app_name: str) -> bool:
    """Attempt graceful quit via osascript."""
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to quit'],
            timeout=10, capture_output=True,
        )
        # Wait for app to quit
        for _ in range(10):
            time.sleep(0.5)
            running, _ = _is_app_running(app_name)
            if not running:
                return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    return False


def _safe_delete(path: Path, size: int, logger: AuditLogger) -> None:
    """Delete a path safely with logging."""
    if is_protected_path(path):
        logger.log_skipped(path, "protected path")
        print(f"  Skipped (protected): {shorten_path(path)}")
        return

    if path.is_symlink():
        logger.log_skipped(path, "symlink — skipping for safety")
        print(f"  Skipped (symlink): {shorten_path(path)}")
        return

    try:
        if path.is_file():
            os.unlink(path)
        elif path.is_dir():
            import shutil
            shutil.rmtree(path, onerror=lambda *a: None)
        logger.log_deletion(path, size)
        print(f"  Removed: {shorten_path(path)}")
    except PermissionError:
        logger.log_skipped(path, "permission denied")
        print(f"  Skipped (permission denied): {shorten_path(path)}")
    except OSError as e:
        logger.log_skipped(path, str(e))
        print(f"  Skipped ({e}): {shorten_path(path)}")
