"""argparse-based CLI entry point."""

import argparse
import sys
from pathlib import Path

from . import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="macpurge",
        description="macOS system maintenance tool. Dry-run by default.",
    )
    parser.add_argument(
        "--version", action="version", version=f"macpurge {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command")

    # --- scan ---
    subparsers.add_parser("scan", help="Scan for reclaimable space")

    # --- clean ---
    clean_p = subparsers.add_parser("clean", help="Clean reclaimable files")
    clean_p.add_argument("--caches", action="store_true", help="System + user + browser caches")
    clean_p.add_argument("--logs", action="store_true", help="Log files")
    clean_p.add_argument("--xcode", action="store_true", help="Xcode derived data, archives, device support")
    clean_p.add_argument("--dev", action="store_true", help="npm, yarn, pip, Homebrew caches")
    clean_p.add_argument("--trash", action="store_true", help="Empty macOS Trash")
    clean_p.add_argument("--ds-store", action="store_true", help=".DS_Store files")
    clean_p.add_argument("--temp", action="store_true", help="Temp and /var/folders cleanup")
    clean_p.add_argument("--all", action="store_true", help="Everything")
    clean_p.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    # --- big-files ---
    big_p = subparsers.add_parser("big-files", help="Find large files")
    big_p.add_argument("--min", default="500MB", dest="min_size",
                       help="Minimum file size (default: 500MB)")
    big_p.add_argument("--path", default=None, help="Root path to search (default: ~/)")

    # --- stale ---
    stale_p = subparsers.add_parser("stale", help="Find stale files")
    stale_p.add_argument("--days", type=int, default=180,
                         help="Days since last access (default: 180)")
    stale_p.add_argument("--path", default=None,
                         help="Path to search (default: ~/Documents, ~/Downloads, ~/Desktop)")

    # --- uninstall ---
    uninstall_p = subparsers.add_parser("uninstall", help="Fully remove an application")
    uninstall_p.add_argument("app_name", help="Application name")
    uninstall_p.add_argument("--force", action="store_true", help="Skip confirmation")

    # --- ram ---
    subparsers.add_parser("ram", help="Free RAM and show memory consumers")

    # --- overview ---
    subparsers.add_parser("overview", help="System health snapshot")

    # --- schedule ---
    sched_p = subparsers.add_parser("schedule", help="Set up automatic cleanup")
    sched_p.add_argument(
        "--interval", choices=["daily", "weekly", "monthly"], default="weekly",
        help="How often cleanup runs (default: weekly)",
    )
    sched_p.add_argument("--remove", action="store_true", help="Remove the schedule")
    sched_p.add_argument("--status", action="store_true", help="Show schedule status")

    # --- log ---
    log_p = subparsers.add_parser("log", help="Review recent activity logs")
    log_p.add_argument("--last", type=int, default=10,
                       help="Number of recent logs to show (default: 10)")

    args = parser.parse_args()

    if not args.command:
        # No subcommand → show the read-only dashboard. It ends with a
        # "Recommended next step" line so the user knows what to run next.
        args.command = "overview"

    try:
        if args.command == "scan":
            _cmd_scan()
        elif args.command == "clean":
            _cmd_clean(args)
        elif args.command == "big-files":
            _cmd_big_files(args)
        elif args.command == "stale":
            _cmd_stale(args)
        elif args.command == "uninstall":
            _cmd_uninstall(args)
        elif args.command == "ram":
            _cmd_ram()
        elif args.command == "overview":
            _cmd_overview()
        elif args.command == "schedule":
            _cmd_schedule(args)
        elif args.command == "log":
            _cmd_log(args)
    except KeyboardInterrupt:
        print()
        sys.exit(130)


def _cmd_scan():
    from .scanner import scan_all
    from .utils import format_size, print_table

    results = scan_all()
    if not results:
        print("No reclaimable space found.")
        return

    print("macpurge scan results:")
    print("─" * 40)

    rows = []
    for r in results:
        name = r.category_name
        if r.note:
            name += " *"
        rows.append((f"  {name}", format_size(r.total_bytes)))

    for left, right in rows:
        print(f"{left:<30} {right:>10}")

    print("─" * 40)
    total = sum(r.total_bytes for r in results if r.deletable)
    print(f"{'  Total reclaimable':<30} {format_size(total):>10}")

    # Print notes for non-deletable items
    notes = [r for r in results if r.note]
    if notes:
        print()
        for r in notes:
            print(f"  * {r.category_name}: {r.note}")

    print(f"\nRun 'macpurge clean' to free this space.")


