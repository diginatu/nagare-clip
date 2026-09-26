"""``director/conversation.md``: the director's conversation, and its done mark.

The director is a step over its own directory (``plan.md``, ``order.json``,
``{stem}_director.json`` and this file).  This file holds what no other file
can: the model's replies verbatim, the pipeline's one-line guidance before each
of them, anything a person wrote, and the done mark::

    ## guide
    Review around lines 1 to 8.

    ## director
    {"range": [1, 8], "reviewed_through": 8, "ops": [...]}

    ## done

    ## editor
    冒頭のあいさつは残して

A run does nothing while a ``done`` entry exists; deleting it is how anyone —
person or script — asks for more.  Nobody is special: an ``editor`` entry is
one more user-side entry, labelled like the pipeline's own ``guide`` entries.

Forgiving to read: an unknown heading, or text before the first heading, is an
``editor`` entry (a person wrote it); blank entries are dropped; nothing raises.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.director.director_llm import strip_code_fence

FILE_NAME = "conversation.md"

GUIDE = "guide"
DIRECTOR = "director"
EDITOR = "editor"
DONE = "done"

#: How a user-side entry is labelled in the message the model reads.  The
#: director is the assistant role and carries no label.
LABELS = {GUIDE: "Guide", EDITOR: "Editor"}

_HEADING = re.compile(r"^##[ \t]+(\S.*?)[ \t]*$")


@dataclass(frozen=True)
class Entry:
    speaker: str
    text: str = ""


def parse(text: str) -> list[Entry]:
    """Every entry of *text*, in order.  Never raises."""
    entries: list[Entry] = []
    speaker = EDITOR
    body: list[str] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        if speaker == DONE or content:
            entries.append(Entry(speaker, content))

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            name = m.group(1).strip().lower()
            speaker = name if name in (GUIDE, DIRECTOR, EDITOR, DONE) else EDITOR
            body = []
        else:
            body.append(line)
    flush()
    return entries


def render(entries: list[Entry]) -> str:
    """*entries* as the file holds them; :func:`parse` reads it back."""
    blocks = [f"## {e.speaker}" + (f"\n{e.text}" if e.text else "") for e in entries]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def load(path: Path) -> list[Entry]:
    try:
        return parse(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def save(path: Path, entries: list[Entry]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(entries), encoding="utf-8")


def is_done(entries: list[Entry]) -> bool:
    """Is there a done mark?  Anywhere: deleting it is the only way on."""
    return any(e.speaker == DONE for e in entries)


def reviewed_through(entries: list[Entry]) -> int:
    """The furthest line any reply of the director said it reviewed."""
    best = 0
    for e in entries:
        if e.speaker != DIRECTOR:
            continue
        try:
            data = json.loads(strip_code_fence(e.text))
        except (ValueError, TypeError):
            continue
        value = data.get("reviewed_through") if isinstance(data, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            best = max(best, value)
    return best


def messages(entries: list[Entry], live: str = "") -> list[dict[str, str]]:
    """The conversation as API messages (the system message not included).

    A ``director`` entry is an assistant message.  Each run of other entries
    becomes ONE user message, every part labelled (``Guide: …``,
    ``Editor: …``), so roles always alternate however the entries fell.
    *live* is the full guidance for the turn about to be taken; it joins the
    trailing user message as its last ``Guide:`` part.
    """
    out: list[dict[str, str]] = []
    parts: list[str] = []

    def flush() -> None:
        if parts:
            out.append({"role": "user", "content": "\n\n".join(parts)})
            parts.clear()

    for e in entries:
        if e.speaker == DONE:
            continue
        if e.speaker == DIRECTOR:
            flush()
            out.append({"role": "assistant", "content": e.text})
        else:
            parts.append(f"{LABELS.get(e.speaker, 'Editor')}: {e.text}")
    if live:
        parts.append(f"{LABELS[GUIDE]}: {live}")
    flush()
    return out


def say(path: Path, text: str) -> None:
    """Remove the done mark and append an ``editor`` entry — what a person does."""
    entries = [e for e in load(path) if e.speaker != DONE]
    entries.append(Entry(EDITOR, text.strip()))
    save(path, entries)


def main(argv: list[str] | None = None) -> int:
    """Tell the director something (see scripts/director_say.sh)."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="nagare-clip director-say",
        description="Remove the director's done mark and append an editor entry, "
        "then re-run the director stage to have it answered.",
    )
    parser.add_argument("text", nargs="*", help="What to tell the director (default: stdin)")
    parser.add_argument("--config", default=None, help="Path to YAML config file")
    parser.add_argument("--output-dir", default=None, dest="output_dir")
    args = parser.parse_args(argv)

    text = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not text:
        print("Nothing to say (empty entry); nothing written.", file=sys.stderr)
        return 1

    output_dir = args.output_dir
    if output_dir is None:
        from nagare_clip.config import get_effective_config

        config_path = Path(args.config).resolve() if args.config else None
        output_dir = get_effective_config(config_path, {})["pipeline"]["output_dir"]

    path = Path(output_dir) / "director" / FILE_NAME
    say(path, text)
    print(f"Appended an editor entry to {path} (done mark removed)")
    print("Re-run: ./scripts/run_pipeline.sh --from-stage director")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
