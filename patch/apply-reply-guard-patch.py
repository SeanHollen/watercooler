#!/usr/bin/env python3
"""Install the watercooler *delivery guard* into ccgram.

Why (2026-10-21): a session's final reply, written 35 s after a ccgram restart,
never reached its Telegram topic and nothing logged a failure. ccgram's relay is
fire-and-forget: the transcript byte offset advances when a message is *read*
(not when it is sent), the per-user send queue is in memory and dropped on
shutdown, a routing miss is a debug-level log, and a failed sendMessage is a
warning with no retry. Any of those loses the reply for good.

The guard closes the loop:
  * every complete assistant text message gets a hash (sha256 of its text). The
    hash rides on the ContentTask and is recorded as *delivered* only once
    Telegram accepted every part. Delivered hashes persist in
    ~/.ccgram/delivery_guard.json, so a restart cannot forget what went out.
  * audits — 20 s and 75 s after every Stop hook, 15 s after startup and then
    every 60 s — compare the last reply in each bound window's transcript with
    the delivered set. An undelivered reply is re-sent through the normal queue,
    with a WARNING in the journal.

Edits (all idempotent, sentinel-guarded):
  ccgram/delivery_guard.py                        (new module, rewritten verbatim)
  handlers/messaging_pipeline/message_task.py     ContentTask.src_hash
  handlers/messaging_pipeline/message_queue.py    carry the hash, report send result
  handlers/messaging_pipeline/message_routing.py  hash + mark pending at enqueue
  handlers/hook_events.py                         audit after Stop
  bootstrap.py                                    start the periodic audit

CCGRAM_PKG_DIR=<path to a copy of the ccgram package> patches that copy instead
(the gate's smoke test). Run with any python3; only string edits happen here.
"""
import glob
import os
import pathlib
import sys

home = pathlib.Path.home()
override = os.environ.get("CCGRAM_PKG_DIR")
if override:
    PKG = pathlib.Path(override)
