#!/usr/bin/env python3
"""Disable ccgram's status bubble entirely.

The status bubble mirrors Claude Code's own spinner into Telegram
("⚙ Baked for 2m 29s", "✻ Cogitating…") and settles to "✓ Ready" when idle.
It carries no information the actual Claude reply doesn't, and because the
bubble's Telegram message id lives only in memory (`_status_msg_info`), every
service restart loses the reference and posts a fresh duplicate — so flaky
connectivity produced a pile of stale "Baked for…" messages in each topic.

``send_status_text`` is the only function that actually sends/edits a status
message (both ``process_status_update`` and ``process_status_clear`` route
through it), so replacing its body with a clear-and-return disables the bubble
for every caller and drops any bubble this process is still tracking.

Idempotent: re-running is a no-op. Writes a .orig backup on first change.
"""

from __future__ import annotations

import glob
import shutil
import sys
from pathlib import Path

ANCHOR = '''async def send_status_text(
    client: TelegramClient,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message with action buttons and track it.

    If a status message already exists for this (user, thread), edit it
    in-place via ``edit_with_fallback`` (entity-formatted, plain-text fallback
    on TelegramError).  Same-window same-text calls are a no-op.
    """
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)

    history = _get_idle_history(user_id, thread_id_or_0, text)
    keyboard = build_status_keyboard(
        window_id,
        history=history,
        user_id=user_id,
    )

    existing = _status_msg_info.get(skey)
    if existing:
        msg_id, stored_wid, last_text, stored_chat_id = existing
        if stored_wid == window_id and text == last_text:
            return
        if stored_wid == window_id:
            success = await edit_with_fallback(
                client, stored_chat_id, msg_id, text, reply_markup=keyboard
            )
            if success:
                _status_msg_info[skey] = (msg_id, window_id, text, stored_chat_id)
                return
            # Edit failed — original message may still exist server-side.
            # Best-effort delete to avoid an orphan before creating a replacement.
            with contextlib.suppress(TelegramError):
                await client.delete_message(chat_id=stored_chat_id, message_id=msg_id)
            _status_msg_info.pop(skey, None)
        else:
            await clear_status_message(client, user_id, thread_id_or_0)

    msg = await safe_send(
        client,
        chat_id,
        text,
        message_thread_id=thread_id,
        reply_markup=keyboard,
    )
    if msg is not None:
        _status_msg_info[skey] = (msg.message_id, window_id, text, chat_id)'''

REPLACEMENT = '''async def send_status_text(
    client: TelegramClient,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Status bubbles are disabled — never render one; drop any existing bubble.

    The bubble mirrored Claude Code's spinner ("⚙ Baked for…", "✻ Cogitating…")
    and idle "✓ Ready" into Telegram — no information the reply doesn't already
    carry — and because its message id is not persisted it duplicated on every
    restart. This is the only function that actually sends a status message, so
    clearing here disables the bubble for every caller and cleans up any bubble
    this process is still tracking.
    """
    await clear_status_message(client, user_id, thread_id_or_0)'''

SENTINEL = "Status bubbles are disabled"


def find_target() -> Path:
    matches = glob.glob(
        str(
            Path.home()
            / ".local/share/uv/tools/ccgram/lib/python*/site-packages/"
            "ccgram/handlers/status/status_bubble.py"
        )
    )
    if not matches:
        print("status_bubble.py not found", file=sys.stderr)
        sys.exit(1)
    return Path(matches[0])


def main() -> None:
    target = find_target()
    src = target.read_text()

    if SENTINEL in src:
        print(f"[no-status-bubble] already applied: {target}")
        return

    if ANCHOR not in src:
        print(
            f"[no-status-bubble] anchor not found in {target} — "
            "ccgram source shape changed, skipping",
            file=sys.stderr,
        )
        sys.exit(1)

    backup = target.with_suffix(target.suffix + ".nobubble.orig")
    if not backup.exists():
        shutil.copy2(target, backup)

    target.write_text(src.replace(ANCHOR, REPLACEMENT, 1))
    print(f"[no-status-bubble] patched: {target}")


if __name__ == "__main__":
    main()
