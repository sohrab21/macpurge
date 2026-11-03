# macpurge

## Project

CLI system maintenance tool for macOS. Python 3.10+, stdlib only, zero external dependencies.

**Install:** `cd` into project root, `python3 -m venv .venv && source .venv/bin/activate && pip install .`

**Dev install:** `pip install -e .` (editable mode may need non-editable reinstall on Python 3.14+ due to .pth issues)

## How to start a session (read this first)

**User's intent:** free up RAM and keep the Mac running smoothly. Disk cleanup is secondary.

**Hard rule:** never delete files, kill processes, or run any destructive command without explicit confirmation from the user *for that specific action*. Dry-run output and prior approval don't count — confirm again before the real run.

**Workflow:**
1. Activate venv: `cd` into project root, `source .venv/bin/activate`.
2. Run `macpurge` (no args → overview). It ends with a "Recommended next step" line that picks `ram` / `scan` / `clean` based on current state.
3. Show the user the overview + recommendation. Wait for them to choose.
4. For `clean` / `uninstall` / `stale`: dry-run first, summarize what would be deleted, then ask before re-running with `--force` (or letting the prompt through).
5. For `ram`: it asks before killing processes — let the user answer the prompt, don't auto-confirm.

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
  menubar.py       # rumps-based menu bar GUI (optional, [gui] extra)
launcher.py        # py2app entry point shim — imports macpurge.menubar:main
setup_py2app.py    # py2app build config for Macpurge.app
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

## GUI / Distribution

The CLI ships as a stdlib-only Python package. The menu bar app is an
**optional** layer on top, gated by extras so the CLI install stays clean.

### Menu bar app (`macpurge.menubar`)

- Wraps the existing CLI — does not duplicate logic. `clean` is called
  in-process (`scan_all` + `run_clean`), never via subprocess: inside the
  bundle `Contents/MacOS/macpurge` is the same file as `Macpurge`
  (case-insensitive APFS), so spawning it starts a second menu bar app.
- `launcher.py` dispatches to the CLI when it gets a subcommand. The
  launchd schedule calls `Contents/MacOS/macpurge clean --all --force`;
  before this dispatch (fixed 02.11.2026) that started a second menu bar
  icon that never exited.
- **Single instance:** `main()` takes an `fcntl` lock on
  `~/.macpurge/menubar.lock`; a second launch logs and exits. The OS drops
  the lock when the process dies, so no stale locks.
- **v0.2.0 menu** (02.11.2026): status lines (RAM + memory pressure, disk
  free, last cleanup with "overdue" flag), "Apps using the most memory"
  (top 5 apps grouped by .app bundle, click = polite AppleScript quit;
  CLI processes are excluded on purpose), Scan now → submenus Reclaimable
  space (clean per category), Large files and Old files (reveal in Finder
  only, never delete), Unused apps (move to Trash via Finder, recoverable),
  Open at login (LaunchAgent `com.macpurge.login`), About with the last 5
  cleanups, keyboard shortcuts. Icon swaps to `menubar_icon_alert.png`
  (sparkle + dot) when `kern.memorystatus_vm_pressure_level` >= 2.
  Notifications: low disk (< 20 GB, max once a day) and "automatic cleanup
  done" when a clean log appears that the GUI didn't write.
- Read-only data logic lives in `insights.py` (GUI-free, testable).
  Unused-app detection never trusts Spotlight alone: an app is "unused"
  only if not running AND its data folders, extension containers, the
  developer's group containers (by codesign TeamIdentifier, e.g. Office's
  `UBF8T346G9.Office`) and Spotlight all show nothing for 60 days.
  Without the group-container check, Outlook showed as "1353 days unused".
- Menu changes from worker threads go through `_on_main` (queue drained by
  a 0.5s main-thread timer). One long job at a time (`_busy` lock).
- **Full Disk Access:** the background scan reads other apps' containers.
  Without FDA, macOS shows a privacy prompt and the scan thread blocks in
  `open()` until it is answered. So the app probes FDA first (reading
  TCC.db fails instantly with EPERM, no prompt), shows "Full Disk Access
  missing · click to fix" and skips scans until it is granted.
- Privileged operations (`purge`, system cache cleanup) use `osascript ...
  with administrator privileges` to surface the native macOS password
  prompt. The CLI's `purge_memory()` requires a TTY and won't work from a
  bundled `.app`; never call it from the GUI.
- Long-running work runs on `threading.Thread(daemon=True)`. Alerts and
  notifications must stay on the main thread (Cocoa requirement). Don't
  call `rumps.alert()` from a worker thread.
- Status refreshes every 60s via `rumps.Timer`.

Run during development:
```bash
pip install -e ".[gui]"
python -m macpurge.menubar
```

### Building Macpurge.app

**Use the script:** `bash tools/build_app.sh` does everything below
(stage outside iCloud, Python 3.13, setuptools<80, site.pyo fix, sign,
install, relaunch, refresh `dist-share/Macpurge.zip`). The manual steps
below are kept as reference for when the script breaks.

