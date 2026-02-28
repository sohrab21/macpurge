"""RAM freeing and process management."""

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass

from .utils import confirm, format_size


@dataclass
class MemoryInfo:
    total_bytes: int
    free_bytes: int
    active_bytes: int
    inactive_bytes: int
    wired_bytes: int
    speculative_bytes: int
    swap_used_bytes: int
    swap_total_bytes: int


@dataclass
class ProcessInfo:
    pid: int
    name: str
    mem_percent: float
    rss_bytes: int


def get_memory_info() -> MemoryInfo:
    """Parse vm_stat and sysctl to get memory state."""
    # Total RAM
    total = int(subprocess.check_output(
        ["sysctl", "-n", "hw.memsize"], text=True
    ).strip())

    # vm_stat for page-level breakdown
    vm_output = subprocess.check_output(["vm_stat"], text=True)
    lines = vm_output.strip().splitlines()

    # Parse page size from first line
    page_match = re.search(r"page size of (\d+) bytes", lines[0])
    page_size = int(page_match.group(1)) if page_match else 16384

    # Parse page counts
    pages = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip().rstrip(".")
        try:
            pages[key] = int(val)
        except ValueError:
            continue

    free = pages.get("pages free", 0) * page_size
    active = pages.get("pages active", 0) * page_size
    inactive = pages.get("pages inactive", 0) * page_size
    wired = pages.get("pages wired down", 0) * page_size
    speculative = pages.get("pages speculative", 0) * page_size

    # Swap
    swap_used = 0
    swap_total = 0
    try:
        swap_output = subprocess.check_output(
            ["sysctl", "-n", "vm.swapusage"], text=True
        ).strip()
        swap_match = re.search(
            r"total\s*=\s*([\d.]+)M\s+used\s*=\s*([\d.]+)M", swap_output
        )
        if swap_match:
            swap_total = int(float(swap_match.group(1)) * 1024 * 1024)
            swap_used = int(float(swap_match.group(2)) * 1024 * 1024)
    except (subprocess.CalledProcessError, OSError):
        pass

    return MemoryInfo(
        total_bytes=total,
        free_bytes=free,
        active_bytes=active,
        inactive_bytes=inactive,
        wired_bytes=wired,
        speculative_bytes=speculative,
        swap_used_bytes=swap_used,
        swap_total_bytes=swap_total,
    )


def get_top_processes(n: int = 10) -> list[ProcessInfo]:
    """Get top N memory-consuming processes. Uses ps -m for macOS sort."""
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid,pmem,rss,comm", "-m"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return []

    processes = []
    for line in output.strip().splitlines()[1:]:  # skip header
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            mem_pct = float(parts[1])
            rss_kb = int(parts[2])
            name = parts[3].strip()
            # Get just the binary name from full path
            name = os.path.basename(name)
            if mem_pct > 0:
                processes.append(ProcessInfo(
                    pid=pid,
                    name=name,
                    mem_percent=mem_pct,
                    rss_bytes=rss_kb * 1024,
                ))
        except (ValueError, IndexError):
            continue

    return processes[:n]


def purge_memory() -> tuple[int, int]:
    """Run 'sudo purge'. Returns (free_before, free_after)."""
    before = get_memory_info()

    if not sys.stdin.isatty():
        print("Cannot run purge in non-interactive mode (no TTY).")
        return before.free_bytes, before.free_bytes

    print("Purging inactive memory (may require password)...")
    try:
        subprocess.run(["sudo", "purge"], timeout=60, check=True)
    except subprocess.TimeoutExpired:
        print("Purge timed out.")
        return before.free_bytes, before.free_bytes
    except subprocess.CalledProcessError:
        print("Purge failed (wrong password or insufficient privileges).")
        return before.free_bytes, before.free_bytes
    except FileNotFoundError:
        print("'purge' command not found.")
        return before.free_bytes, before.free_bytes

    after = get_memory_info()
    return before.free_bytes, after.free_bytes


def kill_process(pid: int) -> bool:
    """Send SIGTERM to a process. Returns True if successful."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        print(f"Process {pid} not found.")
        return False
    except PermissionError:
        print(f"Permission denied to kill process {pid}.")
        return False
    except OSError as e:
        print(f"Failed to kill process {pid}: {e}")
        return False


def ram_command() -> None:
    """Full ram subcommand flow."""
    # 1. Show memory overview
    info = get_memory_info()
    used = info.total_bytes - info.free_bytes - info.speculative_bytes

    print("Memory Overview:")
    print(f"  Total:     {format_size(info.total_bytes)}")
    print(f"  Used:      {format_size(used)} "
          f"(wired: {format_size(info.wired_bytes)}, "
          f"active: {format_size(info.active_bytes)}, "
          f"inactive: {format_size(info.inactive_bytes)})")
    print(f"  Free:      {format_size(info.free_bytes)}")
    if info.swap_used_bytes > 0:
        print(f"  Swap:      {format_size(info.swap_used_bytes)} used")
    print()

    # 2. Offer to purge
    if confirm("Purge inactive memory?"):
        before, after = purge_memory()
        freed = after - before
        if freed > 0:
            print(f"  Freed: ~{format_size(freed)}")
        else:
            print("  No significant memory freed.")
        print()

    # 3. Show top processes
    processes = get_top_processes()
    if not processes:
        print("Could not retrieve process list.")
        return

    print("Top memory consumers:")
    for i, p in enumerate(processes, 1):
        print(f"  {i:>2}.  {format_size(p.rss_bytes):>8}  {p.name} (PID {p.pid})")
    print()

    # 4. Offer to kill
    while True:
        try:
            answer = input("Kill a process? Enter PID or 'skip': ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if answer in ("skip", "done", "q", ""):
            break

        try:
            pid = int(answer)
        except ValueError:
            print("Invalid PID. Enter a number or 'skip'.")
            continue

        if kill_process(pid):
            print(f"  Sent SIGTERM to PID {pid}.")
