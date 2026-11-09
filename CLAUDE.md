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
  uninstaller.py   # CLI app removal via bundle ID + remnant search (old, see open items)
  app_removal.py   # GUI app removal engine: permanent delete, progress callback
  uninstall_window.py  # AppKit "Uninstall apps" window (PyObjC)
  privileged.py    # purge without password (opt-in sudoers rule) + admin prompt
  memory.py        # RAM management (vm_stat, ps, purge)
  overview.py      # system dashboard (read-only)
  schedule.py      # launchd weekly cleanup setup
  logger.py        # AuditLogger (context manager, lazy file open)
  paths.py         # all macOS paths centralized (PathCategory dataclass)
  utils.py         # format_size, parse_size, confirm, is_protected_path, dir_size
  menubar.py       # rumps-based menu bar GUI (optional, [gui] extra)
  livemem.py       # live memory: pressure, swap, per-process footprint (ctypes, no subprocesses)
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
- **Menu (v0.4.0, 09.11.2026), top to bottom:** Memory pressure (normal /
  high / critical), Swap (GB in use), Disk free, Last cleanup (with
  "overdue" flag) · Using the most memory (top 8, see below) · Idle apps
  holding memory · Free RAM now… · Scan now → Reclaimable space (clean per
  category), Large files and Old files (reveal in Finder only, never
  delete), Unused apps (click opens the Uninstall window with that app
  ticked) · Uninstall apps… (⌘U) · Clean caches now… · Automatic cleanup ·
  Open at login (LaunchAgent `com.macpurge.login`) · Free RAM without
  password · Open log folder · About (last 5 cleanups) · Quit. The status
  item stays icon-only (notch). Icon swaps to `menubar_icon_alert.png`
  when pressure is not normal or a process is flagged as growing.
  Notifications: low disk (< 20 GB, max once a day), "automatic cleanup
  done" when a clean log appears that the GUI didn't write, and runaway
  processes (below).
- **Memory, the honest version** (v0.4.0, `livemem.py`), built from the
  Verbatim session's review (09.11.2026): Verbatim's engine held 17 GB, of
  which 16.9 GB was MLX GPU memory, swap was ~6 GB, and Macpurge showed
  almost nothing because it measured RSS and hid CLI processes.
  - Per-process number = **physical footprint** (`proc_pid_rusage`
    ri_phys_footprint, offset 72 of rusage_info_v0), the same as Activity
    Monitor's "Memory" column and `top -stats mem`. Includes GPU memory:
    verified 1 GB MLX array = footprint 1.0 GB, RSS 33 MB. Only the user's
    own processes are readable without root (WindowServer, kernel_task
    and root daemons are not listed).
  - Pressure from `kern.memorystatus_vm_pressure_level` (1/2/4), swap from
    `vm.swapusage` (xsw_usage, used at bytes 16-24), RAM used from
    hw.memsize − (free + speculative pages). All via `sysctlbyname`.
  - Grouping: processes inside an .app (not inside a .framework) → that
    app; macOS services/XPC helpers (`/System/`, `/usr/libexec/`, `.xpc/`)
    → the app macOS reports as "responsible"
    (`responsibility_get_pid_responsible_for_pid`, private but exported);
    virtual machines → "Virtual machine (<owner app>)"; everything else is
    a CLI consumer named like `python engine.py`, `node server.js`,
    `claude` (argv via KERN_PROCARGS2; self-set process titles win;
    versioned installs `…/claude/versions/2.1.318` → `claude`). Do NOT
    attribute ordinary programs to their responsible process: a python
    started in a terminal is the terminal app's child (it showed up as
    "Rex Beta"), and Homebrew's python runs from a hidden `Python.app`
    inside `Python.framework`.
  - "Using the most memory": top 8 incl. CLI, VMs and Macpurge itself,
    footer "Includes GPU memory". macOS's own background processes are
    shown but not clickable.
  - **Runaway alert** (`RunawayWatch`): ≥ 4 GB and +2 GB within 10 min with
    mostly rising samples → row gets "growing for N min" + one
    notification per process per hour. Tested with a growing test process.
  - **Idle apps holding memory:** regular apps (not menu bar agents) with
    ≥ 1 GB footprint, not in front for 2 h. Front/back times come from
    NSWorkspace activate/deactivate notifications; apps not seen since
    Macpurge started count from their launch or Macpurge's start, so a
    Macpurge restart resets idle times.
  - **Never quits or kills anything automatically.** Every click asks
    first. Apps and VMs: `NSRunningApplication.terminate()` (normal quit,
    can save; a VM is stopped by quitting its owner app). CLI programs:
    SIGTERM to each pid, the dialog lists the pids.
  - **Lean:** one sampler thread, ~25 ms per sample, no subprocesses, every
    60 s at normal pressure and every 15 s otherwise; `refresh_soon()`
    wakes it after quit/clean/uninstall. The 0.5 s pump timer is gone
    (`_on_main` = `AppHelper.callAfter`). No scan at login: the first
    background scan runs 12 h after launch, and never while pressure is
    high.
