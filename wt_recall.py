#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Lift the substance of a past conversation into the session running right now.

Why this exists
---------------
Claude Code files a session transcript under its WORKING DIRECTORY, and the
recent-conversations picker is built from the current directory's file alone.
EnterWorktree moves a session's working directory mid-flight, so a conversation
that entered a worktree cannot be seen from the main checkout. /wt-list prints
the id and a `claude --resume` command for it -- but resuming needs a terminal,
and the VS Code extension exposes no command for it (its manifest carries three
commands, all about diffs). So on that setup "reopen the conversation" always
means leaving the editor.

This script answers the other half of the question. It does not reopen the
conversation -- it READS it and prints what was in it, so the assistant already
in front of the user can pick the work up without anybody opening a terminal.
What it gives up is exactness: a new session with a digest, not the original
session with its literal history. What it keeps is everything that decided the
work -- every prompt the human typed, in order, plus what was done to the repo
and how it ended.

The transcript is never written to. Reading one cannot disturb a live tab.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wt_lib  # noqa: E402  (also makes the console tolerate non-ASCII echoes)

# A prompt is quoted up to this length before being cut. Long pasted blobs --
# task briefs, log dumps -- are what the cut is for; the opening sentences of
# one carry its intent, and the rest costs context the reader needs elsewhere.
PROMPT_CHARS = 1200

# Tool calls whose target is worth naming. Reads are deliberately absent: a
# list of everything looked at is long and says little, where a list of what
# was CHANGED is the conversation's effect on the repository.
WRITE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}

# Records that wear the user's role but say only that the user pressed Escape.
# They are real events, but they are not things the user ASKED for, and in a
# session driven by one long brief they outnumbered the actual prompts 2 to 1.
INTERRUPTION_MARKERS = (
    "[Request interrupted by user",
    "[Request cancelled by user",
)

# Scratch scripts written to the session's temp directory are counted apart
# from the repository. They are how the work was done, not what was produced,
# and on one measured session they were 14 of the 20 "files changed" -- enough
# to bury the two documents that were the actual output.
TEMP_ROOT = Path(tempfile.gettempdir())

# A week-long session can touch hundreds of files. Past this many the list stops
# informing and starts costing context, so it is cut and the remainder counted.
FILE_ROWS = 40


def iter_records(transcript: Path):
    """Stream a transcript record by record.

    Line by line rather than json.load of the whole file: these reach tens of
    megabytes, and nothing here needs two records at once.
    """
    with transcript.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def find_transcript(needle: str) -> list[Path]:
    """Every transcript whose file name starts with `needle`.

    A prefix, because the id a human has in front of them is usually the short
    8-character form printed beside a session lock. Returning ALL matches and
    letting the caller refuse an ambiguous one is the point -- silently picking
    the first would hand back somebody else's conversation.
    """
    root = wt_lib.PROJECTS_ROOT
    if not root.is_dir():
        return []
    needle = needle.lower()
    hits = []
    for bucket in root.iterdir():
        if not bucket.is_dir():
            continue
        for transcript in bucket.glob("*.jsonl"):
            if transcript.stem.lower().startswith(needle):
                hits.append(transcript)
    return sorted(hits)


