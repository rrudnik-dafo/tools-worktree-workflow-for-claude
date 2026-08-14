#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Shared library for the universal git-worktree session workflow.

Consumed by wt_hook.py (lifecycle hooks), wt_status.py (/wt-list) and
wt_finish.py (/done). Contains no project-specific knowledge: everything
that differs per repository is read from <repo>/.claude/wt.json.

Design notes
------------
* Session locks live OUTSIDE the repository, under
  ~/.claude/wt-locks/<repo-key>/<session-id>.json. Keeping them out of the
  working tree means no project needs a .gitignore entry for them, which is
  what makes this workflow drop-in for any repo.
* The "is this tab still open?" question has no direct answer -- Claude Code
  exposes no list of open editor tabs. We approximate it with a heartbeat:
  every UserPromptSubmit and every Stop refreshes the lock file's mtime. A
  lock refreshed recently means a live session; a stale lock means the tab is
  probably gone. SessionEnd deletes the lock outright when it fires, which
  turns the approximation into a certainty for that session.
* All output is ASCII-only. Windows consoles default to cp1252 here and a
  stray non-ASCII byte in hook output turns into an encoding crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# git timeouts
# ---------------------------------------------------------------------------
# One number cannot serve every git command, because the COST OF OVERRUNNING
# differs by orders of magnitude between them. A killed `rev-parse` costs a
# line of the report; a killed `merge` can leave a working tree half-rewritten.
# So the commands are grouped by what they touch.
#
# The old single default of 15 seconds was calibrated on queries alone and bit
# twice in production: once killing `git worktree add` mid-checkout, and once
# killing `git merge` AFTER it had already written the merge commit, which was
# then reported to the user as "MERGE FAILED".
#
# The values below are MEASURED, not guessed, against the documentation
# repository these scripts were built in -- 53,036 tracked files / 3.41 GB on
# Windows, which is the largest tree this workflow is known to meet:
#
#     git status (whole tree)              0.24 s
#     git diff --name-only (1,651 files)   0.05 s
#     materialise 1,650 files / 802 MB     6.0 s     <- a big merge
#     materialise all 53,036 files         ~194 s    <- git worktree add
#
# Throughput is bounded by FILE COUNT (~273 files/s), not by byte volume: the
# same 3.41 GB in few large files would take about 26 s. So a repository with
# many small files is the case to size for.
#
# Two rules when adjusting these. A timeout exists to stop a HANG, not to
# express impatience -- any value a healthy command can plausibly reach is too
# low. But an overrun is no longer catastrophic either: since wt_finish.py
# stopped treating a timeout as a verdict and started asking the repository
# what actually happened, the worst outcome is one rerun. That is what keeps
# these ceilings in the minutes rather than the tens of minutes.

# Metadata only -- reads refs, config and the index. Measured at 0.04-0.15 s
# regardless of tree size; the headroom is for a contended index lock.
GIT_QUERY_TIMEOUT = 60

# Walks the working tree: status, diff --name-only, add. Measured at 0.24 s on
# 53k files, so this is ~500x margin -- enough for a cold cache and an
# on-access virus scanner, both of which multiply the figure, not double it.
GIT_SCAN_TIMEOUT = 120

# REWRITES the working tree: checkout, merge, worktree add/remove. Still the
# tightest margin of the five: a full-tree checkout measured ~194 s, so this is
# ~3x the worst real case -- enough to absorb a cold cache or an on-access virus
# scanner, both of which multiply that figure rather than double it. It is the
# one number to raise first if a larger repository ever appears, and the symptom
# will be /done reporting that it could not tell what the merge did.
GIT_WRITE_TIMEOUT = 600

# Talks to the remote. Bounded by the network rather than the tree, and safe to
# kill: a dead push leaves nothing behind locally.
GIT_NETWORK_TIMEOUT = 300

