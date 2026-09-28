#!/usr/bin/env bash
# jobscout bootstrap — deploy on the TARGET Mac (PLAN §8).
#
# ⚠  NEVER run this on the dev Mac. It installs launchd agents and is meant for
#    the always-on deployment Mac. It is idempotent and safe to re-run.
#
# Usage:
#   ./bootstrap.sh [--dry-run] [--with-agent] [--with-dashboard] [--skip-env]
#
#   --dry-run         print steps, change nothing
#   --with-agent      also install com.jobscout.agent (07:00 morning brief)
#   --with-dashboard  also install com.jobscout.dashboard (KeepAlive, :8787)
#   --skip-env        do not prompt for the LLM API key (set it later in .env)

set -euo pipefail

# ── resolve repo root (this script's directory) ──────────────────────────────
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv"
BIN="$VENV/bin"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOGS="$REPO/logs"
UID_="$(id -u)"

DRY_RUN=0; WITH_AGENT=0; WITH_DASHBOARD=0; SKIP_ENV=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --with-agent) WITH_AGENT=1 ;;
    --with-dashboard) WITH_DASHBOARD=1 ;;
    --skip-env) SKIP_ENV=1 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
skip() { printf '\033[2m  (dry-run) %s\033[0m\n' "$*"; }

run() {  # run unless dry-run
  if [ "$DRY_RUN" -eq 1 ]; then skip "$*"; else "$@"; fi
}
write() {  # write file from stdin unless dry-run
  if [ "$DRY_RUN" -eq 1 ]; then skip "write $1"; else cat > "$1"; fi
}

echo "jobscout bootstrap — target: $REPO"
[ "$DRY_RUN" -eq 1 ] && echo "(dry run — nothing will change)"

# ── 1. platform + python ─────────────────────────────────────────────────────
if [ "$(uname)" != "Darwin" ]; then
  echo "✗ this bootstrap is macOS-only (launchd). For other platforms, run jobscout via cron/systemd yourself." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 not found. Install Python ≥3.11 (brew install python@3.12)." >&2
  exit 1
fi
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
say "python3 $PYVER"

# ── 2. venv + package ────────────────────────────────────────────────────────
say "creating venv + installing jobscout (editable, [all])"
if [ "$DRY_RUN" -eq 1 ]; then
  skip "python3 -m venv .venv"
  skip ".venv/bin/pip install -e '.[all]'"
else
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$BIN/pip" install --upgrade pip >/dev/null
  "$BIN/pip" install -e "$REPO[all]"
fi

# ── 3. .env ──────────────────────────────────────────────────────────────────
say "environment (.env)"
if [ "$SKIP_ENV" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ ! -f "$REPO/.env" ]; then
  cp "$REPO/.env.example" "$REPO/.env"
  echo "Enter your LLM API key (OpenRouter/DeepSeek/...). Leave blank to skip:"
  read -rs KEY
  if [ -n "$KEY" ]; then
    python3 - "$REPO/.env" "$KEY" <<'PYEOF'
import re, sys
path, key = sys.argv[1], sys.argv[2]
text = open(path).read()
text = re.sub(r"(JOBSCOUT_LLM_API_KEY=).*", lambda m: m.group(1) + key, text)
open(path, "w").write(text)
PYEOF
    echo "  key stored in .env (gitignored)"
  else
    echo "  skipped — jobscout doctor will warn; edit .env later"
  fi
elif [ "$DRY_RUN" -eq 1 ]; then
  skip "copy .env.example → .env + prompt for JOBSCOUT_LLM_API_KEY"
else
  echo "  .env already present — leaving it alone"
fi

# ── 4. DB + config check ─────────────────────────────────────────────────────
say "database + config"
if [ "$DRY_RUN" -eq 1 ]; then
  skip "jobscout db init; jobscout config check"
else
  "$BIN/jobscout" db init
  "$BIN/jobscout" config check
fi

# ── 5. launchd agents ────────────────────────────────────────────────────────
mkdir -p "$LAUNCH_AGENTS" "$LOGS"

plist_daily() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobscout.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN/jobscout</string>
    <string>run</string>
    <string>--daily</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$LOGS/daily.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/daily.err.log</string>
</dict>
</plist>
EOF
}

plist_agent() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobscout.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN/jobscout</string>
    <string>agent</string>
    <string>--morning</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$LOGS/agent.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/agent.err.log</string>
</dict>
</plist>
EOF
}

plist_dashboard() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobscout.dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN/jobscout</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/dashboard.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/dashboard.err.log</string>
</dict>
</plist>
EOF
}

install_agent_plist() {
  local label="$1" path="$2"
  # idempotent: bootout old, write new, bootstrap new
  launchctl bootout "gui/$UID_/$label" >/dev/null 2>&1 || true
  write "$path"
  launchctl bootstrap "gui/$UID_" "$path" >/dev/null 2>&1 || launchctl load "$path"
  echo "  installed $label"
}

say "launchd: com.jobscout.daily (06:30 — missed jobs fire on wake)"
if [ "$DRY_RUN" -eq 1 ]; then skip "install $LAUNCH_AGENTS/com.jobscout.daily.plist"
else plist_daily | install_agent_plist com.jobscout.daily "$LAUNCH_AGENTS/com.jobscout.daily.plist"; fi

if [ "$WITH_AGENT" -eq 1 ]; then
  say "launchd: com.jobscout.agent (07:00 morning brief)"
  if [ "$DRY_RUN" -eq 1 ]; then skip "install $LAUNCH_AGENTS/com.jobscout.agent.plist"
  else plist_agent | install_agent_plist com.jobscout.agent "$LAUNCH_AGENTS/com.jobscout.agent.plist"; fi
else
  echo "  (agent agent skipped — pass --with-agent when the discovery switch is ready, P5)"
fi

if [ "$WITH_DASHBOARD" -eq 1 ]; then
  say "launchd: com.jobscout.dashboard (KeepAlive, http://127.0.0.1:8787)"
  if [ "$DRY_RUN" -eq 1 ]; then skip "install $LAUNCH_AGENTS/com.jobscout.dashboard.plist"
  else plist_dashboard | install_agent_plist com.jobscout.dashboard "$LAUNCH_AGENTS/com.jobscout.dashboard.plist"; fi
else
  echo "  (dashboard agent skipped — pass --with-dashboard, or run: jobscout serve)"
fi

# ── 6. final doctor ──────────────────────────────────────────────────────────
say "doctor"
if [ "$DRY_RUN" -eq 1 ]; then
  skip "jobscout doctor"
else
  "$BIN/jobscout" doctor || true   # warnings are fine at bootstrap; failures print above
fi

echo
echo "✅ bootstrap complete. Next:"
echo "   • fill in master_resume/resume.yaml, then:  $BIN/jobscout resume validate"
echo "   • fill the watchlist (config/watchlist.yaml) before the first P1 run"
echo "   • logs: $LOGS · data: $REPO/data/jobscout.db"
echo "   • remove everything later:  ./uninstall.sh"
