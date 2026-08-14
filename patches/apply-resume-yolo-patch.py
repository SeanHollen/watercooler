#!/usr/bin/env python3
"""Patch ccgram: resumed sessions launch in bypass (yolo) mode, not "normal".

ccgram's _create_resume_window defaults a resumed session's approval_mode to
"normal", so /resume brings a conversation back in ask-permission mode — it then
stalls on the first edit prompt (looks "stuck"/"ended"). This forces resumed
sessions to launch with --dangerously-skip-permissions like the user's normal
sessions. Idempotent; re-run after `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/handlers/recovery/resume_command.py"
    )
)
if not matches:
    sys.exit("ERROR: resume_command.py not found")
F = pathlib.Path(matches[0])
src = F.read_text()

MARK = "watercooler patch: resumed sessions launch auto-approve"
if MARK in src:
    print("already patched")
    sys.exit(0)

ANCHOR = "    launch_args = provider.make_launch_args(resume_id=session_id)\n"
if ANCHOR not in src:
    sys.exit("ERROR: anchor not found (ccgram version changed?) — not patching")

INSERT = (
    '    approval_mode = "yolo"  # ' + MARK + " (bypass permissions)\n"
)
bak = F.with_name("resume_command.py.orig")
if not bak.exists():
    bak.write_text(src)
F.write_text(src.replace(ANCHOR, INSERT + ANCHOR, 1))
print(f"patched {F}")
