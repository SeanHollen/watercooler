#!/usr/bin/env python3
"""Durable thread→session map so a topic ALWAYS resumes its EXACT conversation.

ccgram keys resume off the tmux window id, which is unstable:
  * ``prune_session_map`` deletes a dormant window's ``window_state`` (and its
    ``session_id``) on the next restart, and
  * tmux reuses window ids (@5 …) after the tmux *server* restarts,
so ``get_session_id_for_window`` can return nothing or the wrong session. With
every session sharing one cwd (~/Desktop), the ``--continue`` fallback then
resumes whatever conversation was most recently active — not the topic's own.

Fix: persist ``user:thread -> {session_id, cwd}`` keyed by the stable session
UUID. This patch:
  1. writes the ``topic_session_state`` module (dependency-free, atomic, cached),
  2. populates it on every Stop hook (hook_events._handle_stop),
  3. consults it first in the always-resume path (text_handler), supplying both
     session_id and cwd so a pruned window still resumes correctly,
  4. bootstraps the map once from the current state.json so already-live topics
     are protected immediately (before their next turn).

Idempotent: re-running is a no-op. Each edit has its own sentinel.
"""

from __future__ import annotations

import glob
import json
import shutil
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# The module we install into the ccgram package.
# --------------------------------------------------------------------------- #
MODULE_SRC = '''"""Persisted thread→session map — added by ~/.ccgram/apply-topic-session-patch.py.

Maps ``user_id:thread_id`` to the topic's Claude ``session_id`` (and its cwd) so
a dormant/dead topic always resumes its EXACT conversation, independent of the
tmux window id (which is pruned on restart and reused after a server restart).

Dependency-free (no ccgram imports) so it can be imported from anywhere without
a cycle. Every failure path degrades to "unknown", i.e. stock behaviour — a
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
    os.getenv("CCGRAM_TOPIC_SESSION_FILE", "~/.ccgram/topic_sessions.json")
).expanduser()

_cache: dict | None = None
_cache_mtime: float | None = None


def _key(user_id: int, thread_id: int) -> str:
    return f"{user_id}:{thread_id}"


def _load() -> dict:
    global _cache, _cache_mtime
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        _cache, _cache_mtime = {}, None
        return _cache
    if _cache is not None and _cache_mtime == mtime:
        return _cache
    try:
        raw = json.loads(_PATH.read_text())
        entries = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError) as e:
        logger.debug("topic_session_load_failed", error=str(e))
        entries = {}
    _cache, _cache_mtime = entries, mtime
    return entries


def _save(entries: dict) -> None:
    global _cache, _cache_mtime
    _cache = dict(entries)
    _cache_mtime = None
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(_PATH.parent), prefix=".topic-sessions-", suffix=".json"
        )
        with os.fdopen(fd, "w") as fh:
            json.dump(entries, fh, indent=2, sort_keys=True)
        os.replace(tmp, _PATH)
        _cache_mtime = _PATH.stat().st_mtime
    except OSError as e:
        logger.debug("topic_session_save_failed", error=str(e))


def get(user_id: int, thread_id: int) -> dict | None:
    """Return {"session_id": str, "cwd": str} for a topic, or None."""
    rec = _load().get(_key(user_id, thread_id))
    return rec if isinstance(rec, dict) and rec.get("session_id") else None


def record(user_id: int, thread_id: int, session_id: str, cwd: str | None) -> None:
    """Persist a topic's session_id (+cwd), writing only on change."""
    if not session_id:
        return
    entries = dict(_load())
    k = _key(user_id, thread_id)
    new = {"session_id": session_id, "cwd": cwd or (entries.get(k, {}) or {}).get("cwd", "")}
    if entries.get(k) != new:
        entries[k] = new
        _save(entries)
'''

# --------------------------------------------------------------------------- #
# Edit 1: populate in hook_events._handle_stop
# --------------------------------------------------------------------------- #
HE_ANCHOR = """    for user_id, thread_id, window_id in users:
        session_lifecycle.handle_stop_task_state(window_id)"""

HE_REPLACEMENT = """    for user_id, thread_id, window_id in users:
        session_lifecycle.handle_stop_task_state(window_id)
        # watercooler: record the topic→session map so a dormant topic can be
        # resumed to its EXACT conversation later (see topic_session_state).
        try:
            from .. import topic_session_state as _wc_tss
            from ..window_query import get_session_id_for_window as _wc_gsid
            _wc_s = _wc_gsid(window_id)
            _wc_c = view.cwd if (view and getattr(view, "cwd", None)) else None
            if _wc_s:
                _wc_tss.record(user_id, thread_id, _wc_s, _wc_c)
        except Exception:
            pass"""

