#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Create an isolated worktree, ready for EnterWorktree.

    wt_create.py [name]

Why this exists rather than letting Claude Code create the worktree itself:
`EnterWorktree` creates the directory and then compares its path against the
session's, without normalising the drive-letter case on Windows. `c:\\Dev\\x`
and `C:/Dev/x` are the same directory, but the comparison says otherwise, so
the call creates a perfectly good worktree and then refuses to enter it.

Doing the creation here sidesteps that entirely: this script creates the
worktree and prints the path in git's OWN spelling, taken from
`git worktree list`, which is the spelling EnterWorktree accepts. It also puts
creation and deletion in the same hands -- /done already removes worktrees, so
the whole lifecycle now lives in this package instead of half here, half in
the tool.

Prints the worktree path on the last line; everything else goes above it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wt_lib  # noqa: E402


# Populating a working tree is not a query: on this machine's documentation
# repository, writing out all 53k tracked files measures ~194 s. This is the
# most expensive command in the package, and the one GIT_WRITE_TIMEOUT is
# actually sized against. Kept as an alias rather than its own number so that
# every tree-rewriting command -- add, checkout, merge, remove -- moves together
# when the limit is tuned.
WORKTREE_ADD_TIMEOUT = wt_lib.GIT_WRITE_TIMEOUT


def estimate_tree_size(main: Path) -> float:
    """Rough size of the tracked tree in MB, or 0.0 when it cannot be read.

    Used only to set the user's expectations before a long wait, so a fast
    approximation from git's index beats walking the filesystem.
    """
    code, out, _ = wt_lib.run_git(["ls-files", "-s"], main, timeout=wt_lib.GIT_QUERY_TIMEOUT)
    if code != 0 or not out:
        return 0.0
    # Counting entries is enough for an order-of-magnitude hint; stat'ing 50k
    # files here would itself take longer than the message is worth.
    entries = out.count("\n") + 1
    code, cat_out, _ = wt_lib.run_git(["count-objects", "-v"], main, timeout=wt_lib.GIT_QUERY_TIMEOUT)
    for line in cat_out.splitlines():
        if line.startswith("size-pack:"):
            try:
                # size-pack is in KiB and covers compressed history; the
                # checkout is larger, so scale it as a lower bound.
                return float(line.split()[1]) / 1024
            except (IndexError, ValueError):
                break
    return entries * 0.05  # ~50 KB per file as a crude fallback


def generate_name() -> str:
    """A short, readable, collision-resistant name.

    Word pairs beat timestamps for something the user will read in a branch
    name and a directory listing all day.
    """
    adjectives = [
        "amber", "brisk", "calm", "dusky", "eager", "fleet", "gentle", "hardy",
        "ivory", "jolly", "keen", "lively", "mellow", "nimble", "olive", "prime",
    ]
    nouns = [
        "otter", "falcon", "cedar", "harbor", "lantern", "meadow", "quartz",
        "ridge", "summit", "thicket", "vale", "willow", "anchor", "beacon",
    ]
    stamp = int(time.time())
    return f"{adjectives[stamp % len(adjectives)]}-{nouns[(stamp // 16) % len(nouns)]}"


def read_worktreeinclude(main: Path) -> list[str]:
    """Patterns from .worktreeinclude, comments and blanks dropped."""
    path = main / ".worktreeinclude"
    if not path.is_file():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    except OSError:
        return []
    return out


def copy_included(main: Path, worktree: Path, patterns: list[str]) -> list[str]:
    """Copy gitignored files matching the patterns into the new worktree.

    Mirrors Claude Code's own `.worktreeinclude` rule: a file is copied only if
    it matches a pattern AND is gitignored, so tracked files are never
    duplicated. Without this a worktree silently starts without .env or
    .mcp.json -- and 'silently' is the problem, since the loss shows up much
    later as a missing MCP server.
    """
    copied: list[str] = []
    for pattern in patterns:
        # Literal paths are the common case (.env, .mcp.json); fall back to a
        # recursive glob only when the pattern actually contains a wildcard.
        if any(ch in pattern for ch in "*?["):
            candidates = [p for p in main.glob(pattern) if p.is_file()]
            candidates += [p for p in main.rglob(pattern) if p.is_file()]
        else:
            candidate = main / pattern
            candidates = [candidate] if candidate.is_file() else []

        for source in dict.fromkeys(candidates):  # de-duplicate, keep order
            try:
                relative = source.relative_to(main)
            except ValueError:
                continue
            # git check-ignore exits 0 when the path IS ignored.
            code, _, _ = wt_lib.run_git(
                ["check-ignore", "-q", str(relative).replace("\\", "/")], main
            )
            if code != 0:
                continue
            target = worktree / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(relative).replace("\\", "/"))
            except OSError as exc:
                print(f"WARNING: could not copy {relative}: {exc}")
    return copied


