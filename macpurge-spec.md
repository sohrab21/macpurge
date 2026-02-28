# macpurge — Technical Specification

## Overview

**macpurge** is a command-line system maintenance tool for macOS. It gives the user full sovereignty over disk and memory cleanup — no GUI, no black box, no phoning home. Every operation is transparent, auditable, and dry-run by default.

Built in Python. Single package. No external dependencies beyond the standard library (use `shutil`, `os`, `subprocess`, `pathlib`). Target: macOS 13+ on Apple Silicon and Intel.

---

## Core Principles

1. **Dry-run by default.** Every destructive command shows what it *would* do and asks for explicit confirmation before acting. Use `--force` flag to skip confirmation (for scripting/cron use).
2. **No magic.** No AI, no heuristics that can't be explained. Deterministic operations on known paths.
3. **Auditable.** Every deletion is logged to `~/.macpurge/logs/` with timestamp, path, and size.
4. **Safe defaults.** Never touch user documents, photos, or anything outside known junk paths without explicit instruction.

---

## Installation

Installable via `pip install .` from the repo root. Should register `macpurge` as a CLI entry point using `setuptools` or `pyproject.toml` with `[project.scripts]`.

```
macpurge/
├── pyproject.toml
├── README.md
├── src/
│   └── macpurge/
│       ├── __init__.py
│       ├── cli.py              # argparse-based CLI entry point
│       ├── scanner.py          # disk scanning logic
│       ├── cleaner.py          # deletion logic with dry-run support
│       ├── uninstaller.py      # app uninstall logic
│       ├── memory.py           # RAM freeing and process management
│       ├── overview.py         # system overview / dashboard
│       ├── logger.py           # audit logging
│       ├── paths.py            # all macOS paths centralized here
│       └── utils.py            # formatting, size calculations, confirmations
```

---

## Subcommands

### `macpurge scan`

Full system scan. Shows a categorized summary of reclaimable space.

**Output example:**
```
macpurge scan results:
───────────────────────────────────
  System caches        2.4 GB
  User caches          5.1 GB
  Browser caches       1.8 GB
  Log files            890 MB
  Xcode derived data   12.3 GB
  .DS_Store files      4 MB
  Homebrew cache       1.1 GB
  npm/yarn cache       3.2 GB
  pip cache            640 MB
  Docker (if present)  8.7 GB
───────────────────────────────────
  Total reclaimable    36.1 GB

Run 'macpurge clean' to free this space.
```

**Paths to scan (centralized in `paths.py`):**

