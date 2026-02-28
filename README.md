# macpurge

CLI system maintenance tool for macOS. No GUI, no black box, no phoning home. Every operation is transparent, auditable, and dry-run by default.

## Install

```bash
cd macpurge
pip install .
```

For development:
```bash
pip install -e .
```

## Usage

### Quick system overview
```bash
macpurge overview
```

### Scan for reclaimable space
```bash
macpurge scan
```

### Clean reclaimable files
```bash
# Clean everything (shows summary, asks for confirmation)
macpurge clean

# Clean specific categories
macpurge clean --caches      # System + user + browser caches
macpurge clean --logs        # Log files
macpurge clean --xcode       # Xcode derived data, archives, device support
macpurge clean --dev         # npm, yarn, pip, Homebrew caches
macpurge clean --trash       # Empty Trash
macpurge clean --ds-store    # .DS_Store files
macpurge clean --temp        # Temp files

# Skip confirmation (for scripting/cron)
macpurge clean --force
```

### Find large files
```bash
macpurge big-files                    # Files > 500MB in ~/
macpurge big-files --min 100MB        # Files > 100MB
macpurge big-files --path /Downloads  # Search specific path
```

### Find stale files
```bash
macpurge stale                  # Not accessed in 180 days
macpurge stale --days 90        # Not accessed in 90 days
macpurge stale --path ~/Videos  # Search specific path
```

### Uninstall an app completely
```bash
macpurge uninstall Slack        # Removes app + all associated files
macpurge uninstall "Google Chrome" --force
```

### Free RAM
```bash
macpurge ram    # Show memory, offer to purge, list top consumers
```

### View logs
```bash
macpurge log           # Last 10 log files
macpurge log --last 5  # Last 5 log files
```

## Design

- **Dry-run by default.** Every destructive command shows what it would do and asks for confirmation. Use `--force` to skip.
- **Auditable.** Every deletion logged to `~/.macpurge/logs/`.
- **Safe.** Never touches `/System`, `/usr`, `~/Documents`, `~/Photos`, `~/Music` unless explicitly targeted.
- **No dependencies.** Python standard library only.

## Requirements

- macOS 13+
- Python 3.10+

## Caveats

- `macpurge stale` uses file access time (`st_atime`), which macOS may not update on APFS volumes by default. Access times may equal modification times.
- Docker space is reported but not deleted — use `docker system prune` directly.
- System-level paths (`/Library/Caches`, `/var/log`) may require `sudo`.
- `macpurge ram` runs `sudo purge` which requires admin password.