else:
    matches = glob.glob(
        str(home / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram")
    )
    if not matches:
        sys.exit("ERROR: could not find the installed ccgram package")
    PKG = pathlib.Path(matches[0])

SENTINEL = "watercooler delivery guard"

GUARD_SRC = r'''"""Delivery guard — no completed assistant reply may be lost silently.

watercooler patch (apply-reply-guard-patch.py). See that file's docstring for the
incident that motivated it. In short: ccgram advances its transcript offset when
it *reads* a reply, keeps its send queue in memory, and treats a routing miss or
a failed sendMessage as a log line. This module records what was actually sent
and re-sends what was not.

State: ~/.ccgram/delivery_guard.json  {"delivered": {window_id: {hash: epoch}}}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

STATE_FILE = Path(
    os.environ.get("CCGRAM_DELIVERY_GUARD_FILE")
    or os.path.expanduser("~/.ccgram/delivery_guard.json")
)
STOP_CHECK_DELAYS = (20.0, 75.0)  # seconds after a Stop hook
STARTUP_DELAY = 15.0
PERIODIC_SECS = 60.0
QUIET_SECS = 30.0  # periodic audit ignores replies younger than this (still in flight)
MAX_AGE_SECS = 2 * 3600.0  # never re-send anything older than this
RESEND_COOLDOWN = 300.0
MAX_RESENDS = 2
KEEP_PER_WINDOW = 80
TAIL_BYTES = 768 * 1024
_NO_CONTENT = "(no content)"
_RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")  # same as TranscriptParser._RE_ANSI

_delivered: dict[str, dict[str, float]] = {}
_pending: dict[str, float] = {}
_resent: dict[str, list[float]] = {}
_gave_up: set[str] = set()
_client: Any = None
_periodic_task: asyncio.Task | None = None
_bg_tasks: set[asyncio.Task] = set()
_loaded = False
_seeded = True


# ── pure helpers (no ccgram imports; unit-testable) ───────────────────────────


def text_hash(text: str) -> str:
    """Stable id of an assistant text as ccgram's parser hands it to routing."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def clean_text(raw: str) -> str:
    """Exactly what TranscriptParser does to an assistant text block."""
    return _RE_ANSI.sub("", raw or "").strip()


def parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tail_lines(path: str | Path) -> list[bytes]:
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()  # drop the partial first line
        data = f.read()
    return data.splitlines()


def last_reply(path: str | Path) -> dict[str, Any] | None:
    """Last assistant text block of a Claude Code transcript.

    Returns {"text", "hash", "ts", "final"}. ``final`` is False when an assistant
    tool_use follows the text (a mid-turn note, not the turn's reply).
    """
    try:
        lines = _tail_lines(path)
    except OSError:
        return None
    last: dict[str, Any] | None = None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        ts = entry.get("timestamp")
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = clean_text(block.get("text", ""))
                if t and t != _NO_CONTENT:
                    last = {"text": t, "hash": text_hash(t), "ts": parse_ts(ts), "final": True}
            elif btype == "tool_use" and last is not None:
                last["final"] = False
    return last


# ── state ─────────────────────────────────────────────────────────────────────


def _load() -> None:
    global _loaded, _seeded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(STATE_FILE.read_text())
        delivered = data.get("delivered", {})
        for wid, hashes in delivered.items():
            if isinstance(hashes, dict):
                _delivered[wid] = {str(h): float(t) for h, t in hashes.items()}
        _seeded = True
    except FileNotFoundError:
        _seeded = False  # first run: seed instead of re-sending history
    except Exception:
        logger.warning("delivery guard: state unreadable, starting fresh")
        _seeded = False


def _save() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"delivered": _delivered}, indent=0))
        os.replace(tmp, STATE_FILE)
    except Exception:
        logger.warning("delivery guard: could not save state")


def _trim(window_id: str) -> None:
    hashes = _delivered.get(window_id)
    if hashes and len(hashes) > KEEP_PER_WINDOW:
        for h in sorted(hashes, key=hashes.get)[: len(hashes) - KEEP_PER_WINDOW]:
            hashes.pop(h, None)


def is_delivered(window_id: str, h: str) -> bool:
    _load()
    return h in _delivered.get(window_id, {})


def mark_pending(h: str | None) -> None:
    if not h:
        return
    now = time.time()
    _pending[h] = now
    if len(_pending) > 500:
        for k in [k for k, t in _pending.items() if now - t > 3600]:
            _pending.pop(k, None)


def mark_delivered(window_id: str, h: str) -> None:
    _load()
    _delivered.setdefault(window_id, {})[h] = time.time()
    _pending.pop(h, None)
    _trim(window_id)
    _save()


def on_task_sent(window_id: str, src_hash: str | None, *, ok: bool) -> None:
    """Called by the queue worker after a ContentTask was processed."""
    try:
        if not src_hash:
            return
        hashes = [h for h in src_hash.split(",") if h]
        if ok:
            for h in hashes:
                mark_delivered(window_id, h)
        else:
            logger.warning(
                "delivery guard: Telegram did not accept a reply; it stays undelivered "
                "and will be re-sent by the audit",
                window_id=window_id,
            )
    except Exception:
        logger.exception("delivery guard: on_task_sent failed")


def set_client(client: Any) -> None:
    global _client
    if client is not None:
        _client = client


# ── ccgram-bound parts (lazy imports: this module loads before the app does) ──


def _bindings_for_window(window_id: str) -> list[tuple[int, int]]:
    from .thread_router import thread_router

    return [
        (user_id, thread_id)
        for user_id, thread_id, wid in thread_router.iter_thread_bindings()
        if wid == window_id
    ]


def _bound_windows() -> list[str]:
    from .thread_router import thread_router
    from .window_state_store import window_store

    seen: list[str] = []
    for _u, _t, wid in thread_router.iter_thread_bindings():
        if wid in window_store.window_states and wid not in seen:
            seen.append(wid)
    return seen


def _transcript_for_window(window_id: str) -> str:
    from .window_state_ports.identity_state import get_transcript_path

    return get_transcript_path(window_id) or ""


def _display(window_id: str) -> str:
    try:
        from .thread_router import thread_router

        return thread_router.get_display_name(window_id) or window_id
    except Exception:
        return window_id


async def _resend(window_id: str, reply: dict[str, Any], reason: str) -> bool:
    h = reply["hash"]
    if h in _gave_up:
        return False
    now = time.time()
    attempts = _resent.setdefault(h, [])
    if attempts and now - attempts[-1] < RESEND_COOLDOWN:
        return False
    if len(attempts) >= MAX_RESENDS:
        _gave_up.add(h)
        logger.error(
            "delivery guard: reply still undelivered after re-sending; giving up",
            window=_display(window_id),
            window_id=window_id,
        )
        return False
    if _client is None:
        logger.warning("delivery guard: no Telegram client yet, cannot re-send")
        return False
    users = _bindings_for_window(window_id)
    if not users:
        return False
    from .handlers.messaging_pipeline.message_queue import enqueue_content_message
    from .handlers.response_builder import build_response_parts

    parts = build_response_parts(reply["text"], True, "text", "assistant")
    if not parts or not "".join(parts).strip():
        mark_delivered(window_id, h)  # nothing sendable; stop auditing it
        return False
    age = now - (reply.get("ts") or now)
    logger.warning(
        "delivery guard: re-sending a reply that never reached Telegram",
        window=_display(window_id),
        window_id=window_id,
        reason=reason,
        age_s=int(age),
        chars=len(reply["text"]),
    )
    attempts.append(now)
    for user_id, thread_id in users:
        mark_pending(h)
        await enqueue_content_message(
            client=_client,
            user_id=user_id,
            window_id=window_id,
            parts=parts,
            content_type="text",
            role="assistant",
            thread_id=thread_id,
            src_hash=h,
        )
    return True


async def _reply_for_window(window_id: str) -> dict[str, Any] | None:
    path = _transcript_for_window(window_id)
    if not path or not os.path.exists(path):
        return None
    return await asyncio.to_thread(last_reply, path)


async def audit_window(window_id: str, reason: str, *, min_age: float = 0.0) -> bool:
    """Re-send the window's last reply if it is final, recent and undelivered."""
    _load()
    reply = await _reply_for_window(window_id)
    if reply is None or not reply["final"]:
        return False
    now = time.time()
    age = now - (reply["ts"] or now)
    if age > MAX_AGE_SECS or age < min_age:
        return False
    h = reply["hash"]
    if is_delivered(window_id, h):
        return False
    if reason == "periodic" and h in _pending and now - _pending[h] < 90:
        return False  # enqueued, give the queue time (flood control pauses up to 60 s)
    return await _resend(window_id, reply, reason)


async def _stop_audit(window_id: str) -> None:
    h: str | None = None
    for i, delay in enumerate(STOP_CHECK_DELAYS):
        await asyncio.sleep(delay)
        reply = await _reply_for_window(window_id)
        if reply is None or not reply["final"]:
            return
        h = reply["hash"]
        if is_delivered(window_id, h):
            return
        if i == 0 and h in _pending:
            continue  # enqueued; the second check verifies it actually went out
        await _resend(window_id, reply, f"stop+{int(delay)}s")
    await asyncio.sleep(45)
    if h and not is_delivered(window_id, h):
        logger.error(
            "delivery guard: reply still undelivered after Stop audits",
            window=_display(window_id),
            window_id=window_id,
        )


def schedule_stop_audit(client: Any, window_id: str) -> None:
    """Called from the Stop hook handler. Never raises."""
    try:
        set_client(client)
        ensure_started(client)
        task = asyncio.get_running_loop().create_task(_stop_audit(window_id))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except Exception:
        logger.exception("delivery guard: could not schedule Stop audit")


async def _periodic_loop() -> None:
    global _seeded
    _load()
    await asyncio.sleep(STARTUP_DELAY)
    while True:
        try:
            windows = _bound_windows()
            if not _seeded:
                now = time.time()
                for wid in windows:
                    reply = await _reply_for_window(wid)
                    if reply is not None:
                        _delivered.setdefault(wid, {})[reply["hash"]] = now
                _seeded = True
                _save()
                logger.info(
                    "delivery guard: first run, seeded current replies as delivered",
                    windows=len(windows),
                )
            else:
                for wid in windows:
                    await audit_window(wid, "periodic", min_age=QUIET_SECS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("delivery guard: periodic audit failed")
        await asyncio.sleep(PERIODIC_SECS)


def ensure_started(client: Any = None) -> None:
    """Start the periodic audit once (needs a running event loop)."""
    global _periodic_task
    set_client(client)
    try:
        if _periodic_task is None or _periodic_task.done():
            _periodic_task = asyncio.get_running_loop().create_task(_periodic_loop())
            logger.info("delivery guard: active", state=str(STATE_FILE))
    except RuntimeError:
        pass  # no running loop (CLI use); the app starts it from bootstrap


__all__ = [
    "audit_window",
    "clean_text",
    "ensure_started",
    "is_delivered",
    "last_reply",
    "mark_delivered",
    "mark_pending",
    "on_task_sent",
    "schedule_stop_audit",
    "set_client",
    "text_hash",
]
'''


def write_module() -> None:
    target = PKG / "delivery_guard.py"
    if target.exists() and target.read_text() == GUARD_SRC:
        print("  = delivery_guard.py: already written")
        return
    target.write_text(GUARD_SRC)
    print(f"  + wrote {target}")


def edit(relpath: str, anchor: str, replacement: str, label: str, *, sentinel: str = SENTINEL) -> None:
    path = PKG / relpath
    src = path.read_text()
    if sentinel in src and sentinel in replacement and replacement.strip() in src:
        print(f"  = {label}: already patched")
        return
    if anchor not in src:
        print(f"  ! {label}: anchor not found in {path} — skipping", file=sys.stderr)
        return
    if src.count(anchor) != 1:
        print(f"  ! {label}: anchor not unique in {path} — skipping", file=sys.stderr)
        return
    bak = path.with_name(path.name + ".guard.orig")
    if not bak.exists():
        bak.write_text(src)
    path.write_text(src.replace(anchor, replacement, 1))
    print(f"  + {label}: patched")


print(f"[reply-guard] ccgram at {PKG}")
write_module()

# 1. ContentTask carries the hash of the transcript text it came from.
edit(
    "handlers/messaging_pipeline/message_task.py",
    '''class ContentTask:
    """A Telegram message to deliver — text, tool_use, or tool_result."""

    window_id: str
    parts: tuple[str, ...]
    content_type: ContentType = "text"
    role: MessageRole = "assistant"
    tool_use_id: str | None = None
    tool_name: str | None = None
    thread_id: int | None = None
''',
    '''class ContentTask:
    """A Telegram message to deliver — text, tool_use, or tool_result."""

    window_id: str
    parts: tuple[str, ...]
    content_type: ContentType = "text"
    role: MessageRole = "assistant"
    tool_use_id: str | None = None
    tool_name: str | None = None
    thread_id: int | None = None
    src_hash: str | None = None  # watercooler delivery guard: sha of the source text
''',
    "ContentTask.src_hash",
)

# 2a. enqueue_content_message accepts and stores it.
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''    thread_id: int | None = None,
) -> None:
    """Enqueue a content message task."""
    if _is_ghost_window_task_at_enqueue(window_id):
        return
    queue = get_or_create_queue(client, user_id)

    task = ContentTask(
        window_id=window_id,
        parts=tuple(parts),
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        content_type=content_type,
        role=role,
        thread_id=thread_id,
    )
