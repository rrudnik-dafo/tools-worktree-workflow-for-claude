# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
End-to-end rehearsal of the /done cycle in a throwaway repository.

    ~/.local/bin/uv run --no-project ~/.claude/scripts/wt/test_workflow.py

Exercises the whole two-phase cycle -- commit, gate, handover, merge, cleanup
-- plus the guards meant to stop it, against a temp repo where a bug costs
nothing. Run this after ANY change to wt_finish.py or wt_lib.py: /done commits,
merges into the base branch, deletes a worktree and deletes a branch, and those
four are not things to debug on real work.

Checks:
  1. dry run changes nothing and announces the untracked stop
  2. --confirm refuses while an untracked file is present
  3. --confirm proceeds once it is gone, writes a handover, and survives a
     non-ASCII commit message (the echo used to crash on a cp1252 console)
  4. the handover is named per-branch, so two finishing sessions cannot collide
  5. --merge merges, removes the worktree, deletes the branch, clears handover
  6. a failing gate stops before merging, leaving the commit intact
  7. a timeout is distinguishable from a git error, and the post-merge state
     is read from the repository rather than inferred from an exit code
  8. a worktree directory that outlives its removal is recorded and swept
     later, never deleted recursively and never mistaken for a worktree
  9. a lock left by a dead session is released; a live or hand-set one is not
 10. a reused worktree is reset to base once its work is merged, and left
     alone when it holds unmerged or uncommitted work
 11. the post-merge gate stops the cleanup and keeps the branch, so a merge
     that breaks the base branch still has something to fix it from
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))
import wt_lib  # noqa: E402

UV = wt_lib.find_uv()
FINISH = str(PKG / "wt_finish.py")
LOCK_ROOT = wt_lib.LOCK_ROOT

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)
        for line in str(detail).splitlines()[:40]:
            print(f"          {line}")


