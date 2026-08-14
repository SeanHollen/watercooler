#!/usr/bin/env python3
"""Patch ccgram's handle_general_topic_message into the shared-room bus.

Idempotent: it always cuts utils.py at the function definition anchor and
re-appends the replacement from ~/.ccgram/general_handler_new.txt.
Re-run this after any `uv tool upgrade ccgram`.
"""
import glob
import os
import pathlib
import sys

home = pathlib.Path.home()
matches = glob.glob(
    str(home / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/utils.py")
)
if not matches:
    sys.exit("ERROR: could not find ccgram utils.py")
F = pathlib.Path(matches[0])
newfunc = (home / ".ccgram" / "general_handler_new.txt").read_text()

src = F.read_text()
anchor = "async def handle_general_topic_message("
if anchor not in src:
    sys.exit("ERROR: anchor not found in utils.py")

# One-time backup of the pristine file
bak = F.with_name("utils.py.orig")
if not bak.exists():
    bak.write_text(src)

i = src.index(anchor)
patched = src[:i].rstrip("\n") + "\n\n\n" + newfunc.rstrip("\n") + "\n"
F.write_text(patched)
print(f"patched {F}")