''',
    '''    thread_id: int | None = None,
    src_hash: str | None = None,  # watercooler delivery guard
) -> None:
    """Enqueue a content message task."""
    if _is_ghost_window_task_at_enqueue(window_id):
        return
    queue = get_or_create_queue(client, user_id)

    task = ContentTask(
        window_id=window_id,
        parts=tuple(parts),
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        content_type=content_type,
        role=role,
        thread_id=thread_id,
        src_hash=src_hash,
    )
''',
    "enqueue_content_message(src_hash)",
)

# 2b. Merged tasks keep every hash they swallowed.
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0
''',
    '''    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0
    merged_hashes: list[str] = [first.src_hash] if first.src_hash else []  # watercooler delivery guard
''',
    "merge: collect hashes",
)
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1
''',
    '''            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1
            if task.src_hash:  # watercooler delivery guard
                merged_hashes.append(task.src_hash)
''',
    "merge: append hashes",
)
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''            role=first.role,
            thread_id=first.thread_id,
        ),
        merge_count,
    )
''',
    '''            role=first.role,
            thread_id=first.thread_id,
            src_hash=",".join(merged_hashes) or None,  # watercooler delivery guard
        ),
        merge_count,
    )
''',
    "merge: carry hashes",
)

# 2c. The worker reports whether Telegram accepted every part.
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None
''',
    '''    first_part = True
    last_msg_id: int | None = None
    _wc_all_ok = True  # watercooler delivery guard
    for part in task.parts:
        sent = None
