#!/usr/bin/env bash
# Generic reauth-recovery spawn for any fiddler-only lane (no launchd label).
# Derives everything (workspace, env_file) from fiddler.json — the single source
# of truth — keyed by tmux session name. Matches fiddler.relaunch_session():
# OAuth token + GitHub identity via `-e` (bypasses keychain), bypassPermissions.
#
# WHY (task #6b281399, 2026-07-20, Orbit): a fiddler-only lane stuck on a stale
# "Please run /login · 401" relaunch state is orphaned from BOTH recoverers —
# fiddler exhausts its 3-attempt ladder ("left for human"), and
# fiddler-reauth-watchdog.py's feeder_agents() can only recover a lane that has a
# launchd label OR a reauth_recovery.spawn_cmd. This script is that spawn_cmd,
# shared by every fiddler-only lane so the whole class self-heals. Generalized
# from spark-gen/delta per-lane scripts.
#
# Usage: fiddler-lane-respawn.sh <tmux_session> [model=sonnet]
set -euo pipefail
TMUX=/opt/homebrew/bin/tmux
# FIDDLER_JSON override lets a second driver instance (e.g. the partner-Mesh
# lane in fiddler-proto.json) reuse this same recovery path. Default unchanged.
FIDDLER_JSON="${FIDDLER_JSON:-/Users/fleet/.config/fiddler/fiddler.json}"
SESSION="${1:?usage: fiddler-lane-respawn.sh <tmux_session> [model]}"
MODEL="${2:-sonnet}"

# Resolve lane config (name, workspace, env_file) from fiddler.json by tmux_session.
read -r NAME WS GH_ENV_FILE < <(python3 -c "
import json,sys
sess=sys.argv[1]
d=json.load(open('$FIDDLER_JSON'))
for a in d.get('agents',[]):
    if (a.get('tmux_session') or a.get('name','').lower())==sess:
        print(a.get('name',''), a.get('workspace',''), a.get('env_file',''))
        break
" "$SESSION")
[ -n "${NAME:-}" ] || { echo "ERROR: no lane with tmux_session=$SESSION in fiddler.json"; exit 1; }
[ -n "${WS:-}" ] || { echo "ERROR: lane $NAME has no workspace in fiddler.json"; exit 1; }

STATE_DIR=/Users/fleet/.fiddler/sessions/$SESSION
mkdir -p "$STATE_DIR"

# OAuth token from keys.env (same source fiddler uses)
# ── AUTH (Orbit 2026-07-24, root fix #8eba4210) ────────────────────────────────
# Do NOT inject CLAUDE_CODE_OAUTH_TOKEN here, and do not "restore" it.
# The CLI's credential resolver takes the env var as the PRIMARY credential and
# builds a session with {accessToken:<env>, refreshToken:null, expiresAt:null} —
# a session that CANNOT refresh, ever. After the first 401 it silently adopts the
# Keychain accessToken (still with no refreshToken) and sits on an ~8h bomb; when
# that expires nobody can renew it and the lane stalls until a human runs /login.
# That is the real source of the "4-hour" 401 windows.
# With no env var the CLI falls through to the Keychain and gets a REAL
# refreshToken + expiresAt, so the lane renews itself.
# The premise this block used to carry ("a launchd-spawned claude cannot read the
# macOS keychain", 2026-07-14) was MEASURED FALSE on this host: the login keychain
# is `no-timeout`, and a scratch pane on the launchd-born tmux server reads the
# keychain and authenticates with no env token. Atlas's 18 reauth events — the
# evidence that block cited — had a different cause.

# GitHub identity via -e (this recovery path is separate from fiddler.relaunch_session,
# so it must set GH_TOKEN/GIT_AUTHOR_* explicitly, else the session inherits the tmux
# server's ambient identity).
GH_ENV_ARGS=()
if [ -n "$GH_ENV_FILE" ] && [ -r "$GH_ENV_FILE" ]; then
  while IFS='=' read -r k v; do
    [ -n "$k" ] && GH_ENV_ARGS+=(-e "$k=$v")
  done < <(bash -c "set -a; source '$GH_ENV_FILE' >/dev/null 2>&1; for k in GITHUB_USERNAME GITHUB_TOKEN GH_TOKEN GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL; do v=\"\${!k}\"; [ -n \"\$v\" ] && echo \"\$k=\$v\"; done")
  echo "respawn[$NAME/$SESSION]: sourced GitHub identity from $(basename "$GH_ENV_FILE") ($((${#GH_ENV_ARGS[@]} / 2)) vars via -e)"
else
  echo "WARN: no readable env_file for $NAME in fiddler.json — session inherits ambient tmux identity; verify gh auth before push/PR" >&2
fi

if "$TMUX" has-session -t "$SESSION" 2>/dev/null; then
  echo "session $SESSION already exists"; exit 0
fi

"$TMUX" new-session -d -s "$SESSION" -n main -c "$WS" \
  -e "FIDDLER_STATE_FILE=$STATE_DIR/current.json" \
  ${GH_ENV_ARGS[@]+"${GH_ENV_ARGS[@]}"}
"$TMUX" send-keys -t "$SESSION" \
  "claude --dangerously-skip-permissions --permission-mode bypassPermissions --model $MODEL" Enter
echo "spawned $SESSION (name=$NAME model=$MODEL) — waiting for idle prompt..."

# wait up to 45s for claude to reach idle; treat a /login·401 pane as FAILURE
for i in $(seq 1 45); do
  sleep 1
  pane="$("$TMUX" capture-pane -t "$SESSION" -p 2>/dev/null || true)"
  case "$pane" in
    *"run /login"*|*"401"*|*"Invalid authentication"*) : ;;  # still stuck — keep waiting
    *"Welcome"*|*"? for shortcuts"*|*"❯"*) echo "session healthy"; exit 0 ;;
  esac
done
echo "WARN: no clean idle prompt within 45s — inspect: tmux attach -t $SESSION"
exit 0
