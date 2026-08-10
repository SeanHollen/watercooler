# watercooler — project notes

## Shipping
`./scripts/ship.sh "<message>"` is the single entry point: gate → commit + push →
deploy. There is one target, `prod`, and it means **this Pi** — watercooler has no
remote environment, so "deploy" is `install.sh` (copy `bin/` + the patch suite into
`~/.ccgram`, apply the patches to the installed ccgram) followed by a ccgram restart.
Restarting is safe: `ccgram.service` uses `KillMode=process`, so live tmux/Claude
sessions survive it.

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
