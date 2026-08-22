#!/usr/bin/env python3
"""Patch ccgram: forward the quoted message's text when the user replies.

Telegram's "reply to a message" (swipe/long-press quote) attaches the quoted
message on ``message.reply_to_message`` — ccgram's text_handler.handle_text_message
never reads it, only ``message.text`` (the user's newly typed words). So
replying to an old message with a pointer ("continue this one") and typing a
short follow-up silently drops the quoted content; the agent only ever sees
the new text, with no way to know what was quoted.

Fix: prepend the quoted content to ``text`` — labelled, so the agent can tell
what's quoted vs freshly typed — before any downstream routing (autostart
handle, forwarding, shell provider). Single insertion point right after
``text = message.text`` covers every path, since they all read ``text``
afterward.

IMPORTANT (fixed 2026-08-13): Telegram's reply-to-message carries the WHOLE
original message on ``message.reply_to_message``, but when the sender selects
only part of it before hitting reply (long-press → drag-select → Reply),
Telegram Bot API 7.0+ separately exposes exactly that selection on
``message.quote`` (a ``TextQuote`` with ``.text``/``.is_manual`` — see
python-telegram-bot's ``telegram/_reply.py``). The original version of this
patch never read ``message.quote`` at all, so a user quoting one specific
sentence out of a long agent reply had that specific sentence silently
discarded and got the ENTIRE original message back instead — indistinguishable
from a plain "reply to this message" with no selection. Prefer
``message.quote.text`` (the actual selected span) when Telegram provides it;
fall back to the full ``reply.text``/``.caption`` only when it doesn't (older
clients, or a reply with no text selection).

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/handlers/text/text_handler.py"
    )
)
if not matches:
    sys.exit("ERROR: text_handler.py not found")
F = pathlib.Path(matches[0])
src = F.read_text()

if "quoted_reply_prepended" in src:
    print("already patched")
    sys.exit(0)

ANCHOR = "    text = message.text\n    thread_id = _get_thread_id(update)\n"
if ANCHOR not in src:
    sys.exit("ERROR: anchor not found (ccgram version changed?) — not patching")

NEW = (
    "    text = message.text\n"
    "    # PATCHED (quote-context): Telegram reply-quotes are otherwise dropped —\n"
    "    # only the newly typed text reached the agent, never the quoted message.\n"
    "    # message.quote is the specific selected span (Bot API 7.0+, TextQuote);\n"
    "    # prefer it over the whole reply_to_message so a partial quote-select\n"
    "    # isn't reported back as the entire original message.\n"
    "    reply = message.reply_to_message\n"
    "    quote_obj = getattr(message, \"quote\", None)\n"
    "    quoted = getattr(quote_obj, \"text\", None) if quote_obj else None\n"
    "    if not quoted and reply:\n"
    "        quoted = getattr(reply, \"text\", None) or getattr(reply, \"caption\", None)\n"
    "    if quoted:\n"
    "        logger.info(\"quoted_reply_prepended\", thread_id=_get_thread_id(update))\n"
    "        text = f'[Replying to: \"{quoted}\"]\\n{text}'\n"
    "    thread_id = _get_thread_id(update)\n"
)

i = src.index(ANCHOR)
bak = F.with_name("text_handler.py.orig")
if not bak.exists():
    bak.write_text(src)
F.write_text(src[:i] + NEW + src[i + len(ANCHOR):])
print(f"patched {F}")
