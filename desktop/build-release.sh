#!/bin/sh
# desktop/build-release.sh — freeze the Python backend + build the .app/.dmg
# See desktop/README.md for what each step produces.
set -e
cd "$(dirname "$0")"

REPO="$(cd .. && pwd)"
VENV="$REPO/.venv/bin"

# Where the LIVE runtime lives. Set JOBSCOUT_HOME when building from a machine
# whose real config lives outside the checkout (the packaged app's home);
# unset it to snapshot the repo's own config/.
if [ -n "$JOBSCOUT_HOME" ]; then
  SRC="$JOBSCOUT_HOME"
  echo "── snapshotting config from JOBSCOUT_HOME: $SRC ──"
else
  SRC="$REPO"
fi

echo "── snapshot config → bundle defaults (the dmg ships your current config) ──"
rm -rf build/defaults-snapshot
mkdir -p build/defaults-snapshot
cp "$SRC/config/settings.yaml" \
   "$SRC/config/profile.yaml" \
   "$SRC/config/models.yaml" \
   "$SRC/config/watchlist.yaml" \
   build/defaults-snapshot/
# resume.yaml is optional here: it is not always present in a runtime home,
# and the repo's copy may be an unfilled skeleton.
if [ -f "$SRC/master_resume/resume.yaml" ]; then
  cp "$SRC/master_resume/resume.yaml" build/defaults-snapshot/
else
  echo "   (no master_resume/resume.yaml under $SRC — the app will seed it on first run)"
fi

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
  --add-data "$REPO/jobscout/packets/templates:jobscout/packets/templates" \
  --add-data "$REPO/desktop/build/defaults-snapshot:jobscout/defaults" \
  freeze/entry.py

echo "── 2/2 building the Tauri bundles ──"
npx tauri build

echo "done:"
ls -lh src-tauri/target/release/bundle/macos/jobscout.app \
      src-tauri/target/release/bundle/dmg/*.dmg 2>/dev/null
