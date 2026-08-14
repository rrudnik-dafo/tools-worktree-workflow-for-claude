---
description: Finish a worktree session — commit, gate, merge into the base branch, clean up
---

Wrap up the work in this worktree: commit it, run the project's gate, merge it
into the base branch, and remove the worktree and its branch.

This runs in two phases with a required step in between, because Claude Code
blocks writes and git redirects aimed at the main checkout while a session is
isolated. The merge therefore cannot happen from inside the worktree.

**Phase 1 — always run the dry run first:**

```
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/wt_finish.py
```

Show the user its output, and specifically call out:

- files that would be committed — flag anything that looks unintended, since
  the commit is `git add -A` and takes everything;
- any PROTECTED PATHS warning — name these explicitly rather than burying them;
- which gate commands will run;
- whether the base branch will be pushed afterwards.

**Phase 2 — ask for the go-ahead.**

Ask plainly whether the work is finished. Wait for an explicit yes. Do not
treat "looks good", a thumbs-up on something else, or silence as approval. If
the answer is no, stop and leave everything as it is.

**Phase 3 — commit and gate:**

```
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/wt_finish.py --confirm -m "<message>"
```

Write a real commit message describing the work, not a placeholder. Use
`$ARGUMENTS` as the message if the user supplied one.

- **exit 2 (gate failed)** — the work is committed and the worktree is intact,
  but nothing was merged. Report what failed, offer to fix it, and rerun. Never
  pass `--skip-gate` on your own initiative.
- **success** — the script prints that the next step is required. Continue.

**Phase 4 — leave the worktree.**

Call the `ExitWorktree` tool. Without this the merge will be blocked, and the
script will refuse to run. Confirm you are back in the main checkout.

**Phase 5 — merge and clean up:**

```
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/wt_finish.py --merge
```

It reads the handover left by phase 3, so no arguments are needed. It merges
with `--no-ff`, removes the worktree, deletes the branch, and pushes the base
branch only if `pushAfterMerge` is set.

Handling the outcomes:

- **STOP (exit 1)** — a safety check refused: the main checkout is on another
  branch with uncommitted changes, or files arriving with the merge are also
  edited locally. Report exactly which, and let the user decide. Do not stash
  or commit their files to get past it.
- **exit 2 (merge conflict)** — the merge was aborted and nothing was lost; the
  branch and worktree still hold the work. Report the conflicting files and
  offer to resolve them.
- **warnings about the worktree or branch not being removed** — the merge
  succeeded regardless. Report the manual command the script printed.

Report at the end: what was merged, whether the base branch was pushed, and
anything left for the user to do.

If the user wants the branch pushed for review instead of merged, that is
`"finish": "push"` in the repository's `.claude/wt.json`.
