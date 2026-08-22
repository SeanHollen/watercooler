#!/usr/bin/env python3
"""Stop ccgram from ever renaming a Telegram forum topic.

Sean's rule: a topic's title is whatever he typed when he created it, forever.
ccgram disagreed on two fronts:

  1. The emoji status system (topic_emoji.py) treats the *tmux window's*
     display name as the topic's "clean name" and re-sends
     "<emoji> <clean name>" on every active/idle/done/dead transition — so the
     very first status flip after a topic is created overwrites Sean's title
     with whatever ccgram's internal window name happens to be.
  2. window_launch_service.py explicitly renames the topic to the newly
     launched window's name when binding a topic to a window.

(The watercooler-autostart patch has its own explicit topic rename for the
same reason; apply-autostart-patch.py patches that one directly since it's
already rewriting that function wholesale.)

Fix: neuter ``PTBTelegramClient.edit_forum_topic`` (telegram_client.py) into a
no-op. Every call site above (and the emoji status site, and recovery/resume
rebinding) goes through this one adapter method, so silencing it here covers
all of them in one place and survives call sites ccgram adds later. The one
call site that bypasses the adapter — window_launch_service.py's raw
``context.bot.edit_forum_topic`` — is patched separately below.

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""

import glob
import pathlib
import sys


def patch_file(pattern, marker, old, new, label):
    matches = glob.glob(
        str(pathlib.Path.home() / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram" / pattern)
    )
    if not matches:
        print(f"ERROR: {pattern} not found — skipping {label}")
        return False
    f = pathlib.Path(matches[0])
    src = f.read_text()
    if marker in src:
        print(f"{label}: already patched")
        return True
    if old not in src:
        print(f"ERROR: anchor not found in {f} (ccgram version changed?) — skipping {label}")
        return False
    bak = f.with_name(f.name + ".orig")
    if not bak.exists():
        bak.write_text(src)
    f.write_text(src.replace(old, new, 1))
    print(f"patched {f} ({label})")
    return True


CLIENT_OLD = '''    async def edit_forum_topic(
        self, chat_id: int | str, message_thread_id: int, **kwargs: Any
    ) -> bool:
        return await self._bot.edit_forum_topic(
            chat_id=chat_id, message_thread_id=message_thread_id, **kwargs
        )'''

CLIENT_NEW = '''    async def edit_forum_topic(
        self, chat_id: int | str, message_thread_id: int, **kwargs: Any
    ) -> bool:
        # PATCHED (no-topic-rename): Sean's topic titles are fixed at creation
        # — never rewritten by status emoji, autostart, or rebind flows.
        return True'''

LAUNCH_OLD = '''    try:
        await context.bot.edit_forum_topic(
            chat_id=thread_router.resolve_chat_id(user_id, pending_thread_id),
            message_thread_id=pending_thread_id,
            name=format_topic_name_for_mode(created_wname, approval_mode),
        )
    except TelegramError as e:
        logger.debug("Failed to rename topic: %s", e)'''

LAUNCH_NEW = '''    # PATCHED (no-topic-rename): never rename the topic on bind.
    pass'''

ok1 = patch_file(
    "telegram_client.py",
    "PATCHED (no-topic-rename)",
    CLIENT_OLD,
    CLIENT_NEW,
    "PTBTelegramClient.edit_forum_topic",
)
ok2 = patch_file(
    "handlers/topics/window_launch_service.py",
    "PATCHED (no-topic-rename)",
    LAUNCH_OLD,
    LAUNCH_NEW,
    "window_launch_service rebind rename",
)

sys.exit(0 if (ok1 and ok2) else 1)
