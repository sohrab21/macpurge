# Macpurge changes

History, newest first. What the app does now is in `CLAUDE.md`.

## 0.4.0 · 09.11.2026 · Honest memory

Prompted by the Verbatim session's review: Verbatim's engine held 17 GB
(16.9 GB MLX GPU memory), swap was ~6 GB, and Macpurge showed almost
nothing.

- New `livemem.py`: pressure, swap, RAM used and per-process **physical
  footprint** via sysctl/libproc (ctypes). Footprint includes GPU memory;
  RSS did not (1 GB MLX array: footprint 1.0 GB, RSS 33 MB).
- Menu status: "Memory pressure: normal/high/critical" and "Swap: X GB in
  use" replace the misleading "RAM: 99% used".
- "Using the most memory" (was "Apps using the most memory", top 5 by
  RSS, apps only): top 8 by footprint, including command-line programs
  (`python engine.py`, `node …`, `claude`), virtual machines and
  Macpurge itself. macOS's own processes are listed but not clickable.
- Runaway alert: a process ≥ 4 GB that grew 2 GB+ within 10 min is marked
  "growing for N min" and triggers one notification per hour.
- New "Idle apps holding memory": apps with 1 GB+ not in front for 2 h.
- Every quit asks first; nothing is quit or killed automatically. CLI
  programs get SIGTERM (pids shown), VMs are stopped via their app.
- Leaner: one background sampler (~25 ms, no subprocesses) every 60 s, or
  15 s under pressure; removed the 0.5 s UI timer and the five
  subprocesses per refresh on the main thread; no scan at login and none
  while memory is tight.
- Free RAM notification now says "File cache cleared" (purge frees no
  memory that apps hold).
- Removed the old RSS-based memory helpers from `insights.py`.
- Kept `/etc/sudoers.d/macpurge` unchanged: Verbatim relies on it.

## 0.3.0 · 04.11.2026 · Uninstall apps, passwordless purge

- "Uninstall apps…" window (⌘U): multi-select with search, sorting, last
  used, sizes incl. leftovers; one confirmation; live per-app progress;
  permanent deletion (CleanMyMac-style), one admin prompt for root-owned
  apps, Finder Trash only as a last resort. Engine in `app_removal.py`.
- Short-lived 0.2.x step the same day: "Remove an app" submenu that moved
  any app to the Trash; replaced by the window.
- "Free RAM without password": opt-in sudoers rule for `/usr/sbin/purge`
  only (`privileged.py`). Touch ID is not possible without a Developer ID
  privileged helper.
- Fix: the bundled app runs with an ASCII locale, so `text=True`
  subprocess calls crashed on "…" in macOS messages (broke five removals).
  All subprocess calls now decode UTF-8 with replacement.
- Leftover matching tightened to exact name / bundle ID / `bundle ID + "."`.

## 0.2.0 · 02.11.2026 · Menu bar insights

- Status lines, top memory apps, scan submenus (reclaimable, large, old,
  unused apps), open at login, single-instance lock, fix for the
  duplicate icon started by the schedule, stable signing keychain.
