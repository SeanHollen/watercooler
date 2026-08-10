#!/usr/bin/env python3
"""Patch ccgram so providers WITH a yolo mode (claude, gemini) skip the
normal/yolo mode picker and launch directly in YOLO.

By default ccgram shows a "normal vs yolo" mode picker every time you create a
topic for a yolo-capable provider. This rewrites the tail of
``_handle_provider_select`` so that instead of building that picker, it calls
``launch_window`` immediately with ``mode="yolo"`` — one fewer tap per session.

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/handlers/topics/provider_mode_callbacks.py"
    )
)
if not matches:
    sys.exit("ERROR: provider_mode_callbacks.py not found")
F = pathlib.Path(matches[0])
src = F.read_text()

if "PATCHED (yolo-default)" in src:
    print("already patched")
    sys.exit(0)

OLD = (
    "    text, keyboard = build_mode_picker(selected_path, provider_name)\n"
    "    await safe_edit(query, text, reply_markup=keyboard)\n"
)

if OLD not in src:
    sys.exit("ERROR: anchor not found (ccgram version changed?) — not patching")

NEW = (
    "    # PATCHED (yolo-default): skip the normal/yolo mode picker and launch\n"
    "    # directly in yolo. Sean wants every ccgram session to assume yolo so he\n"
    "    # never has to tap the picker. build_mode_picker stays imported/unused.\n"
    "    clear_browse_state(context.user_data)\n"
    "    await launch_window(\n"
    "        query,\n"
    "        context,\n"
    "        WindowLaunchRequest(\n"
    "            user_id=user_id,\n"
    "            thread_id=pending_thread_id,\n"
    "            provider_name=provider_name,\n"
    "            cwd=selected_path,\n"
    '            mode="yolo",\n'
    "            pending_text=(\n"
    "                context.user_data.get(PENDING_THREAD_TEXT)\n"
    "                if context.user_data\n"
    "                else None\n"
    "            ),\n"
    "        ),\n"
    "    )\n"
)

bak = F.with_name("provider_mode_callbacks.py.orig")
if not bak.exists():
    bak.write_text(src)
F.write_text(src.replace(OLD, NEW))
print(f"patched {F}")
