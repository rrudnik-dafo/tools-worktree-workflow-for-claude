---
description: Show every worktree in this repo, what it holds, and whether a session is still attending it
---

Run:

```
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/wt_status.py
```

Present the result as a short table. The status markers mean:

| Marker | Meaning |
|---|---|
| `[THIS SESSION]` | you are inside it |
| `[IN USE - do not touch]` | another session is working in it **right now** |
| `[closed - session ended]` | its session ended cleanly |
| `[quiet - ask before touching]` | no recent activity |
| `[unknown - no data, ask]` | predates this workflow; nothing is known |

For anything marked `IN USE`, propose nothing. Do not offer to finish, remove
or enter it, and do not edit files inside it. Report it as context only.

For `closed`, `quiet` and `unknown`, offer the user a choice:

- keep it as is;
- resume in it now (`EnterWorktree` with that path);
- finish it (`/done` must be run from inside it, so offer to enter it first);
- remove it.

Rules when removing:

- Never remove a worktree holding uncommitted or unpushed work without the
  user saying so in as many words, and show them exactly what would be lost
  first.
- A worktree reported as `empty - nothing to lose` has no changes and no
  commits; removing it is safe, but still confirm.
- Remove with `git worktree remove <path>`, then delete the branch separately
  if the user wants it gone. Run these from the MAIN checkout — from inside
  another worktree they will be blocked.

Be explicit about where the status comes from: this workflow's own heartbeat
plus the mtime of Claude Code's transcripts for that directory. There is no
list of open editor tabs to consult. A tab left open but untouched reads as
`quiet`, and a worktree created before this workflow existed reads as
`unknown`. Both are prompts to ask, never grounds to delete.
