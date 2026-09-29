#!/bin/sh
# desktop/build-release.sh — freeze the Python backend + build the .app/.dmg
# See desktop/README.md for what each step produces.
set -e
cd "$(dirname "$0")"

REPO="$(cd .. && pwd)"
VENV="$REPO/.venv/bin"

echo "── 1/2 freezing jobscout backend (PyInstaller onedir) ──"
"$VENV/pyinstaller" \
  --name jobscout-server --onedir \
  --specpath freeze --distpath binaries --workpath build/pyinstaller \
  --paths "$REPO" \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  --noconfirm \
  --add-data "$REPO/jobscout/webapp/templates:jobscout/webapp/templates" \
  --add-data "$REPO/jobscout/webapp/static:jobscout/webapp/static" \
  --add-data "$REPO/jobscout/defaults:jobscout/defaults" \
  --add-data "$REPO/jobscout/packets/templates:jobscout/packets/templates" \
  freeze/entry.py

echo "── 2/2 building the Tauri bundles ──"
npx tauri build

echo "done:"
ls -lh src-tauri/target/release/bundle/macos/jobscout.app \
      src-tauri/target/release/bundle/dmg/*.dmg 2>/dev/null
