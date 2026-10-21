# watercooler — project notes

## Shipping
`./scripts/ship.sh "<message>"` is the single entry point: gate → commit + push →
deploy. There is one target, `prod`, and it means **this Pi** — watercooler has no
remote environment, so "deploy" is `install.sh` (copy `bin/` + the patch suite into
`~/.ccgram`, apply the patches to the installed ccgram) followed by a **deferred** ccgram
restart via `bin/ccgram-safe-restart`. `ccgram.service` uses `KillMode=process`, so live
tmux/Claude sessions survive a restart — but the bridge's in-flight relay work does not:
on 2026-10-21 a restart issued from inside a session's turn lost that session's final reply.
So: **never run `systemctl restart ccgram` yourself.** `ccgram-safe-restart` waits until every
ccgram session has been idle for 20 s, restarts, and reports into your topic. The delivery
guard (`patch/apply-reply-guard-patch.py`) re-sends any reply Telegram never got, as a second
line of defence; `journalctl -u ccgram | grep 'delivery guard'` shows its activity.

## The gate, and why it checks what it does
- **`bash -n` + shellcheck** on `bin/` and `*.sh`.
- **Python syntax on `patch/*.py` using the ccgram venv python** (3.14), never system
  `python3` (3.13). The patch scripts carry ccgram source as string literals and ccgram
  uses PEP 758 `except A, B:`, which only parses on 3.14+ — system python3 reports bogus
  SyntaxErrors.
- **Idempotency**: snapshot the installed ccgram, re-run `apply-all-patches.sh`, diff.
  The suite re-runs on every ccgram start (systemd `ExecStartPre`), so a patch that isn't
  stable on rerun corrupts the install instead of failing loudly. Applying twice and
  diffing is the only check that actually proves it — treat a failure here as a blocker,
  not a flake.

## Patch ordering
`apply-all-patches.sh` runs `apply-*.py` in filename order and never aborts on one
failure. That order matters in exactly one place: `apply-audio-transcribe-patch.py`
**rewrites** `handlers/audio_transcribe.py` wholesale on every run, so any patch that
*edits* that file must sort after it. `apply-media-autostart-patch.py` does. Keep new
patches that touch it sorting after `apply-audio-*`.

## bin/tg-send (added 2026-10-21)
Python, not bash — the gate's `bash -n`/shellcheck loop skips it by shebang. Sends
files to a Telegram topic via the Bot API; see README. Test with `tg-send --resolve`
from inside a ccgram session; it must print `chat_id=… topic=<id>` for that session.

## Patch layout gotcha
`install.sh` installs `patch/` only. `patches/` (added 2026-09-17 from another machine) holds
copies plus seven patches that exist nowhere else in the repo (always-resume, no-status-bubble,
no-topic-rename, quiet-dormant, quote-context, resume-yolo, topic-session); they are installed
on this Pi by hand. A fresh install from `patch/` alone would miss them — consolidate before
relying on install.sh elsewhere. New patches go in `patch/`.
