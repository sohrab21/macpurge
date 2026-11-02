#!/bin/bash
# Build, sign and install Macpurge.app, and refresh the teammate zip.
# Encodes every gotcha from CLAUDE.md: build outside iCloud, Python 3.13,
# setuptools<80, site.pyo fix BEFORE xattr -c, stable signing identity.
#   bash tools/build_app.sh            build + install + relaunch + zip
#   bash tools/build_app.sh --no-install   build + zip only
set -euo pipefail
PROJ="$(cd "$(dirname "$0")/.." && pwd)"
STAGE=/tmp/macpurge-build
APP=/Applications/Macpurge.app
IDENTITY="Macpurge Self-Signed"
INSTALL=1; [[ "${1:-}" == "--no-install" ]] && INSTALL=0

echo "▸ Staging in $STAGE"
mkdir -p "$STAGE"
rsync -a --delete --exclude .venv --exclude build --exclude dist --exclude .git \
  --exclude dist-share "$PROJ/" "$STAGE/"
cd "$STAGE"
if [[ ! -x .venv/bin/python ]]; then
  /opt/homebrew/bin/python3.13 -m venv .venv
fi
.venv/bin/python -m pip install -q -e ".[gui,build]"

echo "▸ py2app"
rm -rf build dist
.venv/bin/python setup_py2app.py py2app > build.log 2>&1 || { tail -20 build.log; exit 1; }
B=dist/Macpurge.app
cp "$B/Contents/Resources/site.pyc" "$B/Contents/Resources/site.pyo"   # broken symlink fix
find "$B" -exec xattr -c {} +

if security find-identity -p codesigning | grep -q "$IDENTITY" || \
   security find-certificate -c "$IDENTITY" >/dev/null 2>&1; then
  echo "▸ Signing with \"$IDENTITY\" (Full Disk Access survives rebuilds)"
  codesign --force --deep --sign "$IDENTITY" "$B"
else
  echo "▸ Ad-hoc signing. Full Disk Access must be re-granted after install."
  echo "  Run tools/make_signing_cert.sh once to fix that."
  codesign --force --deep --sign - "$B"
fi
codesign --verify "$B"

echo "▸ Teammate zip"
mkdir -p "$PROJ/dist-share"
rm -f "$PROJ/dist-share/Macpurge.zip"
ditto -c -k --keepParent "$B" "$PROJ/dist-share/Macpurge.zip"

if [[ $INSTALL == 1 ]]; then
  echo "▸ Installing to $APP"
  pkill -f "$APP/Contents/MacOS/Macpurge" || true
  sleep 1
  rm -rf "$APP"
  ditto "$B" "$APP"
  codesign --verify "$APP"
  open "$APP"
fi
echo "✓ Done: version $(.venv/bin/python -c 'import macpurge;print(macpurge.__version__)')"
