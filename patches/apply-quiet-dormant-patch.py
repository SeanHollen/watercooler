#!/usr/bin/env python3
"""Stop dormant (keep-topic autoclosed) topics being re-announced + re-closed.

THE BUG. apply-keeptopic-patch.py frees an idle session's RAM by killing its tmux
window while KEEPING the Telegram topic and its thread->window binding, so the
topic can later be revived with full context. ccgram's dead-window detector cannot
tell that apart from a crash, and its "already told them" guard (``_dead_notified``
in polling_state.py) is an in-memory set that is wiped on every process start.

So on every ccgram restart — which the systemd drop-in triggers on each patch
run — the poll loop found every dormant topic's binding pointing at a window_id
that no longer exists, re-announced each one as freshly dead (emoji flip +
recovery banner) and armed a fresh "dead" autoclose timer. AUTOCLOSE_DEAD_MINUTES
later all those timers expired together, producing a batch of "topic closed"
notifications for topics that had been closed for hours. Then the next restart did
it again, forever.

THE FIX. Persist the deliberately-freed (user_id, thread_id, window_id) triples to
~/.ccgram/dormant.json, and check that file at the top of the dead-window
notifier. A dormant window is then not a crash: no banner, no emoji flip, and —
the part that actually stops the batch closes — no "dead" autoclose timer. Markers
are cleared in clear_dead_notification(), which every revive path already calls
(text_handler, recovery_banner, resume_command), so resuming a dormant topic keeps
working exactly as before.

ORDERING: must run AFTER apply-keeptopic-patch.py, since it edits the block that
patch installs. "quiet" > "keeptopic" alphabetically, and apply-all-patches.sh
globs in sorted order, so this is handled by the filename. Keep it that way.

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""

import glob
import pathlib
import sys

matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram"
    )
)
if not matches:
    sys.exit("ERROR: ccgram package not found")
PKG = pathlib.Path(matches[0])
print(f"[quiet-dormant] ccgram at {PKG}")


def edit(relpath: str, label: str, anchor: str, new: str, *, before: bool = False):
    """Insert `new` around a unique `anchor` in PKG/relpath. Idempotent."""
    f = PKG / relpath
    src = f.read_text()
    if new.strip() and new.strip() in src:
        print(f"  = {label}: already patched")
        return
    n = src.count(anchor)
    if n != 1:
        sys.exit(f"ERROR: {label}: anchor found {n}x in {relpath} (expected 1)")
    bak = f.with_name(f.name + ".orig")
    if not bak.exists():
        bak.write_text(src)
    i = src.index(anchor)
    src = (
        src[:i] + new + src[i:]
        if before
        else src[: i + len(anchor)] + new + src[i + len(anchor) :]
    )
    f.write_text(src)
    print(f"  + {label}: patched")


# ── 1. the persisted marker store ────────────────────────────────────────────

STORE = '''"""Persisted "deliberately freed" markers for keep-topic autoclose.

Added by ~/.ccgram/apply-quiet-dormant-patch.py — not part of upstream ccgram.

A window killed by keep-topic autoclose is dormant, not dead: its topic and
thread binding survive on purpose so the session can be resumed. ccgram's
dead-window guard is in-memory only, so without an on-disk record every restart
re-announced every dormant topic and re-armed its autoclose timer. This module is
that record.

Deliberately dependency-free (no ccgram imports) so it can be imported from
polling_state, topic_lifecycle and window_tick without forming a cycle. Every
failure path degrades to "not dormant", i.e. to stock ccgram behaviour — a
corrupt or unwritable state file must never break the bridge.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_PATH = Path(
    os.getenv("CCGRAM_DORMANT_FILE", "~/.ccgram/dormant.json")
).expanduser()

_cache: set[str] | None = None
_cache_mtime: float | None = None


def _key(user_id: int, thread_id: int, window_id: str) -> str:
    return f"{user_id}:{thread_id}:{window_id}"


def _load() -> set[str]:
    """Current marker set, re-read only when the file's mtime moves."""
    global _cache, _cache_mtime
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        _cache, _cache_mtime = set(), None
        return _cache
    if _cache is not None and _cache_mtime == mtime:
        return _cache
    try:
        raw = json.loads(_PATH.read_text())
        entries = {str(k) for k in raw} if isinstance(raw, list) else set()
    except (OSError, ValueError) as e:
        logger.debug("dormant_load_failed", error=str(e))
        entries = set()
    _cache, _cache_mtime = entries, mtime
    return entries


