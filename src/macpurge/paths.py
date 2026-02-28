"""Centralized macOS path definitions. Single source of truth for all paths."""

from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()


@dataclass
class PathCategory:
    name: str                              # Display name: "System caches"
    key: str                               # Internal key: "system_caches"
    paths: list[Path]                      # Paths to scan
    clean_flag: str                        # CLI flag: "caches", "logs", "dev", etc.
    deletable: bool = True                 # False = report-only (e.g. Docker)
    requires_sudo: bool = False
    description: str = ""
    skip_patterns: list[str] = field(default_factory=list)
    recursive_file_search: bool = False    # True for .DS_Store (search for files, not dir size)
    file_pattern: str = ""                 # e.g. ".DS_Store" when recursive_file_search=True


CATEGORIES: list[PathCategory] = [
    PathCategory(
        name="System caches",
        key="system_caches",
        paths=[Path("/Library/Caches")],
        clean_flag="caches",
        requires_sudo=True,
    ),
    PathCategory(
        name="User caches",
        key="user_caches",
        paths=[HOME / "Library" / "Caches"],
        clean_flag="caches",
    ),
    PathCategory(
        name="Browser caches",
        key="browser_caches",
        paths=[
            HOME / "Library/Caches/Google/Chrome",
            HOME / "Library/Caches/com.apple.Safari",
            HOME / "Library/Caches/Firefox/Profiles",
        ],
        clean_flag="caches",
    ),
    PathCategory(
        name="Log files",
        key="logs",
        paths=[
            HOME / "Library/Logs",
            Path("/Library/Logs"),
            Path("/var/log"),
        ],
        clean_flag="logs",
    ),
    PathCategory(
        name="Xcode derived data",
        key="xcode",
        paths=[
            HOME / "Library/Developer/Xcode/DerivedData",
            HOME / "Library/Developer/Xcode/Archives",
            HOME / "Library/Developer/Xcode/iOS DeviceSupport",
        ],
        clean_flag="xcode",
    ),
    PathCategory(
        name=".DS_Store files",
        key="ds_store",
        paths=[HOME],
        clean_flag="ds-store",
        recursive_file_search=True,
        file_pattern=".DS_Store",
    ),
    PathCategory(
        name="Homebrew cache",
        key="homebrew",
        paths=[HOME / "Library/Caches/Homebrew"],
        clean_flag="dev",
    ),
    PathCategory(
        name="npm cache",
        key="npm",
        paths=[HOME / ".npm/_cacache"],
        clean_flag="dev",
    ),
    PathCategory(
        name="yarn cache",
        key="yarn",
        paths=[HOME / "Library/Caches/Yarn"],
        clean_flag="dev",
    ),
    PathCategory(
        name="pip cache",
        key="pip",
        paths=[HOME / "Library/Caches/pip"],
        clean_flag="dev",
    ),
    PathCategory(
        name="Docker",
        key="docker",
        paths=[HOME / "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"],
        clean_flag="",
        deletable=False,
        description="Run 'docker system prune' to reclaim space",
    ),
    PathCategory(
        name="Trash",
        key="trash",
        paths=[HOME / ".Trash"],
        clean_flag="trash",
    ),
    PathCategory(
        name="macOS temp",
        key="temp",
        paths=[
            Path("/tmp"),
            Path("/var/folders"),
        ],
        clean_flag="temp",
    ),
]


def get_categories_for_flag(flag: str) -> list[PathCategory]:
    """Return categories matching a CLI flag ('caches', 'dev', etc.)."""
    return [c for c in CATEGORIES if c.clean_flag == flag]


def get_all_cleanable() -> list[PathCategory]:
    """Return all categories where deletable=True."""
    return [c for c in CATEGORIES if c.deletable]


def get_all_categories() -> list[PathCategory]:
    """Return all categories including non-deletable (for scanning)."""
    return list(CATEGORIES)


# Directories to search when uninstalling apps
UNINSTALL_SEARCH_DIRS: list[tuple[str, Path]] = [
    ("Application Support (user)", HOME / "Library/Application Support"),
    ("Preferences (user)", HOME / "Library/Preferences"),
    ("Caches (user)", HOME / "Library/Caches"),
    ("Containers", HOME / "Library/Containers"),
    ("Group Containers", HOME / "Library/Group Containers"),
    ("Saved Application State", HOME / "Library/Saved Application State"),
    ("HTTPStorages", HOME / "Library/HTTPStorages"),
    ("WebKit", HOME / "Library/WebKit"),
    ("Application Support (sys)", Path("/Library/Application Support")),
    ("Preferences (sys)", Path("/Library/Preferences")),
    ("LaunchAgents (sys)", Path("/Library/LaunchAgents")),
    ("LaunchDaemons", Path("/Library/LaunchDaemons")),
    ("LaunchAgents (user)", HOME / "Library/LaunchAgents"),
]

# Directories to skip during recursive file searches
SKIP_DIR_NAMES: set[str] = {".git", "node_modules", "Time Machine Backups", ".Trash"}

# Root paths to skip entirely in big-files/stale searches
SKIP_ROOTS: list[Path] = [Path("/System"), Path("/Volumes")]
