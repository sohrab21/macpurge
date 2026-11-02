"""py2app build configuration for the Macpurge menu bar app.

Build with:
    pip install -e ".[build]"
    python tools/make_icons.py        # one-time, regenerates resources/icon.icns
    python setup_py2app.py py2app

Output:
    dist/Macpurge.app  (~24 MB, arm64)

Notes:
- LSUIElement=True makes this menu-bar-only (no Dock icon).
- The menu bar template image and the app .icns both live in resources/
  and are copied into Contents/Resources/ at build time.
- The bundle includes the macpurge package and its CLI entry point so that
  Clean caches can shell out to `macpurge clean --all --force` and produce
  the same audit log as a CLI run.
"""

from setuptools import setup

APP = ["launcher.py"]

# Files copied to Contents/Resources/ inside the bundle.
DATA_FILES = [
    ("", ["resources/menubar_icon.png", "resources/menubar_icon@2x.png",
            "resources/menubar_icon_alert.png", "resources/menubar_icon_alert@2x.png"]),
]

OPTIONS = {
    "argv_emulation": False,
    "optimize": 2,                         # strip docstrings + asserts from .pyc
    "iconfile": "resources/icon.icns",     # Finder / Dock app icon
    "plist": {
        "CFBundleName": "Macpurge",
        "CFBundleDisplayName": "Macpurge",
        "CFBundleIdentifier": "com.sohrab.macpurge",
        "CFBundleVersion": "0.2.0",
        "CFBundleShortVersionString": "0.2.0",
        "LSUIElement": True,               # menu-bar-only, no Dock icon
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "MIT licensed",
    },
    "packages": ["rumps", "macpurge"],
    "excludes": [
        "tkinter", "test", "unittest", "pydoc_data", "lib2to3",
        "idlelib", "turtledemo", "ensurepip",
    ],
}

setup(
    app=APP,
    name="Macpurge",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
