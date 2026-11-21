"""``python -m nagare_clip.publish.tools {view,frames,chapters,write,render}``.

Deterministic, no LLM call.  Line numbers are the director's display numbering;
every second is computed here from the finished timeline, never carried by the
edit.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from nagare_clip.brief import format_brief
from nagare_clip.broken_pipe import silence_stdout
from nagare_clip.config import get_effective_config
from nagare_clip.director.files import read_ops, read_plan
from nagare_clip.director.load import project_context
from nagare_clip.director.played import played_transcript
from nagare_clip.director.tools.project import Project, current_state, load_project
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.external import run_magick
from nagare_clip.publish.chapters import _spans, format_timestamp
from nagare_clip.publish.document import render_publish_markdown
from nagare_clip.publish.timeline import total_duration
from nagare_clip.publish.tools.build import build_publish
from nagare_clip.publish.tools.edit import (
    ChapterRef,
    chapter_count_warning,
    resolve_chapters,
    validate_edit,
)
from nagare_clip.publish.tools.frames import (
    Still,
    extract_stills,
    format_stills,
    plan_stills,
    read_stills,
    write_stills,
)
from nagare_clip.publish.tools.lines import finished_placements, line_time
from nagare_clip.render.run import run_render
from nagare_clip.render.thumbnail import fallback_font

CHAPTER_TEXT_CHARS = 40

EDIT_HELP = (
    'A publish edit is one JSON object: {"titles": [...], "lead": "...", '
    '"chapters": [{"line": N, "title": "..."}], "thumbnails": [{"background": '
    '"frames/<stem>/<t>.jpg", "lines": [{"text": "...", "role"?: ...}]}]}. '
    "Lines are display numbers from `view`; the tool computes every time."
)


def _parse_lines(value: str) -> tuple[int, int]:
    try:
        a, _, b = value.partition("-")
        first, last = int(a), int(b or a)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected A-B, got {value!r}") from None
    if first < 1 or last < first:
        raise argparse.ArgumentTypeError(f"expected 1 <= A <= B, got {value!r}")
    return first, last


def _parse_line_pos(value: str) -> tuple[int, float]:
    number, _, pos = value.partition("@")
    try:
        n = int(number)
        p = float(pos) if pos else 0.5
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected N or N@pos, got {value!r}") from None
    if n < 1 or not 0.0 <= p <= 1.0:
        raise argparse.ArgumentTypeError(f"expected N>=1 and pos in [0, 1], got {value!r}")
    return n, p


def _load_cfg(paths: list[str] | None) -> dict:
    return get_effective_config([Path(c).resolve() for c in paths or []], {})


def _read_edit(source: str) -> Any:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(text)


def _caption_time(project: Project, placements, stem: str, op) -> tuple[float | None, str]:
    """``(finished seconds | None, label)`` of the caption's first display line."""
    if placements is None:
        return None, "—"
    index = project.segment_index(stem)
    number = None
    if index is not None:
        number = project.view.from_source(index, op.lines[0], silence=op.gap_start)
    if number is None:
        return None, "—"
    t = line_time(placements, project, number)
    return (None, "✂") if t is None else (t, format_timestamp(t))


def _captions(project: Project, placements) -> list[str]:
    """``M:SS  text`` rows in playback order (a text listed once, at its first use)."""
    stems = list(dict.fromkeys(line.stem for line in project.view.lines))
    if placements:
        played = list(dict.fromkeys(p.stem for p in placements))
        stems = [s for s in played if s in stems] + [s for s in stems if s not in played]
    ops_by_stem = read_ops(project.ctx, stems)
    seen: set[str] = set()
    rows: list[tuple[float | None, str, str]] = []
    for stem in stems:
        for op in ops_by_stem.get(stem, []):
            if op.type not in ("overlay", "timelapse"):
                continue
            text = (op.text or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            t, label = _caption_time(project, placements, stem, op)
            rows.append((t, label, text))
    rows.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0.0))
    return [f"{label}  {text}" for _, label, text in rows]


def _fonts_section(fonts: dict[str, str]) -> str:
    head = "## Fonts (render.fonts — the `font` values a thumbnail line may name)"
    if not fonts:
        return (
            head + "\nnone set — a line uses ImageMagick's default face, which may draw "
            "Japanese as nothing; ask the user to set render.fonts"
        )
    fallback = fallback_font(fonts)
    rows = []
    marked = False
    for slot, face in fonts.items():
        row = f"- {slot}: {face}"
        if not marked and face == fallback:
            row += "   (the fallback for a line with no font)"
            marked = True
        rows.append(row)
    return head + "\n" + "\n".join(rows)


