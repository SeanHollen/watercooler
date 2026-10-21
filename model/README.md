# Model + reasoning for ccgram sessions

Every Claude Code session ccgram spawns runs as **Claude Opus 5.5** (`claude-opus-5-5`)
at **max** reasoning effort. (Until 2026-10-21 it was Claude Fable 5.1 via the `fable`
alias; Sean switched it to Opus 5.5.)

- `model.env` — `ANTHROPIC_MODEL=claude-opus-5-5`, `CLAUDE_CODE_EFFORT_LEVEL=max`. Lives at
  `~/.ccgram/model.env` on the Pi.
- `40-model.conf` — ccgram systemd drop-in (`EnvironmentFile`) so a fresh boot inherits it.
- On a running server it's applied with:
  `tmux setenv -g ANTHROPIC_MODEL claude-opus-5-5 && tmux setenv -g CLAUDE_CODE_EFFORT_LEVEL max`
  New windows (= new Telegram topics) pick it up immediately; sessions already running keep
  the model they started with until they are resumed.

Notes:
- `CLAUDE_CODE_EFFORT_LEVEL=max` is the persistent equivalent of typing "ultrathink"
  each turn (which is per-turn only).
- Sub-agents (Task tool) work under any model. A subagent can override its own model via
  frontmatter or `CLAUDE_CODE_SUBAGENT_MODEL`.
- Model ids: Opus 5.5 = `claude-opus-5-5`, Sonnet 5.5 = `claude-sonnet-5-5`,
  Fable 5.1 = `claude-fable-5-1` (alias `fable`). Fable draws down usage faster than Opus.
