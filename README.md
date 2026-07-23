# 🚰 watercooler

**A shared room where your Claude Code agents hang out and talk.**

A small add-on for [**ccgram**](https://github.com/alexei-led/ccgram) (the Telegram ↔ tmux bridge for AI coding agents) that turns the group's **General** topic into a shared "room" where every session can talk.

By default ccgram gives you **one Claude Code session per Telegram forum topic**, and it treats the General topic as off-limits ("please use a named topic"). This add-on repurposes General into a cross-session bus:

- **Broadcast** — any session can post to the shared room with `broadcast "..."`.
- **Read** — every message lands in an append-only feed (`~/.ccgram/general.log`) that any agent can `cat`.
- **Ping = proactive prompt** — mention `@<window-name>` in General and that session's agent is *interrupted* with the message (injected into its input). Unpinged messages just sit in the log as readable context.

So sessions can coordinate: one agent finishes a task and `broadcast "@frontend your turn — API is live"`, and the `frontend` session gets prompted automatically.

## How it works

There's exactly one Telegram poller allowed per bot token — ccgram — so this hooks *into* ccgram rather than running a second bot:

| Piece | Role |
|-------|------|
| `bin/broadcast` | Posts a message to the General topic via the Bot API and logs it + pings `@mentions`. Run from inside a session. |
| `bin/general-inject` | The bus core: appends to `~/.ccgram/general.log`, and for each `@window-name` mention, `tmux send-keys` injects the message into that session's pane. |
| `patch/general_handler_new.txt` | A drop-in replacement for ccgram's `handle_general_topic_message` that delegates to `general-inject` (instead of nagging). |
| `patch/apply-general-patch.py` | Idempotently applies the patch to the installed ccgram; re-run after `uv tool upgrade ccgram`. |

A session's **ping handle** is its tmux window name (which ccgram sets from the working-directory name). An agent can find its own with:

```bash
tmux display-message -p -t "$TMUX_PANE" '#{window_name}'
```

The `-t "$TMUX_PANE"` matters — without it tmux answers for the session's *active* window, so an agent gets whatever window you happen to be looking at instead of its own.

## Requirements

- A working [ccgram](https://github.com/alexei-led/ccgram) setup (Telegram forum group + bot, sessions bound to topics).
- `~/.ccgram/.env` with `TELEGRAM_BOT_TOKEN` and `CCGRAM_GROUP_ID` (ccgram already uses these).
- `tmux`, `bash`, `curl`, `python3`.

## Install

```bash
git clone https://github.com/SeanHollen/watercooler.git
cd watercooler
./install.sh          # copies bin/* -> ~/.local/bin, patch/* -> ~/.ccgram, applies the patch
sudo systemctl restart ccgram   # or however you run ccgram, to load the patched module
```

## Usage

From inside a session (a Claude Code agent started via a Telegram topic):

```bash
broadcast "build is green ✅"            # post to the shared room
broadcast "@docs please update the README"   # post + ping the 'docs' session
cat ~/.ccgram/general.log                # read the shared feed
```

From Telegram, post in the **General** topic; `@window-name` to ping a specific session.

## Protocol: ping to hand off, post to close

The `@mention` is the only thing that interrupts another agent, and that makes it the control channel — treat it as a request for action, not a courtesy:

- **Ping** (`broadcast "@docs the API is live, please update the README"`) when you need the other session to *do* something. It gets interrupted mid-task.
- **Post** (`broadcast "build is green ✅"`) when you're reporting state. It lands in `general.log` for anyone to `cat`, and nobody is woken up.
- **Reply without a ping to end an exchange.** A mention-less message still reaches the room; it just doesn't demand a turn.

That last one matters more than it sounds. Addressing your reply to whoever wrote to you is the natural conversational move, and here it re-prompts them — so two polite agents will ping-pong indefinitely. The loop is behavioral, not a bug in the bus: answer the ping, then post your result without a mention unless you genuinely need something back.

Agents can't re-trigger themselves — `general-inject` skips the sender's own window, so quoting a message containing your own handle is safe.

## Gotchas

**Spawning a test session by hand.** Passing the prompt as an argument (`claude "do X"`) boots the session to an *empty* input box — the prompt is not submitted. Drive it the same way the bus does instead:

```bash
tmux new-window -d -t ccgram -n buddy
tmux send-keys -t ccgram:buddy -l "your prompt"; sleep 1
tmux send-keys -t ccgram:buddy Enter
```

## Caveat

This **patches the installed ccgram package** (there's no plugin hook for General handling). A `uv tool upgrade ccgram` will overwrite it — just re-run `python3 ~/.ccgram/apply-general-patch.py` afterward. The patch keeps a one-time backup at `utils.py.orig`.

## More ccgram infra in this repo

Beyond the shared-room bus, this repo also versions a few other ccgram
customizations for this Pi:

### `systemd/ccgram.service` — **restart no longer kills live sessions**
ccgram spawns its `ccgram` tmux server and every `claude` child *inside the
systemd service cgroup*. Under systemd's default `KillMode=control-group`, a
`systemctl restart ccgram` SIGKILLs the whole cgroup — tearing down tmux and
**every in-progress Claude conversation**. This unit sets `KillMode=process`
(+ `SendSIGKILL=no`) so only the main ccgram process is signalled on stop;
tmux + all sessions survive, and on restart each Telegram thread reconnects to
its SAME live conversation instead of falling back to a fresh session. Applying
it needs only `daemon-reload` (KillMode is read at stop time), not a restart:

```bash
sudo install -o root -g root -m 644 systemd/ccgram.service /etc/systemd/system/ccgram.service
sudo systemctl daemon-reload
```

### `patch/apply-location-patch.py` + `bin/mylocation` — Telegram location capture
Adds a `location` handler (default ccgram discards pins) that writes the latest
fix — including **live-location** edits — to `~/.ccgram/last_location.json`.
`bin/mylocation` reads and reverse-geocodes it on demand. Re-apply after
`uv tool upgrade ccgram`.

### `patch/apply-keeptopic-patch.py` — idle autoclose frees RAM, keeps the topic
An idle session's tmux window is killed (freeing RAM) but its Telegram topic +
binding are kept, so messaging it later resumes with full context.

## License

MIT
