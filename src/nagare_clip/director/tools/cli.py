"""``python -m nagare_clip.director.tools {view,dump,preview,write}``.

Deterministic, no LLM call.  Every subcommand takes ``--config`` (repeatable,
merged left to right like the pipeline's) and reads the project's output dir
the way the director stage does.  Line numbers are always the DISPLAY
numbering ``view`` prints: every source in shooting order, numbered once,
speech and silence lines together.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from nagare_clip.brief import format_brief
from nagare_clip.config import get_effective_config
from nagare_clip.director.director_llm import _max_keep_lines
from nagare_clip.director.files import write_ops, write_order, write_plan
from nagare_clip.director.loop import order_segments
from nagare_clip.director.preview import edit_state, edit_summary
from nagare_clip.director.run import VIEW_HEADER, project_context
from nagare_clip.director.tools.project import (
    Project,
    current_state,
    load_project,
    runtime_status,
)
from nagare_clip.director.tools.validate import (
    Edit,
    Problem,
    skill_keep_limit_note,
    to_display,
    to_source,
    validate_edit,
)
from nagare_clip.order import identity_segments, normalise
from nagare_clip.pipeline.errors import PipelineError

EDIT_HELP = (
    'An edit is one JSON object in DISPLAY numbering: {"plan": "<prose>", '
    '"order": [[first, last], ...], "ops": [{"type": ..., "lines": [a, b], ...}]}. '
    'It is the complete state: "ops" replaces every op, and an absent "order" '
    'means shooting order (order.json is always written). "plan" is optional; '
    "absent, plan.md stays (required while there is none)."
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


def _load_cfg(paths: list[str] | None) -> dict:
    return get_effective_config([Path(c).resolve() for c in paths or []], {})


def _read_edit(source: str) -> Any:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(text)


def _runtime_index(project: Project) -> tuple[str, str]:
    """``(headline, index)``: the whole-video default runtime, then one row per
    source with its own default runtime — or "unknown" and why, never silence."""
    rows: list[str] = []
    total = 0.0
    unknown: list[str] = []
    for seg, inputs, transcript in zip(project.view.segments, project.inputs, project.transcripts):
        seconds, why = runtime_status(inputs, transcript)
        row = f"[{seg.index}] {seg.label}: lines {seg.first}-{seg.last}, "
        if seconds is None:
            rows.append(row + f"default unknown — {why}")
            unknown.append(f"[{seg.index}] {seg.label}")
        else:
            rows.append(row + f"default {seconds:.1f} s")
            total += seconds
    if unknown:
        headline = (
            f"default runtime unknown: {', '.join(unknown)} "
            f"{'has' if len(unknown) == 1 else 'have'} no line timings "
            f"(the other sources: {total:.1f} s)"
        )
    else:
        headline = f"default runtime {total:.1f} s ({total / 60:.1f} min)"
    return headline, "\n".join(rows)


def cmd_view(project: Project, args: argparse.Namespace) -> int:
    view = project.view
    total = len(view.lines)
    first, last = args.lines or (1, total)
    if first > total:
        print(f"the video has {total} display lines", file=sys.stderr)
        return 2
    last = min(last, total)
    parts: list[str] = []
    if not args.no_context:
        cfg = project.ctx.cfg
        brief = format_brief(cfg.get("project"))
        if brief:
            parts.append(brief)
        max_keep = _max_keep_lines(cfg["director"])
        if max_keep > 0:
            parts.append(skill_keep_limit_note(max_keep))
        context = project_context(project.ctx.stage_dir("summary") / "summary.json", view)
        if context:
            parts.append(context)
    headline, index = _runtime_index(project)
    summary = f"{total} display lines in {len(view.segments)} source(s); {headline}"
    parts.append(f"{summary}\n{index}")
    header = VIEW_HEADER
    if (first, last) != (1, total):
        header += f" Showing lines {first}-{last} of {total}."
    parts.append(header)
    parts.append(view.render(first, last))
    print("\n\n".join(parts))
    return 0


def _state_edit(project: Project) -> tuple[dict[str, Any], list[str]]:
    state = current_state(project)
    ops = []
    for index in sorted(state.ops):
        for op in state.ops[index]:
            shown = to_display(project.view, index, op)
            if shown is not None:
                ops.append(shown)
    ops.sort(key=lambda o: (o["lines"][0], o["lines"][1]))
    out: dict[str, Any] = {}
    if state.plan:
        out["plan"] = state.plan
    if state.order:
        out["order"] = [list(r) for r in state.order]
    out["ops"] = ops
    return out, state.unmapped


def cmd_dump(project: Project, args: argparse.Namespace) -> int:
    data, unmapped = _state_edit(project)
    for note in unmapped:
        print(f"warning: does not map onto the view, left out: {note}", file=sys.stderr)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _print_problems(problems: list[Problem]) -> None:
    print(f"{len(problems)} problem(s); nothing written:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)


def _validate(project: Project, data: Any) -> tuple[Edit, list[Problem]]:
    state = current_state(project)
    return validate_edit(
        project.view,
        data,
        has_plan=bool(state.plan),
        max_keep_lines=_max_keep_lines(project.ctx.cfg["director"]),
    )


def _resolve_sources(project: Project, values: list[str] | None) -> set[int] | None:
    """``--source`` values (a ``[k]`` index or a stem) as segment indices."""
    if not values:
        return None
    out: set[int] = set()
    for value in values:
        index = int(value) if value.isdigit() else project.segment_index(value)
        if index is None or not 1 <= index <= len(project.view.segments):
            raise PipelineError(
                f"unknown source {value!r}: give a [k] number or a stem from `view`'s index"
            )
        out.add(index)
    return out


def cmd_preview(project: Project, args: argparse.Namespace) -> int:
    state = current_state(project)
    problems: list[Problem] = []
    if args.edit is None:
        ops, order, plan = state.ops, state.order, state.plan
        for note in state.unmapped:
            print(f"warning: does not map onto the view, left out: {note}", file=sys.stderr)
    else:
        edit, problems = _validate(project, _read_edit(args.edit))
        ops = to_source(project.view, edit)
        order = edit.order or []
        plan = edit.plan if edit.plan is not None else state.plan
    only = _resolve_sources(project, args.source)
    if args.summary:
        text = edit_summary(
            project.view,
            project.transcripts,
            ops,
            order=order,
            min_run_seconds=args.min_run,
            only=only,
            focus=args.lines,
        )
    else:
        text = edit_state(
            project.view,
            project.transcripts,
            ops,
            order=order,
            plan=plan,
            only=only,
            focus=args.lines,
        )
    print(text)
    if problems:
        print(
            "\n(the preview above shows only the ops that passed their own checks)",
            file=sys.stderr,
        )
        _print_problems(problems)
        return 1
    return 0


def write_edit(project: Project, edit: Edit) -> list[Path]:
    """Write a validated *edit*: every ``{stem}_director.json`` and
    ``order.json`` (shooting order when the edit has no ``order``) always, and
    ``plan.md`` when the edit carries a plan.  Returns the paths."""
    ctx = project.ctx
    by_index = to_source(project.view, edit)
    written: list[Path] = []
    for seg in project.view.segments:
        write_ops(ctx, seg.stem, by_index.get(seg.index, []))
        written.append(project.director_dir / f"{seg.stem}_director.json")
    # Always, like the LLM director: the edit is the whole state, so writing
    # the same edit twice gives the same files.
    segments = (
        order_segments(project.view, edit.order) if edit.order else identity_segments(project.stems)
    )
    if segments is None:  # pragma: no cover - validated
        raise PipelineError("order does not map onto the sources")
    write_order(ctx, normalise(segments, project.line_counts))
    written.append(project.director_dir / "order.json")
    if edit.plan is not None:
        write_plan(ctx, edit.plan)
        written.append(project.director_dir / "plan.md")
    return written


def cmd_write(project: Project, args: argparse.Namespace) -> int:
    edit, problems = _validate(project, _read_edit(args.edit))
    if problems:
        _print_problems(problems)
        return 1
    for path in write_edit(project, edit):
        print(f"wrote {path}")
    print(
        "Next: ./scripts/run_pipeline.sh --from-stage guided_edit "
        "(with the same --config) to apply the ops and build the .blend."
    )
    cfg = project.ctx.cfg
    if cfg["director"].get("enabled", False):
        print(
            "warning: director.enabled is true, so running the director stage would let the "
            "LLM director continue from these files. Resume from guided_edit, or disable it.",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="director-tools",
        description=(
            "Deterministic tools for directing a rough cut with a coding agent "
            "(no LLM call). Line numbers are the display numbering `view` prints."
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
        help="Print the whole video under one numbering, with the brief and summaries",
        description=(
            "Print the editorial brief, the keep-width limit, the project summaries, "
            "the source index and the numbered transcript (timed brackets, silence "
            "lines, [silent gap: ...] annotations). Read-only."
        ),
    )
    p.add_argument("--lines", type=_parse_lines, help="Only display lines A-B (e.g. 120-240)")
    p.add_argument(
        "--no-context",
        action="store_true",
        help="Leave out the brief, keep limit and summaries (useful when paging)",
    )
    p.set_defaults(func=cmd_view)

    p = sub.add_parser(
        "dump",
        parents=[common],
        help="Print the director files on disk as an edit file (display numbering)",
        description=(
            "Print plan.md, order.json and every {stem}_director.json as one edit "
            "JSON in display numbering — a starting point to edit and pass to "
            "preview/write. Read-only."
        ),
    )
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser(
        "preview",
        parents=[common],
        help="Show what the current or a candidate edit will play (writes nothing)",
        description=(
            "Print the plan, every op as it will play, the video as it plays in the "
            "order, the captions and the whole-video runtime. With --edit, previews "
            "that candidate instead of the files on disk, and lists its problems "
            "(exit 1). Never writes."
        ),
        epilog=EDIT_HELP,
    )
    p.add_argument("--edit", help="Candidate edit JSON file, or - for stdin")
    p.add_argument(
        "--summary",
        action="store_true",
        help="Compact form: per-source and whole-video runtime, the runs in playback "
        "order, the captions, and the problems — no per-op detail",
    )
    p.add_argument(
        "--min-run",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="With --summary: hide runs shorter than this many on-screen seconds "
        "(cuts are always shown)",
    )
    p.add_argument(
        "--lines",
        type=_parse_lines,
        help="Only the detail (or, with --summary, the runs) touching display lines A-B; "
        "totals, captions and problems stay whole-video",
    )
    p.add_argument(
        "--source",
        action="append",
        help="Only this source's detail ([k] number or stem; repeatable); totals, "
        "captions and problems stay whole-video",
    )
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser(
        "write",
        parents=[common],
        help="Validate an edit and write the director files (all or nothing)",
        description=(
            "Validate the edit; on any problem print them all and write nothing "
            "(exit 1). Otherwise write {stem}_director.json for every source, and "
            "director/order.json (shooting order when the edit has none), and "
            "director/plan.md when the edit carries a plan."
        ),
        epilog=EDIT_HELP,
    )
    p.add_argument("--edit", required=True, help="Edit JSON file, or - for stdin")
    p.set_defaults(func=cmd_write)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        project = load_project(_load_cfg(args.config))
        return args.func(project, args)
    except PipelineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