''',
    "process: track outcome",
)
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''        if sent:
            last_msg_id = sent.message_id

    if _should_send_tts(task) and (tts_text := prepare_tts_text(task.parts)):
''',
    '''        if sent:
            last_msg_id = sent.message_id
        else:
            _wc_all_ok = False  # watercooler delivery guard

    if _should_send_tts(task) and (tts_text := prepare_tts_text(task.parts)):
''',
    "process: failed part",
)
edit(
    "handlers/messaging_pipeline/message_queue.py",
    '''    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tkey)] = last_msg_id
''',
    '''    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tkey)] = last_msg_id

    # watercooler delivery guard: record what Telegram actually accepted.
    if getattr(task, "src_hash", None):
        from ...delivery_guard import on_task_sent

        on_task_sent(
            task.window_id, task.src_hash, ok=_wc_all_ok and last_msg_id is not None
        )
''',
    "process: report delivery",
)

# 3. Routing hashes each complete assistant text and marks it pending.
edit(
    "handlers/messaging_pipeline/message_routing.py",
    '''from ..response_builder import build_response_parts
from .message_queue import enqueue_content_message, get_message_queue
''',
    '''from ..response_builder import build_response_parts
from .message_queue import enqueue_content_message, get_message_queue
from ... import delivery_guard  # watercooler delivery guard
''',
    "routing: import",
)
edit(
    "handlers/messaging_pipeline/message_routing.py",
    '''    active_users = session_query.find_users_for_session(msg.session_id)

    if not active_users:
        logger.debug("No active users for session %s", msg.session_id)
        return
''',
    '''    active_users = session_query.find_users_for_session(msg.session_id)
    delivery_guard.set_client(client)  # watercooler delivery guard

    if not active_users:
        logger.debug("No active users for session %s", msg.session_id)
        return
''',
    "routing: client",
)
edit(
    "handlers/messaging_pipeline/message_routing.py",
    '''        if msg.is_complete:
            await enqueue_content_message(
                client=client,
                user_id=user_id,
                window_id=window_id,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                tool_name=msg.tool_name,
                content_type=msg.content_type,  # type: ignore[arg-type]  # NewMessage.content_type is str, narrows at runtime
                role=msg.role,  # type: ignore[arg-type]  # NewMessage.role is str, narrows at runtime
                thread_id=thread_id,
            )
''',
    '''        if msg.is_complete:
            # watercooler delivery guard: only assistant text replies are audited.
            _wc_hash = None
            if msg.role == "assistant" and msg.content_type == "text":
                _wc_hash = delivery_guard.text_hash(msg.text)
                delivery_guard.mark_pending(_wc_hash)
            await enqueue_content_message(
                client=client,
                user_id=user_id,
                window_id=window_id,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                tool_name=msg.tool_name,
                content_type=msg.content_type,  # type: ignore[arg-type]  # NewMessage.content_type is str, narrows at runtime
                role=msg.role,  # type: ignore[arg-type]  # NewMessage.role is str, narrows at runtime
                thread_id=thread_id,
                src_hash=_wc_hash,
            )
''',
    "routing: hash + pending",
)