def _when(project: Project, placements):
    """display line -> its finished time as text (``—`` unknown, ``✂`` cut)."""

    def when(number: int) -> str:
        if placements is None:
            return "—"
        t = line_time(placements, project, number)
        return "✂" if t is None else format_timestamp(t)

    return when


def _compact_rows(project: Project, placements) -> str | None:
    """The played transcript with finished times; ``None`` (reason on stderr)
    when the director files on disk do not map onto this view."""
    state = current_state(project)
    if state.unmapped:
        print(
            f"warning: {len(state.unmapped)} item(s) in the director files do not map onto "
            "this view, so --compact cannot show the finished video; showing every line:",
            file=sys.stderr,
        )
        for note in state.unmapped:
            print(f"  {note}", file=sys.stderr)
        return None
    return played_transcript(
        project.view,
        project.transcripts,
        state.ops,
        order=state.order,
        when=_when(project, placements),
    )


def cmd_view(project: Project, args: argparse.Namespace) -> int:
    view = project.view
    total = len(view.lines)
    first, last = args.lines or (1, total)
    if first > total:
        print(f"the video has {total} display lines", file=sys.stderr)
        return 2
    last = min(last, total)
    ctx = project.ctx
    placements = finished_placements(project)
    parts: list[str] = [f"output dir: {ctx.output_dir}"]
    brief = format_brief(ctx.cfg.get("project"))
    if brief:
        parts.append(brief)
    context = project_context(ctx.stage_dir("summary") / "summary.json", view)
    if context:
        parts.append(context)
    plan = read_plan(ctx)
    if plan:
        parts.append("## Plan\n" + plan)
    captions = _captions(project, placements)
    if captions:
        parts.append("## Captions\n" + "\n".join(f"- {c}" for c in captions))
    parts.append(_fonts_section(ctx.cfg["render"]["fonts"]))
    if placements:
        seconds = total_duration(placements)
        parts.append(f"finished runtime {format_timestamp(seconds)} ({seconds:.1f} s)")
    else:
        parts.append("finished runtime unknown — no intervals yet")
    compact = _compact_rows(project, placements) if args.compact else None
    if compact is not None:
        parts.append(compact)
    else:
        rows = []
        for line in view.lines[first - 1 : last]:
            when = _when(project, placements)(line.number)
            rows.append(f"{line.number}  {when}  {line.text}".rstrip())
        parts.append("\n".join(rows))
    print("\n\n".join(parts))
    return 0


def _still_from_row(row: dict) -> Still:
    return Still(
        row["stem"],
        float(row["source_time"]),
        row["kind"],
        row["label"],
        row.get("line"),
        row["path"],
    )


def cmd_frames(project: Project, args: argparse.Namespace) -> int:
    ctx = project.ctx
    requests = args.line or []
    notes: list[str] = []
    stills, problems = plan_stills(project, ctx.cfg, requests, notes=notes)
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    stills = extract_stills(project, stills)
    path = ctx.stage_dir("publish") / "stills.json"
    if requests:
        new_paths = {s.path for s in stills}
        kept = [_still_from_row(r) for r in read_stills(path) if r.get("path") not in new_paths]
        stills = kept + stills
    placements = finished_placements(project)
    write_stills(path, stills, placements)
    print(format_stills(stills, placements))
    for note in notes:
        print(note)
    print(f"stills in {ctx.stage_dir('publish')} (the paths above are relative to it)")
    return 0


def _render(project: Project) -> None:
    ctx = project.ctx
    render_dir = ctx.stage_dir("render")
    run_render(
        ctx.stage_dir("publish") / "publish.json",
        render_dir / "render.json",
        ctx.cfg,
        run=run_magick,
        markdown=render_dir / "render.md",
    )
    print(f"rendered into {render_dir}")
    print(f"thumbnails in {render_dir / 'thumbnails'}")


