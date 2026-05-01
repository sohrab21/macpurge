# Macpurge — install on your Mac

A small menu-bar app that frees up RAM and clears caches to keep your Mac
running smoothly. Lives next to your clock, no Dock icon.

## What you get

Click the **macpurge** label in the menu bar:

- **RAM status** — at a glance, with a warning when memory is under pressure
- **Free RAM now…** — runs `purge` (asks for your password)
- **Scan for reclaimable space** — estimates how much could be cleaned
- **Clean caches now…** — deletes user caches, logs, browser caches,
  Homebrew/npm/pip caches, .DS_Store, /var temp files. Never touches
  ~/Documents, ~/Photos, or system directories.
- **Automatic cleanup** — pick Off / Daily / Weekly / Monthly
- **Open log folder** — see exactly what was cleaned and when

## Requirements

- macOS 11 (Big Sur) or newer
- **Apple Silicon** (M1, M2, M3, M4). This build does not run on Intel Macs.
- About 40 MB of disk space

## Install

1. Download **Macpurge.zip** from the latest release:
   <https://github.com/sohrab21/macpurge/releases/latest>
2. Unzip it. You'll get **Macpurge.app**.
3. Drag **Macpurge.app** into your `/Applications` folder.
4. **First launch (this is the annoying part):**
   - Right-click **Macpurge.app** → **Open**.
   - macOS will say *"Apple cannot verify this app is free from malware"* — click **Open** anyway.
   - If macOS just refuses with no Open button: open **System Settings → Privacy & Security**, scroll down to the message about Macpurge, and click **Open Anyway**.
   - You only have to do this once.
5. Look at your menu bar (top right of the screen). You should see **macpurge** near the clock.

## Grant Full Disk Access (recommended)

Without this, scans will miss files in `~/Library` and report less
reclaimable space than is really there.

1. Open **System Settings → Privacy & Security → Full Disk Access**.
2. Click the **+** button.
3. Navigate to `/Applications/Macpurge.app` and add it.
4. Toggle it **on**.

## Uninstall

1. Quit Macpurge from the menu bar (last menu item).
2. Drag `/Applications/Macpurge.app` to the Trash.
3. (Optional) If you turned on the weekly schedule, also delete:
   - `~/Library/LaunchAgents/com.macpurge.weekly.plist`
   - `~/.macpurge/` (logs)

## Things to know

- **Cleaning is destructive** but bounded. It only deletes files inside
  well-known cache directories. The app shows a confirmation dialog before
  every cleanup. Protected paths (Documents, Photos, Music, system dirs)
  are never touched.
- **Your password** is required for "Free RAM" and any cleanup that
  touches system caches. macOS shows the standard password prompt — your
  password never leaves your machine.
- **Automatic cleanup** runs `cleanup --all` quietly in the background and
  writes a log to `~/.macpurge/logs/`. Pick daily / weekly / monthly from
  the **Automatic cleanup** submenu, or set it to Off any time.
- **No telemetry, no network calls.** The app does nothing except what
  you click. Source: [github.com/sohrab21/macpurge](https://github.com/sohrab21/macpurge)

## Bug reports

Ping Sohrab. Include:
- What you clicked
- What you expected to happen
- What actually happened
- The contents of `~/.macpurge/logs/` if relevant
