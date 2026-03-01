# macpurge

CLI system maintenance tool for macOS. No GUI, no black box, no phoning home. Every operation is transparent, auditable, and dry-run by default.

Built in Python. Zero external dependencies.

## Quick Start

```bash
git clone https://github.com/sohrab21/macpurge.git
cd macpurge
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

Then add the alias to your shell so `macpurge` works from anywhere:

```bash
echo 'alias macpurge="~/macpurge/.venv/bin/macpurge"' >> ~/.zshrc
source ~/.zshrc
```

> Adjust the path if you cloned to a different location.

## Set Up Weekly Auto-Cleanup

```bash
macpurge schedule
```

Runs `macpurge clean --all --force` every 7 days automatically. Works even after sleep/standby.

## Usage

```bash
macpurge overview                  # System health snapshot
macpurge scan                      # Show reclaimable space by category
macpurge clean                     # Clean everything (asks for confirmation)
macpurge clean --caches            # System + user + browser caches
macpurge clean --logs              # Log files
macpurge clean --xcode             # Xcode derived data, archives, device support
macpurge clean --dev               # npm, yarn, pip, Homebrew caches
macpurge clean --trash             # Empty Trash
macpurge clean --ds-store          # .DS_Store files
macpurge clean --temp              # Temp files
macpurge clean --all --force       # Clean everything, skip confirmation
macpurge big-files                 # Files > 500MB
macpurge big-files --min 100MB     # Files > 100MB
macpurge stale                     # Files not accessed in 180 days
macpurge stale --days 90           # Files not accessed in 90 days
macpurge uninstall "App Name"      # Full app removal + all associated files
macpurge ram                       # Free RAM, show/kill top consumers
macpurge log                       # Review audit logs
macpurge schedule                  # Set up weekly auto-cleanup
macpurge schedule --status         # Check if schedule is active
macpurge schedule --remove         # Remove weekly schedule
```

## Contributing

```bash
git clone https://github.com/sohrab21/macpurge.git
cd macpurge
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

Make your changes in `src/macpurge/`, reinstall with `pip install .`, test, then submit a PR.

### Project Structure

```
src/macpurge/
  cli.py           # argparse entry point, all subcommands
  scanner.py       # disk scanning (os.scandir-based)
  cleaner.py       # deletion with dry-run + audit logging
  uninstaller.py   # app removal via bundle ID + remnant search
  memory.py        # RAM management (vm_stat, ps, purge)
  overview.py      # system dashboard
  schedule.py      # launchd weekly cleanup
  logger.py        # audit logging to ~/.macpurge/logs/
  paths.py         # all macOS paths centralized
  utils.py         # formatting, size calculations, confirmations
```

## Design

- **Dry-run by default.** Every destructive command shows what it would do and asks for confirmation. Use `--force` to skip.
- **Auditable.** Every deletion logged to `~/.macpurge/logs/` with timestamp, path, and size.
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
