#!/usr/bin/env bash
# Install watercooler: the shared-room bus for ccgram, plus the ccgram source
# patches that ride along with it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME/.local/bin" "$HOME/.ccgram"

install -m 755 "$HERE/bin/broadcast"        "$HOME/.local/bin/broadcast"
install -m 755 "$HERE/bin/general-inject"   "$HOME/.local/bin/general-inject"
install -m 755 "$HERE/bin/roster"           "$HOME/.local/bin/roster"
install -m 644 "$HERE/bin/watercooler-lib.sh" "$HOME/.local/bin/watercooler-lib.sh"
cp "$HERE/patch/general_handler_new.txt"    "$HOME/.ccgram/general_handler_new.txt"

# Every patch, not a hand-maintained list — the previous enumeration silently
# left new patches uninstalled as they were added.
install -m 644 "$HERE"/patch/apply-*.py     "$HOME/.ccgram/"
install -m 755 "$HERE/patch/apply-all-patches.sh" "$HOME/.ccgram/apply-all-patches.sh"

# apply-all-patches.sh runs them in filename order and never aborts on one
# failure. Order matters in one place: apply-audio-transcribe-patch.py rewrites
# handlers/audio_transcribe.py wholesale, so any patch that EDITS that file must
# sort after it (apply-media-autostart-patch.py does).
"$HOME/.ccgram/apply-all-patches.sh"

# Re-apply on every ccgram start, so `uv tool upgrade ccgram` can't quietly
# revert the whole suite.
if [ -d /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    DROPIN=/etc/systemd/system/ccgram.service.d
    echo
    echo "Installing the auto-heal drop-in (needs sudo)…"
    sudo mkdir -p "$DROPIN"
    sed "s#__HOME__#$HOME#g" "$HERE/systemd/10-apply-patches.conf" \
        | sudo tee "$DROPIN/10-apply-patches.conf" >/dev/null
    sudo systemctl daemon-reload
fi

echo
echo "✅ watercooler installed."
echo "   Restart ccgram to load the patches, e.g.:  sudo systemctl restart ccgram"
echo "   Then: 'broadcast \"hello\"' or 'roster' from a session, or @mention a"
echo "   window in the General topic."