| Category | Paths |
|---|---|
| System caches | `/Library/Caches/*` |
| User caches | `~/Library/Caches/*` |
| Browser caches | `~/Library/Caches/Google/Chrome`, `~/Library/Caches/com.apple.Safari`, `~/Library/Caches/Firefox/Profiles/*/cache2` |
| Log files | `~/Library/Logs/*`, `/Library/Logs/*`, `/var/log/*` (read-only, skip if no permission) |
| Xcode | `~/Library/Developer/Xcode/DerivedData/*`, `~/Library/Developer/Xcode/Archives/*`, `~/Library/Developer/Xcode/iOS DeviceSupport/*` |
| .DS_Store | Recursive from `~/` (skip hidden dirs, node_modules, .git) |
| Homebrew | `~/Library/Caches/Homebrew/*` |
| npm | `~/.npm/_cacache/*` |
| yarn | `~/Library/Caches/Yarn/*` |
| pip | `~/Library/Caches/pip/*` |
| Docker | `~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw` (report size only, don't delete — prompt user to prune via Docker CLI) |
| Trash | `~/.Trash/*` |
| macOS temp | `/tmp/*`, `/var/folders/*/*/C/*` |

---

### `macpurge clean [--category] [--force]`

Deletes reclaimable files. Without a category flag, cleans everything found by `scan`.

**Category flags:**
- `--caches` — system + user + browser caches
- `--logs` — log files
- `--xcode` — Xcode derived data, archives, device support
- `--dev` — npm, yarn, pip, Homebrew caches
- `--trash` — empty macOS Trash
- `--ds-store` — .DS_Store files
- `--temp` — temp and /var/folders cleanup
- `--all` — everything

**Behavior:**
1. Run scan for selected categories.
2. Print summary of what will be deleted, grouped by category.
3. Prompt: `Proceed with deletion? [y/N]`
4. On confirmation, delete files and print freed space.
5. Log every deleted path to `~/.macpurge/logs/clean-YYYY-MM-DD-HHMMSS.log`

**With `--force`:** Skip confirmation. Useful for cron jobs or scripting.

---

### `macpurge big-files [--min SIZE] [--path PATH]`

Find large files on disk.

- `--min` — minimum file size, default `500MB`. Accepts `MB`, `GB` suffixes.
- `--path` — root path to search, default `~/`.

**Output example:**
```
Files larger than 500 MB:
  8.7 GB  ~/Library/Containers/com.docker.docker/Data/...
  4.2 GB  ~/Downloads/ubuntu-22.04.iso
  2.1 GB  ~/Movies/screen-recording.mov
  1.8 GB  ~/.gradle/caches/transforms-3/...

Total: 16.8 GB across 4 files
```

Skip scanning inside: `.git`, `node_modules`, `Time Machine Backups`, `/System`, `/Volumes` (external drives).

---

### `macpurge stale [--days N] [--path PATH]`

Find files not accessed in N days (default: 180).

- Uses `os.stat().st_atime` for last access time.
- `--path` default: `~/Documents`, `~/Downloads`, `~/Desktop`.
- Group output by age bucket: 6-12 months, 1-2 years, 2+ years.
- Offer to delete or move to Trash.

---

### `macpurge uninstall <AppName>`

Fully removes an application and all associated files.

**Steps:**
1. Locate app in `/Applications/` or `~/Applications/`.
2. Read the app's `CFBundleIdentifier` from `Info.plist` inside the `.app` bundle.
3. Search and list all associated files using the bundle identifier across:
   - `~/Library/Application Support/<BundleID or AppName>`
   - `~/Library/Preferences/<BundleID>.plist`
   - `~/Library/Caches/<BundleID>`
   - `~/Library/Containers/<BundleID>`
   - `~/Library/Group Containers/*<BundleID>*`
   - `~/Library/Saved Application State/<BundleID>.savedState`
   - `~/Library/HTTPStorages/<BundleID>`
   - `~/Library/WebKit/<BundleID>`
   - `/Library/Application Support/<BundleID or AppName>`
   - `/Library/Preferences/<BundleID>.plist`
   - `/Library/LaunchAgents/*<BundleID>*`
   - `/Library/LaunchDaemons/*<BundleID>*`
   - `~/Library/LaunchAgents/*<BundleID>*`
4. Present full list with sizes.
5. Confirm deletion.
6. Delete app bundle + all found associated files.
7. Log everything.

---

### `macpurge ram`

Free up RAM and show memory consumers.

**Steps:**
1. Show current memory state using `vm_stat` and `sysctl`:
   - Total RAM
   - Used / Free / Inactive / Wired
   - Swap usage
2. Run `sudo purge` to clear inactive memory (will prompt for password).
3. Show top 10 memory-consuming processes (via `ps aux --sort=-%mem`).
4. Offer to kill selected processes: `Kill process [PID / name]? [y/N]`

**Output example:**
```
Memory Overview:
  Total:     16 GB
  Used:      12.4 GB (wired: 3.1 GB, active: 6.2 GB, inactive: 3.1 GB)
  Free:      3.6 GB
  Swap:      2.1 GB used

Purging inactive memory... done.
  Freed: ~3.1 GB

Top memory consumers:
  1.  2.8 GB  Google Chrome (PID 1234) — 47 tabs
  2.  1.9 GB  Docker Desktop (PID 2345)
  3.  1.1 GB  Slack (PID 3456)
  4.  890 MB  Xcode (PID 4567)
  5.  640 MB  node (PID 5678)
  ...

Kill a process? Enter PID or 'skip': 
```

---

### `macpurge overview`

Quick system health snapshot. No destructive actions.

**Shows:**
- macOS version, chip, uptime
- Disk: total / used / free (with bar visualization)
- RAM: total / used / free / swap
- Top 5 disk consumers (directories)
- Top 5 RAM consumers (processes)
- Reclaimable space estimate (quick scan)

---

## Logging

All destructive operations log to `~/.macpurge/logs/`.

**Log format:**
```
[2026-02-28 14:32:01] DELETED  2.4 MB  ~/Library/Caches/com.spotify.client/Data/...
[2026-02-28 14:32:01] DELETED  1.1 MB  ~/Library/Caches/com.spotify.client/http/...
[2026-02-28 14:32:02] SUMMARY  Freed 3.5 MB across 2 operations
```

Include a `macpurge log [--last N]` subcommand to review recent activity.

---

## Error Handling

- Permission denied: Skip file, log warning, continue. Never crash on a single inaccessible file.
- Protected paths: Never touch `/System`, `/usr`, `/bin`, `/sbin`, `~/Documents`, `~/Photos`, `~/Music` (unless explicitly targeted by `big-files` or `stale` with user confirmation).
- SIP-protected files: Detect and skip gracefully.
- Running apps: Warn if trying to uninstall a currently running app. Offer to quit it first.

---

## Future Considerations (Not in v1)

- `macpurge schedule` — set up a launchd plist for periodic cleanup
- `macpurge duplicates` — find duplicate files by hash
- `macpurge startup` — manage login items
- Homebrew formula for easy team distribution
