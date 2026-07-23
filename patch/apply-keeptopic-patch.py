#!/usr/bin/env python3
"""Patch ccgram autoclose: free a session's RAM but KEEP its topic.

Default ccgram deletes the Telegram topic on autoclose, which makes the
conversation unrecoverable. This rewrites _close_expired_topic so it instead
kills only the tmux window (freeing RAM) and leaves the topic + thread binding
intact — so messaging the topic later triggers ccgram's built-in dead-window
recovery (resume with full context).

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/handlers/topics/topic_lifecycle.py"
    )
)
if not matches:
    sys.exit("ERROR: topic_lifecycle.py not found")
F = pathlib.Path(matches[0])
src = F.read_text()

if "autoclose_freed_window_kept_topic" in src:
    print("already patched")
    sys.exit(0)

START = "    chat_id = thread_router.resolve_chat_id(user_id, thread_id)\n    removed = False\n"
END = "        thread_router.unbind_thread(user_id, thread_id)\n"

if START not in src:
    sys.exit("ERROR: start anchor not found (ccgram version changed?) — not patching")
i = src.index(START)
j = src.index(END, i) + len(END)

NEW = (
    "    # PATCHED (keep-topic autoclose): free the session's RAM by killing its\n"
    "    # tmux window, but KEEP the Telegram topic + binding so messaging it later\n"
    "    # triggers ccgram's dead-window recovery (resume with full context).\n"
    "    lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)\n"
    "    if window_id is not None:\n"
    "        try:\n"
    "            await tmux_manager.kill_window(window_id)\n"
    "            logger.info(\n"
    '                "autoclose_freed_window_kept_topic",\n'
    "                thread_id=thread_id,\n"
    "                user_id=user_id,\n"
    "                window_id=window_id,\n"
    "            )\n"
    "        except Exception as e:  # noqa: BLE001\n"
    '            logger.debug("autoclose_kill_failed", thread_id=thread_id, error=str(e))\n'
)

bak = F.with_name("topic_lifecycle.py.orig")
if not bak.exists():
    bak.write_text(src)
F.write_text(src[:i] + NEW + src[j:])
print(f"patched {F}")
