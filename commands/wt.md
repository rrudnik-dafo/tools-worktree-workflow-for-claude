---
description: Move this session into an isolated git worktree
---

Move this session into its own git worktree so it cannot collide with other
open tabs working in the same repository.

Steps:

1. If the session is already inside a worktree (check `pwd` against
   `.claude/worktrees/`), say so and stop — do not nest worktrees.
2. Pick the worktree name:
   - use `$ARGUMENTS` if the user supplied one;
   - otherwise derive a short kebab-case name from what we are about to work
     on, and state the name you chose. Passing no name is also fine — the
     script generates a readable one.
3. Create the worktree with the package script, from the main checkout:

   ```
   ~/.local/bin/uv run --no-project ~/.claude/scripts/wt/wt_create.py <name>
   ```

   It creates the worktree, copies the gitignored files listed in
   `.worktreeinclude`, and prints the path on its LAST line.

4. Call the `EnterWorktree` tool with **exactly** the path that script printed
   — copy it verbatim, do not re-spell it, do not swap separators, do not
   change the drive letter's case.

   Why this matters: `EnterWorktree` compares that path against the session's
   working directory without normalising drive-letter case on Windows, so
   `c:\Dev\x` and `C:/Dev/x` read as different directories. When it creates the
   worktree itself, it can therefore build a perfectly good worktree and then
   refuse to enter it. The script prints git's own spelling, which is the one
   that survives the comparison.

   If `EnterWorktree` still refuses, do NOT create anything again — the
   worktree already exists and is fine. Report the exact refusal message.

5. Verify the environment came across: confirm the gitignored files the project
   needs are present. If `.mcp.json` or `.env` was expected and the script did
   not report copying it, say so — MCP servers will silently be unavailable
   otherwise.
6. Report back: worktree path, branch name, base ref.

Then continue with the actual task.

Remember for the rest of this session:

- While inside a worktree, Claude Code blocks every write and every git
  redirect aimed at the main checkout. That is intentional. Do not attempt to
  work around it.
- Because the session entered by path, `ExitWorktree` will not remove this
  worktree. That is fine and intended: `/done` owns removal, so the worktree
  disappears in exactly one place and only after a successful merge.
- The work ends with `/done`, and only after the user has explicitly said it
  is finished.