- **Uninstall apps window** (v0.3.0, 04.11.2026, `uninstall_window.py`):
  Sohrab wanted CleanMyMac-style removal: pick several apps, watch them go,
  delete for good (no Trash). Plain AppKit window via PyObjC, cell-based
  NSTableView: checkbox · icon · name · size (app + leftovers) · last used.
  Search field, sortable columns, double-click toggles a row. One
  confirmation alert ("can't be undone", lists running apps and the
  password note), then rows switch to Waiting/Quitting/Deleting/Waiting
  for your password/✓ Deleted/✕ reason, with a progress bar and a
  "Done. N apps deleted, X freed" summary. The window can't be closed
  mid-run. After closing, status + a background rescan refresh the menu.
  - Listing (`insights.installed_apps`): every removable app, independent
    of usage. Removable = not Macpurge, not a symlink, not a built-in
    Apple app (`com.apple.*` only with `Contents/_MASReceipt`, i.e. App
    Store apps like iMovie). `/Applications`, `~/Applications` + one vendor
    folder level (skips `Utilities`). "Last used" uses the same signals as
    unused-app detection (`_last_activity`), ~15-30 s for ~30 apps.
  - Engine (`app_removal.uninstall`): per app quit if running (abort after
    15 s) → delete leftovers → rename the bundle into `~/.Trash/.macpurge-…`
    in one step (atomic, so no half-deleted apps) and rmtree it. Bundles
    the user can't rename (root-owned: Office, Zoom, App Store apps) are
    deleted together at the end with ONE `rm -rf` via `osascript … with
    administrator privileges`. Last resort: Finder → Trash, reported as
    "Moved to the Trash". `_allowed()` re-checks every path right before
    deletion (app bundles only directly in an Applications folder or one
    vendor folder below; leftovers only directly in the known ~/Library
    folders). Everything logged to `~/.macpurge/logs/uninstall-*.log`.
  - Leftover matching: exact name, exact bundle ID or `bundle ID + "."`
    prefix (never a bare prefix). Not covered yet: group containers and
    system-level helpers in `/Library`.
  - **Bundled app has an ASCII locale:** `subprocess(..., text=True)`
    crashes with UnicodeDecodeError as soon as macOS answers with "…" or
    curly quotes (broke five v0.2 removals on 04.11.). Always use
    `encoding="utf-8", errors="replace"`; never `text=True`.
  - Threads: work on daemon threads, UI via `AppHelper.callAfter`. Helper
    methods on the NSObject subclass need `@objc.python_method`.
  - Render QA without screen-recording permission: `cacheDisplayInRect`
    the content view to PNG (window background isn't drawn, so force the
    Aqua appearance for readable screenshots).
- Read-only data logic lives in `insights.py` (GUI-free, testable).
  Unused-app detection never trusts Spotlight alone: an app is "unused"
  only if not running AND its data folders, extension containers, the
  developer's group containers (by codesign TeamIdentifier, e.g. Office's
  `UBF8T346G9.Office`) and Spotlight all show nothing for 60 days.
  Without the group-container check, Outlook showed as "1353 days unused".
- Menu changes from worker threads go through `_on_main`
  (`AppHelper.callAfter`). One long job at a time (`_busy` lock).
