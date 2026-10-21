# ccgram patches

Source patches applied to the installed [ccgram](https://github.com/alexei-led/ccgram)
package on the Pi to fix/extend its behavior. They live in `~/.ccgram/` on the Pi and
are re-applied automatically on every ccgram start (and after `uv tool upgrade ccgram`)
by `apply-all-patches.sh`, wired as the ccgram service's `ExecStartPre`.

Each `apply-*.py` is **idempotent** (prints "already patched" as a no-op) and keeps a
one-time `*.orig` backup of the file it edits.

| Patch | What it does |
|-------|--------------|
| `apply-reply-guard-patch.py` | **Delivery guard** (2026-10-21): hashes every assistant reply, records what Telegram accepted (`~/.ccgram/delivery_guard.json`), audits after every Stop / at startup / every 60 s and re-sends any reply that never arrived. Restart-safe. |
| `apply-general-patch.py` + `general_handler_new.txt` | Turns the Telegram **General** topic into the watercooler shared-room bus (log + `@mention` ping-injection) instead of ccgram's "use a named topic" nag. Since 2026-10-21 a human message with no @mention is **forwarded to the last-used session** with a notice, instead of being logged and dropped. |
| `apply-keeptopic-patch.py` | Autoclose frees a session's RAM (kills the tmux window) but **keeps the Telegram topic**, so messaging it later can resume — instead of deleting the topic. |
| `apply-resume-yolo-patch.py` | Resumed sessions launch in **bypass/`--dangerously-skip-permissions` mode**, not "normal" (ask) mode — so a resumed conversation doesn't stall on the first permission prompt. |
| `apply-always-resume-patch.py` + `always-resume-helper.txt` + `always-resume-insert.txt` | A message to a topic whose window died **always resumes that topic's existing conversation** (exact session id, else `--continue`) in bypass mode — never starts a new session / directory browser. Fails safe: any error falls back to ccgram's original recovery UI. |

## Install / re-apply
```bash
# copy the apply-*.py + *.txt into ~/.ccgram/, then:
bash ~/.ccgram/apply-all-patches.sh
ccgram-safe-restart                # waits until no session is mid-turn; never use systemctl restart directly
```

## systemd drop-ins (also here for reference)
- `10-apply-patches.conf` — runs `apply-all-patches.sh` as ccgram's ExecStartPre.
- `20-restart-hardening.conf` — `Restart=always` + no start-limit, so the bridge always revives.
- (`30-auth-token.conf` lives in `../auth/`.)
