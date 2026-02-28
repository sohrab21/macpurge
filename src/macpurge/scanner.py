"""Disk scanning logic: find reclaimable space, big files, stale files."""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .paths import PathCategory, SKIP_DIR_NAMES, SKIP_ROOTS, get_all_categories
from .utils import dir_size, file_size_safe, format_size, progress_line, clear_progress


@dataclass
class ScanResult:
    category_name: str
    category_key: str
    total_bytes: int
    file_count: int
    paths_found: list[tuple[Path, int]]  # (path, size)
    deletable: bool
    note: str = ""


@dataclass
class BigFileResult:
    path: Path
    size: int
    modified: float


@dataclass
class StaleFileResult:
    path: Path
    size: int
    last_accessed: float
    age_days: int


def scan_all(categories: list[PathCategory] | None = None, progress: bool = True) -> list[ScanResult]:
    """Scan all (or specified) categories. Returns list of ScanResult."""
    if categories is None:
        categories = get_all_categories()

    results = []
    for cat in categories:
        if progress:
            progress_line(f"Scanning {cat.name}...")
        result = scan_category(cat)
        if result.total_bytes > 0:
            results.append(result)

    if progress:
        clear_progress()

    return results


def scan_category(category: PathCategory) -> ScanResult:
    """Scan a single category."""
    paths_found: list[tuple[Path, int]] = []
    total_bytes = 0
    file_count = 0

    if category.recursive_file_search and category.file_pattern:
        # Special case: search for specific files recursively (e.g. .DS_Store)
        for root_path in category.paths:
            if not root_path.exists():
                continue
            for entry in _walk_safe(root_path, SKIP_DIR_NAMES):
                if entry.name == category.file_pattern and entry.is_file(follow_symlinks=False):
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                        paths_found.append((Path(entry.path), size))
                        total_bytes += size
                        file_count += 1
                    except OSError:
                        continue
    elif category.key == "temp":
        # Special handling for /var/folders — only scan */*/C/ subdirs
        for root_path in category.paths:
            if not root_path.exists():
                continue
            resolved = root_path.resolve()
            if "var/folders" in str(resolved) or "var/folders" in str(root_path):
                total_bytes += _scan_var_folders(root_path, paths_found)
            else:
                # /tmp — scan normally
                size = dir_size(root_path)
                if size > 0:
                    paths_found.append((root_path, size))
                    total_bytes += size
                    file_count += 1
    elif category.key == "docker":
        # Single file — just stat it
        for p in category.paths:
            if p.exists():
                size = file_size_safe(p)
                if size > 0:
                    paths_found.append((p, size))
                    total_bytes += size
                    file_count += 1
    elif category.key == "trash":
        # Scan contents of .Trash, not the dir itself
        for root_path in category.paths:
            if not root_path.exists():
                continue
            try:
                with os.scandir(root_path) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                size = entry.stat(follow_symlinks=False).st_size
                            elif entry.is_dir(follow_symlinks=False):
                                size = dir_size(Path(entry.path))
                            else:
                                continue
                            paths_found.append((Path(entry.path), size))
                            total_bytes += size
                            file_count += 1
                        except OSError:
                            continue
            except (PermissionError, OSError):
                continue
    else:
        # Standard directory scanning
        for root_path in category.paths:
            if not root_path.exists():
                continue
            if root_path.is_file():
                size = file_size_safe(root_path)
                if size > 0:
                    paths_found.append((root_path, size))
                    total_bytes += size
                    file_count += 1
            elif root_path.is_dir():
                # Scan top-level items within the directory
                try:
                    with os.scandir(root_path) as entries:
                        for entry in entries:
                            try:
                                if entry.is_file(follow_symlinks=False):
                                    size = entry.stat(follow_symlinks=False).st_size
                                elif entry.is_dir(follow_symlinks=False):
                                    size = dir_size(Path(entry.path))
                                else:
                                    continue
                                paths_found.append((Path(entry.path), size))
                                total_bytes += size
                                file_count += 1
                            except OSError:
                                continue
                except (PermissionError, OSError):
                    continue

    return ScanResult(
        category_name=category.name,
        category_key=category.key,
        total_bytes=total_bytes,
        file_count=file_count,
        paths_found=paths_found,
        deletable=category.deletable,
        note=category.description if not category.deletable else "",
    )


def scan_big_files(min_bytes: int, root: Path, skip_dirs: set[str] | None = None) -> list[BigFileResult]:
    """Walk from root, find files >= min_bytes. Skip specified directories."""
    if skip_dirs is None:
        skip_dirs = SKIP_DIR_NAMES
    results = []

    for entry in _walk_safe(root, skip_dirs):
        # Skip paths under SKIP_ROOTS
        entry_path = Path(entry.path)
        if any(_is_under(entry_path, sr) for sr in SKIP_ROOTS):
            continue

        if entry.is_file(follow_symlinks=False):
            try:
                st = entry.stat(follow_symlinks=False)
                if st.st_size >= min_bytes:
                    results.append(BigFileResult(
                        path=entry_path,
                        size=st.st_size,
                        modified=st.st_mtime,
                    ))
            except OSError:
                continue

    results.sort(key=lambda r: r.size, reverse=True)
    return results


def scan_stale_files(days: int, roots: list[Path]) -> list[StaleFileResult]:
    """Find files with st_atime older than N days."""
    cutoff = time.time() - (days * 86400)
    results = []

    for root in roots:
        if not root.exists():
            continue
        for entry in _walk_safe(root, SKIP_DIR_NAMES):
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                if st.st_atime < cutoff:
                    age_days = int((time.time() - st.st_atime) / 86400)
                    results.append(StaleFileResult(
                        path=Path(entry.path),
                        size=st.st_size,
                        last_accessed=st.st_atime,
                        age_days=age_days,
                    ))
            except OSError:
                continue

    results.sort(key=lambda r: r.last_accessed)
    return results


def _walk_safe(root: Path, skip_names: set[str] | None = None) -> Iterator[os.DirEntry]:
    """Safe recursive walk using os.scandir. Skips errors, never follows symlinks."""
    if skip_names is None:
        skip_names = set()
    try:
        with os.scandir(root) as entries:
            dirs = []
            for entry in entries:
                try:
                    if entry.name in skip_names:
                        continue
                    yield entry
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.path)
                except OSError:
                    continue
            for d in dirs:
                yield from _walk_safe(Path(d), skip_names)
    except (PermissionError, OSError):
        return


def _scan_var_folders(root: Path, paths_found: list[tuple[Path, int]]) -> int:
    """Scan /var/folders — only descend into */*/C/ subdirectories."""
    total = 0
    try:
        resolved = root.resolve()
        with os.scandir(resolved) as level1:
            for l1 in level1:
                if not l1.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(l1.path) as level2:
                        for l2 in level2:
                            if not l2.is_dir(follow_symlinks=False):
                                continue
                            cache_dir = Path(l2.path) / "C"
                            if cache_dir.is_dir():
                                size = dir_size(cache_dir)
                                if size > 0:
                                    paths_found.append((cache_dir, size))
                                    total += size
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return total


def _is_under(path: Path, root: Path) -> bool:
    """Check if path is under root."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
