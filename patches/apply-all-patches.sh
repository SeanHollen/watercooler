#!/usr/bin/env bash
# Re-apply every ccgram source patch. Run automatically by ccgram.service's
# ExecStartPre so the patch suite self-heals after `uv tool upgrade ccgram`.
#
# Each apply-*.py is idempotent (prints "already patched" when a no-op). We never
# abort on a single patch failing: if an upgrade moved a code anchor, that one
# patch is skipped and ccgram still starts (unpatched for that feature) rather
# than the whole service failing to boot.
set -u
for p in "$HOME"/.ccgram/apply-*.py; do
    echo "[apply-all-patches] $p"
    python3 "$p" || echo "[apply-all-patches] WARN: $p failed (anchor changed?) — skipping"
done
exit 0
