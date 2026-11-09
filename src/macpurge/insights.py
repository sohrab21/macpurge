"""Read-only system insights for the menu bar app.

Everything here is GUI-free and safe to run on a background thread:
disk space, large/old files, unused and installed apps and cleanup
history. Live memory readings are in `livemem.py`. Nothing here deletes;
app removal lives in `app_removal.py`.
"""

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logger import LOG_DIR
from .paths import SKIP_DIR_NAMES
from .scanner import scan_big_files, scan_stale_files
from .uninstaller import _read_bundle_id
from .utils import dir_size

HOME = Path.home()

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
                           encoding="utf-8", errors="replace", timeout=10)
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
            encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=10,
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
        out = subprocess.check_output(["ps", "-axo", "comm="], encoding="utf-8", errors="replace")
    except (subprocess.CalledProcessError, OSError):
        return set()
    return {m.group(0).rstrip("/") for m in re.finditer(r"^/.*?\.app/", out, re.M)}


def _last_activity(app: Path, bid: str, cutoff: float) -> tuple[float, bool]:
    """(newest activity timestamp, has data folders). Stops early once
    anything is newer than `cutoff`. Second-opinion signals (extension and
    group containers, Spotlight) are only checked when the app looks unused
    by its own data folders."""
    newest, has_data = 0.0, False
    for base, pattern in _DATA_DIRS:
        p = HOME / base / pattern.format(name=app.stem, bid=bid)
        if p.exists():
            has_data = True
            newest = max(newest, _newest_mtime(p, cutoff))
            if newest > cutoff:
                return newest, has_data
    return max(newest, _extra_activity(app, bid, cutoff)), has_data


def _is_running(app: Path, running: set[str]) -> bool:
    return any(p == str(app) or p.startswith(str(app) + "/") for p in running)


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
            if _is_running(entry, running):
                continue
            newest, has_data = _last_activity(entry, bid, cutoff)
            if not has_data or newest > cutoff:
                continue
            days = int((time.time() - newest) / 86400)
            found.append(UnusedApp(entry.stem, entry, bid, days, dir_size(entry)))
    found.sort(key=lambda a: a.size, reverse=True)
    return found[:n]


# --- all apps (remove any app, used or not) ----------------------------------

@dataclass
class InstalledApp:
    name: str
    path: Path
    bundle_id: str
    size: int                 # app bundle only
    running: bool
    last_used: float          # timestamp; 0 = unknown
    leftovers: list[Path]
    leftover_size: int

    @property
    def total_size(self) -> int:
        return self.size + self.leftover_size


# Apple apps that ship with macOS can't be removed (sealed system volume),
# but Apple apps from the App Store (iMovie, Keynote, …) can.
def _removable(app: Path, bid: str) -> bool:
    if app.name == "Macpurge.app" or app.is_symlink():
        return False
    if bid.startswith("com.apple."):
        return (app / "Contents/_MASReceipt").exists()
    return True


APP_ROOTS = (Path("/Applications"), HOME / "Applications")


def _app_bundles() -> list[Path]:
    """.app bundles in /Applications and ~/Applications, plus one folder
    level down (vendors like Adobe put apps in their own folder)."""
    found = []
    for root in APP_ROOTS:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.suffix == ".app":
                found.append(e)
            elif e.is_dir() and not e.is_symlink() and e.name != "Utilities":
                try:
                    found += sorted(c for c in e.iterdir() if c.suffix == ".app")
                except OSError:
                    pass
    return found


def _path_size(p: Path) -> int:
    try:
        if p.is_symlink() or p.is_file():
            st = p.lstat()
            return st.st_blocks * 512
    except OSError:
        return 0
    return dir_size(p)


def installed_apps() -> list[InstalledApp]:
    """Every app the user can remove, whether used or not, alphabetical."""
    running = _running_app_paths()
    now = time.time()
    apps = []
    for entry in _app_bundles():
        bid = _read_bundle_id(entry) or ""
        if not _removable(entry, bid):
            continue
        is_running = _is_running(entry, running)
        last = now if is_running else _last_activity(entry, bid, now - 86400)[0]
        app = InstalledApp(entry.stem, entry, bid, dir_size(entry), is_running, last, [], 0)
        app.leftovers = app_leftovers(app)
        app.leftover_size = sum(_path_size(p) for p in app.leftovers)
        apps.append(app)
    apps.sort(key=lambda a: a.name.lower())
    return apps


def quit_app_and_wait(name: str, path: Path, timeout: float = 15) -> bool:
    """Politely quit an app (it may ask to save), then wait until it's gone."""
    try:
        subprocess.run(["osascript", "-e", f'tell application "{name}" to quit'],
                       capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    end = time.time() + timeout
    while time.time() < end:
        if not _is_running(path, _running_app_paths()):
            return True
        time.sleep(0.5)
    return False


LEFTOVER_BASES = ("Library/Application Support", "Library/Preferences", "Library/Caches",
                  "Library/Containers", "Library/Saved Application State",
                  "Library/HTTPStorages", "Library/WebKit", "Library/LaunchAgents")


def app_leftovers(app) -> list[Path]:
    """User-level leftovers that belong to this app (anything with `.name`
    and `.bundle_id`). Matches the exact bundle ID, the bundle ID plus a
    dot-suffix (extensions, .plist, .savedState) or the exact app name,
    never a substring, so "Go" can't hit "Google"."""
    bid, name = app.bundle_id.lower(), app.name.lower()
    hits = []
    for base in LEFTOVER_BASES:
        d = HOME / base
        try:
            for e in d.iterdir():
                low = e.name.lower()
                if low == name or (bid and (low == bid or low.startswith(bid + "."))):
                    hits.append(e)
        except OSError:
            continue
    return hits


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