def text_of(message: dict) -> str:
    """Concatenate the text blocks of one message, ignoring everything else."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def is_typed_prompt(record: dict) -> bool:
    """Did a human type this, or did the harness put it there?

    Four things wear the `user` role without a human behind them: sub-agent
    turns (`isSidechain`), harness bookkeeping (`isMeta`), tool results, and
    the reminder / selection blocks that arrive wrapped in angle brackets.
    """
    if record.get("type") != "user" or record.get("isSidechain"):
        return False
    if record.get("isMeta"):
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
    text = text_of(message)
    if not text or text.startswith("<"):
        return False
    return not text.startswith(INTERRUPTION_MARKERS)


def clip(text: str, limit: int) -> str:
    """Collapse a prompt to one paragraph, cut long ones, mark the cut."""
    collapsed = "\n".join(
        line.rstrip() for line in text.splitlines() if line.strip()
    )
    if limit <= 0 or len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " …"


def harvest(transcript: Path, prompt_chars: int) -> dict:
    """One pass over a transcript, collecting everything the report needs."""
    prompts: list[dict] = []
    touched: dict[str, int] = {}
    commands: list[str] = []
    last_assistant = ""
    titles: dict[str, str] = {}
    first_stamp = last_stamp = ""
    # Ordered set of the working directories the session passed through, keyed
    # on a normalised spelling. Claude Code writes the drive letter in either
    # case from one record to the next, so comparing raw strings -- or only
    # against the previous entry -- turns one move into hundreds of rows: on an
    # 88 MB transcript it produced a list longer than the rest of the report.
    cwds: dict[str, str] = {}

    for record in iter_records(transcript):
        kind = record.get("type")

        if kind in ("custom-title", "ai-title"):
            value = str(
                record.get("customTitle") or record.get("aiTitle") or ""
            ).strip()
            if value:
                titles[kind] = value  # last one wins: titles are re-appended
            continue

        stamp = str(record.get("timestamp", ""))
        if stamp:
            first_stamp = first_stamp or stamp
            last_stamp = stamp

        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            cwds.setdefault(cwd.replace("\\", "/").lower(), cwd)

        if is_typed_prompt(record):
            prompts.append(
                {
                    "when": stamp,
                    "text": clip(text_of(record["message"]), prompt_chars),
                }
            )
            continue

        if kind != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = text_of(message)
        if text:
            last_assistant = text
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name in WRITE_TOOLS:
                path = str(args.get("file_path") or args.get("notebook_path") or "")
                if path:
                    touched[path] = touched.get(path, 0) + 1
            elif name in ("Bash", "PowerShell"):
                command = " ".join(str(args.get("command", "")).split())
                if command:
                    commands.append(command)

    return {
        "prompts": prompts,
        "touched": touched,
        "commands": commands,
        "last_assistant": last_assistant,
        "title": titles.get("custom-title") or titles.get("ai-title") or "",
        "title_source": "custom-title" if "custom-title" in titles else (
            "ai-title" if "ai-title" in titles else "none"
        ),
        "first_stamp": first_stamp,
        "last_stamp": last_stamp,
        "cwds": list(cwds.values()),
    }


def render(transcript: Path, data: dict, limit: int, tail_chars: int) -> list[str]:
    """Format the harvest as Markdown for the assistant reading this output."""
    size = transcript.stat().st_size
    lines = [
        f"# Recalled session {transcript.stem}",
        "",
        f"- transcript: {transcript}",
        f"- recorded for working directory: {transcript.parent.name}",
        f"- size: {size / 1048576:.1f} MB",
    ]
    if data["title"]:
        lines.append(f"- title: {data['title']}  [{data['title_source']}]")
    if data["first_stamp"]:
        lines.append(f"- first record: {data['first_stamp']}")
    if data["last_stamp"]:
        lines.append(f"- last record:  {data['last_stamp']}")
    if data["cwds"]:
        # More than one means the session MOVED -- normally EnterWorktree. It
        # is worth showing, because it tells the reader which tree the work
        # actually landed in.
        lines.append("- working directories seen, in order:")
        for path in data["cwds"]:
            lines.append(f"  - {path}")

    prompts = data["prompts"]
    shown = prompts if limit <= 0 else prompts[-limit:]
    lines += ["", f"## What the user asked ({len(prompts)} prompts)", ""]
    if len(shown) < len(prompts):
        lines.append(
            f"_Showing the last {len(shown)}; "
            f"{len(prompts) - len(shown)} earlier omitted — "
            f"rerun with --prompts 0 for all._"
        )
        lines.append("")
    if not shown:
        lines.append("_No typed prompts found._")
    for index, prompt in enumerate(shown, start=len(prompts) - len(shown) + 1):
        when = prompt["when"][:19].replace("T", " ") if prompt["when"] else "?"
        lines += [f"**{index}. [{when}]**", "", prompt["text"], ""]

    touched = data["touched"]
    real = {p: n for p, n in touched.items() if not wt_lib.path_within(p, TEMP_ROOT)}
    scratch = len(touched) - len(real)
    lines += ["", f"## Files changed ({len(real)})", ""]
    if real:
        # Ordered by how often each file was written, so a long tail of
        # one-touch files cannot push the file the session actually worked on
        # out of view when the list is cut.
        ranked = sorted(real.items(), key=lambda kv: -kv[1])
        for path, count in ranked[:FILE_ROWS]:
            lines.append(f"- {path}  ({count}x)")
        if len(ranked) > FILE_ROWS:
            lines.append(f"- … and {len(ranked) - FILE_ROWS} more")
    else:
        lines.append("_No write-tool calls outside the scratch directory._")
    if scratch:
        lines.append("")
        lines.append(f"_Plus {scratch} throwaway script(s) in the scratch directory._")

    commands = data["commands"]
    lines += ["", f"## Commands run ({len(commands)}, last 20 shown)", ""]
    if commands:
        for command in commands[-20:]:
            lines.append(f"- `{clip(command, 200)}`")
    else:
        lines.append("_None recorded._")

    lines += ["", "## How it ended", "", clip(data["last_assistant"], tail_chars)
              or "_No closing assistant message._"]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print the substance of a past Claude Code session."
    )
    parser.add_argument(
        "session",
        help="session id, or any prefix of it (the 8-character short form works)",
    )
    parser.add_argument(
        "--prompts",
        type=int,
        default=40,
        help="how many of the most recent prompts to quote; 0 means all "
        "(default: 40)",
    )
    parser.add_argument(
        "--chars",
        type=int,
        default=PROMPT_CHARS,
        help=f"characters quoted per prompt; 0 means whole "
        f"(default: {PROMPT_CHARS})",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=2000,
        help="characters of the closing assistant message (default: 2000)",
    )
    parser.add_argument(
        "--out",
        help="write the report to this file instead of stdout",
    )
    args = parser.parse_args()

    hits = find_transcript(args.session)
    if not hits:
        print(f"No transcript starts with {args.session!r}.")
        print("NOK: nothing to recall")
        return 1

    # One SESSION can own files in two buckets, because entering a worktree
    # moves the session and leaves a stub behind in the directory it came
    # from. That is not ambiguity -- even a complete id matches both -- so
    # same-id matches collapse to the largest file, which is the one holding
    # the conversation. Only genuinely different ids are refused.
    by_id: dict[str, list[Path]] = {}
    for hit in hits:
        by_id.setdefault(hit.stem.lower(), []).append(hit)
    if len(by_id) > 1:
        print(f"{args.session!r} matches {len(by_id)} different sessions:")
        for stem, files in sorted(by_id.items()):
            largest = max(files, key=lambda item: item.stat().st_size)
            print(
                f"  {largest.stem}  {largest.stat().st_size / 1048576:.1f} MB"
                f"  in {largest.parent.name}"
            )
        print("NOK: ambiguous -- give more characters of the id")
        return 1

    candidates = next(iter(by_id.values()))
    transcript = max(candidates, key=lambda item: item.stat().st_size)
    if len(candidates) > 1:
        print(
            f"Note: this session also left {len(candidates) - 1} smaller "
            f"file(s) behind in the directory it started in; reading the "
            f"largest."
        )
    started = time.time()
    data = harvest(transcript, args.chars)
    report = "\n".join(render(transcript, data, args.prompts, args.tail))
    # Counted the same way the report counts it, or the footer contradicts the
    # section above it -- the split between repository files and scratch files
    # is exactly what makes the two numbers differ.
    repo_files = sum(
        1 for path in data["touched"] if not wt_lib.path_within(path, TEMP_ROOT)
    )

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report + "\n", encoding="utf-8", newline="\n")
        print(f"Wrote {len(report)} characters to {target}")
    else:
        print(report)

    print()
    print(
        f"Read {transcript.stat().st_size / 1048576:.1f} MB in "
        f"{time.time() - started:.2f}s: {len(data['prompts'])} prompts, "
        f"{repo_files} repository files changed, "
        f"{len(data['commands'])} commands."
    )
    print(f"OK: recalled {transcript.stem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