def cmd_write(project: Project, args: argparse.Namespace) -> int:
    ctx = project.ctx
    cfg = ctx.cfg
    data = _read_edit(args.edit)
    publish_dir = ctx.stage_dir("publish")
    placements = finished_placements(project)
    edit, chapters, problems, warnings = validate_edit(
        data,
        project,
        placements,
        fonts=cfg["render"]["fonts"],
        publish_dir=publish_dir,
        min_chapter=cfg["publish"]["min_chapter_duration"],
    )
    if problems:
        print(f"{len(problems)} problem(s); nothing written:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    assert edit is not None
    publish_dir.mkdir(parents=True, exist_ok=True)
    edit_path = publish_dir / "edit.json"
    edit_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = build_publish(
        edit,
        chapters,
        placements,
        read_stills(publish_dir / "stills.json"),
        min_chapter=cfg["publish"]["min_chapter_duration"],
    )
    json_path = publish_dir / "publish.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path = publish_dir / "publish.md"
    md_path.write_text(
        render_publish_markdown(result, cfg["general"]["image_markup"]), encoding="utf-8"
    )
    for p in (edit_path, json_path, md_path):
        print(f"wrote {p}")
    if not args.no_render:
        _render(project)
    return 0


def cmd_chapters(project: Project, args: argparse.Namespace) -> int:
    min_chapter = project.ctx.cfg["publish"]["min_chapter_duration"]
    placements = finished_placements(project)
    refs = []
    for number in args.lines:
        text = ""
        if 1 <= number <= len(project.view.lines):
            text = project.view.lines[number - 1].text.strip()
        if len(text) > CHAPTER_TEXT_CHARS:
            text = text[: CHAPTER_TEXT_CHARS - 1] + "…"
        refs.append(ChapterRef(number, text or f"line {number}"))
    chapters, problems = resolve_chapters(refs, project, placements, min_chapter=min_chapter)
    if chapters and placements is not None:
        spans = _spans([c.time for c in chapters], total_duration(placements))
        print("line   time   length  text")
        for c, span in zip(chapters, spans):
            row = f"{c.line:<6} {format_timestamp(c.time):<6} {format_timestamp(span):<7} {c.title}"
            if span < min_chapter:
                row += f"   ← shorter than {min_chapter:g} s"
            print(row)
    warning = chapter_count_warning(len(chapters))
    if warning:
        print(f"warning: {warning}")
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    return 1 if problems else 0


def cmd_render(project: Project, args: argparse.Namespace) -> int:
    _render(project)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish-tools",
        description=(
            "Deterministic tools for writing a video's upload material with a coding "
            "agent (no LLM call). Line numbers are the director's display numbering."
        ),
        epilog=EDIT_HELP,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        action="append",
        default=None,
        help="Path to YAML config file (repeatable; merged left to right, later files win)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "view",
        parents=[common],
        help="Print the brief, plan, captions, runtime and the lines with their times",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--lines", type=_parse_lines, help="Only display lines A-B")
    group.add_argument(
        "--compact",
        action="store_true",
        help=(
            "Instead of every shot line, the finished video: what plays, in playback "
            "order, with its finished time (a cut is one row, a timelapse one row)"
        ),
    )
    p.set_defaults(func=cmd_view)

    p = sub.add_parser(
        "frames",
        parents=[common],
        help="Extract the thumbnail shortlist, or stills at given lines",
        description=(
            "Without --line: write the shortlist fresh. With --line N[@pos] "
            "(repeatable, pos in [0,1], default 0.5): add those stills to the "
            "existing list. One container for every still."
        ),
    )
    p.add_argument("--line", action="append", type=_parse_line_pos, metavar="N[@pos]")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser(
        "write",
        parents=[common],
        help="Validate an edit and write edit.json, publish.json, publish.md, then render",
        epilog=EDIT_HELP,
    )
    p.add_argument("--edit", required=True, help="Edit JSON file, or - for stdin")
    p.add_argument("--no-render", action="store_true", help="Skip compositing the thumbnails")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser(
        "chapters",
        parents=[common],
        help="Try chapter lines: their finished times and lengths, and write's problems",
    )
    p.add_argument("lines", nargs="+", type=int, metavar="N", help="Display lines")
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser(
        "render",
        parents=[common],
        help="Composite the thumbnails from publish.json (no LLM)",
    )
    p.set_defaults(func=cmd_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        project = load_project(_load_cfg(args.config))
        return args.func(project, args)
    except BrokenPipeError:
        silence_stdout()
        return 1
    except (PipelineError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
