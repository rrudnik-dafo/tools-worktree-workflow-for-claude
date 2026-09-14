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
or enter it, and do not edit files inside it. Report it as context only. That
includes its conversations: never offer to reopen one, because the tab holding
it is open right now and two live sessions on one transcript overwrite each
other's turns.

## Conversations

Under each worktree the report lists the conversations recorded for it, newest
first, each with a `reopen:` command. Present them as part of that worktree's
entry, not as a separate table.

This is the answer to "where did yesterday's chat go". Claude Code keys its
session history on the working directory, and the picker only ever shows the
current directory's history — so a conversation that entered a worktree is
invisible from the main checkout even though nothing was lost. The listed
`claude --resume <id>` reopens it from wherever you are **and brings its
original working directory back with it**, so the reopened session is inside
its worktree again. Nobody has to change an editor folder.

The `[...]` before each title says where the title came from: `custom-title`
is the name the user gave it, `ai-title` one Claude Code generated, and
`first prompt` means the conversation was never named and the opening request
is being shown instead.

A final section, `Conversations from worktrees that no longer exist`, covers
the worktrees `/done` has already removed. Those reopen too, but their working
directory is gone, so file tools inside them will fail. Offer them as history
to read, never as a place to resume work — if the user wants to continue that
line of work, the answer is a fresh worktree.

For `closed`, `quiet` and `unknown`, offer the user a choice:

- keep it as is;
- reopen its last conversation (the `reopen:` command), which is the option to
  name FIRST when the user is looking for work they left unfinished — it comes
  back with its history and its worktree, where entering the worktree afresh
  gives the worktree and an empty conversation;
- resume in it now with an empty conversation (`EnterWorktree` with that path);
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
