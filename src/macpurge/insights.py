"""Read-only system insights for the menu bar app.

Everything here is GUI-free and safe to run on a background thread:
memory pressure, per-app memory, disk space, large/old files, unused apps
and cleanup history. The only write operation is `trash_paths`, which moves
items to the macOS Trash via Finder (recoverable), never deletes.
"""

import os
import pwd
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logger import AuditLogger, LOG_DIR
from .paths import SKIP_DIR_NAMES
from .scanner import scan_big_files, scan_stale_files
from .uninstaller import _read_bundle_id
from .utils import dir_size

HOME = Path.home()

# --- memory -----------------------------------------------------------------

PRESSURE_NORMAL, PRESSURE_WARN, PRESSURE_CRITICAL = "normal", "warn", "critical"


def memory_pressure() -> str:
    """macOS's own memory pressure level (the Activity Monitor graph colour)."""
    try:
        level = int(subprocess.check_output(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], text=True
        ).strip())
    except (subprocess.CalledProcessError, OSError, ValueError):
        return PRESSURE_NORMAL
    if level >= 4:
        return PRESSURE_CRITICAL
    if level >= 2:
        return PRESSURE_WARN
    return PRESSURE_NORMAL


@dataclass
class AppMemory:
    name: str            # "Google Chrome" or a process name like "python3.13"
    rss_bytes: int
    pids: list[int]
    is_app: bool         # True = lives in a .app bundle → quit via AppleScript


_APP_RE = re.compile(r"/([^/]+)\.app/")