HE_SENTINEL = "watercooler: record the topic→session map"

# --------------------------------------------------------------------------- #
# Edit 2: consult in text_handler always-resume path
# --------------------------------------------------------------------------- #
TH_ANCHOR = """    _wc_sid = None
    try:
        _wc_sid = window_query.get_session_id_for_window(window_id)
    except Exception:
        _wc_sid = None
    if not _wc_sid and view is not None:
        _wc_sid = getattr(view, "session_id", None)"""

TH_REPLACEMENT = """    _wc_sid = None
    try:
        _wc_sid = window_query.get_session_id_for_window(window_id)
    except Exception:
        _wc_sid = None
    if not _wc_sid and view is not None:
        _wc_sid = getattr(view, "session_id", None)
    # watercooler durable map: window_id is unstable (pruned on restart, reused
    # after a tmux server restart), so fall back to the on-disk thread→session
    # record — it supplies both the exact session_id and the cwd a pruned window
    # would otherwise lack (empty cwd skips auto-resume entirely).
    try:
        from ... import topic_session_state as _wc_tss
        _wc_rec = _wc_tss.get(user_id, thread_id)
    except Exception:
        _wc_rec = None
    if _wc_rec:
        if not _wc_sid:
            _wc_sid = _wc_rec.get("session_id")
        if not cwd:
            cwd = _wc_rec.get("cwd") or cwd"""

TH_SENTINEL = "watercooler durable map"


def _pkg_root() -> Path:
    matches = glob.glob(
        str(
            Path.home()
            / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram"
        )
    )
    if not matches:
        print("ccgram package not found", file=sys.stderr)
        sys.exit(1)
    return Path(matches[0])


def _patch_file(path: Path, anchor: str, replacement: str, sentinel: str, label: str) -> None:
    src = path.read_text()
    if sentinel in src:
        print(f"[topic-session] {label}: already applied")
        return
    if anchor not in src:
        print(
            f"[topic-session] {label}: anchor not found in {path} — skipping",
            file=sys.stderr,
        )
        sys.exit(1)
    backup = path.with_suffix(path.suffix + ".tss.orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(src.replace(anchor, replacement, 1))
    print(f"[topic-session] {label}: patched {path}")


def _bootstrap(root: Path) -> None:
    """Seed topic_sessions.json from the current state.json (one-time)."""
    state_path = Path.home() / ".ccgram" / "state.json"
    out_path = Path("~/.ccgram/topic_sessions.json").expanduser()
    if out_path.exists():
        print("[topic-session] bootstrap: topic_sessions.json exists, skipping")
        return
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        print("[topic-session] bootstrap: no readable state.json, skipping")
        return
    tb = state.get("thread_bindings", {})
    ws = state.get("window_states", {})
    seeded: dict = {}
    for uid, bindings in tb.items():
        for thread_id, window_id in bindings.items():
            w = ws.get(window_id) or {}
            sid = w.get("session_id")
            if sid:
                seeded[f"{uid}:{thread_id}"] = {
                    "session_id": sid,
                    "cwd": w.get("cwd", ""),
                }
    if seeded:
        out_path.write_text(json.dumps(seeded, indent=2, sort_keys=True))
        print(f"[topic-session] bootstrap: seeded {len(seeded)} topics")
    else:
        print("[topic-session] bootstrap: nothing to seed")


def main() -> None:
    root = _pkg_root()

    module_path = root / "topic_session_state.py"
    if not module_path.exists() or module_path.read_text() != MODULE_SRC:
        module_path.write_text(MODULE_SRC)
        print(f"[topic-session] wrote module {module_path}")
    else:
        print("[topic-session] module already present")

    _patch_file(
        root / "handlers" / "hook_events.py",
        HE_ANCHOR, HE_REPLACEMENT, HE_SENTINEL, "populate(hook_events)",
    )
    _patch_file(
        root / "handlers" / "text" / "text_handler.py",
        TH_ANCHOR, TH_REPLACEMENT, TH_SENTINEL, "consult(text_handler)",
    )
    _bootstrap(root)


if __name__ == "__main__":
    main()
