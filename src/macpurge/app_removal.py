"""Permanently delete apps and their leftovers (GUI-free, runs on a worker
thread, reports progress through a callback).

Per app: quit it if running → delete leftovers in ~/Library → delete the
bundle. A bundle is first renamed out of its Applications folder in one
step, then deleted, so a failure can't leave a half-deleted app behind.
Bundles we can't touch as the user (root-owned: Office, Zoom, App Store
apps) are collected and deleted together at the end with ONE native admin
password prompt. If something still can't be deleted, it goes to the
Trash via Finder as a last resort and the app is reported as such.

Every path is checked against `_allowed` right before deletion: only .app
bundles directly in an Applications folder (or one vendor folder below)
and leftover items directly in the known ~/Library folders.
"""

import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .insights import (
    APP_ROOTS, HOME, LEFTOVER_BASES, InstalledApp, _path_size, quit_app_and_wait,
)
from .logger import AuditLogger
from .utils import is_protected_path

# Progress states reported to the callback
WAITING, QUITTING, DELETING, NEEDS_PASSWORD, DONE, IN_TRASH, FAILED = (
    "waiting", "quitting", "deleting", "password", "done", "trash", "failed")


@dataclass
class Result:
    app: InstalledApp
    state: str
    detail: str = ""
    freed: int = 0


Progress = Callable[[InstalledApp, str, str], None]


def _allowed(p: Path) -> bool:
    """Last line of defence before anything is deleted."""
    if p.is_symlink() or not p.exists() or is_protected_path(p):
        return False
    if p.suffix == ".app":
        roots = set(APP_ROOTS)
        return p.parent in roots or (p.parent.parent in roots and p.parent.name != "Utilities")
    return any(p.parent == HOME / base for base in LEFTOVER_BASES)


def _rm(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def _delete_bundle(p: Path) -> str:
    """Delete an app bundle as the user. Returns DONE, IN_TRASH (moved out
    but not fully deleted) or NEEDS_PASSWORD (no rights as the user)."""
    staging = HOME / ".Trash" / f".macpurge-{uuid.uuid4().hex[:8]}-{p.name}"
    try:
        os.rename(p, staging)          # atomic: the app is gone or untouched
    except OSError:
        return NEEDS_PASSWORD
    try:
        shutil.rmtree(staging)
        return DONE
    except OSError:
        # Out of /Applications already; leave the rest visible in the Trash.
        try:
            os.rename(staging, staging.with_name(p.name))
        except OSError:
            pass
        return IN_TRASH


def _admin_delete(paths: list[Path]) -> tuple[bool, str]:
    """rm -rf the given paths with the native macOS password prompt."""
    cmd = "/bin/rm -rf -- " + " ".join(shlex.quote(str(p)) for p in paths)
    script = cmd.replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run(
            ["osascript", "-e", f'do shell script "{script}" with administrator privileges'],
            capture_output=True, encoding="utf-8", errors="replace", timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if "-128" in r.stderr:
        return False, "password cancelled"
    return r.returncode == 0, r.stderr.strip()[:100]


def _finder_trash(p: Path) -> bool:
    posix = str(p).replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(
        ["osascript", "-e", f'tell application "Finder" to delete (POSIX file "{posix}" as alias)'],
        capture_output=True, timeout=120,
    )
    return r.returncode == 0


def uninstall(apps: list[InstalledApp], progress: Progress) -> list[Result]:
    results: dict[str, Result] = {}
    needs_admin: list[InstalledApp] = []

    with AuditLogger(operation="uninstall") as log:
        for app in apps:
            key = str(app.path)
            if not _allowed(app.path):
                results[key] = Result(app, FAILED, "not allowed")
                log.log_skipped(app.path, "failed safety check")
                progress(app, FAILED, "Skipped by safety check")
                continue

            if app.running:
                progress(app, QUITTING, "Quitting…")
                if not quit_app_and_wait(app.name, app.path):
                    results[key] = Result(app, FAILED, "still running")
                    log.log_skipped(app.path, "still running")
                    progress(app, FAILED, "Still running, quit it and try again")
                    continue

            progress(app, DELETING, "Deleting…")
            freed, stayed = 0, 0
            for p in app.leftovers:
                if not _allowed(p):
                    continue
                try:
                    size = _path_size(p)
                    _rm(p)
                    log.log_deletion(p, size)
                    freed += size
                except OSError as e:
                    stayed += 1
                    log.log_skipped(p, str(e)[:100])

            outcome = _delete_bundle(app.path)
            if outcome == DONE:
                log.log_deletion(app.path, app.size)
                freed += app.size
                note = f"{stayed} leftover items stayed" if stayed else ""
                results[key] = Result(app, DONE, note, freed)
                progress(app, DONE, note)
            elif outcome == IN_TRASH:
                log.log_skipped(app.path, "partly deleted, rest left in the Trash")
                results[key] = Result(app, IN_TRASH, "Rest is in the Trash, empty it to finish", freed)
                progress(app, IN_TRASH, results[key].detail)
            else:
                results[key] = Result(app, NEEDS_PASSWORD, "", freed)
                needs_admin.append(app)
                progress(app, NEEDS_PASSWORD, "Waiting for your password…")

        if needs_admin:
            ok, err = _admin_delete([a.path for a in needs_admin if _allowed(a.path)])
            for app in needs_admin:
                res = results[str(app.path)]
                if not app.path.exists():
                    log.log_deletion(app.path, app.size)
                    res.state, res.freed = DONE, res.freed + app.size
                    progress(app, DONE, "")
                elif _finder_trash(app.path):
                    log.log_skipped(app.path, f"moved to Trash instead ({err or 'admin delete failed'})")
                    res.state, res.detail = IN_TRASH, "Moved to the Trash, empty it to finish"
                    progress(app, IN_TRASH, res.detail)
                else:
                    log.log_skipped(app.path, err or "admin delete failed")
                    res.state, res.detail = FAILED, err or "could not delete"
                    progress(app, FAILED, res.detail.capitalize())

    return [results[str(a.path)] for a in apps]

