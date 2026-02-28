"""Audit logging for all destructive operations."""

from datetime import datetime
from pathlib import Path

from .utils import format_size, shorten_path

LOG_DIR = Path.home() / ".macpurge" / "logs"


class AuditLogger:
    """Writes structured audit logs. Use as a context manager."""

    def __init__(self, operation: str = "clean"):
        self._operation = operation
        self._file = None
        self._count = 0
        self._total_bytes = 0
        self._log_path = None

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    def _ensure_log_dir(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _ensure_open(self):
        if self._file is None:
            self._ensure_log_dir()
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            self._log_path = LOG_DIR / f"{self._operation}-{timestamp}.log"
            self._file = open(self._log_path, "a", encoding="utf-8")

    def log_deletion(self, path: Path, size: int) -> None:
        """Log a single file/dir deletion."""
        self._ensure_open()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        size_str = format_size(size)
        display = shorten_path(path)
        self._file.write(f"[{ts}] DELETED  {size_str:>10}  {display}\n")
        self._file.flush()
        self._count += 1
        self._total_bytes += size

    def log_skipped(self, path: Path, reason: str) -> None:
        """Log a skipped file."""
        self._ensure_open()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        display = shorten_path(path)
        self._file.write(f"[{ts}] SKIPPED  {display}  ({reason})\n")
        self._file.flush()

    def log_summary(self) -> None:
        """Write final summary line."""
        if self._file is None:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._file.write(
            f"[{ts}] SUMMARY  Freed {format_size(self._total_bytes)} "
            f"across {self._count} operations\n"
        )
        self._file.flush()

    @property
    def freed_bytes(self) -> int:
        return self._total_bytes

    @property
    def deleted_count(self) -> int:
        return self._count

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.log_summary()
        self.close()

    @staticmethod
    def get_recent_logs(n: int = 10) -> list[Path]:
        """Return the N most recent log files, newest first."""
        if not LOG_DIR.exists():
            return []
        logs = sorted(
            LOG_DIR.glob("*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return logs[:n]
