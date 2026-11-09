"""Live memory readings for the menu bar, without spawning processes.

Everything goes through sysctl and libproc via ctypes (~5 ms per sample):

- Pressure: `kern.memorystatus_vm_pressure_level` (1 normal, 2 warn,
  4 critical): the signal Activity Monitor colours its graph by.
- Swap: `vm.swapusage` (struct xsw_usage, `used` at bytes 16-24).
- RAM used: hw.memsize minus free + speculative pages.
- Per process: the *physical footprint* (`proc_pid_rusage`,
  ri_phys_footprint), the same number as Activity Monitor's "Memory" column
  and `top -stats mem`. Unlike RSS it includes GPU memory (IOAccelerator),
  e.g. an MLX buffer cache. Only the user's own processes are readable
  without root; that is where the big consumers live anyway.

Processes are grouped the way people think about them: an app with all
its helpers and XPC services (macOS reports the "responsible" process),
virtual machines on their own line, and command-line programs by name
("python engine.py", "node server.js", "claude").

This module only reads. Quitting is done by the caller after asking.
"""

import ctypes
import os
import re
import struct
import time
from collections import deque
from dataclasses import dataclass, field

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
_libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)

try:
    _responsible = _libc.responsibility_get_pid_responsible_for_pid
    _responsible.argtypes, _responsible.restype = [ctypes.c_int], ctypes.c_int
except AttributeError:          # private API; fall back to "every process alone"
    _responsible = None

_libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]

GB = 1024 ** 3


# --- system-wide --------------------------------------------------------------

def _sysctl_raw(name: str, size: int = 64) -> bytes:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(size)
    if _libc.sysctlbyname(name.encode(), buf, ctypes.byref(n), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name}")
    return buf.raw[:n.value]


def _sysctl_int(name: str) -> int:
    raw = _sysctl_raw(name, 8)
    return int.from_bytes(raw, "little")


NORMAL, WARN, CRITICAL = "normal", "warn", "critical"


def pressure() -> str:
    level = _sysctl_int("kern.memorystatus_vm_pressure_level")
    return CRITICAL if level >= 4 else WARN if level >= 2 else NORMAL


def swap_used() -> int:
    raw = _sysctl_raw("vm.swapusage", 32)
    return struct.unpack_from("<Q", raw, 16)[0]


@dataclass
class SystemMemory:
    total: int
    used: int
    swap: int
    pressure: str


def system_memory() -> SystemMemory:
    total = _sysctl_int("hw.memsize")
    page = _sysctl_int("hw.pagesize")
    free = (_sysctl_int("vm.page_free_count") + _sysctl_int("vm.page_speculative_count")) * page
    return SystemMemory(total, max(total - free, 0), swap_used(), pressure())


# --- processes ----------------------------------------------------------------

_INTERPRETERS = re.compile(r"^(python[\d.]*|node|ruby|perl[\d.]*|php|java|bun|deno|Rscript|julia)$",
                           re.IGNORECASE)
_APP_RE = re.compile(r"^(.*?/([^/]+)\.app)/")
_VM_MARKERS = ("Virtualization.VirtualMachine", "qemu-system", "com.docker.virtualization",
               "com.docker.hyperkit", "VBoxHeadless", "prl_vm_app", "vmware-vmx")


def _pid_path(pid: int, buf=ctypes.create_string_buffer(4096)) -> str:
    n = _libproc.proc_pidpath(pid, buf, 4096)
    return buf.value[:n].decode("utf-8", "replace") if n > 0 else ""


_RUSAGE = (ctypes.c_uint64 * 40)()


def _footprint(pid: int) -> int | None:
    """ri_phys_footprint (offset 72 in rusage_info_v0), or None if not ours."""
    if _libproc.proc_pid_rusage(pid, 0, ctypes.byref(_RUSAGE)) != 0:
        return None
    return int(_RUSAGE[9])


def _argv(pid: int) -> list[str]:
    """argv via sysctl KERN_PROCARGS2 (only used for interpreters)."""
    mib = (ctypes.c_int * 3)(1, 49, pid)          # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(65536)
    buf = ctypes.create_string_buffer(size.value)
    if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return []
    raw = buf.raw[:size.value]
    if len(raw) < 4:
        return []
    argc = struct.unpack_from("<i", raw, 0)[0]
    # Layout: argc, exec path, NUL padding, argv[0..argc-1], environment.
    end = raw.find(b"\0", 4)
    if end < 0:
        return []
    i = end
    while i < len(raw) and raw[i] == 0:
        i += 1
    args = []
    for _ in range(argc):
        j = raw.find(b"\0", i)
        if j < 0:
            break
        args.append(raw[i:j].decode("utf-8", "replace"))
        i = j + 1
    return args


def _cli_name(pid: int, path: str) -> str:
    base = os.path.basename(path) or f"pid {pid}"
    if re.fullmatch(r"[\d.]+", base):
        # Versioned installs like ~/.local/share/claude/versions/2.1.318
        parts = path.split("/")
        if len(parts) >= 3:
            base = parts[-3] if parts[-2] == "versions" else parts[-2]
    if not _INTERPRETERS.match(base):
        return base
    short = (re.sub(r"[\d.]+$", "", base) or base).lower()   # Python3.13 -> python
    argv = _argv(pid)
    if argv and argv[0] and not _INTERPRETERS.match(os.path.basename(argv[0])):
        return os.path.basename(argv[0])     # process renamed itself (process.title)
    args = [a for a in argv[1:] if a]
    for i, a in enumerate(args):
        if a in ("-m", "-c") and i + 1 < len(args):
            return f"{short} -m {args[i + 1]}" if a == "-m" else f"{short} -c"
        if not a.startswith("-"):
            return f"{short} {os.path.basename(a)}"
    return short


