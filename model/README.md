# Model + reasoning for ccgram sessions

Every Claude Code session ccgram spawns runs as **Fable 5** at **max** reasoning effort.

- `model.env` — `ANTHROPIC_MODEL=fable`, `CLAUDE_CODE_EFFORT_LEVEL=max`. Lives at
  `~/.ccgram/model.env` on the Pi.
- `40-model.conf` — ccgram systemd drop-in (`EnvironmentFile`) so a fresh boot inherits it.
- On a running server it's applied with:
  `tmux setenv -g ANTHROPIC_MODEL fable && tmux setenv -g CLAUDE_CODE_EFFORT_LEVEL max`

Notes:
- `CLAUDE_CODE_EFFORT_LEVEL=max` is the persistent equivalent of typing "ultrathink"
  each turn (which is per-turn only). Fable's thinking is adaptive and always on.
- Sub-agents (Task tool) work under any model; Fable is not required for them. A
  subagent can override its own model via frontmatter or `CLAUDE_CODE_SUBAGENT_MODEL`.
- Fable draws down usage faster than Opus; on Max, ~half the weekly limit is Fable,
  then it bills usage credits (headless sessions bill without a prompt).