# 4. Every Stop hook schedules an audit of that window.
edit(
    "handlers/hook_events.py",
    '''from .status.topic_emoji import update_topic_emoji
''',
    '''from .status.topic_emoji import update_topic_emoji
from .. import delivery_guard  # watercooler delivery guard
''',
    "hook_events: import",
)
edit(
    "handlers/hook_events.py",
    '''        if summary and status_text:
            status_text = status_text.replace("✓ Ready", f"✓ Done — {summary}", 1)
        await enqueue_status_update(
            client, user_id, window_id, status_text, thread_id=thread_id
        )
''',
    '''        if summary and status_text:
            status_text = status_text.replace("✓ Ready", f"✓ Done — {summary}", 1)
        await enqueue_status_update(
            client, user_id, window_id, status_text, thread_id=thread_id
        )
        delivery_guard.schedule_stop_audit(client, window_id)  # watercooler delivery guard
''',
    "hook_events: Stop audit",
)

# 5. The periodic audit starts with the session monitor.
edit(
    "bootstrap.py",
    '''from .handlers.messaging_pipeline.message_routing import handle_new_message
''',
    '''from .handlers.messaging_pipeline.message_routing import handle_new_message
from . import delivery_guard  # watercooler delivery guard
''',
    "bootstrap: import",
)
edit(
    "bootstrap.py",
    '''    monitor.start()
    session_monitor = monitor
''',
    '''    monitor.start()
    session_monitor = monitor
    delivery_guard.ensure_started(client)  # watercooler delivery guard
''',
    "bootstrap: start audit",
)
print("[reply-guard] done")