def top_apps_by_memory(n: int = 5) -> list[AppMemory]:
    """Current user's memory use grouped by app (Chrome + helpers = one row)."""
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        out = subprocess.check_output(
            ["ps", "-U", user, "-o", "pid=,rss=,comm="],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return []

    groups: dict[str, AppMemory] = {}
    own_pid = os.getpid()
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, rss_kb = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        comm = parts[2].strip()
        if pid == own_pid or comm.startswith(("/System/", "/usr/libexec/", "/usr/sbin/")):
            continue
        m = _APP_RE.search(comm)          # first .app = the outer app bundle
        name, is_app = (m.group(1), True) if m else (os.path.basename(comm), False)
        if name in ("Finder", "Dock", "SystemUIServer", "Macpurge", "loginwindow"):
            continue
        g = groups.setdefault(name, AppMemory(name, 0, [], is_app))
        g.rss_bytes += rss_kb * 1024
        g.pids.append(pid)

    # Only real apps: command-line processes (shells, Claude Code, servers)
    # shouldn't be one click away from being quit.
    apps = [g for g in groups.values() if g.is_app]
    return sorted(apps, key=lambda g: g.rss_bytes, reverse=True)[:n]


def quit_app(entry: AppMemory) -> bool:
    """Ask an app to quit (it can save work / ask about unsaved changes)."""
    if entry.is_app:
        r = subprocess.run(
            ["osascript", "-e", f'tell application "{entry.name}" to quit'],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    ok = False
    for pid in entry.pids:
        try:
            os.kill(pid, 15)  # SIGTERM: polite, lets the process clean up
            ok = True
        except OSError:
            pass
    return ok


# --- permissions ------------------------------------------------------------

FDA_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def has_full_disk_access() -> bool:
    """True if this process has Full Disk Access.

    Reading the TCC database fails immediately with EPERM without FDA and,
    unlike other protected folders, never triggers a blocking privacy prompt.
    """
    try:
        with open(HOME / "Library/Application Support/com.apple.TCC/TCC.db", "rb"):
            return True
    except PermissionError:
        return False
    except OSError:
        return True   # missing file etc.: don't block scans on an odd setup


def open_fda_settings() -> None:
    subprocess.run(["open", FDA_SETTINGS_URL])


# --- disk -------------------------------------------------------------------

def disk_free_bytes() -> int:
    """Free space the way Finder counts it (includes purgeable space)."""
    try:
        from Foundation import NSURL, NSURLVolumeAvailableCapacityForImportantUsageKey
        url = NSURL.fileURLWithPath_("/")
        values, _ = url.resourceValuesForKeys_error_(
            [NSURLVolumeAvailableCapacityForImportantUsageKey], None
        )
        value = values and values.get(NSURLVolumeAvailableCapacityForImportantUsageKey)
        if value:
            return int(value)
    except Exception:
        pass
    return shutil.disk_usage("/").free


# --- files ------------------------------------------------------------------

# Top-level home folders that hold the user's own files. ~/Library is left
# out on purpose: what's big there is caches/app data, handled elsewhere.
_USER_FOLDERS = ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures")


def large_files(n: int = 10, min_bytes: int = 200 * 1024**2):
    """Biggest files in the user's own folders, largest first."""
    results = []
    for folder in _USER_FOLDERS:
        root = HOME / folder
        if root.exists():
            results.extend(scan_big_files(min_bytes, root, SKIP_DIR_NAMES | {"Photos Library.photoslibrary"}))
    results.sort(key=lambda r: r.size, reverse=True)
    return results[:n]


def old_files(n: int = 10, days: int = 180, min_bytes: int = 10 * 1024**2):
    """Files in Downloads/Desktop not opened for `days`, largest first."""
    roots = [HOME / "Downloads", HOME / "Desktop"]
    results = [r for r in scan_stale_files(days, roots) if r.size >= min_bytes]
    results.sort(key=lambda r: r.size, reverse=True)
    return results[:n]


def reveal_in_finder(path: Path) -> None:
    subprocess.run(["open", "-R", str(path)])


# --- unused apps ------------------------------------------------------------

@dataclass
class UnusedApp:
    name: str
    path: Path
    bundle_id: str
    days_unused: int
    size: int


_DATA_DIRS = (
    ("Library/Application Support", "{name}"),
    ("Library/Application Support", "{bid}"),
    ("Library/Containers", "{bid}"),
    ("Library/Caches", "{bid}"),
    ("Library/Saved Application State", "{bid}.savedState"),
    ("Library/Preferences", "{bid}.plist"),
    ("Library/HTTPStorages", "{bid}"),
)


def _newest_mtime(path: Path, newer_than: float, budget: int = 20_000) -> float:
    """Newest mtime under `path`. Stops early once something newer than
    `newer_than` is seen (we only need to know the app was used recently)."""
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    if path.is_file() or newest > newer_than:
        return newest
    seen = 0
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames + dirnames:
            try:
                mt = os.lstat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            newest = max(newest, mt)
            if newest > newer_than:
                return newest
            seen += 1
            if seen >= budget:
                return newest
    return newest


def _team_id(app: Path) -> str | None:
    try:
        r = subprocess.run(["codesign", "-dv", str(app)], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"^TeamIdentifier=(\w+)", r.stderr, re.M)
    return m.group(1) if m and m.group(1) != "not" else None


def _spotlight_last_used(app: Path) -> float:
    """Spotlight's last-used date, or 0. Often missing (null) for apps that
    ARE used, so it may only ever prove use, never disprove it."""
    try:
        out = subprocess.check_output(
            ["mdls", "-raw", "-name", "kMDItemLastUsedDate", str(app)],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        ).strip()
        return datetime.strptime(out[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _extra_activity(app: Path, bid: str, cutoff: float) -> float:
    """Second-opinion signals, only checked for apps that look unused:
    app extensions' containers, the developer's shared group containers
    (e.g. Office keeps everything in UBF8T346G9.Office) and Spotlight."""
    newest = _spotlight_last_used(app)
    cands = []
    containers = HOME / "Library/Containers"
    try:
        cands += [e for e in containers.iterdir() if e.name.lower().startswith(bid.lower())]
    except OSError:
        pass
    team = _team_id(app)
    if team:
        try:
            cands += [e for e in (HOME / "Library/Group Containers").iterdir()
                      if e.name.startswith(team + ".")]
        except OSError:
            pass
    for c in cands:
        newest = max(newest, _newest_mtime(c, cutoff))
        if newest > cutoff:
            break
    return newest


def _running_app_paths() -> set[str]:
    try:
        out = subprocess.check_output(["ps", "-axo", "comm="], text=True)
    except (subprocess.CalledProcessError, OSError):
        return set()
    return {m.group(0).rstrip("/") for m in re.finditer(r"^/.*?\.app/", out, re.M)}


def unused_apps(min_days: int = 60, n: int = 10) -> list[UnusedApp]:
    """Third-party apps with no activity in their user data for `min_days`.

    Follows the project rule: never trust Spotlight's last-used date. An app
    counts as unused only if it is not running AND none of its data folders
    changed recently. Apps with no data folders at all are skipped, because
    then we can't tell.
    """
    cutoff = time.time() - min_days * 86400
    running = _running_app_paths()
    found = []
    for app_dir in (Path("/Applications"), HOME / "Applications"):
        if not app_dir.exists():
            continue
        for entry in sorted(app_dir.iterdir()):
            if entry.suffix != ".app" or entry.is_symlink():
                continue
            bid = _read_bundle_id(entry) or ""
            if not bid or bid.startswith("com.apple.") or entry.name == "Macpurge.app":
                continue
            if any(p == str(entry) or p.startswith(str(entry) + "/") for p in running):
                continue
            name = entry.stem
            newest = 0.0
            has_data = False
            for base, pattern in _DATA_DIRS:
                p = HOME / base / pattern.format(name=name, bid=bid)
                if p.exists():
                    has_data = True
                    newest = max(newest, _newest_mtime(p, cutoff))
                    if newest > cutoff:
                        break
            if not has_data or newest > cutoff:
                continue
            newest = max(newest, _extra_activity(entry, bid, cutoff))
            if newest > cutoff:
                continue
            days = int((time.time() - newest) / 86400)
            found.append(UnusedApp(name, entry, bid, days, dir_size(entry)))
    found.sort(key=lambda a: a.size, reverse=True)
    return found[:n]


def app_leftovers(app: UnusedApp) -> list[Path]:
    """User-level leftovers that belong to this app. Matches the bundle ID,
    or the exact app name — never a substring, so "Go" can't hit "Google"."""
    bid, name = app.bundle_id.lower(), app.name.lower()
    hits = []
    for base in ("Library/Application Support", "Library/Preferences", "Library/Caches",
                 "Library/Containers", "Library/Saved Application State",
                 "Library/HTTPStorages", "Library/WebKit", "Library/LaunchAgents"):
        d = HOME / base
        try:
            for e in d.iterdir():
                low = e.name.lower()
                if low == name or low.startswith(bid) or low == bid + ".plist":
                    hits.append(e)
        except OSError:
            continue
    return hits


def trash_paths(paths: list[Path], operation: str = "uninstall") -> tuple[int, list[str]]:
    """Move paths to the Trash via Finder (recoverable). Logs each one.
    Returns (bytes moved, error messages)."""
    moved, errors = 0, []
    with AuditLogger(operation=operation) as logger:
        for p in paths:
            size = dir_size(p) if p.is_dir() else p.stat().st_size if p.exists() else 0
            posix = str(p).replace("\\", "\\\\").replace('"', '\\"')
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "Finder" to delete (POSIX file "{posix}" as alias)'],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                logger.log_deletion(p, size)
                moved += size
            else:
                logger.log_skipped(p, r.stderr.strip()[:100] or "Finder refused")
                errors.append(f"{p.name}: {r.stderr.strip()[:80]}")
    return moved, errors


# --- cleanup history --------------------------------------------------------

_SUMMARY_RE = re.compile(r"^\[(.+?)\] SUMMARY\s+Freed (.+?) across")


@dataclass
class CleanupRecord:
    when: datetime
    freed: str           # human-readable, as written in the log
    log_path: Path


def cleanup_history(n: int = 5) -> list[CleanupRecord]:
    """Most recent cleanups (newest first), read from the audit logs."""
    records = []
    logs = sorted(LOG_DIR.glob("clean-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in logs:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            m = _SUMMARY_RE.match(line)
            if m:
                when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                records.append(CleanupRecord(when, m.group(2), path))
                break
        if len(records) >= n:
            break
    return records


def ago(when: datetime) -> str:
    days = (datetime.now().date() - when.date()).days
    if days <= 0:
        return f"today {when:%H:%M}"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"
