#!/usr/bin/env python3
"""Patch ccgram: a message to a topic whose window died ALWAYS resumes that
topic's existing conversation (bypass mode) instead of starting a new session.

Inserts _auto_resume_dead_topic() and calls it from _handle_dead_window before
the original browser/banner fallback. Idempotent; re-run after ccgram upgrades.
Reads the helper + insert snippets from files next to this script.
"""
import glob
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
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

if "_auto_resume_dead_topic" in src:
    print("already patched")
    sys.exit(0)

helper = (HERE / "always-resume-helper.txt").read_text()
insert = (HERE / "always-resume-insert.txt").read_text()

A1 = "async def _handle_dead_window("
if A1 not in src:
    sys.exit("ERROR: _handle_dead_window not found")

A2 = (
    '    view = window_query.view_window(window_id)\n'
    '    cwd = view.cwd if view else ""\n\n'
    "    if not cwd or not Path(cwd).is_dir():"
)
if A2 not in src:
    sys.exit("ERROR: cwd anchor not found (ccgram version changed?) — not patching")

bak = F.with_name("text_handler.py.orig")
if not bak.exists():
    bak.write_text(src)

# 1) insert helper function above _handle_dead_window
src = src.replace(A1, helper.rstrip("\n") + "\n\n\n" + A1, 1)

# 2) insert the auto-resume attempt inside _handle_dead_window
N2 = (
    '    view = window_query.view_window(window_id)\n'
    '    cwd = view.cwd if view else ""\n'
    + insert.rstrip("\n")
    + "\n\n    if not cwd or not Path(cwd).is_dir():"
)
src = src.replace(A2, N2, 1)

F.write_text(src)
print(f"patched {F}")
