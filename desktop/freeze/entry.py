"""PyInstaller entry — the jobscout CLI as a frozen backend binary.

The Tauri shell spawns this as `jobscout-server serve --port N`.
Path resolution: paths.repo_root() walks up from the frozen binary's
location (works when bundled from desktop/binaries/), or the shell sets
JOBSCOUT_HOME to the OS app-data dir.
"""

from jobscout.cli import app

if __name__ == "__main__":
    app()
