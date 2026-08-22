#!/usr/bin/env python3
"""Media works from the first message, and whisper output goes straight to the agent.

Two stock ccgram behaviours this removes:

1. "Topic not bound — send a text message first to pick a directory, then
   re-record." Every media handler (voice, photo, document) resolved the topic's
   window BEFORE doing any work and bailed if there wasn't one, so the first
   message in a fresh topic could only ever be text. The autostart patch already
   launches a session from a first text message; this routes media through the
   same `_handle_unbound_topic` path, so a voice note / photo / file opens a
   session too.

2. The "✓ Send to agent / ✗ Discard" keyboard on every transcription. Speech is
   now delivered to the agent directly — the transcript is still echoed into the
   topic so you can see what whisper heard, but nothing waits on a tap.

Idempotent; re-applied automatically by ~/.ccgram/apply-all-patches.sh.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARK = "PATCHED (media-autostart)"


def ccgram_dir() -> Path:
    root = Path.home() / ".local/share/uv/tools/ccgram/lib"
    for lib in sorted(root.glob("python*/site-packages/ccgram")):
        return lib
    sys.exit("ccgram package not found — is it installed via `uv tool`?")


CC = ccgram_dir()


def edit(rel: str, old: str, new: str, *, label: str, done: str) -> bool:
    """Apply one anchored replacement. False = anchor gone (upgrade moved it).

    `done` is a string that exists only once this edit has landed. A per-edit
    marker is the only reliable idempotency test here: several edits share one
    file, so "the file mentions this patch" goes true after the first of them.
    """
    path = CC / rel
    src = path.read_text()
    if done in src:
        print(f"  = {label}: already patched")
        return True
    if old not in src:
        print(f"  ! {label}: ANCHOR NOT FOUND — skipped")
        return False
    path.write_text(src.replace(old, new, 1))
    print(f"  + {label}: patched")
    return True


# ---------------------------------------------------------------- shared module

HELPER = '''"""Bind-on-first-media, and send-to-agent without a confirmation tap.

Added by ~/.ccgram/apply-media-autostart-patch.py — not part of upstream ccgram.

`ensure_window` is the piece the stock media handlers are missing: they resolve
the topic's window and give up when there isn't one, which made "send a text
message first" a hard precondition on every photo, file and voice note. Text
messages don't have that problem because they go through
`_handle_unbound_topic`, which the autostart patch turned into a zero-tap
session launch — so media just calls the same thing.

`deliver_to_agent` is the body of the old vc:send callback with the callback
removed, so a transcript reaches the agent on arrival instead of on a tap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from telegram.error import TelegramError

from ..multiplexer.window_ops import send_to_window
from ..providers import get_provider_for_window
from ..telegram_client import PTBTelegramClient
from ..thread_router import thread_router
from ..window_query import get_window_provider
from .messaging_pipeline.message_sender import (
    REACT_DONE,
    REACT_SEEN,
    ack_reaction,
    react,
)

if TYPE_CHECKING:
    from telegram import Message
    from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)


async def ensure_window(
    user_id: int,
    thread_id: int | None,
    prompt: str,
    message: "Message",
    context: "ContextTypes.DEFAULT_TYPE | None",
) -> tuple[str | None, bool]:
    """Resolve this topic's window, launching a session if the topic is unbound.

    Returns (window_id, prompt_already_delivered). The second element matters
    because autostart hands `prompt` to the new window as its first input — a
    caller that also sent it would deliver it twice.

    (None, False) means the topic is unbound and autostart declined to launch;
    `_handle_unbound_topic` has already put the stock pickers on screen, so the
    caller should just report its own failure and stop.
    """
    if thread_id is None:
        return None, False

    window_id = thread_router.resolve_window_for_thread(user_id, thread_id)
    if window_id:
        return window_id, False

    # Lazy: text_handler pulls in most of the handler tree and would cycle back
    # through here at import time.
    from .text.text_handler import _handle_unbound_topic

    user_data = context.user_data if context is not None else None
    await _handle_unbound_topic(user_id, thread_id, prompt, user_data, message, context)

    window_id = thread_router.resolve_window_for_thread(user_id, thread_id)
    if not window_id:
        return None, False
    return window_id, bool(prompt)


