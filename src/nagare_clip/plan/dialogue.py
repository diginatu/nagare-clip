"""The ``plan_revise`` stage's conversation with the human editor.

A single append-only markdown file — ``output/plan_dialogue/history.md`` — holds
alternating turns.  ``plan_revise`` reads the turns since the last divider and
appends its own reply; it fires only while a human turn is unanswered.

``plan`` writes into the file too, but only a **divider**: a plan re-run rebuilds
the directions the turns above it refer to, so those turns stop being applied —
divided, not deleted, because the record is often still worth copying down.

The on-disk format is deliberately forgiving: role headings (``## human`` /
``## plan``) in plain markdown, so opening the file in an editor and typing
works as well as the helper script.  Anything unparseable degrades to text the
LLM can still read rather than to an error that loses the run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HUMAN = "human"
PLAN = "plan"

# Its own output subdir, not plan/: a stage rewrites its own directory wholesale,
# and the conversation must survive that.  It lives under the output tree because
# every turn refers to line ranges that only this summary run defines.
DIALOGUE_DIR = "plan_dialogue"
HISTORY_NAME = "history.md"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(rf"^\s{{0,3}}#{{1,6}}\s*({HUMAN}|{PLAN})\b.*$", re.IGNORECASE)

# A ``plan`` re-run divides the conversation rather than clearing it: the turns
# above the last divider refer to directions that run may no longer have
# produced, so they stop being applied — but they stay readable, and a still
# valid instruction can be copied down instead of being reconstructed.
_DIVIDER_RE = re.compile(r"^\s{0,3}-{3,}\s*plan re-ran\b.*$", re.IGNORECASE)

FILE_HEADER = (
    "<!--\n"
    "plan_dialogue/history.md — the plan_revise stage's conversation with you.\n"
    "\n"
    "Append a turn under a '## human' heading and re-run\n"
    "  ./scripts/run_pipeline.sh --from-stage plan_revise --to-stage plan_revise\n"
    "(one LLM call).  plan_revise revises only the directions the conversation\n"
    "calls for and appends its own '## plan' reply.  With nothing unanswered it\n"
    "makes no call at all.\n"
    "\n"
    "Do NOT re-run the plan stage to apply a turn: plan rebuilds the whole plan\n"
    "from the summaries and retires the turns above it (see the divider below).\n"
    "\n"
    "Refer to parts by source stem and line range, e.g. 'foo [31,83]'.\n"
    "\n"
    "A '--- plan re-ran ... ---' line divides the log: only the turns below\n"
    "the last one are still applied.  Copy an older instruction down if it\n"
    "still holds.\n"
    "-->\n"
)


def history_path(output_dir: Path | str) -> Path:
    return Path(output_dir) / DIALOGUE_DIR / HISTORY_NAME


@dataclass(frozen=True)
class DialogueTurn:
    role: str  # "human" | "plan"
    text: str


def parse_history(text: str) -> list[DialogueTurn]:
    """Split a history file into turns.  Never raises."""
    body = _COMMENT_RE.sub("", text or "")
    turns: list[DialogueTurn] = []
    role = HUMAN  # text before the first heading is the human typing
    buf: list[str] = []

    def flush() -> None:
        content = "\n".join(buf).strip()
        if content:
            turns.append(DialogueTurn(role, content))
        buf.clear()

    for line in body.splitlines():
        if _DIVIDER_RE.match(line):
            continue
        match = _HEADING_RE.match(line)
        if match:
            flush()
            role = match.group(1).lower()
            continue
        buf.append(line)
    flush()
    return turns


def read_history(path: Path | None) -> list[DialogueTurn]:
    if path is None:
        return []
    try:
        return parse_history(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def format_divider(when: datetime | None = None) -> str:
    """The line a ``plan`` re-run writes to retire the turns above it."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%dT%H:%M")
    return f"--- plan re-ran {stamp} — turns above this line no longer apply ---"


def active_turns(text: str) -> list[DialogueTurn]:
    """The turns after the last divider — what ``plan_revise`` still applies."""
    body = _COMMENT_RE.sub("", text or "")
    lines = body.splitlines()
    last = -1
    for i, line in enumerate(lines):
        if _DIVIDER_RE.match(line):
            last = i
    return parse_history("\n".join(lines[last + 1 :]))


def read_active_history(path: Path | None) -> list[DialogueTurn]:
    if path is None:
        return []
    try:
        return active_turns(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def has_unanswered_human(turns: list[DialogueTurn]) -> bool:
    """Whether the human spoke last — the structural firing condition.

    ``plan_revise`` answers every turn it acts on, so "the last turn is the
    human's" is exactly "there is something not yet answered".
    """
    return bool(turns) and turns[-1].role == HUMAN


def format_turn(role: str, text: str) -> str:
    return f"## {role}\n\n{text.strip()}\n"


def append_turn(path: Path, role: str, text: str) -> None:
    """Append one turn, creating the file (with its header) if needed."""
    path = Path(path)
    body = format_turn(role, text)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_text(FILE_HEADER, encoding="utf-8")
        existing = path.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n") or not existing else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{sep}\n{body}")
    except OSError as e:
        logger.warning("plan: could not append to %s: %s", path, e)


def append_divider(path: Path, when: datetime | None = None) -> None:
    """Retire the turns written so far, leaving a heading to reply under.

    Nothing is appended when nothing has been said since the last divider, so
    repeated ``plan`` runs do not pile dividers up.  The file is still created,
    so a human can always find where to write.
    """
    path = Path(path)
    ensure_history(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("plan: could not read %s: %s", path, e)
        return
    if not active_turns(existing):
        return
    try:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{sep}\n{format_divider(when)}\n\n## {HUMAN}\n")
    except OSError as e:
        logger.warning("plan: could not append to %s: %s", path, e)


def ensure_history(path: Path) -> None:
    """Create the history file (header only) so a human can find it."""
    path = Path(path)
    if path.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FILE_HEADER, encoding="utf-8")
    except OSError as e:
        logger.warning("plan: could not create %s: %s", path, e)


def render_history(turns: list[DialogueTurn]) -> str:
    return "\n\n".join(format_turn(t.role, t.text).rstrip("\n") for t in turns)


def main(argv: list[str] | None = None) -> int:
    """Append a human turn to the plan conversation (see scripts/plan_say.sh)."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="nagare-clip plan-say",
        description="Append a turn to the plan conversation, then re-run the "
        "plan_revise stage to have it applied.",
    )
    parser.add_argument("text", nargs="*", help="What to tell plan_revise (default: read stdin)")
    parser.add_argument("--config", default=None, help="Path to YAML config file")
    parser.add_argument("--output-dir", default=None, dest="output_dir")
    parser.add_argument(
        "--role",
        default=HUMAN,
        choices=[HUMAN, PLAN],
        help="Whose turn this is (default: human)",
    )
    args = parser.parse_args(argv)

    text = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not text:
        print("Nothing to say (empty turn); nothing written.", file=sys.stderr)
        return 1

    output_dir = args.output_dir
    if output_dir is None:
        from nagare_clip.config import get_effective_config

        config_path = Path(args.config).resolve() if args.config else None
        output_dir = get_effective_config(config_path, {})["pipeline"]["output_dir"]

    path = history_path(output_dir)
    append_turn(path, args.role, text)
    print(f"Appended a {args.role} turn to {path}")
    print("Re-run: ./scripts/run_pipeline.sh --from-stage plan_revise --to-stage plan_revise")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
