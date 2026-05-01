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

try:
    from macpurge.menubar import main
    log.info("imported macpurge.menubar.main")
    main()
    log.info("main() returned cleanly")
except Exception:
    log.error("fatal:\n%s", traceback.format_exc())
    raise
