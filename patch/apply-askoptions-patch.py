#!/usr/bin/env python3
"""Patch ccgram so AskUserQuestion (and other numbered-option prompts) render
ONE TAPPABLE BUTTON PER OPTION in Telegram, instead of forcing arrow-key
navigation.

Why: ccgram already detects AskUserQuestion and sends the terminal text with a
row of ↑↓←→/Enter buttons. Selecting an option means tapping arrows repeatedly,
and typing a sentence instead gets sent as raw keystrokes into the picker and
registers as a rejection (the user's words are lost). This adds a labeled button
for each numbered option; tapping it closed-loop-navigates the TUI cursor to that
option (using the visible ❯ marker) and presses Enter. The old arrow buttons are
kept as a fallback, so nothing regresses.

Additive & idempotent:
  - new callback prefix ``aqo:`` (CB_ASK_OPT), registered separately via the
    registry's longest-prefix dispatch — existing ``aq:*`` handlers untouched.
  - option buttons only appear when numbered options are parsed; otherwise the
    keyboard is exactly as before.

Re-run after any ``uv tool upgrade ccgram`` (wired into apply-all-patches.sh).
"""
import glob
import pathlib
import sys

MARKER = "PATCHED (ask-options)"


def find(rel: str) -> pathlib.Path:
    matches = glob.glob(
        str(
            pathlib.Path.home()
            / f".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/{rel}"
        )
    )
    if not matches:
        sys.exit(f"ERROR: {rel} not found")
    return pathlib.Path(matches[0])


CBD = find("handlers/callback_data.py")
UI = find("handlers/interactive/interactive_ui.py")
CBK = find("handlers/interactive/interactive_callbacks.py")

cbd = CBD.read_text()
ui = UI.read_text()
cbk = CBK.read_text()

if MARKER in cbd and MARKER in ui and MARKER in cbk:
    print("already patched")
    sys.exit(0)


def need(src: str, anchor: str, where: str) -> None:
    if anchor not in src:
        sys.exit(f"ERROR: anchor not found in {where}: {anchor!r} (ccgram changed?)")


# ── 1. callback_data.py: add CB_ASK_OPT ──────────────────────────────────
CBD_ANCHOR = 'CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>\n'
need(cbd, CBD_ANCHOR, "callback_data.py")
cbd = cbd.replace(
    CBD_ANCHOR,
    CBD_ANCHOR
    + '# ' + MARKER + '\n'
    + 'CB_ASK_OPT = "aqo:"  # aqo:<option_index>:<window>  (tappable option button)\n',
    1,
)

# ── 2. interactive_ui.py ─────────────────────────────────────────────────
need(ui, "import time\n", "interactive_ui.py")
ui = ui.replace("import time\n", "import re\nimport time\n", 1)

need(ui, "    CB_ASK_DOWN,\n", "interactive_ui.py (import)")
ui = ui.replace("    CB_ASK_DOWN,\n", "    CB_ASK_DOWN,\n    CB_ASK_OPT,\n", 1)

UI_PARSER = '''# ''' + MARKER + '''
_NUM_OPT_RE = re.compile(r"^\\s*([❯›▶])?\\s*(\\d+)\\.\\s+(\\S.*?)\\s*$")


def parse_numbered_options(text: str) -> tuple[list[str], int | None]:
    """Extract numbered option labels from a Claude Code selection prompt.

    Claude renders AskUserQuestion / selection UIs as::

        ❯ 1. Tea
          2. Coffee
          3. Water

    Returns ``(labels, current_index)`` where ``current_index`` is the option
    marked by the ❯ cursor (or None if not visible). Description lines and
    other chrome are ignored.
    """
    labels: list[str] = []
    current: int | None = None
    for line in (text or "").splitlines():
        m = _NUM_OPT_RE.match(line)
        if not m:
            continue
        if m.group(1):
            current = len(labels)
        labels.append(m.group(3).strip())
    return labels, current


'''
need(ui, "def _build_interactive_keyboard(", "interactive_ui.py (build kb)")
ui = ui.replace(
    "def _build_interactive_keyboard(",
    UI_PARSER + "def _build_interactive_keyboard(",
    1,
)

KB_SIG = (
    "    ui_name: str = \"\",\n"
    "    pane_id: str | None = None,\n"
    ") -> InlineKeyboardMarkup:"
)
need(ui, KB_SIG, "interactive_ui.py (kb signature)")
ui = ui.replace(
    KB_SIG,
    "    ui_name: str = \"\",\n"
    "    pane_id: str | None = None,\n"
    "    options: list[str] | None = None,\n"
    ") -> InlineKeyboardMarkup:",
    1,
)

KB_ROWS = "    rows: list[list[InlineKeyboardButton]] = []\n"
need(ui, KB_ROWS, "interactive_ui.py (kb rows)")
KB_OPT_BUTTONS = (
    KB_ROWS
    + "    # " + MARKER + ": one tappable button per numbered option.\n"
    + "    for _oi, _label in enumerate(options or []):\n"
    + "        _txt = f\"{_oi + 1}. {_label}\".strip()\n"
    + "        if len(_txt) > 60:\n"
    + "            _txt = _txt[:57] + \"…\"\n"
    + "        rows.append(\n"
    + "            [\n"
    + "                InlineKeyboardButton(\n"
    + "                    _txt, callback_data=f\"{CB_ASK_OPT}{_oi}:{target}\"[:64]\n"
    + "                )\n"
    + "            ]\n"
    + "        )\n"
)
ui = ui.replace(KB_ROWS, KB_OPT_BUTTONS, 1)

