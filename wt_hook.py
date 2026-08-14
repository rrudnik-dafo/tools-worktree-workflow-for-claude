#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Single entry point for every lifecycle hook of the worktree workflow.

    wt_hook.py session-start   -> SessionStart
    wt_hook.py heartbeat       -> UserPromptSubmit, Stop
    wt_hook.py session-end     -> SessionEnd

One script rather than four keeps the settings.json wiring readable and means
the shared "am I even in a git repo?" guard exists in exactly one place.

Every path exits 0. A hook that fails must never be able to break a session,
so any unexpected error is swallowed and recorded in the event log instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json  # noqa: E402

import wt_lib  # noqa: E402


def emit(payload: dict) -> None:
    """Write a hook response object to stdout."""
    print(json.dumps(payload))


def handle_session_start(data: dict) -> None:
    """Report worktrees that no live session appears to be attending.

    This is the whole point of the hook: on opening a new tab you should
    immediately see what was left behind, and decide there and then. It says
    nothing when the repo has no worktrees, so a single-tab day costs nothing.
    """
    cwd = data.get("cwd") or str(Path.cwd())
    main = wt_lib.find_main_checkout(cwd)
    if main is None:
        return  # Not a git repo -- stay completely silent.

    # .claude/wt.json is a repository's opt-in. These hooks are installed
    # globally, so without this check every unrelated repo on the machine
    # would start asking about worktrees it has no use for.
    if not (main / ".claude" / "wt.json").is_file():
        return

    session_id = data.get("session_id", "")
    wt_lib.prune_locks(main)

    # Whether to ask depends on WHY the session started. A resume or a context
    # compaction continues a session whose isolation decision was already
    # made; asking again would be noise. The field has gone by several names,
    # and when it is absent entirely we fall back to "have we already asked
    # this session?", recorded in the lock.
    reason = (
        data.get("session_start_reason")
        or data.get("source")
        or data.get("reason")
        or ""
    ).lower()
    already_asked = wt_lib.session_was_asked(main, session_id)
    if reason in ("resume", "compact"):
        should_ask = False
    elif reason == "clear":
        should_ask = True  # /clear usually means "new task" -- worth asking.
    else:
        should_ask = not already_asked

    wt_lib.touch_lock(
        main,
        session_id,
        cwd,
        data.get("transcript_path", ""),
        asked=already_asked or should_ask,
    )

    inventory = wt_lib.collect_inventory(main, cwd)
    report = wt_lib.format_inventory(inventory)

    # A session already inside a worktree has nothing to decide.
    if should_ask and inventory["current"] is None:
        others = [
            lock
            for lock in wt_lib.read_locks(main)
            if not lock["ended"]
            and lock.get("session_id") != session_id
            and lock["age_seconds"] <= 45 * 60
        ]
        ask = [
            "BEFORE CHANGING ANY FILE, ask the user this and wait for an answer:",
            "",
            '  "Working in an isolated worktree, or directly in main?"',
            "",
            "Use AskUserQuestion with these options:",
            "  - Isolated worktree (recommended) -> call EnterWorktree",
            "  - Directly in main -> proceed as normal",
            "",
            "Why this is asked every time: two sessions editing the same",
            "checkout have overwritten each other's work in this repo before,",
            "and a mass script has rewritten the tree under a live session.",
            "Isolation has to be chosen BEFORE the first edit, not after the",
            "collision. Reading, searching and answering questions need no",
            "worktree -- only file changes do.",
        ]
        if others:
            ask.insert(1, f"NOTE: {len(others)} other session(s) are live in this repo right now.")
        if report:
            ask += ["", "Existing worktrees:", report]
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n".join(ask),
                }
            }
        )
        return

    if not report:
        return

    in_use = [w for w in inventory["worktrees"] if w["activity"] == "active"]
    dormant = [
        w for w in inventory["worktrees"] if w["activity"] in ("closed", "idle", "unknown")
    ]
    empty_dormant = [w for w in dormant if w.get("empty")]

    # Two audiences, two messages. systemMessage is what the user reads in the
    # terminal; additionalContext is what Claude reads and must act on.
    lines = [
        "Open worktrees in this repository:",
        report,
        "",
        "Status is inferred from two signals -- this workflow's own session",
        "heartbeat, and the mtime of Claude Code's transcripts for that",
        "directory. Neither is a list of open editor tabs, which does not exist.",
    ]
    if in_use:
        lines += [
            "",
            f"{len(in_use)} worktree(s) are IN USE by another session right now.",
            "Do not propose removing, finishing or entering them. Do not edit",
            "files inside them from here. Mention them only as context.",
        ]
    if dormant:
        lines += [
            "",
            f"{len(dormant)} worktree(s) show no current activity. For each, offer:",
            "  - keep it (do nothing)",
            "  - resume in it now (EnterWorktree with that path)",
            "  - run /done in it to commit + push the branch",
            "  - remove it (ONLY with explicit confirmation, and never when it",
            "    holds uncommitted or unpushed work)",
            "",
            "'quiet' and 'unknown' are not evidence of abandonment: a tab open but",
            "untouched reads as quiet, and a worktree older than this workflow has",
            "no data at all. Ask; never conclude.",
        ]
    if empty_dormant:
        lines += [
            "",
            f"Of those, {len(empty_dormant)} is/are empty (no changes, no commits) and",
            "can be removed without losing anything -- still confirm before removing.",
        ]

    emit(
        {
            "systemMessage": "Worktree inventory:\n" + report,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(lines),
            },
        }
    )


def handle_heartbeat(data: dict) -> None:
    """Refresh this session's lock so its worktree reads as attended."""
    cwd = data.get("cwd") or str(Path.cwd())
    main = wt_lib.find_main_checkout(cwd)
    if main is None:
        return
    wt_lib.touch_lock(
        main,
        data.get("session_id", ""),
        cwd,
        data.get("transcript_path", ""),
    )


def handle_session_end(data: dict) -> None:
    """Release the lock, turning the heartbeat guess into a certainty.

    Whether this fires when a VS Code tab is closed (as opposed to /exit) is
    undocumented, which is why every invocation is logged: the event log is the
    evidence that settles it.
    """
    cwd = data.get("cwd") or str(Path.cwd())
    main = wt_lib.find_main_checkout(cwd)
    if main is None:
        return
    wt_lib.mark_ended(main, data.get("session_id", ""))


HANDLERS = {
    "session-start": handle_session_start,
    "heartbeat": handle_heartbeat,
    "session-end": handle_session_end,
}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = HANDLERS.get(event)
    if handler is None:
        return 0

    data = wt_lib.read_hook_input()

    # Heartbeats fire on every prompt and every turn; logging them would bloat
    # the log without adding evidence. The two events we are actually trying to
    # observe are start and end.
    if event != "heartbeat":
        # The reason field has gone by several names across versions
        # (session_start_reason / source / reason), so take whichever is
        # present rather than betting on one.
        reason = (
            data.get("session_start_reason")
            or data.get("source")
            or data.get("reason")
            or "-"
        )
        wt_lib.log_event(
            event,
            f"session={data.get('session_id', '?')[:8]} "
            f"reason={reason} cwd={data.get('cwd', '?')}",
        )

    try:
        handler(data)
    except Exception as exc:  # noqa: BLE001 -- a hook must never break a session
        wt_lib.log_event(f"{event}-error", repr(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