def git_spelling(main: Path, worktree: Path) -> str:
    """The path exactly as git records it.

    This is the whole point of the script: git's own spelling is the one that
    survives the comparison EnterWorktree performs, whatever case the session's
    working directory happens to use.
    """
    code, out, _ = wt_lib.run_git(["worktree", "list", "--porcelain"], main)
    if code == 0:
        for line in out.splitlines():
            if line.startswith("worktree "):
                candidate = line[len("worktree ") :].strip()
                if wt_lib.paths_equal(candidate, worktree):
                    return candidate
    return str(worktree).replace("\\", "/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an isolated worktree.")
    parser.add_argument("name", nargs="?", default="", help="worktree name")
    parser.add_argument(
        "--base", default="", help="branch/ref to start from (default: from wt.json)"
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    main_checkout = wt_lib.find_main_checkout(cwd)
    if main_checkout is None:
        print("ERROR: not inside a git repository.")
        return 1

    # Creating a worktree from inside another worktree would branch off the
    # wrong tree and confuse the inventory; require the main checkout.
    inventory = wt_lib.collect_inventory(main_checkout, str(cwd))
    if inventory["current"] is not None:
        print(f"ERROR: this session is already in a worktree: {inventory['current']}")
        print("       Finish it with /done, or leave it before creating another.")
        return 1

    name = args.name.strip() or generate_name()
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in name).strip("-")
    if not safe:
        print(f"ERROR: '{name}' has no usable characters for a directory name.")
        return 1

    config = wt_lib.load_config(main_checkout)
    base = args.base or wt_lib.resolve_base_ref(main_checkout, config)
    worktree = main_checkout / ".claude" / "worktrees" / safe
    branch = f"worktree-{safe}"

    if worktree.is_dir():
        # Reopening an existing worktree is a normal thing to want; say so
        # plainly rather than failing.
        print(f"Worktree already exists, reusing it: {worktree}")
        print(git_spelling(main_checkout, worktree))
        return 0

    code, _, err = wt_lib.run_git(["rev-parse", "--verify", "--quiet", branch], main_checkout)
    branch_args = ["-B", branch] if code == 0 else ["-b", branch]
    if code == 0:
        print(f"NOTE: branch {branch} already existed; resetting it to {base}.")

    # Size the wait honestly. `git worktree add` writes out the entire tracked
    # tree; on a repository of several GB over Windows filesystems that is
    # minutes, not seconds, and a silent wait reads as a hang.
    tracked_mb = estimate_tree_size(main_checkout)
    if tracked_mb:
        print(f"Tracked tree is about {tracked_mb:.0f} MB -- this can take a few minutes.")
    print(f"Creating worktree from {base} ... (waiting up to {WORKTREE_ADD_TIMEOUT // 60} minutes)")
    started = time.time()

    # --no-track is essential, not cosmetic. Branching off a remote-tracking
    # ref (origin/main) makes git set that ref as the new branch's upstream, so
    # a plain `git push` from inside the worktree goes STRAIGHT INTO main --
    # past /done, past the gate, past every check this workflow exists to
    # apply. The branch must have no upstream at all.
    code, out, err = wt_lib.run_git(
        ["worktree", "add", "--no-track", *branch_args, str(worktree), base],
        main_checkout,
        timeout=WORKTREE_ADD_TIMEOUT,
    )
    elapsed = time.time() - started
    if code != 0:
        print(f"ERROR: git worktree add failed after {elapsed:.0f}s: {err or out}")
        if "timed out" in err:
            print()
            print("git was killed midway, so it never released its own")
            print("'initializing' lock on the half-built worktree. Clear it with:")
            print(f"  git worktree unlock {worktree}")
            print(f"  git worktree remove --force {worktree}")
            print("Then rerun. If it times out again, raise GIT_WRITE_TIMEOUT")
            print(f"in wt_lib.py -- the current limit is "
                  f"{WORKTREE_ADD_TIMEOUT // 60} minutes.")
        return 1
    print(f"Worktree checked out in {elapsed:.0f}s.")

    # Belt and braces: if any git version or config still attached an upstream,
    # take it off. Inheriting origin/main as upstream is what once sent a push
    # from a worktree straight into main.
    wt_lib.run_git(["branch", "--unset-upstream", branch], main_checkout)

    # Removing the upstream alone would leave `git push` with no destination at
    # all, which is why this workflow briefly relied on a "never push" rule.
    # autoSetupRemote gives it the RIGHT destination instead: the first push
    # creates origin/<same-name> and binds to it. Mechanism, not discipline.
    #
    # Scoped to this repository, and it cannot misfire in the main checkout --
    # main already has an upstream, so autoSetupRemote never engages there.
    code, current, _ = wt_lib.run_git(
        ["config", "--get", "push.autoSetupRemote"], main_checkout
    )
    if current.strip().lower() != "true":
        code, _, err = wt_lib.run_git(
            ["config", "push.autoSetupRemote", "true"], main_checkout
        )
        if code == 0:
            print("Set push.autoSetupRemote=true for this repository, so that")
            print(f"`git push` from the worktree creates origin/{branch}")
            print("instead of failing or targeting the base branch.")
        else:
            print(f"WARNING: could not set push.autoSetupRemote: {err}")
            print(f"         Push explicitly instead: git push -u origin {branch}")

    patterns = read_worktreeinclude(main_checkout)
    if patterns:
        copied = copy_included(main_checkout, worktree, patterns)
        if copied:
            print(f"Copied {len(copied)} gitignored file(s) from .worktreeinclude:")
            for path in copied[:20]:
                print(f"  {path}")
        else:
            print("No .worktreeinclude files matched (nothing gitignored to copy).")
    else:
        print("No .worktreeinclude -- worktree starts without gitignored files.")

    print(f"Branch: {branch}")
    print()
    print("Now call EnterWorktree with EXACTLY this path:")
    print(git_spelling(main_checkout, worktree))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
