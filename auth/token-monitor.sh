#!/usr/bin/env bash
# watercooler-token-monitor
# Daily check: is Claude auth working, and is the long-lived token near expiry?
# Alerts the Telegram group so you're never surprised by a broken session.
set -u
CCG="$HOME/.ccgram"
AUTH="$CCG/auth.env"
CREATED="$CCG/auth.created"
WARNED="$CCG/auth-warned"
WARN_DAYS=7            # start nagging this many days before expiry
LIFETIME_DAYS=365     # setup-token validity

alert() {
  local msg="$1"
  local tok gid
  tok=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$CCG/.env" 2>/dev/null)
  gid=$(sed -n 's/^CCGRAM_GROUP_ID=//p'   "$CCG/.env" 2>/dev/null)
  [ -n "$tok" ] && [ -n "$gid" ] && curl -s -o /dev/null --max-time 15 \
    -X POST "https://api.telegram.org/bot$tok/sendMessage" \
    --data-urlencode "chat_id=$gid" --data-urlencode "text=$msg"
}

warn_once_per_day() {  # $1 = message; only sends once per calendar day
  local today; today=$(date +%Y-%m-%d)
  [ "$(cat "$WARNED" 2>/dev/null)" = "$today" ] && return 0
  echo "$today" > "$WARNED"
  alert "$1"
}

# Don't false-alarm during a network blip — only proceed if the internet is up.
curl -s -o /dev/null --max-time 10 https://api.anthropic.com/ || exit 0

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
set -a; [ -f "$AUTH" ] && . "$AUTH"; set +a

# 1. Is auth actually working right now?
logged_in=$(claude auth status 2>/dev/null | tr -d ' ,' | sed -n 's/.*"loggedIn":\([a-z]*\).*/\1/p')
if [ "$logged_in" != "true" ]; then
  warn_once_per_day "🔴 watercooler: Claude auth is BROKEN on the Pi (loggedIn=$logged_in). New sessions will fail. Renew: run 'claude setup-token', then 'watercooler-set-token <token>'."
  exit 0
fi

# 2. Approaching the 1-year expiry?
if [ -f "$CREATED" ]; then
  created=$(cat "$CREATED"); now=$(date +%s)
  left=$(( LIFETIME_DAYS - (now - created) / 86400 ))
  if [ "$left" -le "$WARN_DAYS" ]; then
    warn_once_per_day "🟡 watercooler: Claude auth token expires in ~${left} days. Renew soon: run 'claude setup-token', then 'watercooler-set-token <token>'."
  fi
fi
exit 0
