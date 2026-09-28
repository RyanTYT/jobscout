#!/usr/bin/env bash
# jobscout uninstall — mirror of bootstrap.sh (PLAN §8).
# Boots out launchd agents, removes plists and the venv.
# Asks before touching data/ (SQLite DB + digests contain your history).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_="$(id -u)"

say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }

for label in com.jobscout.daily com.jobscout.agent com.jobscout.dashboard; do
  if launchctl bootout "gui/$UID_/$label" >/dev/null 2>&1; then
    say "booted out $label"
  else
    say "$label not loaded (fine)"
  fi
  rm -f "$LAUNCH_AGENTS/$label.plist"
done
say "removed launchd plists"

if [ -d "$REPO/.venv" ]; then
  say "removing .venv"
  rm -rf "$REPO/.venv"
fi

echo
read -r -p "Also delete data/ (SQLite DB — posting history, packets state)? [y/N] " ANSWER
case "$ANSWER" in
  y|Y) rm -rf "$REPO/data"; say "removed data/" ;;
  *)   say "kept data/" ;;
esac

say "done. The repo checkout itself ($REPO) is untouched."
