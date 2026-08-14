#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
The mechanics behind /done: commit the worktree's work, run the project gate,
push the branch. It deliberately stops there -- merging into the main branch
stays a human decision, so this script never touches the main checkout.

Two-phase by design:

    wt_finish.py              -> dry run: shows exactly what would happen
    wt_finish.py --confirm    -> executes it

The dry run is what the user says "done" to. Running --confirm without having
shown the dry run first defeats the entire point of the workflow.

Everything project-specific comes from <main>/.claude/wt.json:

    {
      "gate": ["uv run tools/verify_anchors.py --product acron --version 10.2"],
      "protectedPaths": ["**/i18n.lock"],
      "remote": "origin",
      "baseBranch": "main"
    }
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wt_lib  # noqa: E402


def pending_path(main_checkout: Path, branch: str) -> Path:
    """Where phase 1 leaves the handover note for phase 2.

    The two phases run in different working directories -- phase 1 inside the
    worktree, phase 2 in the main checkout after ExitWorktree -- so the branch
    and worktree path have to survive the move.

    ONE FILE PER BRANCH, deliberately. A single shared handover file would be
    overwritten whenever two sessions finished at once, and the second merge
    would then target the first one's branch. That is the exact failure this
    whole workflow exists to prevent, so it must not be reintroduced by the
    workflow's own bookkeeping.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "unnamed"
    return wt_lib.locks_dir(main_checkout) / f"pending-merge-{safe}.json"


def write_pending(main_checkout: Path, branch: str, payload: dict) -> None:
    path = pending_path(main_checkout, branch)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def list_pending(main_checkout: Path) -> list[dict]:
    """Every outstanding handover, newest first."""
    directory = wt_lib.locks_dir(main_checkout)
    if not directory.is_dir():
        return []
    out = []
    for entry in directory.glob("pending-merge-*.json"):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("branch"):
            out.append(data)
    return sorted(out, key=lambda d: d.get("created_at", 0), reverse=True)


def read_pending(main_checkout: Path, branch: str | None = None) -> dict:
    """Resolve which handover phase 2 should act on.

    With a branch named, read exactly that one. Without, accept the single
    outstanding handover -- and refuse when there are several, rather than
    guessing which finished session the user meant.
    """
    if branch:
        path = pending_path(main_checkout, branch)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    outstanding = list_pending(main_checkout)
    if len(outstanding) == 1:
        return outstanding[0]
    if len(outstanding) > 1:
        return {"_ambiguous": [d["branch"] for d in outstanding]}
    return {}


def clear_pending(main_checkout: Path, branch: str) -> None:
    try:
        pending_path(main_checkout, branch).unlink(missing_ok=True)
    except OSError:
        pass


def match_protected(rel_path: str, patterns: list[str]) -> bool:
    """Test a repo-relative path against wt.json's protectedPaths.

    fnmatch has no '**' concept and its '*' already crosses '/', so a leading
    '**/' is simply folded into '*'. That keeps the familiar gitignore-ish
    spelling working without pulling in a real glob engine.
    """
    for pattern in patterns:
        normalised = pattern.replace("**/", "*")
        if fnmatch.fnmatch(rel_path, normalised) or fnmatch.fnmatch(
            rel_path, f"*/{normalised.lstrip('*/')}"
        ):
            return True
    return False


def changed_files(worktree: Path) -> list[tuple[str, str]]:
    """Return [(status, path)] from git status --porcelain."""
    code, out, _ = wt_lib.run_git(["status", "--porcelain"], worktree)
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, _, path = line[:2], line[2:3], line[3:]
        # Renames read as "old -> new"; the new path is what matters here.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        rows.append((status.strip() or "?", path.strip().strip('"')))
    return rows


def run_gate(commands: list[str], worktree: Path) -> tuple[bool, list[str]]:
    """Run each gate command in the worktree; stop at the first failure.

    The gate is advisory infrastructure, not a security boundary: it exists so
    that a repo can refuse to push work that fails its own checks. A repo with
    no wt.json simply has no gate.
    """
    log: list[str] = []
    uv = wt_lib.find_uv()
    for raw in commands:
        # ${UV} keeps wt.json portable: the file is committed and travels to
        # machines with a different home directory, where an absolute uv path
        # would be wrong and a bare `~` would not expand in cmd.exe.
        command = raw.replace("${UV}", uv)
        log.append(f"$ {command}")
        try:
            proc = subprocess.run(
                command,
                cwd=str(worktree),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.append(f"  could not run: {exc!r}")
            return False, log
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
        log.extend(f"  {line}" for line in tail)
        if proc.returncode != 0:
            log.append(f"  FAILED with exit code {proc.returncode}")
            return False, log
        log.append("  ok")
    return True, log


def merge_phase(args) -> int:
    """Phase 2: merge the finished branch into the base and clean up.

    Must run from the MAIN checkout. Claude Code blocks every write and every
    git redirect aimed at the main checkout while a session is isolated, so
    this cannot be folded into phase 1 -- the session has to leave the worktree
    (ExitWorktree) between the two. That constraint is the reason /done is
    two-phase at all.
    """
    cwd = Path.cwd()
    main_checkout = wt_lib.find_main_checkout(cwd)
    if main_checkout is None:
        print("ERROR: not inside a git repository.")
        return 1

    pending = read_pending(main_checkout, args.branch)
    if pending.get("_ambiguous"):
        print("ERROR: several finished sessions are waiting to merge:")
        for name in pending["_ambiguous"]:
            print(f"         {name}")
        print("       Name the one to merge with --branch <name>.")
        return 1

    branch = args.branch or pending.get("branch")
    worktree_raw = args.worktree or pending.get("worktree")
    if not branch or not worktree_raw:
        print("ERROR: nothing to merge -- no pending handover and no --branch given.")
        print("       Run the commit phase first (wt_finish.py --confirm).")
        return 1
    worktree = Path(worktree_raw)

    config = wt_lib.load_config(main_checkout)
    remote = config.get("remote", "origin")
    base = pending.get("base") or wt_lib.resolve_base_ref(main_checkout, config)
    base_local = base.split("/")[-1]
    push_after = bool(config.get("pushAfterMerge", False))
    delete_worktree = bool(config.get("deleteWorktree", True))

    # Refuse to run from inside the worktree being merged: git would be
    # operating on a checkout we are about to delete.
    if wt_lib.path_within(cwd, worktree) or wt_lib.paths_equal(cwd, worktree):
        print("ERROR: still inside the worktree. Leave it first (ExitWorktree),")
        print(f"       then rerun from {main_checkout}.")
        return 1

    code, current, _ = wt_lib.run_git(["rev-parse", "--abbrev-ref", "HEAD"], main_checkout)
    if code != 0:
        print("ERROR: cannot read the main checkout's current branch.")
        return 1

    dirty = [path for _, path in changed_files(main_checkout)]
    code, out, _ = wt_lib.run_git(
        ["diff", "--name-only", f"{base_local}...{branch}"], main_checkout
    )
    incoming = [line.strip() for line in out.splitlines() if line.strip()]

    print(f"Main checkout: {main_checkout}")
    print(f"On branch:     {current}")
    print(f"Merging:       {branch}  ->  {base_local}")
    print(f"Files arriving: {len(incoming)}")
    if dirty:
        print(f"Uncommitted in main checkout: {len(dirty)}")

    # Two distinct hazards, handled separately. Switching branches under a
    # dirty tree can lose work outright; merging into the branch we are already
    # on is safe as long as nothing arriving collides with local edits.
    if current != base_local:
        if dirty:
            print()
            print(f"STOP: the main checkout is on '{current}' with uncommitted changes.")
            print(f"      Switching to '{base_local}' could lose them.")
            print("      Commit or stash them, then rerun.")
            return 1
        code, _, err = wt_lib.run_git(["checkout", base_local], main_checkout)
        if code != 0:
            print(f"ERROR: could not switch to {base_local}: {err}")
            return 1
        print(f"Switched to {base_local}.")
    else:
        overlap = sorted(set(dirty) & set(incoming))
        if overlap:
            print()
            print("STOP: these files are edited in the main checkout AND arrive")
            print("      with the merge. Merging would collide:")
            for path in overlap[:20]:
                print(f"        {path}")
            print("      Commit or stash them, then rerun.")
            return 1

    # Other sessions working in the main checkout are warned about, not
    # blocked: a merge here is exactly what the user asked for, and blocking on
    # it would make /done unusable in the parallel setup it exists to serve.
    busy = [
        lock
        for lock in wt_lib.read_locks(main_checkout)
        if not lock["ended"]
        and lock["age_seconds"] <= 45 * 60
        and lock.get("cwd")
        and wt_lib.paths_equal(lock["cwd"], main_checkout)
    ]
    if busy:
        print()
        print(f"NOTE: {len(busy)} other session(s) are working in the main checkout.")
        print("      Files there are about to change under them.")

    print()
    code, out, err = wt_lib.run_git(
        ["merge", "--no-ff", branch, "-m", f"Merge worktree branch {branch}"],
        main_checkout,
    )
    if code != 0:
        print("MERGE FAILED:")
        print((out + "\n" + err).strip())
        # Leave no half-merged state behind; the branch and worktree survive.
        wt_lib.run_git(["merge", "--abort"], main_checkout)
        print()
        print("Merge aborted. The branch and worktree are untouched --")
        print("resolve the conflict manually, or rerun after rebasing.")
        return 2
    print((out + "\n" + err).strip() or "Merged.")

    if delete_worktree:
        print()
        code, _, err = wt_lib.run_git(["worktree", "remove", str(worktree)], main_checkout)
        if code != 0:
            # Files copied in by .worktreeinclude are gitignored, so git counts
            # them as reasons to refuse removal. The tracked tree is committed
            # and merged by now, so forcing only discards those copies -- but
            # name them first: silently deleting a .env or an .mcp.json the
            # user had put there by hand is a nasty surprise.
            code2, ignored, _ = wt_lib.run_git(
                ["status", "--porcelain", "--ignored=matching"], worktree
            )
            doomed = [
                line[3:].strip()
                for line in ignored.splitlines()
                if line.startswith("!!") or line.startswith("??")
            ] if code2 == 0 else []
            if doomed:
                print("These untracked/ignored files exist only in the worktree")
                print("and will be lost when it is removed:")
                for path in doomed[:20]:
                    print(f"    {path}")
                if len(doomed) > 20:
                    print(f"    ... and {len(doomed) - 20} more")
                print("(They are gitignored copies -- normally .env / .mcp.json")
                print(" brought in by .worktreeinclude, which still exist in the")
                print(" main checkout. Say so now if any of them was hand-made.)")

            code, _, err2 = wt_lib.run_git(
                ["worktree", "remove", "--force", str(worktree)], main_checkout
            )
            if code != 0:
                print(f"WARNING: could not remove the worktree: {err2 or err}")
                print(f"         Remove it manually: git worktree remove --force {worktree}")
            else:
                print(f"Removed worktree (forced): {worktree}")
        else:
            print(f"Removed worktree: {worktree}")

        # Ask the question we actually care about: is every commit on this
        # branch now contained in the base branch? `git branch -d` asks a
        # different one -- it compares against the branch's UPSTREAM, so a
        # branch pushed for backup and then worked on further looks "not fully
        # merged" to it even when the work is safely in the base branch.
        code, _, _ = wt_lib.run_git(
            ["merge-base", "--is-ancestor", branch, base_local], main_checkout
        )
        if code != 0:
            print(f"WARNING: {branch} is NOT fully contained in {base_local};")
            print("         keeping it. Inspect before deleting anything:")
            print(f"           git log {base_local}..{branch}")
        else:
            # -D rather than -d: we have just verified containment ourselves,
            # and -d would refuse over the upstream comparison described above.
            code, _, err = wt_lib.run_git(["branch", "-D", branch], main_checkout)
            if code != 0:
                print(f"WARNING: could not delete branch {branch}: {err}")
            else:
                print(f"Deleted branch: {branch}")

        # The branch may also exist on the remote, because pushing from a
        # worktree is allowed and creates origin/<branch>. Deleting the local
        # copy while leaving the remote one behind would pile up dead branches
        # on a shared server, one per finished task.
        code, _, _ = wt_lib.run_git(
            ["ls-remote", "--exit-code", "--heads", remote, branch],
            main_checkout,
            timeout=60,
        )
        if code == 0:
            code, out, err = wt_lib.run_git(
                ["push", remote, "--delete", branch], main_checkout, timeout=120
            )
            if code == 0:
                print(f"Deleted {remote}/{branch} (work is merged into {base_local}).")
            else:
                print(f"WARNING: could not delete {remote}/{branch}: {err or out}")
                print(f"         Remove it later: git push {remote} --delete {branch}")

    if push_after:
        print()
        code, out, err = wt_lib.run_git(["push", remote, base_local], main_checkout)
        if code != 0:
            print(f"WARNING: push of {base_local} failed: {err or out}")
        else:
            print((out + "\n" + err).strip() or f"Pushed {base_local}.")

    clear_pending(main_checkout, branch)
    print()
    print(f"Done. {branch} is merged into {base_local}.")
    if not push_after:
        print(f"Not pushed -- run `git push {remote} {base_local}` when you want it up.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Finish a worktree session.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually commit, gate and push (default is a dry run)",
    )
    parser.add_argument("--message", "-m", default="", help="commit message")
    parser.add_argument(
        "--skip-gate",
        action="store_true",
        help="skip the project gate (requires an explicit human decision)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="phase 2: merge the finished branch and clean up (run from the main checkout)",
    )
    parser.add_argument("--branch", default="", help="phase 2: branch to merge")
    parser.add_argument("--worktree", default="", help="phase 2: worktree to remove")
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="commit new (untracked) files too; without this they stop the run",
    )
    args = parser.parse_args()

    if args.merge:
        return merge_phase(args)

    cwd = Path.cwd()
    main_checkout = wt_lib.find_main_checkout(cwd)
    if main_checkout is None:
        print("ERROR: not inside a git repository.")
        return 1

    # Refuse to operate on the main checkout. /done means "wrap up an isolated
    # piece of work"; in the main checkout there is nothing isolated to wrap up,
    # and committing everything there would be a surprise, not a service.
    inventory = wt_lib.collect_inventory(main_checkout, str(cwd))
    worktree = inventory["current"]
    if worktree is None:
        print("ERROR: this session is in the MAIN checkout, not in a worktree.")
        print("       /done only finishes isolated worktree sessions.")
        print("       Use /wt first, or commit and push manually.")
        return 1

    config = inventory["config"]
    remote = config.get("remote", "origin")
    gate = list(config.get("gate", []))
    protected = list(config.get("protectedPaths", []))
    base = inventory["base"]

    code, branch, _ = wt_lib.run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
    if code != 0 or not branch or branch == "HEAD":
        print("ERROR: worktree is in detached HEAD state; cannot push a branch.")
        return 1

    files = changed_files(worktree)
    state = wt_lib.worktree_state(worktree, base)
    hits = [path for _, path in files if protected and match_protected(path, protected)]

    print(f"Worktree:  {worktree}")
    print(f"Branch:    {branch}")
    print(f"Base ref:  {base}")
    print(f"Remote:    {remote}")
    print()
    print(f"Uncommitted changes: {len(files)}")
    for status, path in files[:40]:
        print(f"  {status:<3} {path}")
    if len(files) > 40:
        print(f"  ... and {len(files) - 40} more")
    print(f"Commits ahead of {base}: {state['ahead']}")
    if state["has_upstream"]:
        print(f"Unpushed commits: {state['unpushed']}")
    else:
        print("Upstream: none yet (branch has never been pushed)")

    if hits:
        print()
        print("PROTECTED PATHS TOUCHED -- review these before merging:")
        for path in hits:
            print(f"  {path}")

    print()
    if gate:
        print(f"Gate ({len(gate)} command(s)) from .claude/wt.json:")
        for command in gate:
            print(f"  {command}")
    else:
        print("Gate: none configured (.claude/wt.json absent or has no 'gate').")

    mode = str(config.get("finish", "merge")).lower()
    base_local = base.split("/")[-1]

    if not args.confirm:
        print()
        print("DRY RUN -- nothing was changed.")
        print("On confirmation this would:")
        step = 1
        new_files = [path for status, path in files if status == "??"]
        if new_files:
            print(f"  0. STOP -- {len(new_files)} untracked file(s) need your word first:")
            for path in new_files[:10]:
                print(f"       {path}")
            if len(new_files) > 10:
                print(f"       ... and {len(new_files) - 10} more")
        if files:
            print(f"  {step}. git add -A && git commit")
            step += 1
        if gate and not args.skip_gate:
            print(f"  {step}. run the gate; stop here if it fails")
            step += 1
        if mode == "merge":
            print(f"  {step}. leave the worktree (ExitWorktree)")
            print(f"  {step + 1}. git merge --no-ff {branch} into {base_local}")
            print(f"  {step + 2}. remove the worktree and delete the branch")
            if config.get("pushAfterMerge", False):
                print(f"  {step + 3}. git push {remote} {base_local}")
            else:
                print(f"  (not pushing {base_local} -- pushAfterMerge is off)")
        else:
            print(f"  {step}. git push -u {remote} HEAD:{branch}")
            print(f"  {step + 1}. leave the merge into {base_local} to you")
        return 0

    # ---- execution -------------------------------------------------------
    # Untracked files are the ones that quietly ride into main: scratch
    # scripts, stray dumps, half-finished notes. Modified tracked files are
    # the work itself and need no ceremony, but a NEW file is a decision, so
    # stop and make it one.
    untracked = [path for status, path in files if status == "??"]
    if untracked and not args.include_untracked:
        print()
        print(f"STOP: {len(untracked)} new file(s) are not tracked by git:")
        for path in untracked[:30]:
            print(f"    {path}")
        if len(untracked) > 30:
            print(f"    ... and {len(untracked) - 30} more")
        print()
        print("These would be committed and end up in the base branch.")
        print("Delete what is scratch, then rerun -- or rerun with")
        print("--include-untracked once you have confirmed they belong.")
        print("Nothing was changed.")
        return 1

    # Commit first: whatever happens afterwards, the work is safe in git.
    if files:
        message = args.message or f"wt({branch}): work in progress"
        code, _, err = wt_lib.run_git(["add", "-A"], worktree)
        if code != 0:
            print(f"ERROR: git add failed: {err}")
            return 1
        code, out, err = wt_lib.run_git(["commit", "-m", message], worktree)
        if code != 0:
            print(f"ERROR: git commit failed: {err or out}")
            return 1
        print(f"Committed: {message}")
    else:
        print("Nothing to commit.")

    if gate and not args.skip_gate:
        print()
        print("Running gate...")
        ok, log = run_gate(gate, worktree)
        for line in log:
            print(line)
        if not ok:
            print()
            print("GATE FAILED -- not pushing.")
            print("The work is committed and the worktree is intact. Fix and rerun.")
            return 2
    elif gate and args.skip_gate:
        print("Gate SKIPPED by explicit request.")

    if mode == "merge":
        # Hand phase 2 everything it needs, because after ExitWorktree the
        # worktree is no longer the cwd and none of this is derivable.
        write_pending(
            main_checkout,
            branch,
            {
                "branch": branch,
                "worktree": str(worktree),
                "base": base,
                "created_at": time.time(),
            },
        )
        print()
        print("Committed and gated. NEXT STEP IS REQUIRED:")
        print("  1. leave the worktree with the ExitWorktree tool")
        print("  2. rerun this script with --merge")
        print()
        print(f"Nothing is merged yet; {branch} still holds the work.")
        return 0

    print()
    code, out, err = wt_lib.run_git(
        ["push", "-u", remote, f"HEAD:{branch}"], worktree
    )
    if code != 0:
        print(f"ERROR: push failed: {err or out}")
        return 1
    print((out + "\n" + err).strip() or "Pushed.")

    print()
    print(f"Branch {branch} is pushed. Merging is yours to do:")
    print(f"  git checkout {base_local}")
    print(f"  git merge --no-ff {branch}")
    print(f"The worktree at {worktree} is left in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