async def deliver_to_agent(
    message: "Message",
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> bool:
    """Send `text` to the agent window as if it had been typed into the topic."""
    client = PTBTelegramClient(message.get_bot())
    await react(client, message.chat.id, message.message_id, REACT_SEEN)

    provider = get_provider_for_window(
        window_id, provider_name=get_window_provider(window_id)
    )
    if provider.capabilities.chat_first_command_path and thread_id is not None:
        # Lazy: shell_commands ↔ media via the approval callback wiring.
        from .shell.shell_commands import handle_shell_message

        try:
            await handle_shell_message(client, user_id, thread_id, window_id, text)
        except (OSError, TelegramError) as exc:
            logger.warning("media_deliver_failed", error=str(exc))
            return False
    else:
        ok, err = await send_to_window(window_id, text)
        if not ok:
            logger.warning("media_deliver_failed", error=err)
            return False

    await react(client, message.chat.id, message.message_id, REACT_DONE)
    await ack_reaction(client, message.chat.id, message.message_id)
    return True
'''

# ---------------------------------------------------------------- voice handler

VOICE_NEW = '''async def handle_voice_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Transcribe a voice message and send it straight to the agent.

    PATCHED (media-autostart). Two changes from upstream:
      * Transcription happens BEFORE the topic is resolved, so a voice note can
        be the first message in a fresh topic — the transcript then becomes the
        new session's name and its first prompt.
      * No confirm/discard keyboard. The transcript is echoed into the topic for
        review and delivered at the same time.
    """
    from ..media_autostart import deliver_to_agent, ensure_window

    user = update.effective_user
    message = update.message
    if not user or not message or not message.voice:
        return

    if not config.is_user_allowed(user.id):
        await safe_reply(message, "You are not authorized to use this bot.")
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            message,
            "\\u274c Please use a named topic. Create a new topic to start a session.",
        )
        return

    voice = message.voice
    if voice.file_size is not None and voice.file_size > _MAX_VOICE_SIZE:
        size_mb = voice.file_size / (1024 * 1024)
        await safe_reply(
            message,
            f"❌ Voice message too large ({size_mb:.1f} MB). Maximum 25 MB.",
        )
        return

    transcriber = await _get_transcriber_or_reply(message)
    if transcriber is None:
        return

    audio_bytes = await _download_voice(message, voice.file_id)
    if audio_bytes is None:
        return

    await message.get_bot().send_chat_action(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        action=ChatAction.TYPING,
    )

    result = await _transcribe_audio(message, transcriber, audio_bytes)
    if result is None:
        return

    text = result.text.strip()
    if not text:
        await safe_reply(message, "⚠️ Could not transcribe audio (empty result).")
        return

    window_id, already_sent = await ensure_window(
        user.id, thread_id, text, message, context
    )
    if window_id is None:
        await safe_reply(message, f"🎤 {text}\\n\\n⚠ No session to send that to.")
        return

    # Echo what whisper heard — the only thing the confirm keyboard was really
    # good for — then get out of the way.
    await safe_reply(message, f"🎤 {text}")

    if already_sent:
        return
    if not await deliver_to_agent(message, user.id, thread_id, window_id, text):
        await safe_reply(message, "❌ Failed to send that to the agent.")
'''

# ---------------------------------------------------------------- file handler

FILE_SIG_OLD = """    claude_msg_tpl: str,
    success_emoji: str,
) -> None:
    \"\"\"Shared upload flow: resolve dir, download, notify Claude, reply to user.\"\"\"
    window_id, upload_path, error = _resolve_upload_dir(user_id, thread_id)
    if error or not window_id or not upload_path:
        await safe_reply(message, f"\\u274c {error}")
        return
"""

FILE_SIG_NEW = """    claude_msg_tpl: str,
    success_emoji: str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    kind_hint: str = "upload",
) -> None:
    \"\"\"Shared upload flow: resolve dir, download, notify Claude, reply to user.\"\"\"
    # PATCHED (media-autostart): an unbound topic used to end the upload here.
    # A file can now be the first thing in a topic — launch a session for it and
    # carry on, so uploading never depends on having typed something first.
    if thread_router.resolve_window_for_thread(user_id, thread_id) is None:
        from .media_autostart import ensure_window

        await ensure_window(
            user_id,
            thread_id,
            _sanitize_caption(message.caption or "") or kind_hint,
            message,
            context,
        )

    window_id, upload_path, error = _resolve_upload_dir(user_id, thread_id)
    if error or not window_id or not upload_path:
        await safe_reply(message, f"\\u274c {error}")
        return
