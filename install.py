#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Installer for the worktree workflow. Makes moving it to another machine a
copy of one directory plus one command.

    uv run --no-project ~/.claude/scripts/wt/install.py          # install
    uv run --no-project ~/.claude/scripts/wt/install.py --check  # report only

What it does, all idempotently:

  1. seeds this package's own commands/ and policy.md on first run, so a
     machine that already has the workflow installed by hand becomes the
     source of truth for the package rather than losing anything;
  2. copies commands/*.md into ~/.claude/commands/;
  3. splices policy.md into ~/.claude/CLAUDE.md between marker comments, so a
     rerun updates the block instead of appending a second copy;
  4. merges the four hook entries into ~/.claude/settings.json, rewriting the
     absolute paths for THIS machine's home directory and uv location --
     which is the whole reason a hand copy breaks: the paths in settings.json
     embed a username;
  5. checks that uv and git are actually reachable.

Existing settings are preserved: only the four hook entries this workflow
owns are touched, matched by the script path they invoke.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
CLAUDE_HOME = Path.home() / ".claude"
COMMAND_NAMES = ("wt.md", "done.md", "wt-list.md")

POLICY_START = "<!-- wt-policy:start -->"
POLICY_END = "<!-- wt-policy:end -->"
POLICY_HEADING = "## Worktree sessions (policy)"

# event name -> (hook script argument, timeout seconds, async)
#
# SessionStart is the only one that runs git against the working tree, and it
# must outlast wt_lib.GIT_REPORT_TIMEOUT with room for several calls -- a hook
# killed halfway produces no inventory at all. It normally finishes in well
# under a second; this budget only comes into play on a cold cache over a
# multi-GB tree. The heartbeats just rewrite a small JSON file and are async,
# so they stay tight.
HOOK_EVENTS = {
    "SessionStart": ("session-start", 60, False),
    "UserPromptSubmit": ("heartbeat", 15, True),
    "Stop": ("heartbeat", 15, True),
    "SessionEnd": ("session-end", 15, False),
}

DEFAULT_POLICY = """## Worktree sessions (policy)

Several sessions usually run in parallel on the same repo, so isolation matters.
Mechanics live in `/wt`, `/done`, `/wt-list`; this is the policy they obey.

- **Isolation is opt-in, not automatic.** Do not move a session into a worktree
  on your own. The user runs `/wt` when they want isolation.
- **Enter early or not at all.** `EnterWorktree` changes the working directory,
  so paths gathered before the move go stale.
- **Work ends only when the user says so.** Never run `/done --confirm`, push a
  branch, or merge anything on the strength of "that looks finished".
- **Never merge into the main branch.** `/done` commits, gates and pushes; the
  merge belongs to the user.
- **A worktree flagged quiet or unknown is a question, not a verdict.** Never
  remove one holding uncommitted or unpushed work without the user saying so.
- **Do not work around the isolation checks.** Inside a worktree, Claude Code
  blocks writes and git redirects aimed at the main checkout. That is the
  feature working, not an obstacle.
"""


def find_uv() -> str | None:
    """Locate uv, preferring the per-user install this machine actually uses."""
    for candidate in (
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".local" / "bin" / "uv",
    ):
        if candidate.is_file():
            return str(candidate).replace("\\", "/")
    found = shutil.which("uv")
    return found.replace("\\", "/") if found else None


def seed_package() -> list[str]:
    """Populate the package from an existing hand-installed layout.

    Only relevant on the machine where the workflow was first built by hand:
    it lifts the commands and policy into the package so that the package is
    the thing you copy elsewhere. On a fresh machine the files are already in
    the package and nothing happens.
    """
    notes = []
    commands_dir = PKG / "commands"
    if not commands_dir.is_dir():
        installed = [CLAUDE_HOME / "commands" / name for name in COMMAND_NAMES]
        if all(path.is_file() for path in installed):
            commands_dir.mkdir(parents=True, exist_ok=True)
            for path in installed:
                shutil.copy2(path, commands_dir / path.name)
            notes.append(f"seeded package commands/ from {CLAUDE_HOME / 'commands'}")

    policy = PKG / "policy.md"
    if not policy.is_file():
        # Prefer the block already living in CLAUDE.md over the built-in
        # default, so local edits survive being packaged.
        existing = extract_policy(CLAUDE_HOME / "CLAUDE.md")
        policy.write_text(existing or DEFAULT_POLICY, encoding="utf-8", newline="\n")
        notes.append(f"seeded {policy.name}")
    return notes


def extract_policy(claude_md: Path) -> str | None:
    """Pull an already-installed policy block out of CLAUDE.md, if present."""
    if not claude_md.is_file():
        return None
    text = claude_md.read_text(encoding="utf-8")
    if POLICY_START in text and POLICY_END in text:
        return text.split(POLICY_START, 1)[1].split(POLICY_END, 1)[0].strip() + "\n"
    if POLICY_HEADING in text:
        after = text.split(POLICY_HEADING, 1)[1]
        # The block runs until the next top-level section.
        end = re.search(r"^## ", after, flags=re.MULTILINE)
        body = after[: end.start()] if end else after
        return (POLICY_HEADING + body).strip() + "\n"
    return None


def install_commands(dry: bool) -> list[str]:
    notes = []
    source = PKG / "commands"
    target = CLAUDE_HOME / "commands"
    if not source.is_dir():
        return ["ERROR: package has no commands/ directory and none to seed from"]
    for name in COMMAND_NAMES:
        src = source / name
        if not src.is_file():
            notes.append(f"ERROR: missing {src}")
            continue
        dst = target / name
        same = dst.is_file() and dst.read_bytes() == src.read_bytes()
        if same:
            notes.append(f"ok      commands/{name}")
            continue
        if not dry:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        notes.append(f"{'would write' if dry else 'wrote'}  commands/{name}")
    return notes


def install_policy(dry: bool) -> list[str]:
    """Splice policy.md into CLAUDE.md between markers.

    Markers exist so this is repeatable: without them a second install would
    append a duplicate policy, and two copies of a rule that later diverge are
    worse than no rule at all.
    """
    policy_file = PKG / "policy.md"
    if not policy_file.is_file():
        return ["ERROR: policy.md missing from package"]
    body = policy_file.read_text(encoding="utf-8").strip()
    block = f"{POLICY_START}\n{body}\n{POLICY_END}"

    claude_md = CLAUDE_HOME / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8") if claude_md.is_file() else ""

    if POLICY_START in text and POLICY_END in text:
        head, rest = text.split(POLICY_START, 1)
        _, tail = rest.split(POLICY_END, 1)
        new_text = head + block + tail
        action = "updated policy block"
    elif POLICY_HEADING in text:
        # Upgrade an unmarked block installed by hand into a marked one.
        head, rest = text.split(POLICY_HEADING, 1)
        end = re.search(r"^## ", rest, flags=re.MULTILINE)
        tail = rest[end.start() :] if end else ""
        new_text = head + block + "\n\n" + tail
        action = "wrapped existing policy block in markers"
    else:
        separator = "" if text.endswith("\n\n") or not text else "\n\n"
        new_text = text + separator + block + "\n"
        action = "appended policy block"

    if new_text == text:
        return ["ok      CLAUDE.md policy block"]
    if not dry:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(new_text, encoding="utf-8", newline="\n")
    return [f"{'[dry] ' if dry else ''}{action} in CLAUDE.md"]


def install_hooks(uv_path: str, dry: bool) -> list[str]:
    """Merge our four hook entries into settings.json, leaving the rest alone.

    Ours are identified by the wt_hook.py path they invoke, so re-running after
    a home directory or uv location change replaces the stale entries rather
    than accumulating a second set pointing at paths that no longer exist.
    """
    settings_path = CLAUDE_HOME / "settings.json"
    try:
        settings = (
            json.loads(settings_path.read_text(encoding="utf-8"))
            if settings_path.is_file()
            else {}
        )
    except ValueError:
        return [f"ERROR: {settings_path} is not valid JSON -- fix it and rerun"]
    if not isinstance(settings, dict):
        return [f"ERROR: {settings_path} does not contain a JSON object"]

    hook_script = str(PKG / "wt_hook.py").replace("\\", "/")
    hooks = settings.setdefault("hooks", {})
    notes = []

    for event, (argument, timeout, is_async) in HOOK_EVENTS.items():
        command = f"{uv_path} run --no-project {hook_script} {argument}"
        entry: dict = {"type": "command", "command": command, "timeout": timeout}
        if is_async:
            entry["async"] = True
        if event == "SessionStart":
            entry["statusMessage"] = "Checking worktrees..."

        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        # Drop any previous entry of ours (matched by script path), keep the
        # user's own hooks for this event untouched.
        kept = []
        for group in existing:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            inner = [
                h
                for h in group.get("hooks", [])
                if not (
                    isinstance(h, dict) and "wt_hook.py" in str(h.get("command", ""))
                )
            ]
            if inner:
                kept.append({**group, "hooks": inner})
            elif not group.get("hooks"):
                kept.append(group)
        kept.append({"hooks": [entry]})
        hooks[event] = kept
        notes.append(f"{'would wire' if dry else 'wired'}   {event} -> {argument}")

    if not dry:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return notes


def check_environment(uv_path: str | None) -> list[str]:
    notes = []
    notes.append(f"uv:  {uv_path or 'NOT FOUND -- install uv first'}")
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10
        )
        notes.append(f"git: {proc.stdout.strip() or 'NOT FOUND'}")
    except (OSError, subprocess.SubprocessError):
        notes.append("git: NOT FOUND -- the workflow needs git on PATH")
    for name in (
        "wt_lib.py", "wt_hook.py", "wt_status.py", "wt_finish.py",
        "wt_create.py", "test_workflow.py",
    ):
        state = "ok" if (PKG / name).is_file() else "MISSING"
        notes.append(f"{state:<4} {name}")
    return notes


WT_JSON_TEMPLATE = """{
  "_note": "Project-specific half of the worktree workflow. The universal half",

  "remote": "origin",

  "_finish_note": [
    "What /done does at the end:",
    "  merge -- merge into the base branch, remove the worktree, delete the",
    "           branch (the default; nothing is pushed unless pushAfterMerge)",
    "  push  -- push the branch and leave the merge to a human"
  ],
  "finish": "merge",
  "pushAfterMerge": false,
  "deleteWorktree": true,

  "_gate_note": [
    "Commands /done runs after committing, before merging; a non-zero exit",
    "stops the merge (the commit is already made, so no work is lost).",
    "${UV} is replaced with this machine's uv path -- do not hard-code it.",
    "Keep gate commands fast; they run on every finish.",
    "Example: \\"${UV} run tests/run_tests.py -q\\""
  ],
  "gate": [],

  "_protected_note": [
    "Paths surfaced for human review in the /done dry run. Never blocked,",
    "only highlighted. Good candidates: lockfiles, generated ledgers."
  ],
  "protectedPaths": [],

  "staleMinutes": 45
}
"""

WORKTREEINCLUDE_TEMPLATE = """# Gitignored files copied into every worktree Claude Code creates.
# Syntax is .gitignore syntax; only files that match AND are gitignored are
# copied, so tracked files are never duplicated.
#
# Without this, a worktree starts without them and says nothing about it --
# which is how a session silently loses its MCP servers or its API keys.
# Uncomment what this project actually needs:

# .env
# .env.local
# .mcp.json
"""

GITIGNORE_BLOCK = """
# Isolated per-session checkouts created by `claude --worktree` / EnterWorktree.
# Session heartbeat locks live OUTSIDE the repo (~/.claude/wt-locks/), so they
# need no entry here.
/.claude/worktrees/
"""


def init_repo(dry: bool) -> list[str]:
    """Set up the current repository for the workflow.

    Nothing here is required for the commands to work -- a repo with none of
    these files still gets /wt, /done and /wt-list, just with no gate and no
    protected paths. It exists so the three optional files are one command
    away instead of three copy-paste jobs.
    """
    sys.path.insert(0, str(PKG))
    import wt_lib  # noqa: PLC0415 -- only needed for this subcommand

    root = wt_lib.find_main_checkout(Path.cwd())
    if root is None:
        return ["ERROR: not inside a git repository -- run this from a repo root"]

    notes = [f"Repository: {root}"]

    config = root / ".claude" / "wt.json"
    if config.is_file():
        notes.append("ok      .claude/wt.json (exists, left alone)")
    else:
        if not dry:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(WT_JSON_TEMPLATE, encoding="utf-8", newline="\n")
        notes.append(f"{'[dry] ' if dry else ''}created .claude/wt.json")

    include = root / ".worktreeinclude"
    if include.is_file():
        notes.append("ok      .worktreeinclude (exists, left alone)")
    else:
        if not dry:
            include.write_text(
                WORKTREEINCLUDE_TEMPLATE, encoding="utf-8", newline="\n"
            )
        notes.append(f"{'[dry] ' if dry else ''}created .worktreeinclude (all commented out)")

    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if "/.claude/worktrees/" in text:
        notes.append("ok      .gitignore already ignores /.claude/worktrees/")
    else:
        if not dry:
            separator = "" if text.endswith("\n") or not text else "\n"
            gitignore.write_text(
                text + separator + GITIGNORE_BLOCK, encoding="utf-8", newline="\n"
            )
        notes.append(f"{'[dry] ' if dry else ''}appended /.claude/worktrees/ to .gitignore")

    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the worktree workflow.")
    parser.add_argument(
        "--check", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--init-repo",
        action="store_true",
        help="set up the CURRENT repository (wt.json, .worktreeinclude, .gitignore)",
    )
    args = parser.parse_args()
    dry = args.check

    # Repo setup is a separate job from installing the workflow itself: the
    # workflow is installed once per machine, this runs once per repository.
    if args.init_repo:
        for line in init_repo(dry):
            print(f"  {line}")
        print()
        print("--check: nothing was written." if dry else "Repository ready.")
        return 0

    print(f"Package:     {PKG}")
    print(f"Claude home: {CLAUDE_HOME}")
    print()

    uv_path = find_uv()
    for line in check_environment(uv_path):
        print(f"  {line}")
    if uv_path is None:
        print("\nAborting: uv is required to run the hooks.")
        return 1

    print()
    for line in seed_package():
        print(f"  {line}")
    for line in install_commands(dry):
        print(f"  {line}")
    for line in install_policy(dry):
        print(f"  {line}")
    for line in install_hooks(uv_path, dry):
        print(f"  {line}")

    print()
    if dry:
        print("--check: nothing was written.")
        return 0
    print("Installed. Hooks take effect in NEW sessions.")
    print()
    print("Per repository, still needed:")
    print("  .claude/wt.json          project config (may be just {})")
    print("  .worktreeinclude         gitignored files to copy into worktrees")
    print("  .gitignore               += /.claude/worktrees/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
