---
description: Pull a past conversation's substance into this session, without a terminal
---

The user wants to carry on work from an earlier conversation, in THIS tab.

`$ARGUMENTS` is a session id (any prefix works, including the 8-character short
form), or the name of a worktree, or a description of the work. If it is not
already an id, run `/wt-list` first and pick the matching conversation from the
report — confirm the choice with the user when more than one could fit, showing
the titles and dates rather than guessing.

Then run:

```
~/.local/bin/uv run --no-project ~/.claude/scripts/wt/wt_recall.py <id>
```

Useful flags: `--prompts 0` quotes every prompt instead of the last 40,
`--chars 0` stops clipping long ones, `--out <path>` writes the report to a
file rather than stdout (use it when the report is large enough to be worth
keeping).

The script reads the transcript and prints: the titles, the working directories
the session passed through, every prompt the user typed, the repository files
that were changed, the commands that were run, and how the conversation ended.
It never writes to the transcript, so recalling a session that another tab has
open is safe.

## What to do with the output

Do NOT dump the report back at the user — they were there. Instead:

1. Say in a few sentences where that work stood: what it was for, what got
   done, what was left open. Quote dates and file names, not impressions.
2. Check the claims against the repository as it is NOW. The transcript
   records what was true when it was written; branches have moved since, and a
   file the session created may have been merged, reverted or renamed. A
   difference is worth reporting.
3. Ask what to pick up, unless the user already said.

## Be honest about what this is

This is a NEW session holding a digest, not the old session reopened. Say so
once, plainly, if the user seems to expect the original:

- the id is different, and this conversation is recorded separately;
- what you have is every prompt the user typed plus the record of what was
  done — not the full turn-by-turn history, and not the reasoning between
  those turns;
- the original transcript is untouched and can still be opened literally with
  `claude --resume <id>` in a terminal, which is the only way to get the exact
  conversation back.

If the work lived in a worktree that still exists, mention that this session is
NOT inside it. Offer `EnterWorktree` before changing any file there, and follow
the usual rule for a worktree another session is attending: if `/wt-list` marks
it `IN USE`, propose nothing.
