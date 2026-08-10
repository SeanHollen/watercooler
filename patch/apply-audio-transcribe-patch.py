#!/usr/bin/env python3
"""Patch ccgram to transcribe UPLOADED AUDIO FILES via Whisper.

Upstream ccgram already has a whisper stack (`ccgram/whisper/`, an
OpenAI-compatible transcriber) but wires it only to `filters.VOICE` — Telegram
*voice notes* recorded in-app. An uploaded audio file never reaches it:
  - mp3/m4a sent as music  -> filters.AUDIO      -> unsupported_content_handler
  - anything sent as "File" -> filters.Document.ALL -> file_handler (saved, not read)

This:
  1. Writes `ccgram/handlers/audio_transcribe.py`, a handler covering both
     shapes (filters.AUDIO and any audio/* document) that transcribes and
     replies with the plain transcript. Nothing is forwarded to the agent and no
     confirm keyboard is shown — unlike voice notes, an uploaded file is treated
     as "transcribe this for me", not as dictated input.
  2. Patches handlers/registry.py to register it BEFORE the Document.ALL
     handler (otherwise file_handler eats audio documents first — within one
     PTB handler group only the first match runs) and to exclude filters.AUDIO
     from the catch-all filter.

Requires whisper config in ~/.ccgram/.env, e.g.:
    CCGRAM_WHISPER_PROVIDER=groq
    CCGRAM_WHISPER_API_KEY=gsk_...
(providers: groq | openai; optional CCGRAM_WHISPER_MODEL / _LANGUAGE / _BASE_URL)

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

# ---------------------------------------------------------------- handler module
HANDLER_SRC = '''\
"""Transcribe uploaded audio files with Whisper and reply with the text.

Added by ~/.ccgram/apply-audio-transcribe-patch.py — not part of upstream ccgram.

