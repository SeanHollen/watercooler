#!/usr/bin/env python3
"""Patch ccgram to capture Telegram location pins (incl. LIVE location).

Default ccgram has no location handler, so a shared pin falls through to the
`unsupported_content_handler` catch-all and is discarded. This:
  1. Writes a handler module `ccgram/handlers/location_handler.py` that stores
     the latest coordinates to ~/.ccgram/last_location.json. Live-location
     updates arrive as *edited* messages and silently overwrite the file (no
     session spam); the first (non-edited) pin gets a one-time confirmation reply.
  2. Patches handlers/registry.py to register that handler BEFORE the catch-all
     and to exclude filters.LOCATION from the catch-all filter.

Read the pin from any Claude session with the `mylocation` helper, or:
    cat ~/.ccgram/last_location.json

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

# ---------------------------------------------------------------- handler module
HANDLER_SRC = '''\
"""Capture Telegram location pins (including live location) to a JSON file.

Added by ~/.ccgram/apply-location-patch.py — not part of upstream ccgram.
Live location arrives as a stream of *edited* messages; each overwrites the
file so ~/.ccgram/last_location.json always holds the most recent fix.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import TYPE_CHECKING

import structlog

from ..config import config
from .messaging_pipeline.message_sender import safe_reply

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

LOCATION_FILE = pathlib.Path.home() / ".ccgram" / "last_location.json"


async def handle_location_message(
    update: "Update",
    _context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    """Store the latest location fix; confirm once on the initial (non-edited) pin."""
    msg = update.effective_message
    if not msg or not msg.location:
        return
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return

    loc = msg.location
    is_edited = update.edited_message is not None
    data = {
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "horizontal_accuracy": loc.horizontal_accuracy,
        "live_period": loc.live_period,
        "heading": loc.heading,
        "is_live": loc.live_period is not None,
        "is_edited_update": is_edited,
        "message_id": msg.message_id,
        "chat_id": msg.chat_id,
        "thread_id": getattr(msg, "message_thread_id", None),
        "updated_at": time.time(),
        "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
    }
    try:
        LOCATION_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:  # noqa: BLE001
        logger.debug("location_write_failed", error=str(e))
        return

    logger.info(
        "location_captured",
        lat=loc.latitude,
        lon=loc.longitude,
        live=data["is_live"],
        edited=is_edited,
    )

    # Reply only on the first (non-edited) pin to avoid spamming live updates.
    if not is_edited:
        kind = "Live location" if data["is_live"] else "Location"
        await safe_reply(
            msg,
            f"\\U0001F4CD {kind} received \\u2014 Claude can read it via `mylocation` "
            "or ~/.ccgram/last_location.json.",
        )
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
HANDLERS_DIR = REG.parent
HANDLER = HANDLERS_DIR / "location_handler.py"

# 1) write / refresh the handler module (always, so upgrades restore it)
HANDLER.write_text(HANDLER_SRC)
print(f"wrote {HANDLER}")

# 2) patch registry.py
src = REG.read_text()

bak = REG.with_name("registry.py.orig")
if not bak.exists():
    bak.write_text(src)

if "handle_location_message" in src:
    print("registry.py already patched")
    sys.exit(0)

# 2a) import — after the voice import
IMPORT_ANCHOR = "from .voice import handle_voice_message\n"
if IMPORT_ANCHOR not in src:
    sys.exit("ERROR: voice import anchor not found (ccgram version changed?)")
src = src.replace(
    IMPORT_ANCHOR,
    IMPORT_ANCHOR + "from .location_handler import handle_location_message\n",
    1,
)

# 2b) register the LOCATION handler right after the VOICE handler block,
#     before the catch-all unsupported handler.
VOICE_BLOCK = (
    "    application.add_handler(\n"
    "        MessageHandler(filters.VOICE & group_filter, handle_voice_message)\n"
    "    )\n"
)
if VOICE_BLOCK not in src:
    sys.exit("ERROR: voice handler block anchor not found (ccgram version changed?)")
LOCATION_BLOCK = (
    "    application.add_handler(\n"
    "        MessageHandler(filters.LOCATION & group_filter, handle_location_message)\n"
    "    )\n"
)
src = src.replace(VOICE_BLOCK, VOICE_BLOCK + LOCATION_BLOCK, 1)

# 2c) exclude LOCATION from the catch-all filter
CATCHALL_ANCHOR = "            & ~filters.VOICE\n"
if CATCHALL_ANCHOR not in src:
    sys.exit("ERROR: catch-all filter anchor not found (ccgram version changed?)")
src = src.replace(
    CATCHALL_ANCHOR,
    CATCHALL_ANCHOR + "            & ~filters.LOCATION\n",
    1,
)

REG.write_text(src)
print(f"patched {REG}")
