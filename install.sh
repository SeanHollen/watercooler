#!/usr/bin/env bash
# Install watercooler: the shared-room bus for ccgram.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME/.local/bin" "$HOME/.ccgram"

install -m 755 "$HERE/bin/broadcast"        "$HOME/.local/bin/broadcast"
install -m 755 "$HERE/bin/general-inject"   "$HOME/.local/bin/general-inject"
cp "$HERE/patch/general_handler_new.txt"    "$HOME/.ccgram/general_handler_new.txt"
cp "$HERE/patch/apply-general-patch.py"     "$HOME/.ccgram/apply-general-patch.py"

python3 "$HOME/.ccgram/apply-general-patch.py"

echo
echo "✅ watercooler installed."
echo "   Restart ccgram to load the patch, e.g.:  sudo systemctl restart ccgram"
echo "   Then: 'broadcast \"hello\"' from a session, or @mention a window in the General topic."