# The inventory rendered inside a SessionStart hook. Short ON PURPOSE, and the
# one place where a low limit is right: Claude Code caps hook runtime, so a
# measurement that outlives the hook is worse than an honest "could not
# measure". Callers outside a hook (/done, /wt-list) pass GIT_SCAN_TIMEOUT.
# Kept comfortably under the SessionStart hook budget in install.py, which the
# inventory has to fit into SEVERAL of these calls, not one.
GIT_REPORT_TIMEOUT = 30

# Prefix of the stderr line run_git synthesises on a timeout. Callers match on
# it to tell "git was killed" apart from "git said no", which are different
# situations with different recoveries.
TIMEOUT_MARKER = "git timed out after"

# A lock untouched for longer than this is reported as "idle" -- the tab is
# likely closed. Deliberately generous: a session you simply have not typed
# into for a while must not be mistaken for an abandoned one. Overridable per
# repo via wt.json -> staleMinutes.
DEFAULT_STALE_MINUTES = 45

# Locks are keyed by repository so that several repos can be worked on in
# parallel without their session inventories bleeding into each other.
LOCK_ROOT = Path.home() / ".claude" / "wt-locks"

# Append-only record of lifecycle events. Its first job is empirical: it lets
# us confirm whether SessionEnd actually fires when a VS Code tab is closed,
# a contract the docs do not specify.
EVENT_LOG = Path.home() / ".claude" / "wt-events.log"

# Claude Code stores session transcripts per working directory, so a worktree
# gets its own directory here. The newest .jsonl mtime inside it is a liveness
# signal that needs none of our own bookkeeping and works retroactively -- it
# is what keeps a freshly installed workflow from declaring every existing
# worktree abandoned simply because no heartbeat has been recorded yet.
PROJECTS_ROOT = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def run_git(
    args: list[str], cwd: str | Path, timeout: float = GIT_QUERY_TIMEOUT
) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr), all trimmed.

    Never raises on a non-zero exit: every caller here treats git failure as
    "this piece of information is unavailable" rather than as a crash, because
    the hooks must stay silent in non-git directories.

    The default covers metadata queries only. Any command that walks, rewrites
    or transmits the tree must pass the matching constant from the block above
    -- GIT_SCAN_TIMEOUT, GIT_WRITE_TIMEOUT or GIT_NETWORK_TIMEOUT -- because the
    default is far too short for those and a kill there does real damage.

    A timeout is reported distinctly from other failures, and prefixed with
    TIMEOUT_MARKER so callers can branch on it. Both used to collapse into one
    opaque "git invocation failed", which sent a real diagnosis down the wrong
    path entirely.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            1,
            "",
            f"{TIMEOUT_MARKER} {timeout:g}s: git {' '.join(args[:2])} "
            f"(the command was killed midway)",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"git could not be started: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def timed_out(stderr: str) -> bool:
    """Did this failure come from our own timeout rather than from git?

    The distinction matters most after a tree-rewriting command. git's own
    error means git decided not to proceed and left a defined state behind. A
    timeout means we killed it at an unknown point, so the exit code carries no
    information about what was or was not done -- the repository has to be
    asked instead.
    """
    return TIMEOUT_MARKER in (stderr or "")


def find_main_checkout(cwd: str | Path) -> Path | None:
    """Return the repository's MAIN checkout, or None outside a git repo.

    --git-common-dir resolves to the shared .git directory regardless of which
    worktree we are standing in, so its parent is always the main checkout.
    That is the anchor every other function in this module hangs off: locks,
    config and the worktree inventory are all main-checkout scoped, so two
    sessions in two different worktrees of one repo see the same picture.
    """
    code, out, _ = run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd
    )
    if code != 0 or not out:
        return None
    common = Path(out)
    # A normal repo yields <main>/.git; a bare repo yields the repo dir itself.
    return common.parent if common.name == ".git" else common


