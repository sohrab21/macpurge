# macpurge

## Project

CLI system maintenance tool for macOS. Python 3.10+, stdlib only, zero external dependencies.

**Install:** `cd` into project root, `python3 -m venv .venv && source .venv/bin/activate && pip install .`

**Dev install:** `pip install -e .` (editable mode may need non-editable reinstall on Python 3.14+ due to .pth issues)

## Architecture

```
src/macpurge/
  cli.py           # argparse entry point, 9 subcommands
  scanner.py       # disk scanning (os.scandir-based, never follows symlinks)
  cleaner.py       # deletion with dry-run, protected-path guards, audit logging
  uninstaller.py   # app removal via bundle ID + remnant search
  memory.py        # RAM management (vm_stat, ps, purge)
  overview.py      # system dashboard (read-only)
  schedule.py      # launchd weekly cleanup setup
  logger.py        # AuditLogger (context manager, lazy file open)
  paths.py         # all macOS paths centralized (PathCategory dataclass)
  utils.py         # format_size, parse_size, confirm, is_protected_path, dir_size
```

## Key Patterns

- **Dry-run by default.** Every destructive command shows what it would do and prompts. `--force` skips confirmation.
- **Audit logging.** All deletions logged to `~/.macpurge/logs/` with timestamps and sizes.
- **Protected paths.** `/System`, `/usr`, `/bin`, `/sbin`, `~/Documents`, `~/Photos`, `~/Music` are never touched.
- **Sparse file handling.** Use `st_blocks * 512` (not `st_size`) for actual disk usage on APFS sparse files (e.g., Docker.raw).
- **macOS-specific:** `vm_stat` page size parsed dynamically (16KB Apple Silicon, 4KB Intel). `ps -m` for memory sort (not `--sort=-%mem`). `/var` resolves to `/private/var`.

## Critical: Determining App Usage for Uninstall Recommendations

**DO NOT rely on Spotlight metadata (`kMDItemLastUsedDate` / `mdls`).** It is frequently wrong — returns `(null)` for actively used apps (e.g., Slack running daily showed "never opened").

**Instead, check all of these signals:**

1. **Is it running right now?** (`pgrep -fi <app_name>`)
2. **File activity in user data directories** — scan for the most recently modified file in:
   - `~/Library/Application Support/<bundle_id or app_name>`
   - `~/Library/Containers/<bundle_id>`
   - `~/Library/Group Containers/*<bundle_id>*`
   - `~/Library/Preferences/<bundle_id>.plist`
   - `~/Library/Caches/<bundle_id>`
   - `~/Library/Saved Application State/<bundle_id>.savedState`
3. **Use `os.walk()` + `os.path.getmtime()`** to find the newest file in each matching directory. This is the most reliable signal of when an app was actually last used.
4. **Get the bundle ID** from `/Applications/<App>.app/Contents/Info.plist` using `plistlib`.

An app is safe to recommend for removal only when ALL of these are true:
- Not currently running
- No file activity in any user data directory for 30+ days
- Not a macOS system app

## Subcommands

| Command | Description |
|---------|-------------|
| `macpurge scan` | Categorized summary of reclaimable space |
| `macpurge clean [--caches\|--logs\|--xcode\|--dev\|--trash\|--ds-store\|--temp\|--all] [--force]` | Delete reclaimable files |
| `macpurge big-files [--min SIZE] [--path PATH]` | Find large files |
| `macpurge stale [--days N] [--path PATH]` | Find stale files |
| `macpurge uninstall <AppName> [--force]` | Full app removal |
| `macpurge ram` | Free RAM, show/kill top consumers |
| `macpurge overview` | System health snapshot |
| `macpurge schedule [--status\|--remove]` | Weekly automatic cleanup via launchd |
| `macpurge log [--last N]` | Review audit logs |

## GitHub

- Repo: https://github.com/sohrab21/macpurge (private)
- Branch: main
