"""System overview / dashboard. Read-only, no destructive actions."""

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from .memory import get_memory_info, get_top_processes
from .scanner import scan_all
from .utils import clear_progress, dir_size, format_size, shorten_path


def show_overview() -> None:
    """Print system health snapshot."""
    _print_system_info()
    print()
    disk_total, disk_used, _ = _get_disk_usage()
    _print_disk_info(disk_total, disk_used)
    print()
    mem_info = get_memory_info()
    _print_ram_info(mem_info)
    print()
    _print_top_disk_dirs()
    print()
    _print_top_ram_processes()
    print()
    reclaimable_total = _print_reclaimable_estimate()
    print()
    _print_recommendation(mem_info, disk_total, disk_used, reclaimable_total)


def _print_system_info() -> None:
    """macOS version, chip, uptime."""
    print("System Info:")

    # macOS version
    mac_ver = platform.mac_ver()[0]
    print(f"  macOS:     {mac_ver}")

    # Chip
    try:
        chip = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        chip = platform.processor() or "Unknown"
    print(f"  Chip:      {chip}")

    # Uptime
    try:
        boottime_str = subprocess.check_output(
            ["sysctl", "-n", "kern.boottime"], text=True
        ).strip()
        match = re.search(r"sec\s*=\s*(\d+)", boottime_str)
        if match:
            boot_sec = int(match.group(1))
            uptime_sec = int(time.time() - boot_sec)
            days = uptime_sec // 86400
            hours = (uptime_sec % 86400) // 3600
            if days > 0:
                print(f"  Uptime:    {days} days, {hours} hours")
            else:
                minutes = (uptime_sec % 3600) // 60
                print(f"  Uptime:    {hours} hours, {minutes} minutes")
    except (subprocess.CalledProcessError, OSError, ValueError):
        pass


def _print_disk_info(total: int, used: int) -> None:
    """Disk usage with bar visualization."""
    free = total - used
    print("Disk Usage:")
    print(f"  Total:     {format_size(total)}")
    print(f"  Used:      {format_size(used)}")
    print(f"  Free:      {format_size(free)}")
    print(f"  {_format_bar(used, total)}")


def _get_disk_usage() -> tuple[int, int, int]:
    """Get disk total/used/free. Try diskutil first for APFS accuracy."""
    try:
        output = subprocess.check_output(
            ["diskutil", "info", "/"], text=True
        )
        total = _parse_diskutil_bytes(output, "Container Total Space")
        free = _parse_diskutil_bytes(output, "Container Free Space")
        if total and free:
            return total, total - free, free
    except (subprocess.CalledProcessError, OSError):
        pass

    # Fallback to shutil
    usage = shutil.disk_usage("/")
    return usage.total, usage.used, usage.free


def _parse_diskutil_bytes(output: str, key: str) -> int | None:
    """Parse a byte value from diskutil info output."""
    for line in output.splitlines():
        if key in line:
            # Format: "Container Total Space:  494384795648 B (494.4 GB)"
            match = re.search(r"(\d+)\s*B\s*\(", line)
            if match:
                return int(match.group(1))
            # Alternative: just bytes
            match = re.search(r":\s*(\d+)\s", line)
            if match:
                return int(match.group(1))
    return None


def _format_bar(used: int, total: int, width: int = 30) -> str:
    """Create ASCII bar: [##########..........] 65%"""
    if total == 0:
        return "[" + "." * width + "]  0%"
    pct = min(used / total, 1.0)
    filled = int(pct * width)
    return f"  [{'#' * filled}{'.' * (width - filled)}] {pct:.0%}"


def _print_ram_info(info) -> None:
    """RAM summary."""
    used = info.total_bytes - info.free_bytes - info.speculative_bytes

    print("Memory:")
    print(f"  Total:     {format_size(info.total_bytes)}")
    print(f"  Used:      {format_size(used)}")
    print(f"  Free:      {format_size(info.free_bytes)}")
    if info.swap_used_bytes > 0:
        print(f"  Swap:      {format_size(info.swap_used_bytes)} used")


def _print_top_disk_dirs(n: int = 5) -> None:
    """Top N largest directories in ~/."""
    home = Path.home()
    print(f"Top {n} disk consumers (~/):")

    dirs: list[tuple[Path, int]] = []
    try:
        with os.scandir(home) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith(".") and entry.name not in (".Trash",):
                    # Still include hidden dirs for size reporting
                    pass
                try:
                    size = dir_size(Path(entry.path))
                    dirs.append((Path(entry.path), size))
                except OSError:
                    continue
    except OSError:
        print("  Could not scan home directory.")
        return

    dirs.sort(key=lambda x: x[1], reverse=True)
    for path, size in dirs[:n]:
        print(f"  {format_size(size):>10}  {shorten_path(path)}")


def _print_top_ram_processes(n: int = 5) -> None:
    """Top N memory-consuming processes."""
    processes = get_top_processes(n)
    if not processes:
        return

    print(f"Top {n} memory consumers:")
    for i, p in enumerate(processes, 1):
        print(f"  {i}. {format_size(p.rss_bytes):>8}  {p.name} (PID {p.pid})")


def _print_reclaimable_estimate() -> int:
    """Quick scan for reclaimable space estimate. Returns total bytes."""
    print("Reclaimable space (estimated):")
    results = scan_all(progress=False)
    total = sum(r.total_bytes for r in results)
    if total == 0:
        print("  No significant reclaimable space found.")
        return 0
    for r in results:
        if r.total_bytes > 0:
            print(f"  {format_size(r.total_bytes):>10}  {r.category_name}")
    print(f"  {'─' * 35}")
    print(f"  {format_size(total):>10}  Total")
    return total


def _print_recommendation(
    mem_info, disk_total: int, disk_used: int, reclaimable: int
) -> None:
    """Suggest the most impactful next command based on current state."""
    used_ram = mem_info.total_bytes - mem_info.free_bytes - mem_info.speculative_bytes
    ram_pct = used_ram / mem_info.total_bytes if mem_info.total_bytes else 0
    swap_used = mem_info.swap_used_bytes > 0
    disk_pct = disk_used / disk_total if disk_total else 0

    gb = 1024 ** 3
    print("Recommended next step:")
    if ram_pct >= 0.85 or swap_used:
        detail = f"{ram_pct:.0%} RAM used"
        if swap_used:
            detail += f", {format_size(mem_info.swap_used_bytes)} swap"
        print(f"  Memory is under pressure ({detail}) → run `macpurge ram`.")
        if reclaimable >= 5 * gb:
            print(
                f"  After that, ~{format_size(reclaimable)} is reclaimable → "
                f"`macpurge scan`, then `macpurge clean`."
            )
    elif disk_pct >= 0.85:
        print(f"  Disk is {disk_pct:.0%} full → run `macpurge scan`, then `macpurge clean`.")
    elif reclaimable >= 5 * gb:
        print(
            f"  ~{format_size(reclaimable)} reclaimable → "
            f"run `macpurge scan` (then `macpurge clean`)."
        )
    elif reclaimable >= 1 * gb:
        print(
            f"  Optional: ~{format_size(reclaimable)} reclaimable. "
            f"Run `macpurge scan` to review."
        )
    else:
        print("  System looks healthy. No action needed.")
