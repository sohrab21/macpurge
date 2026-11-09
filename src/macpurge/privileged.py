"""Run `purge` (Free RAM) without a password prompt.

`purge` needs root. Opt-in: a sudoers rule that allows exactly
`/usr/sbin/purge` for this user without a password and nothing else:

    /etc/sudoers.d/macpurge:  <user> ALL=(root) NOPASSWD: /usr/sbin/purge

Installing or removing the rule needs the admin password once. The rule
is checked with `visudo -c` before it is installed, because a broken
sudoers file would break sudo for the whole Mac.

Without the rule, `run_purge` falls back to the native password prompt.
"""

import os
import pwd
import re
import shlex
import subprocess
import tempfile

SUDOERS_FILE = "/etc/sudoers.d/macpurge"
PURGE = "/usr/sbin/purge"


def _user() -> str:
    name = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"unexpected user name: {name!r}")
    return name


def passwordless_enabled() -> bool:
    # The file is root-only (0440) but its existence is visible.
    return os.path.exists(SUDOERS_FILE)


def _admin(shell_cmd: str) -> tuple[bool, str]:
    """Run a shell command as root with the native macOS password prompt."""
    script = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(
        ["osascript", "-e", f'do shell script "{script}" with administrator privileges'],
        capture_output=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if "-128" in r.stderr:
        return False, "Password cancelled."
    return r.returncode == 0, r.stderr.strip()[:200]


def enable_passwordless() -> tuple[bool, str]:
    rule = f"{_user()} ALL=(root) NOPASSWD: {PURGE}\n"
    fd, tmp = tempfile.mkstemp(prefix="macpurge-sudoers-")
    with os.fdopen(fd, "w") as f:
        f.write(rule)
    q_tmp, q_dst = shlex.quote(tmp), shlex.quote(SUDOERS_FILE)
    try:
        return _admin(
            f"/usr/sbin/visudo -cf {q_tmp} && "
            f"/usr/bin/install -m 0440 -o root -g wheel {q_tmp} {q_dst}"
        )
    finally:
        os.unlink(tmp)


def disable_passwordless() -> tuple[bool, str]:
    return _admin(f"/bin/rm -f {shlex.quote(SUDOERS_FILE)}")


def run_purge() -> tuple[bool, str]:
    """Run purge: silently via the sudoers rule if present, else with the
    password prompt."""
    if passwordless_enabled():
        r = subprocess.run(["/usr/bin/sudo", "-n", PURGE], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=180)
        if r.returncode == 0:
            return True, ""
    return _admin(PURGE)