**Stable signing:** `tools/make_signing_cert.sh` (run once, by Sohrab in
Terminal; auto mode blocks keychain writes) creates the identity "Macpurge
Build" in its own keychain `~/Library/Keychains/macpurge-signing.keychain-db`
with a random password in `~/.macpurge/signing-keychain-pass`. The build
script unlocks it and signs with it, so there are no keychain dialogs and
the designated requirement (identifier + certificate leaf) stays the same,
so Full Disk Access survives rebuilds. The login keychain is not used: its
password differs from the Mac password on this machine, and codesign
prompts per subcomponent there (`errSecInternalComponent` when run
non-interactively). A leftover "Macpurge Self-Signed" identity in the login
keychain is unused.

**Stage outside iCloud.** This project lives under `~/Desktop` which is
iCloud Drive synced. The iCloud File Provider keeps re-applying
`com.apple.fileprovider.fpfs#P` and `com.apple.FinderInfo` xattrs to every
file in the bundle, which corrupts code signatures (`spctl` rejects with
"code has no resources but signature indicates they must be present").
Always build from a non-iCloud staging copy:

```bash
cp -R "$PWD" /tmp/macpurge-build
cd /tmp/macpurge-build && rm -rf .venv build dist
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[gui,build]"
.venv/bin/python setup_py2app.py py2app
```

Output: `dist/Macpurge.app` (~38 MB).

**Pin `setuptools<80`** (in the `build` extra). setuptools 84 breaks
py2app 0.28 with `Popen.__init__() got an unexpected keyword argument
'verbose'`.

**Patch `site.pyo` before `xattr -c`**, otherwise `find ... xattr -c`
aborts on the dangling symlink and signing never runs.

**Use Python 3.13, not 3.14.** rumps 0.4.0 + PyObjC + Python 3.14 builds
fine but the menu bar item silently never renders (NSStatusItem API
returns success but macOS doesn't draw it). Build via Homebrew's
`python@3.13`. py2app on 3.14 also produces a noisy "Modules with invalid
relative imports" warning that 3.13 avoids.

**Fix the broken `site.pyo` symlink** before signing. With `optimize=2`
(the default), py2app writes a symlink `Contents/Resources/lib/python3.X/
site.pyo → ../../site.pyo`, but the actual file is `site.pyc`. The
signature seal references `site.pyo` so verification fails. Patch every
build before codesign:

```bash
cp Contents/Resources/site.pyc Contents/Resources/site.pyo
```

**Install + sign:**

```bash
rm -rf /Applications/Macpurge.app
cp -R dist/Macpurge.app /Applications/
find /Applications/Macpurge.app -exec xattr -c {} +
cp /Applications/Macpurge.app/Contents/Resources/site.pyc \
   /Applications/Macpurge.app/Contents/Resources/site.pyo
codesign --force --deep --sign - /Applications/Macpurge.app
open /Applications/Macpurge.app
```

**Architecture caveat:** the build matches the host Python's architecture.
On Apple Silicon with a stock arm64 Python, the resulting `.app` runs on
arm64 only. To support Intel Macs, build with a `universal2` Python (e.g.
the python.org installer) — not done yet.

### Menu bar status item — design constraints

- **No title text** in the rumps `App(..., title=...)` constructor. A wide
  title (~100px) gets the status item placed underneath the notch on a
  MacBook with notch + busy menu bar — geometrically present, API reports
  `visible=True`, physically hidden. Icon-only is ~36px and fits in the
  gap between the notch and the leftmost existing right-side item.
- **Use `template=True`** with a chunky icon. Thin line art tints to
  near-invisibility on a dark menu bar. Current `resources/menubar_icon.png`
  is an 8-vertex 4-point star, tip radius 0.46×size, dent radius 0.10×size.

### Sharing with teammates

`Macpurge.zip` is ready under `dist-share/` (~10 MB). Use `ditto` to
preserve the signature and bundle structure — `zip` mangles symlinks:

```bash
cd /tmp/macpurge-build/dist
ditto -c -k --keepParent Macpurge.app Macpurge.zip
cp Macpurge.zip "<project>/dist-share/Macpurge.zip"
```

Drop it in Slack/Drive. Teammates follow `TEAM_README.md` — TL;DR: unzip,
drag to /Applications, right-click → Open the first time (Gatekeeper),
grant Full Disk Access.

**Not signed, not notarized.** Skipping the $99/year Apple Developer
Program means teammates see a Gatekeeper warning on first launch. Fine for
internal sharing. If we ever want clean install with no warnings, the path
is: enroll → codesign with Developer ID → notarize via `notarytool` →
staple. Not worth it unless the audience grows.

### Future GUI work

- Bundle a real template icon (PNG/PDF, black-on-transparent) at
  `resources/macpurge.icns` and reference it in `setup_py2app.py` plist +
  `OPTIONS["iconfile"]`. Currently the menu bar shows the text "macpurge".
- Universal2 build for Intel support.
- A SwiftUI front-end calling the same CLI is a clean upgrade path if a
  windowed app is ever wanted — keep CLI as the single source of truth.

## GitHub

- Repo: https://github.com/sohrab21/macpurge (private)
- Branch: main