- **Full Disk Access:** the background scan reads other apps' containers.
  Without FDA, macOS shows a privacy prompt and the scan thread blocks in
  `open()` until it is answered. So the app probes FDA first (reading
  TCC.db fails instantly with EPERM, no prompt), shows "Full Disk Access
  missing · click to fix" and skips scans until it is granted.
- **Free RAM without password** (04.11.2026, `privileged.py`): opt-in menu
  toggle. Installs `/etc/sudoers.d/macpurge` =
  `<user> ALL=(root) NOPASSWD: /usr/sbin/purge` (only that command),
  validated with `visudo -cf` before `install -m 0440 -o root -g wheel`,
  one admin prompt to turn on or off. When on, Free RAM runs
  `sudo -n /usr/sbin/purge` with no confirmation dialog and no prompt;
  if that fails it falls back to the password prompt. Touch ID was asked
  for and is not possible with `osascript … with administrator
  privileges`; it would need an SMAppService privileged helper, which
  needs a Developer ID. Uninstalling root-owned apps still asks for the
  password (deliberately not covered by the rule).
  **Contract with Verbatim:** Verbatim runs `sudo -n /usr/sbin/purge`
  itself (at most every 10 min) when `/etc/sudoers.d/macpurge` exists.
  Keep that path and the rule stable. Sohrab turned it on (seen 09.11.).
  Note: `purge` only drops the file cache; it frees nothing an app holds,
  so the notification now says "File cache cleared".
- Privileged operations (`purge`, system cache cleanup) use `osascript ...
  with administrator privileges` to surface the native macOS password
  prompt. The CLI's `purge_memory()` requires a TTY and won't work from a
  bundled `.app`; never call it from the GUI.
- Long-running work runs on `threading.Thread(daemon=True)`. Alerts and
  notifications must stay on the main thread (Cocoa requirement). Don't
  call `rumps.alert()` from a worker thread.

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

### Open items (as of 09.11.2026)

- **FDA survives rebuilds:** confirmed 04.11.2026 (no "Full Disk Access
  missing" after the build). Remove this line next time.
- **v0.4.0 memory menus: quit flows not click-tested** (data, grouping,
  runaway detection and GPU footprint were tested 09.11.). Watch for
  wrong grouping (a process under the wrong app) and alert noise.
- **Idle apps:** idle times reset when Macpurge restarts; persist them in
  `~/.macpurge/` if that turns out to matter.
- **Menu click-through not yet tested by Sohrab** (quit an app from "Apps
  using the most memory", the four scan submenus). The data side was
  tested against his Mac on 02.11.
- **Unused-app detection:** watch for false positives (an app in use that
  shows up as unused). Outlook/Excel/OneNote were false positives before
  the group-container check.
- **Uninstall window: no real deletion tested yet** (UI and progress were
  tested with a stand-in engine; the v0.2 Trash flow removed ClipGrab,
  Goodnotes, Loom, Outlook, Handy, Sublime Text for real on 04.11.). Watch
  for: App Management privacy block on bundle rename, root-owned apps via
  the password prompt, a running app.
- **Uninstall, later:** group containers (only if no other app from the
  same developer stays), system-level helpers in `/Library`
  (LaunchDaemons, PrivilegedHelperTools, Application Support) for Zoom,
  Webex, Teams, OneDrive, CleanMyMac.
- **CLI `macpurge uninstall` is still unsafe:** hard-deletes (rmtree) and
  matches names by substring. Should reuse `insights.installed_apps` +
  `app_removal.uninstall`.
- **Tests** for `insights.unused_apps` / `app_leftovers` / `installed_apps`
  and `app_removal._allowed` (the risky parts).
- **Re-send `dist-share/Macpurge.zip`** to teammates: older copies have the
  duplicate-icon bug. Teammates' copies are signed with Sohrab's
  self-signed cert, so they still see the Gatekeeper warning.
- Later, only if teammates use it: update check (needs a public version
  endpoint; the repo is private), Universal2 build for Intel Macs,
  Developer ID + notarization.

## GitHub

- Repo: https://github.com/sohrab21/macpurge (private)
- Branch: main
