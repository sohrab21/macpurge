"""Utility functions: formatting, size calculations, confirmations."""

import os
import re
import sys
from pathlib import Path

# macOS SIP restricted flag (not in Python's stat module)
SF_RESTRICTED = 0x00080000

PROTECTED_PATHS = [
    Path("/System"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path.home() / "Documents",
    Path.home() / "Photos",
    Path.home() / "Music",
]

_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def format_size(byte_count: int) -> str:
    """Convert bytes to human-readable string (1024-based)."""
    if byte_count <= 0:
        return "0 B"
    size = float(byte_count)
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def parse_size(size_str: str) -> int:
    """Parse human-readable size to bytes. Accepts '500MB', '1.5GB', etc."""
    match = re.match(r"^\s*([\d.]+)\s*(B|KB|MB|GB|TB)\s*$", size_str.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid size format: {size_str!r}. Use e.g. '500MB', '1.5GB'.")
    value = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * multipliers[unit])


def confirm(prompt: str, default_no: bool = True) -> bool:
    """Interactive yes/no confirmation. Returns True only on explicit 'y'/'yes'."""
    suffix = " [y/N] " if default_no else " [Y/n] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if default_no:
        return answer in ("y", "yes")
    return answer not in ("n", "no")


def is_protected_path(path: Path) -> bool:
    """Check if a path is inside a protected directory."""
    try:
        resolved = path.resolve()
    except OSError:
        return True  # can't resolve -> treat as protected
    for protected in PROTECTED_PATHS:
        try:
            resolved.relative_to(protected.resolve())
            return True
        except ValueError:
            continue
    return False


def is_sip_protected(path: Path) -> bool:
    """Check if a file has the macOS SIP restricted flag."""
    try:
        st = os.stat(path, follow_symlinks=False)
        return bool(getattr(st, "st_flags", 0) & SF_RESTRICTED)
    except OSError:
        return False


def dir_size(path: Path) -> int:
    """Recursively calculate directory size in bytes. Skips errors, no symlink follow."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += dir_size(Path(entry.path))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return total


def file_size_safe(path: Path) -> int:
    """Get file size, return 0 on any error."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def shorten_path(path: Path) -> str:
    """Replace /Users/<username>/ with ~/ for display."""
    home = str(Path.home())
    s = str(path)
    if s.startswith(home):
        return "~" + s[len(home):]
    return s


def print_table(rows: list[tuple[str, str]], header: str = "", separator: str = "─") -> None:
    """Print aligned two-column output with optional header."""
    if not rows:
        return
    max_left = max(len(r[0]) for r in rows)
    max_right = max(len(r[1]) for r in rows)
    width = max_left + max_right + 4

    if header:
        print(header)
        print(separator * width)

    for left, right in rows:
        print(f"  {left:<{max_left}}  {right:>{max_right}}")

    if header:
        print(separator * width)


def progress_line(message: str) -> None:
    """Print a single-line progress indicator that overwrites itself."""
    sys.stdout.write(f"\r\033[K{message}")
    sys.stdout.flush()


def clear_progress() -> None:
    """Clear the progress line."""
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
