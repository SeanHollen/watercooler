#!/usr/bin/env bash
# watercooler auth installer — headless, browser-free Claude Code auth that
# keeps your Max subscription and never silently breaks.
#
# Usage:
#   ./install-auth.sh <token>   # first-time: token from `claude setup-token`
#   ./install-auth.sh           # re-run: reuse existing ~/.ccgram/auth.env
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CCG="$HOME/.ccgram"
mkdir -p "$CCG"

install -m 755 "$HERE/set-token.sh"     "$HOME/.local/bin/watercooler-set-token"
install -m 755 "$HERE/token-monitor.sh" "$HOME/.local/bin/watercooler-token-monitor"

# 1. Token
if [ -n "${1:-}" ]; then
  "$HOME/.local/bin/watercooler-set-token" "$1"
elif [ ! -f "$CCG/auth.env" ]; then
  echo "No token yet. Run:  claude setup-token   then:  $0 <token>" >&2
  exit 1
fi
[ -f "$CCG/auth.created" ] || date +%s > "$CCG/auth.created"

# 2. Source token in interactive shells + ccgram panes
if ! grep -q "ccgram/auth.env" "$HOME/.bashrc" 2>/dev/null; then
  printf '\n# Claude Code long-lived auth token (watercooler)\nset -a; [ -f "$HOME/.ccgram/auth.env" ] && . "$HOME/.ccgram/auth.env"; set +a\n' >> "$HOME/.bashrc"
fi

# 3. Reboot persistence: ccgram loads the token via EnvironmentFile
sudo install -o root -g root -m 644 "$HERE/30-auth-token.conf" \
  /etc/systemd/system/ccgram.service.d/30-auth-token.conf

# 4. Health/expiry monitor (daily; alerts Telegram before anything breaks)
sudo install -o root -g root -m 644 "$HERE/watercooler-token-monitor.service" \
  /etc/systemd/system/watercooler-token-monitor.service
sudo install -o root -g root -m 644 "$HERE/watercooler-token-monitor.timer" \
  /etc/systemd/system/watercooler-token-monitor.timer
sudo systemctl daemon-reload
sudo systemctl enable --now watercooler-token-monitor.timer

# 5. Wire the running tmux server now
TOKEN="$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' "$CCG/auth.env")"
[ -n "$TOKEN" ] && tmux setenv -g CLAUDE_CODE_OAUTH_TOKEN "$TOKEN" 2>/dev/null || true

echo "✅ watercooler auth installed: token wired (shells + systemd + tmux) and daily monitor enabled."
