#!/usr/bin/env bash
# watercooler-set-token <CLAUDE_CODE_OAUTH_TOKEN>
# Store a new long-lived Claude token, record its birth date, and wire it into
# the running tmux server. Use after `claude setup-token` prints a fresh token.
set -u
TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  echo "usage: watercooler-set-token <token-from-claude-setup-token>" >&2
  exit 1
fi
CCG="$HOME/.ccgram"
mkdir -p "$CCG"
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" > "$CCG/auth.env"
chmod 600 "$CCG/auth.env"
date +%s > "$CCG/auth.created"      # for the expiry monitor
rm -f "$CCG/auth-warned"            # reset the "warned" throttle
# Make live sessions' server hand it to new panes immediately
tmux setenv -g CLAUDE_CODE_OAUTH_TOKEN "$TOKEN" 2>/dev/null || true
echo "Token stored (600) + wired. New sessions will use it; restart old ones."
