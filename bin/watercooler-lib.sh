# shellcheck shell=bash
# Shared helpers for the watercooler bus. Sourced by broadcast / general-inject
# / roster — not executable on its own.
#
# The log IS the registry. There is no separate conversation state file: every
# message carries its conversation id and its depth, so a thread can be
# reconstructed (and its depth recomputed) from ~/.ccgram/general.log alone.
# Nothing to corrupt, nothing to migrate, nothing to prune.

# WATERCOOLER_DIR / WATERCOOLER_TMUX_SESSION exist so the bus can be exercised
# against a throwaway feed and tmux session without touching the live room.
WC_DIR="${WATERCOOLER_DIR:-$HOME/.ccgram}"
WC_LOG="$WC_DIR/general.log"
WC_STATE="$WC_DIR/state.json"
WC_MUX="${WATERCOOLER_TMUX_SESSION:-ccgram}"
# ccgram's own control window lives in the same tmux session; it is not a peer.
WC_BOT_WINDOW="__main__"

# Live session handles (tmux window names), one per line.
wc_live_windows() {
  tmux list-windows -t "$WC_MUX" -F '#{window_name}' 2>/dev/null \
    | grep -vxF "$WC_BOT_WINDOW" || true
}

# Resolve a handle (with or without @) to an exact live window name, or "".
wc_resolve() {
  local want="${1#@}"
  wc_live_windows | grep -ixF "$want" | head -1
}

# Every session ccgram knows about, as "<handle>\t<live|dormant>\t<cwd>".
# Dormant = the Telegram topic still exists but idle-autoclose freed its tmux
# window; it is addressable in principle but cannot receive an injection now.
wc_roster() {
  local live
  live="$(wc_live_windows)"
  LIVE="$live" python3 - "$WC_STATE" <<'PY'
import json, os, sys

try:
    with open(sys.argv[1]) as fh:
        state = json.load(fh)
except Exception:
    sys.exit(0)

live = {n.lower() for n in os.environ.get("LIVE", "").split("\n") if n.strip()}
names = state.get("window_display_names") or {}
wins = state.get("window_states") or {}
bound = {
    wid
    for threads in (state.get("thread_bindings") or {}).values()
    for wid in threads.values()
}

rows = []
for wid in sorted(bound):
    name = names.get(wid) or (wins.get(wid) or {}).get("window_name") or wid
    cwd = (wins.get(wid) or {}).get("cwd", "")
    rows.append((name, "live" if name.lower() in live else "dormant", cwd))
for name, state_, cwd in sorted(rows):
    print(f"{name}\t{state_}\t{cwd}")
PY
}

# True if a handle names a session ccgram knows but whose window is not live.
wc_is_dormant() {
  local want="${1#@}"
  wc_roster | awk -F'\t' -v w="$want" 'tolower($1)==tolower(w) && $2=="dormant"{f=1} END{exit !f}'
}

# Extract @handles from a message body.
wc_mentions() {
  printf '%s' "$1" | grep -oE '@[A-Za-z0-9_.-]+' | sed 's/^@//' || true
}

# A short conversation id that does not already appear in the log.
wc_new_conv() {
  local id
  while :; do
    id="$(printf '%04x' $(( (RANDOM ^ $$) & 0xffff )))"
    grep -qF " #$id " "$WC_LOG" 2>/dev/null || { printf '%s' "$id"; return; }
  done
}

# Depth of the NEXT message in a conversation: agent messages delivered since
# the last human one. A human message resets it to 0 (they are the reason the
# room exists; their turn is never "depth"). Logged and surfaced, never
# enforced — a hard cap would cut off work mid-thread, and the failure mode
# here is politeness loops, which a visible number is enough to break.
wc_next_depth() {
  local conv="$1" role="$2"
  [ "$role" = human ] && { printf '0'; return; }
  [ "$conv" = "-" ] && { printf '0'; return; }
  awk -v conv="#$conv" '
    $2 == conv {
      d = $3; sub(/^d/, "", d)
      last = (d ~ /^[0-9]+$/) ? d + 0 : last
    }
    END { print last + 1 }
  ' "$WC_LOG" 2>/dev/null || printf '1'
}

# Append one message to the shared feed.
#   [19:33] #4f2a d2 agent/frontend: text
# Field order is fixed so awk can read it; the `role/sender` form keeps it
# obvious to a human (and to an agent that just cats the file) who spoke.
wc_log_line() {
  local conv="$1" depth="$2" role="$3" sender="$4" text="$5"
  mkdir -p "$WC_DIR"
  printf '[%s] #%s d%s %s/%s: %s\n' \
    "$(date '+%H:%M')" "$conv" "$depth" "$role" "$sender" "$text" >> "$WC_LOG"
}
