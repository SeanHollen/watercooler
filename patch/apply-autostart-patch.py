#!/usr/bin/env python3
"""Patch ccgram so a new topic starts a session with ZERO taps.

Stock ccgram answers the first message in an unbound topic with a window picker
(ccgram's own ``__main__`` tmux window is never bound, so this always fires),
and then a directory browser — several laggy Telegram round-trips before any
agent exists. This replaces ``_handle_unbound_topic`` so it launches the
default provider in the default directory immediately, and renames the session
after the first message.

The rename is not cosmetic: a tmux window name is the handle watercooler's
@mentions resolve against, and every session started in the same directory was
called Desktop / Desktop-2 / Desktop-3, which makes the shared room
unaddressable.

Config (environment of the ccgram process):
  WATERCOOLER_AUTOSTART=0            restore the stock pickers
  WATERCOOLER_DEFAULT_DIR=~/Desktop  cwd for every new session
  WATERCOOLER_DEFAULT_PROVIDER=claude
  WATERCOOLER_DEFAULT_MODE=yolo

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

if "PATCHED (watercooler-autostart)" in src:
    print("already patched")
    sys.exit(0)

START = "async def _handle_unbound_topic(\n"
END = "async def _handle_dead_window(\n"
if START not in src or END not in src:
    sys.exit("ERROR: anchors not found (ccgram version changed?) — not patching")

CALL_OLD = (
    "    if await _handle_unbound_topic(\n"
    "        user.id, thread_id, text, context.user_data, message\n"
    "    ):\n"
)
if CALL_OLD not in src:
    sys.exit("ERROR: call site not found (ccgram version changed?) — not patching")

NEW_FUNC = '''# PATCHED (watercooler-autostart): a new topic launches a session with no taps.


class _AutoStartQuery:
    """Minimal CallbackQuery stand-in for ``launch_window``.

    ``launch_window`` was written for a button tap: it only touches ``.message``
    and edits itself to report progress. Here the trigger was the user's own
    text, so there is no bot message to edit — an "edit" becomes a fresh reply
    into the topic instead.
    """

    def __init__(self, message: Message) -> None:
        self.message = message

    async def edit_message_text(self, text: str, **_kwargs: object) -> None:
        await safe_reply(self.message, text)


def _autostart_enabled() -> bool:
    import os

    raw = os.environ.get("WATERCOOLER_AUTOSTART", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _autostart_dir() -> str | None:
    """The cwd every new session starts in, or None to use the stock browser.

    A missing directory falls back rather than failing: the agent can always cd
    itself, but a topic that cannot start a session at all is a dead end.
    """
    import os

    raw = os.environ.get("WATERCOOLER_DEFAULT_DIR") or str(Path.home() / "Desktop")
    path = Path(os.path.expanduser(raw))
    if path.is_dir():
        return str(path)
    logger.warning(
        "Autostart dir %s does not exist; falling back to the directory browser",
        path,
    )
    return None


def _autostart_handle(text: str, taken: set[str]) -> str:
    """Build a tmux-safe, @mentionable handle from the topic's first message.

    Truncated on a word boundary rather than sliced, so the handle stays
    readable, and suffixed on collision — watercooler resolves an @mention to
    an exact window name, so two sessions sharing a handle would misroute.
    """
    import re

    words = [w for w in re.split(r"[^0-9A-Za-z]+", (text or "").lower()) if w]
    handle = ""
    for word in words:
        candidate = f"{handle}-{word}" if handle else word
        if len(candidate) > 24:
            break
        handle = candidate
    # A first word longer than the cap breaks before anything is accepted; slice
    # it rather than falling through to the generic name.
    handle = handle or (words[0][:24] if words else "session")
    if handle not in taken:
        return handle
    return next(
        (f"{handle}-{n}" for n in range(2, 100) if f"{handle}-{n}" not in taken),
        handle,
    )


async def _autostart_session(
    user_id: int,
    thread_id: int,
    text: str,
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    start_dir: str,
) -> bool:
    """Launch the default provider for a fresh topic. False = fall back."""
    import os

    # Lazy: topics ↔ text cycle, same as the other topic imports here.
    from ..topics.directory_browser import clear_workspace_state
    from ..topics.window_launch_service import WindowLaunchRequest, launch_window
    from ..status.topic_emoji import (
        format_topic_name_for_mode,
        update_stored_topic_name,
    )
    from ...session import session_manager
    from telegram.error import TelegramError

    provider = (os.environ.get("WATERCOOLER_DEFAULT_PROVIDER") or "claude").strip()
    mode = (os.environ.get("WATERCOOLER_DEFAULT_MODE") or "yolo").strip()

    # Residue from an abandoned browse/worktree/workspace flow would otherwise
    # attach a stale checkout or workspace to this brand-new session.
    clear_browse_state(context.user_data)
    clear_worktree_state(context.user_data)
    clear_workspace_state(context.user_data)

    existing = {w.window_name for w in await tmux_manager.list_windows()}
    handle = _autostart_handle(text, existing)
    logger.info(
        "Autostart: launching %s (%s) in %s as %r (user=%d, thread=%d)",
        provider,
        mode,
        start_dir,
        handle,
        user_id,
        thread_id,
    )

    result = await launch_window(
        _AutoStartQuery(message),
        context,
        WindowLaunchRequest(
            user_id=user_id,
            thread_id=thread_id,
            provider_name=provider,
            cwd=start_dir,
            mode=mode,
            pending_text=text,
        ),
    )
    if not result.success or not result.window_id:
        return False

    window_id = result.window_id
    # launch_window names the window after its directory, which is the same for
    # every session here. Rename through the same three steps ccgram uses when
    # the user renames a topic by hand, so the tmux name, the stored display
    # name and the Telegram title stay one string.
    if await tmux_manager.rename_window(window_id, handle):
        session_manager.set_display_name(window_id, handle)
        thread_router.bind_thread(
            user_id, thread_id, window_id, window_name=handle
        )
        chat = getattr(message, "chat", None)
        if chat is not None:
            update_stored_topic_name(chat.id, thread_id, handle)
            try:
                await context.bot.edit_forum_topic(
                    chat_id=chat.id,
                    message_thread_id=thread_id,
                    name=format_topic_name_for_mode(handle, mode),
                )
            except TelegramError as exc:
                logger.debug("Autostart topic rename failed: %s", exc)
    return True


async def _handle_unbound_topic(
    user_id: int,
    thread_id: int,
    text: str,
    user_data: dict | None,
    message: Message,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> bool:
    """Start a session for an unbound topic, or show the stock pickers.

    Returns True if the topic is unbound (handled), False if already bound.
    """
    window_id = thread_router.get_window_for_thread(user_id, thread_id)
    if window_id is not None:
        return False

    # PATCHED (watercooler-autostart): no picker, no browser — launch now.
    # Every failure path below falls through to the stock UI, so a bad config
    # or a launch error can never leave a topic with no way to start.
    if context is not None and _autostart_enabled():
        start_dir = _autostart_dir()
        if start_dir is not None:
            try:
                if await _autostart_session(
                    user_id, thread_id, text, message, context, start_dir
                ):
                    return True
            except Exception:
                logger.exception("Autostart failed; falling back to the pickers")
            else:
                logger.warning("Autostart declined; falling back to the pickers")

    all_windows = await tmux_manager.list_windows()
    bound_ids = {bound_wid for _, _, bound_wid in thread_router.iter_thread_bindings()}
    unbound = [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids
    ]

    if unbound:
        logger.info(
            "Unbound topic: showing window picker (%d unbound windows, user=%d, thread=%d)",
            len(unbound),
            user_id,
            thread_id,
        )
        msg_text, keyboard, win_ids = build_window_picker(unbound)
        if user_data is not None:
            user_data[STATE_KEY] = STATE_SELECTING_WINDOW
            user_data[UNBOUND_WINDOWS_KEY] = win_ids
            user_data[PENDING_THREAD_ID] = thread_id
            user_data[PENDING_THREAD_TEXT] = text
        await safe_reply(message, msg_text, reply_markup=keyboard)
        await safe_reply(message, PENDING_DELIVERY_NOTICE)
        return True

    # No unbound windows — show directory browser to create a new session
    logger.info(
        "Unbound topic: showing directory browser (user=%d, thread=%d)",
        user_id,
        thread_id,
    )
    start_path = str(Path.cwd())
    msg_text, keyboard, subdirs = build_directory_browser(start_path, user_id=user_id)
    if user_data is not None:
        user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        user_data[BROWSE_PATH_KEY] = start_path
        user_data[BROWSE_PAGE_KEY] = 0
        user_data[BROWSE_DIRS_KEY] = subdirs
        user_data[PENDING_THREAD_ID] = thread_id
        user_data[PENDING_THREAD_TEXT] = text
    await safe_reply(message, msg_text, reply_markup=keyboard)
    await safe_reply(message, PENDING_DELIVERY_NOTICE)
    return True


'''

CALL_NEW = (
    "    if await _handle_unbound_topic(\n"
    "        user.id, thread_id, text, context.user_data, message, context\n"
    "    ):\n"
)

i = src.index(START)
j = src.index(END, i)
patched = src[:i] + NEW_FUNC + src[j:]
patched = patched.replace(CALL_OLD, CALL_NEW)

bak = F.with_name("text_handler.py.orig")
if not bak.exists():
    bak.write_text(src)
F.write_text(patched)
print(f"patched {F}")
