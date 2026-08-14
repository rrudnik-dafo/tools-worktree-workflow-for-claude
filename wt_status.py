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
    report = wt_lib.format_inventory(inventory, include_current=True)
    if report:
        print("Worktrees:")
        print(report)
    else:
        print("No worktrees. Every session is sharing the main checkout.")

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