UI_UNPACK = "    ui_name, text = captured\n"
need(ui, UI_UNPACK, "interactive_ui.py (unpack)")
ui = ui.replace(
    UI_UNPACK,
    UI_UNPACK + "    ask_options, _ = parse_numbered_options(text)\n",
    1,
)

UI_KB_CALL = (
    "    keyboard = _build_interactive_keyboard(window_id, ui_name=ui_name, pane_id=pane_id)"
)
need(ui, UI_KB_CALL, "interactive_ui.py (kb call)")
ui = ui.replace(
    UI_KB_CALL,
    "    keyboard = _build_interactive_keyboard(\n"
    "        window_id, ui_name=ui_name, pane_id=pane_id, options=ask_options\n"
    "    )",
    1,
)

# ── 3. interactive_callbacks.py ──────────────────────────────────────────
need(cbk, "    CB_ASK_DOWN,\n", "interactive_callbacks.py (import)")
cbk = cbk.replace("    CB_ASK_DOWN,\n", "    CB_ASK_DOWN,\n    CB_ASK_OPT,\n", 1)

need(cbk, "    CB_ASK_UP,\n)", "interactive_callbacks.py (import end)")
cbk = cbk.replace(
    "    CB_ASK_UP,\n)",
    "    CB_ASK_UP,\n    CB_PANE_DELIMITER,\n)",
    1,
)

CBK_UI_IMPORT = (
    "from .interactive_ui import clear_interactive_msg, handle_interactive_ui"
)
need(cbk, CBK_UI_IMPORT, "interactive_callbacks.py (ui import)")
cbk = cbk.replace(
    CBK_UI_IMPORT,
    "from .interactive_ui import (\n"
    "    clear_interactive_msg,\n"
    "    handle_interactive_ui,\n"
    "    parse_numbered_options,\n"
    ")",
    1,
)

CBK_HANDLER = '''

# ''' + MARKER + ''': tappable per-option selection.
_ASK_OPT_MAX_STEPS = 40
_ASK_OPT_SETTLE_S = 0.18


async def _ask_capture(window_id: str, pane_id: str | None) -> str | None:
    if pane_id:
        return await tmux_manager.capture_pane_by_id(pane_id, window_id=window_id)
    w = await tmux_manager.find_window_by_id(window_id)
    return await tmux_manager.capture_pane(w.window_id) if w else None


async def _ask_send(window_id: str, pane_id: str | None, key: str) -> bool:
    if pane_id:
        return await tmux_manager.send_keys_to_pane(
            pane_id, key, enter=False, literal=False, window_id=window_id
        )
    w = await tmux_manager.find_window_by_id(window_id)
    return bool(w) and await tmux_manager.send_keys(
        w.window_id, key, enter=False, literal=False
    )


async def _handle_option_select(
    query: CallbackQuery,
    user_id: int,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Navigate the TUI selection cursor to a tapped option, then press Enter."""
    from ..callback_helpers import get_thread_id, user_owns_window

    rest = data[len(CB_ASK_OPT):]
    try:
        idx_str, target = rest.split(":", 1)
        target_idx = int(idx_str)
    except ValueError:
        await query.answer()
        return

    if CB_PANE_DELIMITER in target:
        window_id, pane_id = target.split(CB_PANE_DELIMITER, 1)
    else:
        window_id, pane_id = target, None

    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return

    thread_id = get_thread_id(update)
    client = PTBTelegramClient(context.bot)

    reached = False
    for _ in range(_ASK_OPT_MAX_STEPS):
        text = await _ask_capture(window_id, pane_id)
        opts, cur = parse_numbered_options(text or "")
        if not opts or cur is None or target_idx >= len(opts):
            break
        if cur == target_idx:
            reached = True
            break
        n = len(opts)
        down = (target_idx - cur) % n
        up = (cur - target_idx) % n
        await _ask_send(window_id, pane_id, "Down" if down <= up else "Up")
        await asyncio.sleep(_ASK_OPT_SETTLE_S)

    if reached:
        await _ask_send(window_id, pane_id, "Enter")
        await asyncio.sleep(0.4)
        # Multi-question pickers advance to the next question; re-render it.
        shown = await handle_interactive_ui(
            client, user_id, window_id, thread_id, pane_id=pane_id
        )
        if not shown:
            await clear_interactive_msg(user_id, client, thread_id)
        await query.answer("✓")
    else:
        await handle_interactive_ui(
            client, user_id, window_id, thread_id, pane_id=pane_id
        )
        await query.answer("Couldn't jump — use the arrows")


@register(CB_ASK_OPT)
async def _dispatch_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    assert query is not None and query.data is not None and user is not None
    await _handle_option_select(query, user.id, query.data, update, context)
'''
need(cbk, "async def _dispatch(update: Update", "interactive_callbacks.py (dispatch)")
cbk = cbk.rstrip("\n") + "\n" + CBK_HANDLER

# ── write ────────────────────────────────────────────────────────────────
for path, content in ((CBD, cbd), (UI, ui), (CBK, cbk)):
    path.with_suffix(path.suffix + ".bak-askopts").write_text(path.read_text())
    path.write_text(content)

print("patched ask-options into:")
print(f"  {CBD}")
print(f"  {UI}")
print(f"  {CBK}")
