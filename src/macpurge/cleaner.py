"""Deletion logic with dry-run support and audit logging."""

import errno
import os
import shutil
from pathlib import Path

from .logger import AuditLogger
from .scanner import ScanResult
from .utils import (
    confirm,
    format_size,
    is_protected_path,
    is_sip_protected,
    print_table,
    shorten_path,
)


def clean(scan_results: list[ScanResult], force: bool = False) -> bool:
    """
    Main clean entry point.

    1. Print summary of what will be deleted.
    2. Prompt for confirmation (unless --force).
    3. Delete files and log everything.
    4. Print freed space.
    Returns True if cleaning happened, False if skipped/cancelled.
    """
    # Filter to deletable categories only
    deletable = [r for r in scan_results if r.deletable]
    if not deletable:
        print("Nothing to clean.")
        return False

    total = sum(r.total_bytes for r in deletable)
    if total == 0:
        print("Nothing to clean.")
        return False

    # Print summary
    _print_clean_summary(deletable, total)

    # Non-deletable items (Docker, etc.) — show notes
    for r in scan_results:
        if not r.deletable and r.note and r.total_bytes > 0:
            print(f"\n  Note: {r.category_name} ({format_size(r.total_bytes)}) — {r.note}")

    # Confirm
    if not force:
        print()
        if not confirm("Proceed with deletion?"):
            print("Cancelled.")
            return False

    # Delete
    print()
    with AuditLogger(operation="clean") as logger:
        for result in deletable:
            for path, size in result.paths_found:
                freed, err = _delete_path(path, logger)
                if err:
                    print(f"  Skipped: {shorten_path(path)} ({err})")

        print(f"\nFreed {format_size(logger.freed_bytes)} across {logger.deleted_count} items.")
        if logger.log_path:
            print(f"Log: {shorten_path(logger.log_path)}")

    return True


def _print_clean_summary(results: list[ScanResult], total: int) -> None:
    """Print the pre-deletion summary table."""
    rows = [(r.category_name, format_size(r.total_bytes)) for r in results]
    print_table(rows, header="Will be deleted:")
    print(f"  {'Total':<30}  {format_size(total):>10}")


def _delete_path(path: Path, logger: AuditLogger) -> tuple[int, str | None]:
    """
    Delete a single file or directory.
    Returns (bytes_freed, error_message_or_none).
    """
    # Safety check
    if is_protected_path(path):
        logger.log_skipped(path, "protected path")
        return 0, "protected path"

    if is_sip_protected(path):
        logger.log_skipped(path, "SIP-protected")
        return 0, "SIP-protected"

    try:
        # Get actual size at deletion time
        if path.is_file() or path.is_symlink():
            size = path.stat().st_size if path.exists() else 0
            os.unlink(path)
            logger.log_deletion(path, size)
            return size, None
        elif path.is_dir():
            size = _dir_size_quick(path)
            shutil.rmtree(path, onerror=_rmtree_onerror)
            logger.log_deletion(path, size)
            return size, None
        else:
            return 0, None
    except FileNotFoundError:
        # Race condition: file disappeared between scan and delete
        return 0, None
    except PermissionError:
        logger.log_skipped(path, "permission denied")
        return 0, "permission denied"
    except OSError as e:
        if e.errno in (errno.EPERM, errno.EACCES):
            logger.log_skipped(path, "permission denied")
            return 0, "permission denied"
        logger.log_skipped(path, str(e))
        return 0, str(e)


def _dir_size_quick(path: Path) -> int:
    """Quick directory size for logging. Best-effort."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += _dir_size_quick(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _rmtree_onerror(func, path, exc_info):
    """Error handler for shutil.rmtree. Skip permission errors."""
    # Just skip files we can't delete — don't crash
    pass