Covers both shapes an uploaded audio file can arrive as: filters.AUDIO (mp3/m4a
sent as music) and audio/* documents (anything sent via "File"). Upstream only
transcribes voice notes. The transcript is replied in-topic and goes nowhere
else — no confirm keyboard, no send-to-agent, no window binding required, so
this works in an unbound topic too.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from telegram.constants import ChatAction
from telegram.error import TelegramError

from ..config import config
from ..telegram_sender import split_message
from ..whisper import get_transcriber
from .messaging_pipeline.message_sender import safe_reply

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

# Telegram caps *bot* downloads (getFile) at 20 MB — a lower ceiling than both
# the 25 MB OpenAI/Groq upload limit and ccgram's own transcriber check, so this
# is the one that actually bites. Check it up front for a clear error.
_MAX_AUDIO_SIZE = 20 * 1024 * 1024

# The transcription API sniffs the format from the multipart filename, so the
# extension must be one it actually accepts — a document can carry an audio
# mime type under any name at all ("voice memo", "notes.txt"), in which case we
# derive the extension from the mime type instead of trusting the name.
_API_EXTS = {
    "flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "opus", "wav", "webm",
}

_MIME_EXT = {
    "mpeg": "mp3",
    "mp3": "mp3",
    "mp4": "m4a",
    "x-m4a": "m4a",
    "aac": "aac",
    "ogg": "ogg",
    "opus": "ogg",
    "wav": "wav",
    "x-wav": "wav",
    "vnd.wave": "wav",
    "webm": "webm",
    "flac": "flac",
    "x-flac": "flac",
}


def _audio_obj(msg: Any) -> Any:
    """Return the Audio/Document carrying audio, or None if this isn't audio."""
    if getattr(msg, "audio", None) is not None:
        return msg.audio
    doc = getattr(msg, "document", None)
    if doc is not None and (doc.mime_type or "").startswith("audio/"):
        return doc
    return None


def _filename_for(obj: Any) -> str:
    """Best-effort upload filename ending in an extension the API accepts."""
    name = getattr(obj, "file_name", None)
    if name:
        stem = Path(name).name  # strip any directory component
        if Path(stem).suffix.lstrip(".").lower() in _API_EXTS:
            return stem
    subtype = (getattr(obj, "mime_type", None) or "").split("/")[-1].lower()
    return f"audio.{_MIME_EXT.get(subtype, 'mp3')}"


def _duration_hint(obj: Any) -> str:
    """ ' 4m12s of audio' when Telegram tells us the duration, else ''. """
    secs = getattr(obj, "duration", None)
    if not secs:
        return ""
    m, s = divmod(int(secs), 60)
    return f" {m}m{s:02d}s of audio" if m else f" {s}s of audio"


def _eta_hint(obj: Any, transcriber: Any) -> str:
    """ ' — small.en, about 2 min' for on-device runs, which are slow enough that
    a silent multi-minute wait reads as a hang. Naming the model also makes the
    accuracy/speed trade the ladder just made visible rather than mysterious.
    Hosted providers expose no progress_hint and get nothing."""
    hint = getattr(transcriber, "progress_hint", None)
    secs = getattr(obj, "duration", None)
    if hint is None or not secs:
        return ""
    return hint(secs)


async def _clear(status: Any) -> None:
    """Remove the progress message; harmless if it is already gone."""
    if status is None:
        return
    try:
        await status.delete()
    except TelegramError:
        pass


async def handle_audio_message(
    update: "Update",
    _context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    """Transcribe an uploaded audio file and reply with the transcript."""
    msg = update.effective_message
    if msg is None:
        return
    obj = _audio_obj(msg)
    if obj is None:
        return

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        await safe_reply(msg, "You are not authorized to use this bot.")
        return

    size = getattr(obj, "file_size", None)
    if size is not None and size > _MAX_AUDIO_SIZE:
        await safe_reply(
            msg,
            f"\\u274c Audio too large ({size / (1024 * 1024):.1f} MB). Telegram "
            "only lets bots download files up to 20 MB \\u2014 split it or "
            "compress it first.",
        )
        return

    try:
        transcriber = get_transcriber()
    except (ValueError, RuntimeError) as e:
        await safe_reply(msg, f"\\u274c {e}")
        return
    if transcriber is None:
        await safe_reply(
            msg,
            "\\u26a0\\ufe0f Transcription is not configured. Set "
            "CCGRAM_WHISPER_PROVIDER (groq or openai) and CCGRAM_WHISPER_API_KEY "
            "in ~/.ccgram/.env, then restart ccgram.",
        )
        return

    try:
        await msg.get_bot().send_chat_action(
            chat_id=msg.chat_id,
            message_thread_id=getattr(msg, "message_thread_id", None),
            action=ChatAction.TYPING,
        )
    except TelegramError:
        pass  # cosmetic only

    try:
        tg_file = await msg.get_bot().get_file(obj.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())
    except TelegramError as e:
        logger.warning("audio_download_failed", error=str(e))
        await safe_reply(msg, f"\\u274c Failed to download the audio file: {e}")
        return

    filename = _filename_for(obj)

    # On-device inference runs for minutes, well past the ~5 s life of a typing
    # indicator, so leave a real message behind and clear it when the text lands.
    status = await safe_reply(
        msg,
        f"\\U0001f3a7 Transcribing{_duration_hint(obj)}"
        f"{_eta_hint(obj, transcriber)}\\u2026",
    )

    try:
        result = await transcriber.transcribe(audio_bytes, filename)
    except (ValueError, RuntimeError) as e:
        logger.warning("audio_transcribe_failed", error=str(e))
        await _clear(status)
        await safe_reply(msg, f"\\u274c {e}")
        return

    await _clear(status)

    text = (result.text or "").strip()
    if not text:
        await safe_reply(msg, "\\u26a0\\ufe0f Could not transcribe that audio (empty result).")
        return

    logger.info(
        "audio_transcribed", filename=filename, bytes=len(audio_bytes), chars=len(text)
    )

    for chunk in split_message(f"\\U0001f3a7 Transcript \\u2014 {filename}\\n\\n{text}"):
        if await safe_reply(msg, chunk) is None:
            return
'''

# ---------------------------------------------------------------- locate package
matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/handlers/registry.py"
    )
)
if not matches:
    sys.exit("ERROR: could not find ccgram handlers/registry.py")
REG = pathlib.Path(matches[0])
HANDLER = REG.parent / "audio_transcribe.py"

# 1) write / refresh the handler module (always, so upgrades restore it)
HANDLER.write_text(HANDLER_SRC)
print(f"wrote {HANDLER}")

# 2) patch registry.py
src = REG.read_text()

if "handle_audio_message" in src:
    print("registry.py already patched")
    sys.exit(0)

# 2a) import — after the voice import
IMPORT_ANCHOR = "from .voice import handle_voice_message\n"
if IMPORT_ANCHOR not in src:
    sys.exit("ERROR: voice import anchor not found (ccgram version changed?)")
src = src.replace(
    IMPORT_ANCHOR,
    IMPORT_ANCHOR + "from .audio_transcribe import handle_audio_message\n",
    1,
)

# 2b) register BEFORE the Document.ALL handler — audio sent as a "File" matches
#     Document.ALL too, and within a PTB handler group only the first match runs.
DOC_BLOCK = (
    "    application.add_handler(\n"
    "        MessageHandler(filters.Document.ALL & group_filter, handle_document_message)\n"
    "    )\n"
)
if DOC_BLOCK not in src:
    sys.exit("ERROR: document handler block anchor not found (ccgram version changed?)")
AUDIO_BLOCK = (
    "    application.add_handler(\n"
    "        MessageHandler(\n"
    '            (filters.AUDIO | filters.Document.Category("audio/")) & group_filter,\n'
    "            handle_audio_message,\n"
    "        )\n"
    "    )\n"
)
src = src.replace(DOC_BLOCK, AUDIO_BLOCK + DOC_BLOCK, 1)

# 2c) exclude AUDIO from the catch-all filter (audio documents are already
#     excluded there via ~filters.Document.ALL)
CATCHALL_ANCHOR = "            & ~filters.VOICE\n"
if CATCHALL_ANCHOR not in src:
    sys.exit("ERROR: catch-all filter anchor not found (ccgram version changed?)")
src = src.replace(
    CATCHALL_ANCHOR,
    CATCHALL_ANCHOR + "            & ~filters.AUDIO\n",
    1,
)

REG.write_text(src)
print(f"patched {REG}")
