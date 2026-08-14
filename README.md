# Worktree Workflow for Claude Code

Keep several Claude Code tabs open on one repository without them overwriting
each other's work — and finish each piece of work with one command.

> Ukrainian version with additional implementation notes: [README-uk.md](README-uk.md)

## What it is

Claude Code lets you open many conversations at once. They all edit the **same
files in the same directory**, so two tabs working in parallel can silently
overwrite each other. A mass operation in one tab — a code generator, a
formatter, a bulk import — rewrites files under the other tab while it is
reasoning about them.

This package puts each session in its own [git worktree](https://git-scm.com/docs/git-worktree):
a separate directory with its own branch, sharing the repository's history.
Edits in one session become invisible to the others until the work is finished
and merged deliberately.

It is not a wrapper around Claude Code and does not replace anything. It adds
four lifecycle hooks and three slash commands.

### What it gives you

- **Isolation that is actually switched on.** The workflow asks at the start of
  every session, before the first file is touched, instead of relying on you to
  remember a command at the one moment it matters.
- **A one-command finish.** `/done` commits, runs the project's checks, merges
  into the base branch, removes the worktree and deletes the branch — local and
  remote — after you confirm.
- **An honest inventory.** Every session start lists the worktrees that exist,
  what work each holds, and whether a session still appears to be attending it.
- **Guardrails against the ways this goes wrong.** New files stop the finish
  until you say they belong. A merge that would collide with uncommitted work in
  the main checkout stops. Nothing is ever removed without your word.

### What it does NOT do

- It does not push to your base branch, ever. Merging is local; pushing `main`
  stays your decision (or opt in with `pushAfterMerge`).
- It does not remove a worktree that holds unmerged work.
- It does not decide that work is finished. `/done` runs when you say so.

## Requirements

| | |
|---|---|
| **Claude Code** | with hooks and the `EnterWorktree` tool (v2.1.49+) |
| **git** | 2.37 or newer — needs `push.autoSetupRemote` |
| **uv** | in `~/.local/bin/` or on `PATH` — every script runs through it |
| **A git repository** | with at least one commit; a remote is optional |
| **Disk space** | each worktree is a full checkout of your tracked tree |

No Python packages to install: the scripts declare their own (empty)
dependencies inline and `uv` handles the rest. No Node, no build step.

**Platforms.** Developed and tested on Windows 11. Everything is Python and
plain git — no shell scripts — so macOS and Linux should work, but they have
not been exercised. Windows-specific handling (drive-letter case, path
separators) is present and harmless elsewhere.

**Disk space is the one real cost.** A worktree materialises every tracked file.
Check yours before opening several tabs:

```bash
git ls-files | wc -l          # how many files
du -sh .                      # rough upper bound
```

On a 3.4 GB repository, three parallel sessions cost about 10 GB of working
files. The `.git` directory is shared and not duplicated.

## Install

Once per machine:

```bash
git clone https://github.com/rrudnik-dafo/tools-worktree-workflow-for-claude.git ~/.claude/scripts/wt
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/install.py
```

To see what it would change without writing anything, add `--check`.

The installer:

- copies the three slash commands into `~/.claude/commands/`;
- inserts the behaviour rules into `~/.claude/CLAUDE.md`, between
  `<!-- wt-policy:start -->` / `<!-- wt-policy:end -->` markers;
- registers four hooks in `~/.claude/settings.json`, **with this machine's
  absolute paths**;
- verifies `git` and `uv` are present.

It merges into existing settings rather than replacing them, recognising its own
entries by the path to `wt_hook.py`, and re-running is idempotent.

> Do not copy the folder between machines by hand. The hook paths in
> `settings.json` contain your username, and a hook that fails to start reports
> nothing at all — the workflow would simply be silently absent. Clone and run
> the installer instead.

Then, once per repository you want to use it in:

```bash
cd /path/to/your/repo
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/install.py --init-repo
```

This creates `.claude/wt.json` (the only project-specific file), a commented
`.worktreeinclude`, and adds `/.claude/worktrees/` to `.gitignore`. Existing
files are left alone.

**Hooks take effect in new sessions.** Tabs already open will not pick them up.

## How your work changes

Only in four places. Everything else about Claude Code stays the same.

### 1. Every session starts with one question

In a repository that has `.claude/wt.json`, Claude asks before touching any
file:

> Working in an isolated worktree, or directly in main?

Reading, searching, and answering questions need no worktree — only file changes
do. Answer "main" freely when you are just looking around.

It asks **every time**, not only when it looks risky, because a collision gives
no warning: by the time two tabs are editing the same file, the moment to choose
isolation has passed. It does not re-ask after a context compaction or when you
resume a session, and it does ask again after `/clear`, which usually means a new
task.

### 2. You see what is left over

Alongside the question, existing worktrees are listed with what they hold:

```
worktree-anchors-fix   [IN USE - do not touch]
  .claude/worktrees/anchors-fix
  3 commits ahead of origin/main, 2 uncommitted

worktree-old-attempt   [quiet - ask before touching]
  .claude/worktrees/old-attempt
  empty - nothing to lose
```

The markers mean:

| Marker | Meaning |
|---|---|
| `[THIS SESSION]` | you are inside it |
| `[IN USE - do not touch]` | another session is working there **now** |
| `[closed - session ended]` | its tab was closed cleanly |
| `[quiet - ask before touching]` | no recent activity |
| `[unknown - no data, ask]` | predates this workflow, or no signal |
| `DIRECTORY MISSING` | git metadata only — the branch may still hold commits |

`quiet` and `unknown` are questions, not verdicts: a tab left open but untouched
looks identical to an abandoned one. Nothing is removed on their strength.

### 3. Three commands

```
/wt [name]     move this session into an isolated worktree
/wt-list       show every worktree, what it holds, who is attending it
/done          finish: commit, gate, merge, clean up
```

`/wt` creates `.claude/worktrees/<name>` on branch `worktree-<name>`, branched
from your repository's default branch, and copies in the gitignored files listed
in `.worktreeinclude`. Without a name it generates a readable one.

### 4. `/done` replaces the manual wrap-up

It runs in two halves with your decision in between.

**First, a dry run.** Nothing is written. You see what would be committed, which
protected paths were touched, and which gate commands would run:

```
Worktree:  .claude/worktrees/anchors-fix
Branch:    worktree-anchors-fix
Uncommitted changes: 12
  M   src/parser.py
  ??  scratch-notes.txt
Commits ahead of origin/main: 3

PROTECTED PATHS TOUCHED -- review these before merging:
  config/registry.lock
```

**Then it waits for you** to say the work is finished. Approval of something
else does not count; silence does not count.

**Then it executes:**

```
1. commit          (stops first if there are new untracked files)
2. gate            (your project's checks; a failure stops here)
3. ExitWorktree    (required — a worktree cannot merge into the main checkout)
4. merge --no-ff   into the base branch
5. cleanup         remove the worktree, delete the branch locally and on the remote
```

The order is deliberate: the commit happens **before** the gate, so a failing
check never costs you work. If the gate fails, everything is committed, the
worktree is intact, and nothing has been merged.

## Project configuration

`.claude/wt.json` is the only file that differs between repositories.

```json
{
  "remote": "origin",
  "baseBranch": "main",
  "gate": ["${UV} run tests/run_tests.py -q"],
  "protectedPaths": ["**/*.lock"],
  "finish": "merge",
  "pushAfterMerge": false,
  "deleteWorktree": true,
  "staleMinutes": 45
}
```

| Key | Effect |
|---|---|
| `gate` | commands run after the commit, before the merge; non-zero stops the merge |
| `protectedPaths` | `.gitignore`-syntax patterns surfaced for review — **never blocking** |
| `finish` | `merge` (default) or `push` to leave merging to you |
| `pushAfterMerge` | push the base branch after a successful merge |
| `deleteWorktree` | remove the worktree and branch after merging |
| `staleMinutes` | silence after which a session is reported `quiet` |
| `baseBranch` | defaults to the remote's default branch |

`${UV}` in a gate command is replaced with this machine's `uv` path. Do not
hard-code it — this file is committed and travels to machines with a different
home directory, and `~` does not expand in `cmd.exe`.

Keep gate commands **fast**; they run on every finish. Seconds are fine, minutes
are not. Heavy checks belong in CI or in a manual run.

`.worktreeinclude` lists gitignored files to copy into each new worktree, in
`.gitignore` syntax. Without it a worktree starts without your `.env` or MCP
configuration — and says nothing about it, which surfaces much later as a
mysteriously missing capability.

## Notes worth knowing

**A worktree isolates files, not git.** All worktrees share one `.git`, so
commits, branches and remotes are common to all of them. Editing in a worktree
is invisible to other sessions — but a `git push` is a push of the same
repository.

**Pushing from a worktree is allowed and goes to that worktree's own branch.**
The branch is created with `--no-track`, and `push.autoSetupRemote` is enabled
for the repository, so the first `git push` creates `origin/worktree-<name>`.
This is a useful backup mid-task. What must never happen is naming another
destination explicitly (`git push origin HEAD:main`).

**Merging while another session works in the main checkout is warned about, not
blocked.** The merge changes files under that session. `/done` does stop when a
file it is about to merge is also uncommitted in the main checkout, because that
is a guaranteed loss rather than a risk.

**Liveness is inferred, not known.** There is no list of open editor tabs to
query. Two signals are combined: this workflow's own heartbeat, refreshed on
every prompt and every turn, and the modification time of Claude Code's session
transcripts. A closed tab is detected precisely, through `SessionEnd`. An open
but idle tab is indistinguishable from an abandoned one — hence `quiet` rather
than a verdict.

**Session locks live outside your repositories**, under `~/.claude/wt-locks/`,
so no project needs a `.gitignore` entry for them.

**Claude Code bugs this works around.** `EnterWorktree` compares paths without
normalising Windows drive-letter case and can refuse to enter a worktree it just
created ([#36194](https://github.com/anthropics/claude-code/issues/36194) covers
the related upstream problem). This package therefore creates worktrees itself
and passes git's own spelling of the path. A side effect: `ExitWorktree` will not
remove such a worktree — which is intended, since `/done` owns removal.

**Large repositories need patience on `/wt`.** Checking out a multi-gigabyte
tree takes minutes; the timeout is 300 seconds (`WORKTREE_ADD_TIMEOUT` in
`wt_create.py`). If it is exceeded, git is killed mid-initialisation and leaves
the worktree holding its own lock:

```bash
git worktree unlock <path>
git worktree remove --force <path>
```

## Development

After changing `wt_finish.py` or `wt_lib.py`, run the rehearsal:

```bash
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/test_workflow.py
```

It builds a throwaway repository with a real remote and exercises the whole
cycle — creation, the untracked stop, commit, gate, handover, merge, cleanup of
both local and remote branches — plus the guards, and a negative control proving
the upstream assertion is not vacuous. `/done` commits, merges, deletes a
worktree and deletes a branch; those are not operations to debug on real work.

The package is the source of truth. Files under `~/.claude/commands/` and the
block in `~/.claude/CLAUDE.md` are installed copies that `install.py` overwrites
— edit the package, then reinstall.

## Uninstall

Remove the four hook entries from `~/.claude/settings.json`, delete the block
between the `wt-policy` markers in `~/.claude/CLAUDE.md`, and delete
`~/.claude/commands/{wt,done,wt-list}.md`. Existing worktrees are ordinary git
worktrees and keep working; remove them with `git worktree remove`.
