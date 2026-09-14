#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Render the worktree inventory for /wt-list.

Same data the SessionStart hook reports, but including the current session's
own worktree and with the main checkout's state shown for orientation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wt_lib  # noqa: E402


def main() -> int:
    cwd = str(Path.cwd())
    main_checkout = wt_lib.find_main_checkout(cwd)
    if main_checkout is None:
        print("Not inside a git repository.")
        return 1

    # Retry any leftover directory a previous /done could not remove. /wt-list
    # is the natural place to SHOW the result rather than hide it: this command
    # exists to answer "what is actually lying around".
    swept = wt_lib.sweep_leftovers(main_checkout)

    inventory = wt_lib.collect_inventory(main_checkout, cwd)
    print(f"Repository:   {main_checkout}")
    print(f"Base ref:     {inventory['base']}")
    print(f"Current cwd:  {cwd}")
    if inventory["current"]:
        print(f"This session is inside worktree: {inventory['current']}")
    else:
        print("This session is in the MAIN checkout (not isolated).")

    config = inventory["config"]
    print(f"Project config (.claude/wt.json): {'present' if config else 'absent'}")
    if config.get("gate"):
        print(f"  gate commands: {len(config['gate'])}")
    if config.get("protectedPaths"):
        print(f"  protected paths: {', '.join(config['protectedPaths'])}")

    print()
    report = wt_lib.format_inventory(
        inventory, include_current=True, include_sessions=True
    )
    if report:
        print("Worktrees:")
        print(report)
    else:
        print("No worktrees. Every session is sharing the main checkout.")

    # Conversations whose worktree is already gone. They are listed apart from
    # the inventory above because git no longer knows about them at all -- and
    # they are listed at all because this is the state somebody actually comes
    # back to: /done ran yesterday, the tab is gone, and the discussion that
    # produced the work is the only copy of the reasoning behind it.
    orphans = wt_lib.orphan_session_buckets(main_checkout)
    if orphans:
        launcher = wt_lib.claude_launcher()
        print()
        print("Conversations from worktrees that no longer exist:")
        for orphan in orphans:
            print(f"  {orphan['name']}  (worktree removed)")
            for line in wt_lib.format_sessions(orphan["sessions"], "    ", launcher):
                print(line)
        print("  Reopening one restores its original working directory, which")
        print("  for these no longer exists -- expect file tools to fail there.")
        print("  They are readable history, not a place to resume work.")

    if swept["unlocked"]:
        print()
        print("Released locks left by sessions that are gone:")
        for item in swept["unlocked"]:
            print(f"  {item['path']}")
            print(f"    was: {item['reason']}")
        print("  (a locked worktree refuses removal even with --force, so an")
        print("   unreleased lock makes it uncleanable)")

    if swept["removed"] or swept["held"] or swept["occupied"]:
        print()
        print("Leftover worktree directories:")
        for path in swept["removed"]:
            print(f"  swept    {path}")
        for path in swept["held"]:
            print(f"  held     {path}  (empty; a live process has it as its cwd)")
        for path in swept["occupied"]:
            print(f"  OCCUPIED {path}  (still has files -- not touched)")
        if swept["held"]:
            print("  'held' needs no action: it is retried every session and")
            print("  clears itself once the holding tab is closed.")

    # Locks are shown separately because they answer a different question:
    # not "what work exists" but "who is still holding it".
    locks = wt_lib.read_locks(main_checkout)
    print()
    print(f"Session locks: {len(locks)}")
    for lock in sorted(locks, key=lambda item: item["age_seconds"]):
        minutes = lock["age_seconds"] / 60
        print(
            f"  {lock.get('session_id', '?')[:8]}  "
            f"last seen {minutes:.0f} min ago  {lock.get('cwd', '?')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
