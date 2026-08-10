#!/usr/bin/env bash
# scripts/ship.sh — single entry point to ship a feature:
#   gate (checks) -> commit + push -> deploy (migrate + serve)
#
# Usage: ./scripts/ship.sh "<commit message>" [prod]
#
# The agent runs this after every completed, verified feature. Never ship red.
# See the `agent-ship` skill for the why.
set -euo pipefail

MSG="${1:-}"
TARGET="${2:-prod}"
if [ -z "$MSG" ]; then
  echo 'usage: ./scripts/ship.sh "<commit message>" [prod]' >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── project-specific ─────────────────────────────────────────────────────────

# The ccgram venv, NOT system python3. ccgram's own sources use PEP 758
# (`except A, B:`), which only parses on 3.14+ — and the patch scripts here
# carry ccgram source as string literals, so they get checked against the
# interpreter that will actually run the result.
CCGRAM_PY="$(ls -d "$HOME"/.local/share/uv/tools/ccgram/bin/python 2>/dev/null || true)"

run_gate() {
  local rc=0

  echo "    -> bash -n (shell syntax)"
  local sh_files
  sh_files="$(git ls-files '*.sh' bin/ | sort -u)"
  for f in $sh_files; do
    head -c2 "$f" | grep -q '#!' || continue
    head -1 "$f" | grep -qE 'bash|sh$' || continue
    bash -n "$f" || rc=1
  done

  if command -v shellcheck >/dev/null 2>&1; then
    echo "    -> shellcheck"
    # SC1091: sourced files live in ~/.local/bin at runtime, not in the repo.
    shellcheck -e SC1091 $sh_files || rc=1
  else
    echo "    -> shellcheck not installed, skipped"
  fi

  echo "    -> python syntax (patch scripts)"
  local py="${CCGRAM_PY:-python3}"
  [ -n "$CCGRAM_PY" ] || echo "       WARNING: ccgram venv python not found, using system python3"
  $py -m py_compile $(git ls-files 'patch/*.py') || rc=1

  echo "    -> patches are idempotent (re-apply must be a no-op)"
  # The suite re-runs on every ccgram start, so a patch that isn't stable on
  # rerun corrupts the installed ccgram rather than failing loudly. Applying
  # twice and diffing the tree is the only check that actually proves it.
  local cc snap
  cc="$(ls -d "$HOME"/.local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram 2>/dev/null || true)"
  if [ -n "$cc" ] && [ -x "$HOME/.ccgram/apply-all-patches.sh" ]; then
    snap="$(mktemp -d)"
    cp -a "$cc" "$snap/before"
    "$HOME/.ccgram/apply-all-patches.sh" >/dev/null 2>&1 || true
    if diff -rq -x '__pycache__' -x '*.pyc' "$snap/before" "$cc" >/dev/null; then
      echo "       ok (tree unchanged on re-apply)"
    else
      echo "       FAIL: re-applying the patch suite changed the installed ccgram" >&2
      diff -rq -x '__pycache__' -x '*.pyc' "$snap/before" "$cc" >&2 || true
      rc=1
    fi
    rm -rf "$snap"
  else
    echo "       ccgram not installed here, skipped"
  fi

  return $rc
}

migrate() {
  local target="$1"
  echo "    no database in this project — no migrations to run"
}

serve() {
  # "Serving" watercooler means installing it onto this box: copy bin/ and the
  # patch suite into place, apply the patches to the installed ccgram, and
  # restart the service so it loads them. ccgram.service uses KillMode=process,
  # so a restart does NOT kill live tmux/Claude sessions.
  local target="$1"
  if [ "$target" != "prod" ]; then
    echo "    unconfigured target: $target" >&2
    exit 1
  fi
  ./install.sh
  if systemctl list-unit-files ccgram.service >/dev/null 2>&1; then
    echo "    restarting ccgram"
    sudo systemctl restart ccgram
    sleep 3
    systemctl is-active --quiet ccgram || { echo "    ccgram did not come back up" >&2; exit 1; }
    echo "    ccgram active"
  fi
}

VALID_TARGETS="prod" # this Pi; watercooler has no remote environment
# ─────────────────────────────────────────────────────────────────────────────

case " $VALID_TARGETS " in
  *" $TARGET "*) ;;
  *) echo "unknown/unconfigured target: $TARGET (valid: $VALID_TARGETS)" >&2; exit 2 ;;
esac

echo "==> [1/3] Gate"
run_gate

echo "==> [2/3] Version control"
git add -A
if git diff --cached --quiet; then
  echo "    (no changes to commit — shipping current HEAD)"
else
  git commit -m "$MSG" \
    -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
fi
git push origin "$(git rev-parse --abbrev-ref HEAD)"

echo "==> [3/3] Deploy -> $TARGET"
migrate "$TARGET"
serve "$TARGET"

echo "==> Shipped: $MSG ($TARGET)"