def git(args: list[str], cwd: Path):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def finish(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [UV, "run", "--no-project", FINISH, *args], cwd=str(cwd),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def handovers() -> list[Path]:
    return list(LOCK_ROOT.glob("repo-*/pending-merge-*.json"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="wt-rehearsal-"))
    repo = root / "repo"
    repo.mkdir()
    print(f"Sandbox: {repo}\n")

    git(["init", "-b", "main"], repo)
    git(["config", "user.email", "rehearsal@example.invalid"], repo)
    git(["config", "user.name", "Rehearsal"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8", newline="\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "wt.json").write_text(
        json.dumps({"finish": "merge", "gate": [], "deleteWorktree": True}, indent=2),
        encoding="utf-8", newline="\n",
    )
    git(["add", "-A"], repo)
    git(["commit", "-m", "initial"], repo)

    # A gitignored file the worktree must inherit, plus the manifest that says so.
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8", newline="\n")
    (repo / ".worktreeinclude").write_text(".env\n", encoding="utf-8", newline="\n")
    git(["add", "-A"], repo)
    git(["commit", "-m", "add worktreeinclude"], repo)
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8", newline="\n")

    # A real remote, because the nastiest bug this suite guards against only
    # exists when there is one: branching off origin/main makes git set that as
    # the new branch's upstream, and then a plain `git push` from inside the
    # worktree lands straight in main, bypassing /done entirely.
    origin = root / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True, text=True,
    )
    git(["remote", "add", "origin", str(origin)], repo)
    git(["push", "-u", "origin", "main"], repo)

    # Commit counts are asserted RELATIVE to this, so adding setup commits to
    # the fixture never again breaks assertions that are about /done.
    def commits(where: Path) -> int:
        text = git(["log", "--oneline"], where).stdout.strip()
        return len(text.splitlines()) if text else 0

    baseline = commits(repo)

    print("0. wt_create.py")
    proc = subprocess.run(
        [UV, "run", "--no-project", str(PKG / "wt_create.py"), "feature"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    created_out = proc.stdout + proc.stderr
    check("exits 0", proc.returncode == 0, created_out)
    wt = repo / ".claude" / "worktrees" / "feature"
    check("worktree created", wt.is_dir(), created_out)
    check("gitignored .env copied in", (wt / ".env").is_file(), created_out)
    printed = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    check("last line is the worktree path", wt_lib.paths_equal(printed, wt), printed)
    check("path is git's own spelling (forward slashes)", "/" in printed and "\\" not in printed,
          printed)
    # The branch must have NO upstream. With one pointing at main, `git push`
    # from the worktree pushes into main directly -- past the gate, past the
    # merge checks, past everything.
    upstream = git(["config", "--get", "branch.worktree-feature.merge"], repo).stdout.strip()
    check("branch has no upstream", upstream == "", f"upstream is {upstream!r}")
    check("base really was a remote ref (so the check is meaningful)",
          "origin/main" in created_out, created_out)

    # Negative control: prove the assertion above is not vacuously true. Git
    # WITHOUT --no-track must attach the upstream, otherwise "no upstream"
    # would pass forever regardless of whether the fix is still in place.
    control = repo / ".claude" / "worktrees" / "control"
    git(["worktree", "add", "-b", "control-branch", str(control), "origin/main"], repo)
    control_upstream = git(["config", "--get", "branch.control-branch.merge"], repo).stdout.strip()
    check("control: git DOES attach upstream without --no-track",
          control_upstream == "refs/heads/main",
          f"got {control_upstream!r} -- if empty, the no-upstream check proves nothing")
    git(["worktree", "remove", "--force", str(control)], repo)
    git(["branch", "-D", "control-branch"], repo)

    # A plain push from the worktree must reach the worktree's OWN branch --
    # this is what replaced the old "never push" rule with a mechanism.
    push = git(["push"], wt)
    remote_heads = git(["ls-remote", "--heads", "origin"], repo).stdout
    check("plain `git push` from the worktree succeeds", push.returncode == 0,
          push.stdout + push.stderr)
    check("it created origin/worktree-feature",
          "refs/heads/worktree-feature" in remote_heads, remote_heads)
    check("it did NOT touch origin/main",
          git(["rev-parse", "origin/main"], repo).stdout.strip()
          == git(["rev-parse", "main"], repo).stdout.strip(),
          "origin/main moved -- a worktree push reached the base branch")

    # Real work plus a stray scratch file of the kind that must not reach main.
    (wt / "README.md").write_text("hello\nworld\n", encoding="utf-8", newline="\n")
    (wt / "scratch-notes.txt").write_text("temp\n", encoding="utf-8", newline="\n")

    print("\n1. dry run")
    code, out = finish([], wt)
    check("exits 0", code == 0, out)
    check("announces the untracked stop", "untracked file(s) need your word" in out, out)
    check("changed nothing", commits(wt) == baseline, f"{commits(wt)} vs {baseline}")

    print("\n2. --confirm with an untracked file present")
    code, out = finish(["--confirm"], wt)
    check("refuses (exit 1)", code == 1, out)
    check("names the stray file", "scratch-notes.txt" in out, out)
    check("committed nothing", commits(wt) == baseline, f"{commits(wt)} vs {baseline}")

    print("\n3. --confirm once the stray file is gone")
    (wt / "scratch-notes.txt").unlink()
    # The commit message is deliberately NON-ASCII -- do not "tidy" it back. wt_finish echoes the
    # message back, and on a cp1252 stdout (the Windows default, pipes included) that print raised
    # UnicodeEncodeError. What made it expensive was WHERE: after `git commit` succeeded and before
    # the gate, so the work was committed while the gate never ran and no handover was written,
    # leaving the following --merge with nothing to read (2026-08-17). wt_lib makes the console
    # UTF-8 tolerant on import; these two checks are what keep it that way.
    code, out = finish(["--confirm", "-m", "feat: додає світ"], wt)
    check("exits 0", code == 0, out)
    check("echoes a non-ASCII commit message without crashing", "додає" in out, out)
    check("committed", commits(wt) == baseline + 1, f"{commits(wt)} vs {baseline + 1}")
    check("tells the user to ExitWorktree next", "ExitWorktree" in out, out)

    print("\n4. handover is per-branch")
    found = handovers()
    check("exactly one handover", len(found) == 1, [str(p) for p in found])
    if found:
        check("named after the branch", "worktree-feature" in found[0].name, found[0].name)

    print("\n5. --merge from the main checkout")
    # Lock the worktree first, the way Claude Code's EnterWorktree does for the
    # whole life of a session. ExitWorktree keeps that lock (it has to: /done
    # leaves with `keep`, because `remove` there would delete the branch while
    # the work is still unmerged), so EVERY real run reaches this point locked --
    # while the rehearsal, which creates its worktree through wt_create.py, never
    # did. That gap is why a single `--force` shipped: it overrides a DIRTY
    # worktree but not a LOCKED one, and git demands `-f -f` or an unlock. The
    # "worktree removed" check below is the one that catches it.
    git(["worktree", "lock", "--reason", "claude session feature (pid 1234)", str(wt)], repo)
    code, out = finish(["--merge"], repo)
    check("exits 0", code == 0, out)
    check("merge landed on main", "Merge worktree branch" in git(["log", "--oneline"], repo).stdout)
    check("work is present",
          (repo / "README.md").read_text(encoding="utf-8") == "hello\nworld\n")
    check("worktree removed", not wt.exists())
    check("branch deleted", "worktree-feature" not in git(["branch"], repo).stdout, out)
    check("remote branch deleted too",
          "refs/heads/worktree-feature" not in git(["ls-remote", "--heads", "origin"], repo).stdout,
          "origin/worktree-feature survived the merge -- dead branches will pile up")
    check("handover cleared", not handovers())

    print("\n6. a failing gate stops before merging")
    (repo / ".claude" / "wt.json").write_text(
        json.dumps({"finish": "merge", "gate": ["exit 3"]}, indent=2),
        encoding="utf-8", newline="\n",
    )
    git(["add", "-A"], repo)
    git(["commit", "-m", "add failing gate"], repo)
    wt2 = repo / ".claude" / "worktrees" / "second"
    git(["worktree", "add", "-b", "worktree-second", str(wt2)], repo)
    (wt2 / "README.md").write_text("hello\nworld\nagain\n", encoding="utf-8", newline="\n")
    before_gate = commits(wt2)
    code, out = finish(["--confirm", "-m", "feat: again"], wt2)
    check("gate failure exits 2", code == 2, out)
    check("work committed anyway", commits(wt2) == before_gate + 1,
          f"{commits(wt2)} vs {before_gate + 1}")
    check("no handover written", not handovers())
    check("main untouched", "again" not in (repo / "README.md").read_text(encoding="utf-8"))

    print("\n7. a timeout is not a verdict")
    # The defect this guards against: `git merge` was killed by our own 15s
    # limit AFTER it had written the merge commit, and the non-zero exit code
    # was reported to the user as "MERGE FAILED" while the merge sat in the
    # history. The fix is that a timeout sends us to ask the repository what
    # actually happened, so these are the three questions it asks.
    import wt_create  # noqa: PLC0415 -- sections 7-10 need these
    import wt_finish  # noqa: PLC0415

    _, _, err = wt_lib.run_git(["status", "--porcelain"], repo, timeout=0.001)
    check("a real timeout is recognised as one", wt_lib.timed_out(err), err)
    check("an ordinary git error is not",
          not wt_lib.timed_out("fatal: not a git repository"))
    check("no merge in progress on a clean repo",
          not wt_finish.merge_in_progress(repo))
    # worktree-second was never merged; worktree-feature was, in step 5.
    check("containment: an unmerged branch reads as unmerged",
          not wt_finish.branch_is_merged(repo, "worktree-second", "main"))
    check("containment: a merged branch reads as merged",
          wt_finish.branch_is_merged(repo, "HEAD~1", "main"))

    print("\n8. leftover worktree directories are swept, not forgotten")
    # The measured failure: /done merged, deleted the branch and dropped git's
    # admin entry, yet the empty directory stayed on disk because a live process
    # held it as its cwd. It cannot be removed from the finishing session, so it
    # has to be recorded and retried later -- and never mistaken for a worktree.
    ghost = repo / ".claude" / "worktrees" / "ghost"
    ghost.mkdir(parents=True, exist_ok=True)
    wt_lib.record_leftover(repo, ghost, "worktree-ghost")
    check("leftover is recorded", len(wt_lib.read_sweep(repo)) == 1)

    # An occupied leftover must be reported and LEFT ALONE -- the sweep is not
    # an rm -rf, and files inside mean removal failed long before the rmdir.
    (ghost / "stray.txt").write_text("x\n", encoding="utf-8", newline="\n")
    swept = wt_lib.sweep_leftovers(repo)
    check("occupied leftover is not deleted", (ghost / "stray.txt").is_file())
    check("occupied leftover is reported", swept["occupied"] == [str(ghost)], swept)
    check("occupied leftover stays on the list", len(wt_lib.read_sweep(repo)) == 1)

    # Emptied, it is swept on the next attempt and the list clears itself.
    (ghost / "stray.txt").unlink()
    swept = wt_lib.sweep_leftovers(repo)
    check("empty leftover is swept", not ghost.exists(), swept)
    check("list clears itself once swept", wt_lib.read_sweep(repo) == [],
          wt_lib.read_sweep(repo))

    # Containment guard: a corrupted list must never aim the sweep elsewhere.
    outsider = root / "not-in-the-repo"
    outsider.mkdir(parents=True, exist_ok=True)
    wt_lib.record_leftover(repo, outsider, "")
    wt_lib.sweep_leftovers(repo)
    check("sweep refuses paths outside .claude/worktrees", outsider.is_dir(),
          "the sweep deleted a directory outside the repo's worktree dir")

    # /wt must refuse a directory git does not know, rather than "reusing" it.
    ghost.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [UV, "run", "--no-project", str(PKG / "wt_create.py"), "ghost"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ghost_out = proc.stdout + proc.stderr
    check("/wt refuses an unregistered leftover directory", proc.returncode == 1, ghost_out)
    check("/wt does not claim to reuse it", "reusing it" not in ghost_out, ghost_out)
    shutil.rmtree(ghost, ignore_errors=True)

    print("\n9. locks left by a dead session are released, live ones are not")
    # A worktree locked by a session that never ran /done stays locked forever,
    # and a locked worktree refuses `git worktree remove` even with --force --
    # one crashed tab becomes a worktree nobody can clean up. The release is
    # keyed on the pid git records in the lock reason, so the two cases that
    # must NOT be touched are a live pid and a hand-written reason.
    check("a dead pid reads as dead", wt_lib.process_alive(999_999_999) is False)
    check("our own pid reads as alive", wt_lib.process_alive(os.getpid()) is True)

    git(["worktree", "lock", "--reason", "claude session x (pid 999999999)", str(wt2)], repo)
    freed = wt_lib.release_dead_locks(repo)
    check("a lock from a dead session is released", len(freed) == 1, freed)
    check("git agrees it is unlocked",
          not [w for w in wt_lib.list_worktrees(repo)
               if w.get("locked") and not w["is_main"]])

    git(["worktree", "lock", "--reason", f"claude session x (pid {os.getpid()})", str(wt2)], repo)
    check("a lock from a LIVE session is left alone", wt_lib.release_dead_locks(repo) == [])
    git(["worktree", "lock", "--reason", "keep this, I am using it", str(wt2)], repo)
    check("a hand-written lock reason is never touched",
          wt_lib.release_dead_locks(repo) == [])
    git(["worktree", "unlock", str(wt2)], repo)

    print("\n10. a reused worktree is reset once its work is merged")
    # Reopening a name used to hand back the old tip unconditionally, so the
    # next task started on a branch still carrying the previous one's commits.
    spent = repo / ".claude" / "worktrees" / "spent"
    git(["worktree", "add", "-b", "worktree-spent", str(spent), "main"], repo)
    (spent / "spent.txt").write_text("done\n", encoding="utf-8", newline="\n")
    git(["add", "-A"], spent)
    git(["commit", "-m", "work that will be merged"], spent)
    git(["merge", "--no-ff", "worktree-spent", "-m", "merge spent"], repo)

    before = git(["rev-parse", "HEAD"], spent).stdout.strip()
    did = wt_create.reset_if_spent(repo, spent, "worktree-spent", "main")
    check("merged work resets the worktree to base", did, "no reset happened")
    check("it really moved", git(["rev-parse", "HEAD"], spent).stdout.strip() != before)

    # Unmerged work must survive untouched -- this is a reset --hard, after all.
    (spent / "unmerged.txt").write_text("keep\n", encoding="utf-8", newline="\n")
    git(["add", "-A"], spent)
    git(["commit", "-m", "not merged anywhere"], spent)
    check("unmerged commits are left alone",
          not wt_create.reset_if_spent(repo, spent, "worktree-spent", "main"))
    check("the unmerged file survives", (spent / "unmerged.txt").is_file())

    # So must uncommitted work.
    git(["reset", "--hard", "main"], spent)
    (spent / "dirty.txt").write_text("wip\n", encoding="utf-8", newline="\n")
    check("a dirty worktree is left alone",
          not wt_create.reset_if_spent(repo, spent, "worktree-spent", "main"))
    check("the dirty file survives", (spent / "dirty.txt").is_file())
    git(["worktree", "remove", "--force", str(spent)], repo)
    git(["branch", "-D", "worktree-spent"], repo)

    print("\n11. the post-merge gate guards the base branch, and keeps the way back")
    # The ordinary gate runs in the worktree BEFORE the merge, so it can only
    # ever test the branch. Two branches can each be green and still break the
    # base together -- a semantic conflict leaves no textual conflict to report.
    # When this gate fails the merge is already in, so the branch and worktree
    # must survive: they are the only way to fix it.
    (repo / ".claude" / "wt.json").write_text(
        json.dumps({"finish": "merge", "gate": [], "postMergeGate": ["exit 7"]}, indent=2),
        encoding="utf-8", newline="\n",
    )
    git(["add", "-A"], repo)
    git(["commit", "-m", "add failing post-merge gate"], repo)

    pg = repo / ".claude" / "worktrees" / "postgate"
    git(["worktree", "add", "-b", "worktree-postgate", str(pg), "main"], repo)
    # A tracked file, deliberately: an untracked one would trip the new-file
    # stop from section 2 before this section's subject is even reached.
    (pg / "README.md").write_text("hello\nworld\npostgate\n", encoding="utf-8", newline="\n")
    code, out = finish(["--confirm", "-m", "feat: postgate"], pg)
    check("phase 1 succeeds", code == 0, out)

    code, out = finish(["--merge", "--branch", "worktree-postgate"], repo)
    check("a failing post-merge gate exits 2", code == 2, out)
    check("the merge itself did land",
          "postgate" in (repo / "README.md").read_text(encoding="utf-8"),
          "merge did not reach the base branch")
    check("the worktree is KEPT so the fix can be made", pg.is_dir(), out)
    check("the branch is KEPT", "worktree-postgate" in git(["branch"], repo).stdout)
    check("it offers both ways out", "fix forward" in out and "reset --hard" in out, out)
    check("the handover survives for the rerun", len(handovers()) == 1, handovers())

    # Fixed: the same rerun now completes and cleans up.
    (repo / ".claude" / "wt.json").write_text(
        json.dumps({"finish": "merge", "gate": [], "postMergeGate": ["exit 0"]}, indent=2),
        encoding="utf-8", newline="\n",
    )
    git(["add", "-A"], repo)
    git(["commit", "-m", "fix the post-merge gate"], repo)
    code, out = finish(["--merge", "--branch", "worktree-postgate"], repo)
    check("the rerun completes once the gate passes", code == 0, out)
    check("now the worktree is gone", not pg.exists(), out)
    check("now the branch is gone", "worktree-postgate" not in git(["branch"], repo).stdout)

    git(["worktree", "remove", "--force", str(wt2)], repo)
    for leftover in LOCK_ROOT.glob("repo-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    os.chdir(str(Path.home()))
    shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"NOK: {len(failures)} check(s) failed:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("OK: the full /done cycle behaves as designed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
