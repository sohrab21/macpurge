"""Set up a launchd job for automatic cleanup at a chosen interval."""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.macpurge.weekly"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / ".macpurge" / "logs"

# Supported intervals → seconds.
# StartInterval guarantees execution after sleep — launchd fires the job
# as soon as the machine wakes if the interval has elapsed.
INTERVALS = {
    "daily": 86_400,
    "weekly": 604_800,
    "monthly": 2_592_000,  # 30 days
}


def setup_schedule(interval: str = "weekly") -> None:
    """Create and load a launchd plist for macpurge clean --all --force."""
    if interval not in INTERVALS:
        valid = ", ".join(INTERVALS)
        raise ValueError(f"Unknown interval {interval!r}. Use one of: {valid}.")

    macpurge_bin = _find_macpurge_bin()
    if not macpurge_bin:
        print("Could not find macpurge executable. Is it installed?")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LABEL,
        "ProgramArguments": [macpurge_bin, "clean", "--all", "--force"],
        "StartInterval": INTERVALS[interval],
        "StandardOutPath": str(LOG_DIR / "schedule-stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "schedule-stderr.log"),
    }

    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
        )

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True, encoding="utf-8", errors="replace",
    )

    if result.returncode != 0:
        print(f"Failed to load schedule: {result.stderr.strip()}")
        return

    print(f"{interval.capitalize()} cleanup scheduled.")
    print(f"  Runs: every {INTERVALS[interval]:,} seconds (even after sleep/standby)")
    print(f"  Command: macpurge clean --all --force")
    print(f"  Plist: {PLIST_PATH}")
    print(f"  Logs: ~/.macpurge/logs/schedule-*.log")
    print(f"\nRemove with: macpurge schedule --remove")


def remove_schedule() -> None:
    """Unload and delete the launchd plist."""
    if not PLIST_PATH.exists():
        print("No schedule found. Nothing to remove.")
        return

    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        capture_output=True,
    )
    PLIST_PATH.unlink()
    print("Schedule removed.")


def current_interval() -> str | None:
    """Return the configured interval ('daily'/'weekly'/'monthly') or None.

    Reads StartInterval from the plist on disk and matches it back to a
    named interval. Returns None if no plist exists or the value doesn't
    match a known interval.
    """
    if not PLIST_PATH.exists():
        return None
    try:
        with open(PLIST_PATH, "rb") as f:
            plist = plistlib.load(f)
    except Exception:
        return None
    seconds = plist.get("StartInterval")
    for name, secs in INTERVALS.items():
        if seconds == secs:
            return name
    return None


def show_status() -> None:
    """Show whether the schedule is active and at what interval."""
    interval = current_interval()
    if interval is None:
        if PLIST_PATH.exists():
            print("Schedule plist exists but uses an unrecognized interval.")
        else:
            print("No schedule configured.")
            print(f"Run 'macpurge schedule' to set one up.")
        return

    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True, encoding="utf-8", errors="replace",
    )

    if result.returncode == 0:
        print(f"{interval.capitalize()} cleanup is active.")
        print(f"  Runs: every {INTERVALS[interval]:,} seconds (even after sleep/standby)")
        print(f"  Command: macpurge clean --all --force")
        print(f"  Plist: {PLIST_PATH}")
    else:
        print(f"Schedule plist ({interval}) exists but is not loaded.")
        print(f"  Run 'macpurge schedule' to reload it.")


def _find_macpurge_bin() -> str | None:
    """Find the full path to the macpurge executable."""
    found = shutil.which("macpurge")
    if found:
        return found

    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "macpurge"
    if candidate.exists():
        return str(candidate)

    return None