def _cmd_clean(args):
    from .cleaner import clean
    from .paths import get_all_cleanable, get_categories_for_flag
    from .scanner import scan_all

    # Determine categories
    flag_map = {
        "caches": "caches",
        "logs": "logs",
        "xcode": "xcode",
        "dev": "dev",
        "trash": "trash",
        "ds_store": "ds-store",
        "temp": "temp",
    }

    selected = []
    for attr, flag in flag_map.items():
        if getattr(args, attr, False):
            selected.extend(get_categories_for_flag(flag))

    if args.all or not selected:
        categories = get_all_cleanable()
    else:
        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for cat in selected:
            if cat.key not in seen:
                seen.add(cat.key)
                deduped.append(cat)
        categories = deduped

    results = scan_all(categories=categories)
    clean(results, force=args.force)


def _cmd_big_files(args):
    from .scanner import scan_big_files
    from .paths import SKIP_DIR_NAMES
    from .utils import format_size, parse_size, shorten_path, progress_line, clear_progress

    min_bytes = parse_size(args.min_size)
    root = Path(args.path) if args.path else Path.home()

    progress_line(f"Searching for files larger than {format_size(min_bytes)}...")
    results = scan_big_files(min_bytes, root, SKIP_DIR_NAMES)
    clear_progress()

    if not results:
        print(f"No files larger than {format_size(min_bytes)} found.")
        return

    print(f"Files larger than {format_size(min_bytes)}:")
    total = 0
    for r in results:
        print(f"  {format_size(r.size):>10}  {shorten_path(r.path)}")
        total += r.size

    print(f"\nTotal: {format_size(total)} across {len(results)} files")


def _cmd_stale(args):
    from .scanner import scan_stale_files
    from .utils import format_size, shorten_path, confirm, progress_line, clear_progress
    import shutil

    if args.path:
        roots = [Path(args.path)]
    else:
        roots = [Path.home() / d for d in ("Documents", "Downloads", "Desktop")]

    progress_line(f"Searching for files not accessed in {args.days} days...")
    results = scan_stale_files(args.days, roots)
    clear_progress()

    if not results:
        print(f"No files found that haven't been accessed in {args.days} days.")
        return

    # Group by age bucket
    buckets = {
        "6-12 months": [],
        "1-2 years": [],
        "2+ years": [],
    }
    for r in results:
        if r.age_days < 365:
            buckets["6-12 months"].append(r)
        elif r.age_days < 730:
            buckets["1-2 years"].append(r)
        else:
            buckets["2+ years"].append(r)

    total_size = 0
    total_count = 0
    for label, items in buckets.items():
        if not items:
            continue
        bucket_size = sum(r.size for r in items)
        total_size += bucket_size
        total_count += len(items)
        print(f"\n{label} ({len(items)} files, {format_size(bucket_size)}):")
        for r in items[:20]:  # Show max 20 per bucket
            print(f"  {format_size(r.size):>10}  {shorten_path(r.path)}")
        if len(items) > 20:
            print(f"  ... and {len(items) - 20} more")

    print(f"\nTotal: {format_size(total_size)} across {total_count} files")

    if confirm("\nMove these files to Trash?"):
        trash = Path.home() / ".Trash"
        moved = 0
        for bucket_items in buckets.values():
            for r in bucket_items:
                try:
                    dest = trash / r.path.name
                    # Avoid overwriting files in Trash
                    if dest.exists():
                        stem = dest.stem
                        suffix = dest.suffix
                        i = 1
                        while dest.exists():
                            dest = trash / f"{stem} ({i}){suffix}"
                            i += 1
                    shutil.move(str(r.path), str(dest))
                    moved += 1
                except OSError:
                    continue
        print(f"Moved {moved} files to Trash.")


def _cmd_uninstall(args):
    from .uninstaller import uninstall
    uninstall(args.app_name, force=args.force)


def _cmd_ram():
    from .memory import ram_command
    ram_command()


def _cmd_overview():
    from .overview import show_overview
    show_overview()


def _cmd_schedule(args):
    from .schedule import setup_schedule, remove_schedule, show_status

    if args.remove:
        remove_schedule()
    elif args.status:
        show_status()
    else:
        setup_schedule(args.interval)


def _cmd_log(args):
    from .logger import AuditLogger

    logs = AuditLogger.get_recent_logs(args.last)
    if not logs:
        print("No logs found. Run 'macpurge clean' or 'macpurge uninstall' to create logs.")
        return

    for log_path in logs:
        print(f"\n{'─' * 50}")
        print(f"  {log_path.name}")
        print(f"{'─' * 50}")
        with open(log_path, encoding="utf-8") as f:
            print(f.read())


if __name__ == "__main__":
    main()