@dataclass
class Consumer:
    key: str                 # stable id for history/alerts
    name: str                # what the menu shows
    footprint: int
    pids: list[int] = field(default_factory=list)
    kind: str = "app"        # app | cli | vm
    app_path: str = ""       # .app bundle path for apps (and the VM's owner)
    system: bool = False     # macOS's own background process: shown, never offered to quit
    growing: str = ""        # filled by RunawayWatch, e.g. "+6.2 GB in 10 min"


def consumers() -> list[Consumer]:
    """All of the user's processes grouped into apps, VMs and CLI programs,
    largest footprint first."""
    n = _libproc.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 128))()
    n = _libproc.proc_listallpids(buf, ctypes.sizeof(buf))
    own = os.getpid()
    paths: dict[int, str] = {}
    groups: dict[str, Consumer] = {}
    for pid in buf[:n]:
        if pid <= 0:
            continue
        fp = _footprint(pid)
        if fp is None:
            continue
        path = paths.get(pid) or _pid_path(pid)
        paths[pid] = path
        resp = _responsible(pid) if _responsible else pid
        rpath = paths.get(resp) or _pid_path(resp) if resp > 0 else ""
        if resp > 0:
            paths[resp] = rpath
        owner = _APP_RE.match(rpath)

        if any(m in path for m in _VM_MARKERS):
            owner = owner or _APP_RE.match(path)
            label = owner.group(2) if owner else "unknown"
            c = groups.setdefault(f"vm:{label}", Consumer(
                f"vm:{label}", f"Virtual machine ({label})", 0, kind="vm",
                app_path=owner.group(1) if owner else ""))
        elif (m := _APP_RE.match(path)) and ".framework/" not in m.group(1):
            # Inside an app bundle: belongs to the outermost app.
            if pid == own:
                continue
            c = groups.setdefault(f"app:{m.group(1)}", Consumer(
                f"app:{m.group(1)}", m.group(2), 0, kind="app", app_path=m.group(1)))
        elif owner and resp != pid and (path.startswith(("/System/", "/usr/libexec/"))
                                         or ".xpc/" in path):
            # A system service working for an app (e.g. Safari's WebContent).
            # Not for ordinary programs: python started in a terminal is the
            # terminal's "responsible" child, but it is its own consumer.
            c = groups.setdefault(f"app:{owner.group(1)}", Consumer(
                f"app:{owner.group(1)}", owner.group(2), 0, kind="app", app_path=owner.group(1)))
        else:
            name = _cli_name(pid, path)
            c = groups.setdefault(f"cli:{name}", Consumer(
                f"cli:{name}", name, 0, kind="cli",
                system=path.startswith(("/System/", "/usr/", "/Library/Apple/")) or not path))
        c.footprint += fp
        c.pids.append(pid)
    return sorted(groups.values(), key=lambda c: c.footprint, reverse=True)


# --- runaway detection -------------------------------------------------------

class RunawayWatch:
    """Remembers footprints over time and flags consumers that keep growing.

    Flagged when a consumer is at least `min_size`, grew by at least
    `min_growth` within the last `window` seconds, and most samples in that
    window went up. Each one is reported at most once per `quiet` seconds.
    Never acts on anything; the caller asks the user.
    """

    def __init__(self, min_size=4 * GB, min_growth=2 * GB, window=600, quiet=3600):
        self.min_size, self.min_growth = min_size, min_growth
        self.window, self.quiet = window, quiet
        self.history: dict[str, deque] = {}
        self.reported: dict[str, float] = {}

    def update(self, items: list[Consumer], now: float | None = None) -> list[Consumer]:
        now = now or time.time()
        new_alerts = []
        seen = set()
        for c in items:
            seen.add(c.key)
            h = self.history.setdefault(c.key, deque())
            h.append((now, c.footprint))
            while h and now - h[0][0] > self.window * 1.5:
                h.popleft()
            past = [(t, v) for t, v in h if now - t <= self.window]
            if len(past) < 3 or c.footprint < self.min_size:
                continue
            t0, v0 = past[0]
            ups = sum(1 for (_, a), (_, b) in zip(past, past[1:]) if b > a)
            if c.footprint - v0 >= self.min_growth and ups >= (len(past) - 1) * 0.6:
                minutes = max(int((now - t0) / 60), 1)
                c.growing = f"growing for {minutes} min"
                if now - self.reported.get(c.key, float("-inf")) > self.quiet:
                    self.reported[c.key] = now
                    new_alerts.append(c)
        for key in list(self.history):            # forget processes that ended
            if key not in seen:
                del self.history[key]
        return new_alerts


# --- idle apps -----------------------------------------------------------------

class ActivityLog:
    """When each app was last in front. Fed by NSWorkspace activation
    notifications (main thread). Apps not seen since Macpurge started count
    from their launch time or Macpurge's start, whichever is later."""

    def __init__(self):
        self.started = time.time()
        self.last_active: dict[str, float] = {}

    def activated(self, app_path: str, when: float | None = None) -> None:
        self.last_active[app_path] = when or time.time()

    def idle_seconds(self, app_path: str, launched: float | None, now: float | None = None) -> float:
        now = now or time.time()
        since = self.last_active.get(app_path) or max(self.started, launched or 0)
        return max(now - since, 0)


def format_gb(n: int) -> str:
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / 1024**2:.0f} MB"