"""

PHOTO_OLD = """async def handle_photo_message(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:"""
PHOTO_NEW = """async def handle_photo_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:"""

DOC_OLD = """async def handle_document_message(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:"""
DOC_NEW = """async def handle_document_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:"""

PHOTO_CALL_OLD = """        "\\U0001f4f7",
    )"""
PHOTO_CALL_NEW = """        "\\U0001f4f7",
        context=context,
        kind_hint="photo",
    )"""

DOC_CALL_OLD = """        "\\U0001f4ce",
    )"""
DOC_CALL_NEW = """        "\\U0001f4ce",
        context=context,
        kind_hint="file",
    )"""

# ------------------------------------------------------------- audio_transcribe

AUDIO_SIG_OLD = '''async def handle_audio_message(
    update: "Update",
    _context: "ContextTypes.DEFAULT_TYPE",
) -> None:'''
AUDIO_SIG_NEW = '''async def handle_audio_message(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
) -> None:'''

AUDIO_TAIL_OLD = '''    for chunk in split_message(f"\\U0001f3a7 Transcript \\u2014 {filename}\\n\\n{text}"):
        if await safe_reply(msg, chunk) is None:
            return
'''

AUDIO_TAIL_NEW = '''    # PATCHED (media-autostart): the transcript is the point of the upload, so
    # it goes to the agent (launching a session if the topic is fresh) rather
    # than being read out and dropped.
    from .callback_helpers import get_thread_id
    from .media_autostart import deliver_to_agent, ensure_window

    for chunk in split_message(f"\\U0001f3a7 Transcript \\u2014 {filename}\\n\\n{text}"):
        if await safe_reply(msg, chunk) is None:
            return

    thread_id = get_thread_id(update)
    window_id, already_sent = await ensure_window(user.id, thread_id, text, msg, context)
    if window_id is None or already_sent:
        return
    await deliver_to_agent(msg, user.id, thread_id, window_id, text)
'''


def main() -> None:
    print(f"[media-autostart] ccgram at {CC}")

    helper = CC / "handlers/media_autostart.py"
    if helper.exists() and helper.read_text() == HELPER:
        print("  = handlers/media_autostart.py: already written")
    else:
        helper.write_text(HELPER)
        print("  + handlers/media_autostart.py: written")

    # --- voice: transcribe first, autostart, no keyboard
    vpath = CC / "handlers/voice/voice_handler.py"
    vsrc = vpath.read_text()
    if MARK in vsrc:
        print("  = voice_handler.handle_voice_message: already patched")
    else:
        idx = vsrc.find("async def handle_voice_message(")
        if idx == -1:
            print("  ! voice_handler.handle_voice_message: ANCHOR NOT FOUND — skipped")
        else:
            vpath.write_text(vsrc[:idx] + VOICE_NEW)
            print("  + voice_handler.handle_voice_message: patched")

    # --- photo / document: autostart before resolving the upload dir
    edit("handlers/file_handler.py", FILE_SIG_OLD, FILE_SIG_NEW,
         label="file_handler._upload_and_notify", done='kind_hint: str = "upload"')
    edit("handlers/file_handler.py", PHOTO_OLD, PHOTO_NEW,
         label="file_handler.handle_photo_message sig", done=PHOTO_NEW)
    edit("handlers/file_handler.py", DOC_OLD, DOC_NEW,
         label="file_handler.handle_document_message sig", done=DOC_NEW)
    edit("handlers/file_handler.py", PHOTO_CALL_OLD, PHOTO_CALL_NEW,
         label="file_handler photo call", done='kind_hint="photo"')
    edit("handlers/file_handler.py", DOC_CALL_OLD, DOC_CALL_NEW,
         label="file_handler document call", done='kind_hint="file"')

    # --- uploaded audio: deliver the transcript too
    edit("handlers/audio_transcribe.py", AUDIO_SIG_OLD, AUDIO_SIG_NEW,
         label="audio_transcribe sig", done=AUDIO_SIG_NEW)
    edit("handlers/audio_transcribe.py", AUDIO_TAIL_OLD, AUDIO_TAIL_NEW,
         label="audio_transcribe delivery", done="from .media_autostart import")

    print("[media-autostart] done")


main()
