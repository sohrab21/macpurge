"""Set up a weekly launchd job for automatic cleanup."""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.macpurge.weekly"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / ".macpurge" / "logs"


def setup_schedule() -> None:
    """Create and load a weekly launchd plist for macpurge clean --all --force."""
    macpurge_bin = _find_macpurge_bin()
    if not macpurge_bin:
        print("Could not find macpurge executable. Is it installed?")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LABEL,
        "ProgramArguments": [macpurge_bin, "clean", "--all", "--force"],
        # Run every 7 days (604800 seconds). Unlike StartCalendarInterval,
        # StartInterval guarantees execution even after sleep — launchd
        # fires it as soon as the machine wakes if the interval has elapsed.
        "StartInterval": 604800,
        "StandardOutPath": str(LOG_DIR / "schedule-stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "schedule-stderr.log"),
    }

    # Unload existing if present
    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
        )

    # Write plist
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    # Load it
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        print(f"Failed to load schedule: {result.stderr.strip()}")
        return

    print("Weekly cleanup scheduled.")
    print(f"  Runs: Every 7 days (even after sleep/standby)")
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
    print("Weekly schedule removed.")


def show_status() -> None:
    """Show whether the schedule is active."""
    if not PLIST_PATH.exists():
        print("No weekly schedule configured.")
        print(f"Run 'macpurge schedule' to set one up.")
        return

    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        print("Weekly cleanup is active.")
        print(f"  Runs: Every 7 days (even after sleep/standby)")
        print(f"  Command: macpurge clean --all --force")
        print(f"  Plist: {PLIST_PATH}")
    else:
        print("Schedule plist exists but is not loaded.")
        print(f"  Run 'macpurge schedule' to reload it.")


def _find_macpurge_bin() -> str | None:
    """Find the full path to the macpurge executable."""
    # Try shutil.which first (works if macpurge is on PATH)
    found = shutil.which("macpurge")
    if found:
        return found

    # Try the same directory as the current Python interpreter
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "macpurge"
    if candidate.exists():
        return str(candidate)

    return None
