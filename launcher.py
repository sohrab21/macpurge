"""py2app entry point.

Imports the package properly so relative imports work, and writes a
startup log to ~/.macpurge/logs/menubar.log so silent failures inside a
bundled .app are visible after the fact.
"""

import logging
import sys
import traceback
from pathlib import Path

LOG_DIR = Path.home() / ".macpurge" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "menubar.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("macpurge.launcher")
log.info("=== launcher.py start (python=%s) ===", sys.executable)

# CLI mode: called with a subcommand (e.g. by the launchd schedule as
# `Contents/MacOS/macpurge clean --all --force`). On case-insensitive APFS
# that path is the same file as the GUI binary, so without this dispatch the
# scheduled job would start a second menu bar app instead of cleaning.
# Finder launches pass no args (or a legacy -psn_ arg), so they stay GUI.
_cli_args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
if _cli_args:
    log.info("CLI mode: %s", " ".join(_cli_args))
    sys.argv = ["macpurge", *_cli_args]
    from macpurge.cli import main as cli_main
    sys.exit(cli_main())

try:
    from macpurge.menubar import main
    log.info("imported macpurge.menubar.main")
    main()
    log.info("main() returned cleanly")
except Exception:
    log.error("fatal:\n%s", traceback.format_exc())
    raise