def list_worktrees(main: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts.

    Each entry: {path: Path, branch: str|None, locked: bool, is_main: bool}.
    The first record git prints is always the main checkout.
    """
    code, out, _ = run_git(["worktree", "list", "--porcelain"], main)
    if code != 0:
        return []

    entries: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {
                "path": Path(line[len("worktree ") :]),
                "branch": None,
                "locked": False,
            }
        elif current is None:
            continue
        elif line.startswith("branch "):
            # refs/heads/foo -> foo
            current["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
        elif line.startswith("locked"):
            current["locked"] = True
    if current:
        entries.append(current)

    for i, entry in enumerate(entries):
        entry["is_main"] = i == 0
    return entries


def resolve_base_ref(main: Path, config: dict) -> str:
    """Determine the ref that worktree work is measured against.

    Order: explicit wt.json setting -> the remote's default branch -> common
    fallbacks. Everything downstream ("N commits ahead") is relative to this,
    so getting it wrong only distorts the report, never the merge -- the merge
    is the user's job in this workflow.
    """
    remote = config.get("remote", "origin")
    explicit = config.get("baseBranch")
    if explicit:
        return f"{remote}/{explicit}" if "/" not in explicit else explicit

    code, out, _ = run_git(["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], main)
    if code == 0 and out:
        return out

    for candidate in (f"{remote}/main", f"{remote}/master", "main", "master"):
        code, _, _ = run_git(["rev-parse", "--verify", "--quiet", candidate], main)
        if code == 0:
            return candidate
    return "HEAD"


def worktree_state(
    path: Path, base: str, timeout: float = GIT_REPORT_TIMEOUT
) -> dict:
    """Summarise how much unsaved work a worktree holds.

    Three independent quantities, because they have different consequences:
      dirty    -- uncommitted/untracked files; lost outright on removal
      ahead    -- commits this branch has that `base` does not; the actual work
      unpushed -- commits not yet on the branch's upstream; what /done pushes

    `empty` is the only state safe to clean up without asking: nothing to lose.

    `timeout` defaults to the hook-sized limit because the commonest caller is
    the SessionStart inventory, which must finish inside the hook's own budget.
    Callers with time to spare (/done, /wt-list) pass GIT_SCAN_TIMEOUT.

    Both probes must SUCCEED for `measured` to be set. A status that timed out
    would otherwise leave dirty=0 and ahead=0 behind, and those two zeros are
    exactly the pattern the renderer prints as "nothing to lose" -- a failed
    measurement must never be able to produce the reassuring answer.
    """
    state: dict = {
        "exists": path.is_dir(),
        "dirty": 0,
        "ahead": 0,
        "unpushed": None,
        "has_upstream": False,
        # `empty` must never be absent: the renderer treats a falsy value as
        # "nothing to lose", so a state we could not measure has to say so
        # explicitly rather than default into the reassuring answer.
        "empty": False,
        "measured": False,
        "problem": "",
    }
    if not state["exists"]:
        # Directory gone, git metadata left behind. The BRANCH may still hold
        # commits, so this is emphatically not "nothing to lose" -- a real
        # f1-anchors worktree in this repo was reported exactly that way.
        state["problem"] = "directory missing"
        return state

    code, out, err = run_git(["status", "--porcelain"], path, timeout=timeout)
    if code != 0:
        state["problem"] = err or "git status failed"
        return state
    state["dirty"] = len([ln for ln in out.splitlines() if ln.strip()])

    code, out, err = run_git(
        ["rev-list", "--count", f"{base}..HEAD"], path, timeout=timeout
    )
    if code != 0 or not out.isdigit():
        state["problem"] = err or f"cannot count commits against {base}"
        return state
    state["ahead"] = int(out)

    # An upstream may legitimately not exist yet (branch never pushed), so this
    # probe is allowed to fail without spoiling the measurement.
    code, out, _ = run_git(["rev-list", "--count", "@{u}..HEAD"], path, timeout=timeout)
    if code == 0 and out.isdigit():
        state["has_upstream"] = True
        state["unpushed"] = int(out)

    state["empty"] = state["dirty"] == 0 and state["ahead"] == 0
    state["measured"] = True
    return state


# ---------------------------------------------------------------------------
# per-repo configuration
# ---------------------------------------------------------------------------


def find_uv() -> str:
    """Absolute path to uv, for substituting into gate commands.

    Gate commands run through the platform shell -- cmd.exe on Windows -- which
    does not expand `~`. Hard-coding an absolute path into wt.json is worse
    still: that file is committed and travels to machines with a different
    username. Hence the `${UV}` placeholder, resolved here at run time.
    """
    for candidate in (
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".local" / "bin" / "uv",
    ):
        if candidate.is_file():
            return str(candidate).replace("\\", "/")
    import shutil

    found = shutil.which("uv")
    return found.replace("\\", "/") if found else "uv"


def load_config(main: Path) -> dict:
    """Read <main>/.claude/wt.json, the single project-specific file.

    Absent or malformed config is not an error: the workflow degrades to "no
    gate, no protected paths, origin/<default branch>", which is exactly right
    for a repo that has not opted into anything.
    """
    path = main / ".claude" / "wt.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# session locks (the heartbeat)
# ---------------------------------------------------------------------------


def repo_key(main: Path) -> str:
    """Stable, filesystem-safe id for a repository.

    Directory name plus a hash of the resolved path: readable at a glance, but
    still unique when two checkouts share a basename. Path case is normalised
    because Windows hands us the same repo as both c:/ and C:/ depending on how
    the session was launched.
    """
    resolved = str(main.resolve()).replace("\\", "/").lower()
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    return f"{main.name}-{digest}"


def locks_dir(main: Path) -> Path:
    return LOCK_ROOT / repo_key(main)


def session_was_asked(main: Path, session_id: str) -> bool:
    """Has this session already been asked the isolation question?

    The fallback for when SessionStart does not tell us why it fired: without
    it, every context compaction would re-ask a question the user answered
    once already.
    """
    if not session_id:
        return False
    path = locks_dir(main) / f"{session_id}.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("asked"))


def touch_lock(
    main: Path,
    session_id: str,
    cwd: str,
    transcript: str = "",
    asked: bool | None = None,
) -> None:
    """Create or refresh this session's lock.

    Called on SessionStart and on every heartbeat. `cwd` is re-recorded each
    time on purpose: a session that starts in the main checkout and later
    enters a worktree must have its lock follow it, otherwise the worktree
    would look unattended while someone is actively working in it.
    """
    if not session_id:
        return
    directory = locks_dir(main)
    path = directory / f"{session_id}.json"

    # Preserve the `asked` flag across heartbeats unless the caller overrides
    # it, and drop any `ended_at`: a session that is heartbeating is alive
    # again, whatever a stale SessionEnd claimed (fork and rewind both emit
    # one for a session that keeps running).
    previous_asked = False
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            previous_asked = bool(isinstance(old, dict) and old.get("asked"))
        except (OSError, ValueError):
            pass

    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "cwd": str(cwd),
            "transcript_path": transcript,
            "updated_at": time.time(),
            "asked": previous_asked if asked is None else asked,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass  # A lock we cannot write only costs us report accuracy.


def mark_ended(main: Path, session_id: str) -> None:
    """Record that this session has ended (SessionEnd).

    Deliberately a tombstone rather than a deletion. A deleted lock is
    indistinguishable from a lock that never existed, and those two states
    mean opposite things: "this tab was closed" versus "we have no idea".
    Keeping the record with an `ended_at` stamp is what lets a closed tab be
    reported as closed instead of merely unknown.
    """
    if not session_id:
        return
    path = locks_dir(main) / f"{session_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["session_id"] = session_id
    payload["ended_at"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def drop_lock(main: Path, session_id: str) -> None:
    """Delete a lock outright (pruning only)."""
    if not session_id:
        return
    try:
        (locks_dir(main) / f"{session_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def read_locks(main: Path) -> list[dict]:
    """Return every lock for this repo, each with an added `age_seconds`."""
    directory = locks_dir(main)
    if not directory.is_dir():
        return []
    locks: list[dict] = []
    now = time.time()
    for entry in directory.glob("*.json"):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data["age_seconds"] = max(0.0, now - float(data.get("updated_at", 0)))
        data["ended"] = "ended_at" in data
        data["end_age_seconds"] = (
            max(0.0, now - float(data["ended_at"])) if data["ended"] else None
        )
        locks.append(data)
    return locks


def _slug_key(text: str | Path) -> str:
    """Collapse a path or directory name into a comparable key.

    Claude Code derives its transcript directory names from the working
    directory by substituting punctuation, and the exact rule is neither
    documented nor stable (it even preserves the drive letter's case, which
    varies between launches). Rather than reconstruct the rule, both sides are
    reduced to lowercase alphanumeric runs joined by single dashes -- any
    substitution scheme collapses to the same key.
    """
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def transcript_age(path: Path) -> float | None:
    """Seconds since the newest session transcript for `path`, or None.

    None means "no transcript directory found" -- genuinely no information,
    which is a different answer from "last touched a long time ago".
    """
    if not PROJECTS_ROOT.is_dir():
        return None
    try:
        key = _slug_key(path.resolve())
    except OSError:
        key = _slug_key(path)

    try:
        candidates = list(PROJECTS_ROOT.iterdir())
    except OSError:
        return None

    newest: float | None = None
    for directory in candidates:
        if not directory.is_dir() or _slug_key(directory.name) != key:
            continue
        for transcript in directory.glob("*.jsonl"):
            try:
                stamp = transcript.stat().st_mtime
            except OSError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
    return None if newest is None else max(0.0, time.time() - newest)


def prune_locks(main: Path, max_age_days: int = 7) -> None:
    """Drop locks so old they can only be debris from crashed sessions."""
    cutoff = max_age_days * 86400
    for lock in read_locks(main):
        if lock["age_seconds"] > cutoff:
            drop_lock(main, lock.get("session_id", ""))


def paths_equal(a: str | Path, b: str | Path) -> bool:
    """Compare two paths tolerantly (Windows drive-letter case, separators)."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a).replace("\\", "/").lower() == str(b).replace("\\", "/").lower()


def path_within(child: str | Path, parent: str | Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# the inventory that /wt-list and SessionStart both render
# ---------------------------------------------------------------------------


def collect_inventory(main: Path, current_cwd: str) -> dict:
    """Build the full picture of this repo's worktrees and their liveness.

    Returns {"base": str, "current": Path|None, "worktrees": [...]}, where each
    worktree carries its git state plus an `activity` verdict:

      active  -- a session lock inside it was refreshed within staleMinutes
      idle    -- no fresh lock; the tab is probably closed
      current -- this very session is sitting in it

    "idle" is a hint, never a verdict: a tab left open and untouched for hours
    looks identical from here. Nothing is ever removed on the strength of it.
    """
    config = load_config(main)
    stale_seconds = float(config.get("staleMinutes", DEFAULT_STALE_MINUTES)) * 60
    base = resolve_base_ref(main, config)
    locks = read_locks(main)

    current_wt: Path | None = None
    rows: list[dict] = []
    for entry in list_worktrees(main):
        if entry["is_main"]:
            continue
        path = entry["path"]
        is_current = paths_equal(path, current_cwd) or path_within(current_cwd, path)
        if is_current:
            current_wt = path

        # Locks are matched by containment, not equality, so a session that has
        # cd'd into a subdirectory of its worktree still counts as attending it.
        own = [
            lock
            for lock in locks
            if lock.get("cwd")
            and (paths_equal(lock["cwd"], path) or path_within(lock["cwd"], path))
        ]
        live = [
            lock
            for lock in own
            if not lock["ended"] and lock["age_seconds"] <= stale_seconds
        ]
        ends = [lock["end_age_seconds"] for lock in own if lock["ended"]]
        newest_end = min(ends) if ends else None
        t_age = transcript_age(path)

        # Two independent liveness signals, consulted in order of certainty.
        # Our own heartbeat is the most direct, but it only exists for sessions
        # that ran after this workflow was installed -- transcript mtime covers
        # everything else, including worktrees that predate it entirely.
        if is_current:
            activity, source = "current", "this session"
        elif live:
            activity, source = "active", "heartbeat"
        elif newest_end is not None and (t_age is None or newest_end <= t_age):
            activity, source = "closed", "SessionEnd"
        elif t_age is not None and t_age <= stale_seconds:
            activity, source = "active", f"transcript {t_age / 60:.0f} min ago"
        elif t_age is not None:
            activity, source = "idle", f"transcript {t_age / 3600:.0f} h ago"
        elif own:
            activity, source = "idle", "heartbeat gone stale"
        else:
            # No heartbeat, no transcript. This is genuinely "no information",
            # and it must not be reported as abandonment.
            activity, source = "unknown", "no session data"

        row = {
            "path": path,
            "branch": entry["branch"],
            "locked": entry["locked"],
            "activity": activity,
            "source": source,
        }
        row.update(worktree_state(path, base))
        rows.append(row)

    return {"base": base, "current": current_wt, "worktrees": rows, "config": config}


def format_inventory(inventory: dict, include_current: bool = False) -> str:
    """Render the inventory as a compact ASCII table, or '' when there is
    nothing worth reporting."""
    rows = [
        row
        for row in inventory["worktrees"]
        if include_current or row["activity"] != "current"
    ]
    if not rows:
        return ""

    lines = []
    for row in rows:
        bits = []
        if not row.get("measured"):
            # Never claim safety about a worktree we could not inspect. The
            # branch can still carry commits even when the directory is gone.
            # Two different unmeasured states, and conflating them would be a
            # lie in one direction or the other: a missing directory is a fact
            # about the worktree, a timed-out probe is a fact about us.
            if row.get("exists"):
                bits.append(
                    f"COULD NOT MEASURE ({row.get('problem') or 'unknown reason'}) "
                    "- state unknown, inspect it before removing anything"
                )
            else:
                bits.append(
                    "DIRECTORY MISSING - git metadata only; the branch may still "
                    "hold commits, so inspect it before removing anything"
                )
        else:
            if row["dirty"]:
                bits.append(f"{row['dirty']} uncommitted")
            if row["ahead"]:
                bits.append(f"{row['ahead']} commits ahead of {inventory['base']}")
            if row.get("has_upstream") and row.get("unpushed"):
                bits.append(f"{row['unpushed']} unpushed")
            elif not row.get("has_upstream") and row["ahead"]:
                bits.append("never pushed")
            if not bits:
                bits.append("empty - nothing to lose")

        marker = {
            "current": "[THIS SESSION]",
            "active": "[IN USE - do not touch]",
            "closed": "[closed - session ended]",
            "idle": "[quiet - ask before touching]",
            "unknown": "[unknown - no data, ask]",
        }[row["activity"]]

        lines.append(f"  {row['branch'] or '(detached)'} {marker}")
        lines.append(f"    {row['path']}")
        lines.append(f"    {', '.join(bits)}  ({row['source']})")
    return "\n".join(lines)


def log_event(event: str, detail: str = "") -> None:
    """Append one line to the lifecycle log (best effort, never fatal)."""
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with EVENT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{event}\t{detail}\n")
    except OSError:
        pass


def read_hook_input() -> dict:
    """Read the hook's JSON payload from stdin, tolerating an empty stream."""
    import sys

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def env_pid_note() -> str:
    """Small provenance string for the event log."""
    return f"pid={os.getpid()}"
