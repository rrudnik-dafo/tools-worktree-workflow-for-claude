# Worktree Workflow for Claude Code

Keep several Claude Code tabs open on one repository without them overwriting
each other's work — and finish each piece of work with one command.

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

**Large repositories need patience, and the timeouts are sized for them.**
Every git call this package makes has an explicit limit, chosen by what the
command touches rather than by one global number. They live in one block at the
top of `wt_lib.py`:

| Constant | Limit | Covers | Measured cost |
|---|---|---|---|
| `GIT_QUERY_TIMEOUT` | 60 s | metadata — `rev-parse`, `worktree list`, `config` | 0.04–0.15 s |
| `GIT_SCAN_TIMEOUT` | 2 min | walks the tree — `status`, `diff`, `add` | 0.24 s |
| `GIT_WRITE_TIMEOUT` | 10 min | rewrites the tree — `checkout`, `merge`, `worktree add/remove` | 6 s / **194 s** |
| `GIT_NETWORK_TIMEOUT` | 5 min | talks to the remote — `push`, `ls-remote` | link-dependent |
| `GIT_REPORT_TIMEOUT` | 30 s | the inventory, which must fit inside a hook's budget | 0.24 s |

The measurements come from a 53,036-file / 3.41 GB repository on Windows — the
largest this workflow is known to meet. Only the write class is anywhere near
its limit: materialising a big merge (1,650 files) takes ~6 s, but the full tree
`git worktree add` writes takes ~194 s. Throughput is bounded by **file count**
(~273 files/s), not byte volume — the same 3.41 GB in a few large files would
take about 26 s — so a repository of many small files is the case to size for.

A timeout exists to stop a hang, not to express impatience: any limit a healthy
command can plausibly reach is too low. `GIT_WRITE_TIMEOUT` is the one to raise
first if a larger repository appears; the others carry 100× margin or more.

**A timeout is never treated as a verdict.** When a merge does not return in
time, git was killed at an unknown point and its exit code says nothing about
what was done. `/done` therefore asks the repository instead — is `MERGE_HEAD`
present, and is the branch already contained in the base? — and reports one of
three outcomes: the merge completed anyway (cleanup continues), a merge is
half-applied (it stops and hands you the three recovery commands, rather than
aborting on your behalf), or nothing was merged.

If `/wt` is nonetheless killed mid-initialisation, the worktree is left holding
git's own lock:

```bash
git worktree unlock <path>
git worktree remove --force <path>
```

**`/done` unlocks before removing, and has to.** Claude Code's `EnterWorktree`
locks the worktree for the whole session, and `ExitWorktree` keeps that lock —
correctly, since `/done` must leave with `keep`: a `remove` at that point would
delete the branch while the work is still unmerged. So the cleanup step always
meets a locked worktree, and a single `--force` is not enough for one (it
overrides a *dirty* worktree; git wants `-f -f` or an unlock for a *locked*
one). `/done` therefore unlocks first. Until 2026-08-15 it did not, and every
run ended by printing a manual `git worktree remove` for the user to paste. The
rehearsal missed it because `wt_create.py` does not lock, so its worktrees were
never in the state a real session produces; `test_workflow.py` now locks the
worktree before the `--merge` step.

**The last empty directory outlives the removal, and cannot be helped on the
spot.** Measured 2026-08-17: a `/done` merged, deleted the branch locally and on
the remote, dropped git's admin entry and removed every file — and still left the
worktree *directory* on disk. All three ways out were refused:

```
rmdir (Git Bash)    -> Device or resource busy
Remove-Item (Win32) -> used by another process
Rename-Item (Win32) -> used by another process
```

The holder is a live process whose **current working directory** is that
directory — normally a Claude Code tab that once entered the worktree, because
`EnterWorktree`/`ExitWorktree` move the session's directory while the OS-level
cwd of the process never moves back. Windows will not delete *or rename* a
directory that is any process's cwd.

That last line is why the obvious design does not port. [Worktrunk](https://worktrunk.dev/remove/)
renames the worktree into a trash directory (instant on the same filesystem) and
lets a detached `rm -rf` finish afterwards; on Windows the rename is refused for
exactly the same reason the delete is.

So the leftover is **recorded and retried later**, from a session that is not the
holder — every `SessionStart`, `/wt`, `/wt-list` and `/done` sweeps the list in
`~/.claude/wt-locks/<repo>/sweep.json`. The rules:

- only an **empty** directory is ever removed — a leftover with files in it means
  removal failed long before the final `rmdir`, and is reported for a human
  instead of being deleted unattended;
- only paths under `<repo>/.claude/worktrees/` are touched at all;
- `/wt` refuses to *reuse* a directory git does not know as a worktree, instead of
  dropping the session into an ordinary folder with no branch and no isolation.

Nothing needs doing about a `held` entry: closing the tab that once worked in
that worktree releases it, and the next session removes it.

## Troubleshooting

**MCP servers disappeared inside the worktree.** Their configuration file is
gitignored, so a fresh worktree never received it. Add it to
`.worktreeinclude` — the file is copied at creation time, so the worktree has to
be recreated for it to arrive.

**"Blocked: command targets the main checkout".** Not a fault: while a session
is isolated, Claude Code refuses writes, working directories and git redirects
aimed at the main checkout. Merging is done from the main checkout, which is why
`/done` leaves the worktree before it merges. Do not try to work around it.

**`/done` says the session is in the MAIN checkout.** No worktree was entered,
or `EnterWorktree` did not take effect. Check with `/wt-list`, which reports
where the session actually is.

**The gate failed.** The work is committed, the worktree is intact, nothing was
merged. Fix the cause and run `/done` again. `--skip-gate` exists but is a
deliberate human decision, not a way past a red check.

**`/done` refuses because files collide.** A file arriving with the merge is
also uncommitted in the main checkout. That is a guaranteed loss, not a risk —
commit or stash there, then rerun.

**Several sessions are waiting to merge.** The handover is per-branch, so
`/done --merge` asks which one you mean; pass `--branch <name>`.

**`/done` reported "MERGE FAILED" but the merge is in `git log`.** Fixed — this
was the 15-second default killing git after it had already committed. Update the
package (`git pull` in `~/.claude/scripts/wt/`, then rerun `install.py`) and the
timeout path now checks the repository before reporting anything.

**A worktree shows `COULD NOT MEASURE`.** `git status` or `rev-list` did not
answer within the report timeout, so its state is genuinely unknown. It is not a
claim that anything is wrong — but do not remove it on that reading; run
`/wt-list` again, or check the worktree by hand.

**An empty worktree directory is still there after `/done`.** Expected, not a
fault — a running process holds it as its working directory. It is on the sweep
list and disappears by itself once that tab is closed; `/wt-list` shows it as
`held`. Do **not** run `git worktree remove --force` on it: git has already
dropped the admin entry, so that command answers *"is not a working tree"*.

**`git worktree list` shows a worktree marked `prunable`.** Its directory is
gone but git's metadata remains. `git worktree prune` clears the metadata and
touches nothing else; the branch survives.

## Development

Package layout:

```
install.py         install for this machine, and --init-repo for a repository
policy.md          behaviour rules, injected into ~/.claude/CLAUDE.md
commands/          source of the slash commands (wt, done, wt-list)
wt_lib.py          core: git plumbing, session locks, worktree inventory
wt_hook.py         all lifecycle hooks, one entry point
wt_create.py       worktree creation (/wt)
wt_status.py       inventory report (/wt-list)
wt_finish.py       the two-phase finish (/done)
test_workflow.py   end-to-end rehearsal in a throwaway repository
```

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