def _save(entries: set[str]) -> None:
    """Atomically replace the marker file; cache stays authoritative on failure."""
    global _cache, _cache_mtime
    _cache = set(entries)
    _cache_mtime = None
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(_PATH.parent), prefix=".dormant-", suffix=".json"
        )
        with os.fdopen(fd, "w") as fh:
            json.dump(sorted(entries), fh)
        os.replace(tmp, _PATH)
        _cache_mtime = _PATH.stat().st_mtime
    except OSError as e:
        logger.debug("dormant_save_failed", error=str(e))


def is_dormant(user_id: int, thread_id: int, window_id: str) -> bool:
    """True if this window was deliberately freed rather than lost."""
    return _key(user_id, thread_id, window_id) in _load()


def mark_dormant(user_id: int, thread_id: int, window_id: str) -> None:
    """Record that keep-topic autoclose freed this window on purpose."""
    entries = set(_load())
    k = _key(user_id, thread_id, window_id)
    if k not in entries:
        entries.add(k)
        _save(entries)


def clear_dormant(user_id: int, thread_id: int) -> None:
    """Drop every marker for a topic — it is being revived or torn down."""
    entries = _load()
    prefix = f"{user_id}:{thread_id}:"
    remaining = {k for k in entries if not k.startswith(prefix)}
    if len(remaining) != len(entries):
        _save(remaining)
'''

store_path = PKG / "dormant_state.py"
if store_path.exists() and store_path.read_text() == STORE:
    print("  = dormant_state.py: already written")
else:
    store_path.write_text(STORE)
    print("  + dormant_state.py: written")


# ── 2. suppress the dead-window announcement for dormant windows ─────────────
# This is the load-bearing edit: it also prevents start_autoclose_timer("dead"),
# which is what produced the delayed batch of "closed" notifications.

edit(
    "handlers/polling/window_tick/apply.py",
    "apply.py import",
    "from .... import window_query\n",
    "from .... import dormant_state\n",
)

edit(
    "handlers/polling/window_tick/apply.py",
    "dead-notification guard",
    """    lc = runtime.lifecycle if runtime is not None else lifecycle_strategy
    ps = runtime.poll_state if runtime is not None else terminal_poll_state
    if lc.is_dead_notified(user_id, thread_id, wid):
        return
""",
    """    # PATCHED (quiet-dormant): a window keep-topic autoclose freed on purpose is
    # dormant, not crashed — never announce it. lc's _dead_notified set is
    # in-memory, so without this on-disk check every ccgram restart re-banners
    # every dormant topic AND re-arms a "dead" autoclose timer, which fires
    # AUTOCLOSE_DEAD_MINUTES later as a batch of duplicate "closed" notices.
    if dormant_state.is_dormant(user_id, thread_id, wid):
        lc.mark_dead_notified(user_id, thread_id, wid)
        return
""",
)


# ── 3. write the marker when keep-topic autoclose frees a window ─────────────

edit(
    "handlers/topics/topic_lifecycle.py",
    "topic_lifecycle import",
    "from ...thread_router import thread_router\n",
    "from ... import dormant_state\n",
)

edit(
    "handlers/topics/topic_lifecycle.py",
    "mark-dormant on free",
    """    lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
    if window_id is not None:
""",
    """        # PATCHED (quiet-dormant): record the free BEFORE attempting the kill, so
        # an already-gone window is still marked and cannot be re-announced.
        dormant_state.mark_dormant(user_id, thread_id, window_id)
        if await tmux_manager.find_window_by_id(window_id) is None:
            return
""",
)


# ── 4. clear markers wherever ccgram already clears the dead-notified guard ──
# (text_handler, recovery_banner, resume_command and topic_state teardown all
# funnel through clear_dead_notification, so this one edit covers every revive.)

edit(
    "handlers/polling/polling_state.py",
    "polling_state import",
    "from ...topic_state_registry import topic_state\n",
    "from ... import dormant_state\n",
)

edit(
    "handlers/polling/polling_state.py",
    "clear-dormant on revive",
    '''    def clear_dead_notification(self, user_id: int, thread_id: int) -> None:
        """Remove dead notification tracking for a topic."""
''',
    """        # PATCHED (quiet-dormant): a revived topic is no longer dormant.
        dormant_state.clear_dormant(user_id, thread_id)
""",
)

print("[quiet-dormant] done")
