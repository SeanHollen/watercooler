# PI2 — second Telegram bus (Zamua `telegram-topics`)

A quieter alternative to PI1/ccgram. Same "one bot, many forum topics, one Claude
session per topic" shape, but **outbound only happens when Claude calls the
`reply` tool** (via an MCP server), so a long turn with many internal requests
produces just a few deliberate messages instead of a streamed feed.

## Components
- Plugin: `telegram@zamua` (`/plugin marketplace add Zamua/claude-plugins`,
  `/plugin install telegram@zamua`) — installed at
  `~/.claude/plugins/cache/zamua/telegram/<version>/`.
- Runtime: **Bun** (`~/.bun/bin/bun`, symlinked into `~/.local/bin/bun` so the
  plugin's `start-proxy.sh` PATH finds it).
- Proxy: sole `getUpdates` consumer + token owner + session/multiplexer manager,
  `bun run start` on `127.0.0.1:8790`. Run as systemd `telegram-topics.service`
  (`Restart=always`, `KillMode=process` so spawned sessions survive a restart).
- Config: `~/.claude/channels/telegram-topics/.env` (chmod 600; token lives only
  here). Template in `env.example`.
- Spawn dir: `~/pi2` (isolated from PI1's `~/Desktop`). Pre-trusted once so
  spawned sessions don't stall on Claude Code's "trust this folder?" prompt.

## Coexistence with PI1 (ccgram)
- Different bot token + different group (access control is the group id).
- Different tmux session names (`claude-<slug>-<tid>` vs `ccgram`), shared tmux
  server, port 8790 (ccgram uses none).
- Auth: spawned sessions read `~/.claude/.credentials.json` (same Max account).

## Tuning per topic (Telegram `/` commands)
- `/model` — provider/model/effort/ultracode. Default here is Fable + medium,
  subagents locked. Set effort max + ultracode on to enable subagent/Workflow.
- `/usage`, `/relaunch`, `/secret`.

## Not installed (optional, harmless "not found" log lines)
Codex bridge (`claude-code-proxy`), OpenCode (`opencode`), Antigravity (`agy`).
Only needed for `/localcode` / `/antigravity` / Codex routes.
