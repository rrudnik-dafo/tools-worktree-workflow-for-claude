## Worktree sessions (policy)

Several sessions usually run in parallel on the same repo, so isolation matters.
Mechanics live in `/wt`, `/done`, `/wt-list`; this is the policy they obey.

- **Ask at the start of every session, before touching any file.** When the
  SessionStart hook asks you to, put the question to the user with
  AskUserQuestion: isolated worktree, or straight into main? Wait for the
  answer. Reading, searching and answering questions need no worktree; only
  file changes do. The reason it is asked every time and not only when it
  looks risky: in this repo two sessions have already overwritten each other's
  work, and a mass script has rewritten the tree under a live session. The
  choice has to be made BEFORE the first edit, because a collision gives no
  warning.
- **Never enter a worktree on your own initiative.** Ask, then act on the
  answer. The question above is the only prompt for it.
- **Enter early or not at all.** `EnterWorktree` changes the working directory,
  so paths gathered before the move go stale. Offer it at the start of a task,
  not halfway through one.
- **New files are a decision.** `/done` stops when the worktree holds untracked
  files and names them. Do not reach for `--include-untracked` to get past it:
  show the list, ask which are real work and which are scratch, and let the
  user say. Untracked files are how a stray dump reaches the base branch.
- **Work ends only when the user says so.** Never run `/done --confirm`, push a
  branch, or merge anything on the strength of "that looks finished". Wait for
  an explicit statement that the work is done.
- **The merge happens inside `/done`, never outside it.** Do not run `git
  merge`, `git worktree remove` or `git branch -d` by hand to "help". The
  script's safety checks exist because a merge into the base branch changes
  files under whoever else is working there.
- **A push from a worktree goes to that worktree's own branch, never to the
  base branch.** Plain `git push` is fine and is a useful backup: the branch
  carries no upstream, and `push.autoSetupRemote` makes the first push create
  `origin/worktree-<name>`. What is forbidden is naming another destination —
  `git push origin HEAD:main` and anything like it sends work into the base
  branch with no gate, no review and no merge check. A worktree branch once
  tracked `main` by accident, and one ordinary `git push` put unreviewed work
  straight into it; that is the failure this rule prevents.
- **Never work around a safety STOP.** If `/done --merge` refuses because the
  main checkout is dirty or files collide, report it and let the user decide.
  Do not stash, commit or discard their changes to get past it.
- **A worktree flagged `IN USE` belongs to another live session.** Propose
  nothing for it: do not enter it, finish it, remove it, or edit inside it.
- **`quiet` and `unknown` are questions, not verdicts.** `quiet` means no
  recent activity, which an open-but-untouched tab also produces; `unknown`
  means there is no data at all. Never remove a worktree holding uncommitted
  or unpushed work without the user saying so in as many words.
- **Do not work around the isolation checks.** Inside a worktree, Claude Code
  blocks writes and git redirects aimed at the main checkout. That is the
  feature working, not an obstacle.
