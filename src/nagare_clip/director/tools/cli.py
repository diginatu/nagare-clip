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
from nagare_clip.director.director_llm import _max_keep_lines, keep_limit_note
from nagare_clip.director.loop import order_segments
from nagare_clip.director.preview import edit_state
from nagare_clip.director.run import VIEW_HEADER, project_context
from nagare_clip.director.tools.project import Project, current_state, load_project
from nagare_clip.director.tools.validate import (
    Edit,
    Problem,
    to_display,
    to_source,
    validate_edit,
)
from nagare_clip.order import normalise
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.stages import (
    _write_director_ops,
    _write_director_order,
    _write_director_plan,
)

EDIT_HELP = (
    'An edit is one JSON object in DISPLAY numbering: {"plan": "<prose>", '
    '"order": [[first, last], ...], "ops": [{"type": ..., "lines": [a, b], ...}]}. '
    '"ops" is required and replaces every op; "plan"/"order" are optional and, '
    "when absent, the ones on disk stay in force."
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


def _segment_index_line(project: Project) -> str:
    rows = []
    for seg in project.view.segments:
        rows.append(f"[{seg.index}] {seg.label}: lines {seg.first}-{seg.last}")
    return "\n".join(rows)


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
            parts.append(keep_limit_note(max_keep))
        context = project_context(project.ctx.stage_dir("summary") / "summary.json", view)
        if context:
            parts.append(context)
    runtime = [t.default_runtime() for t in project.transcripts]
    known = all(r is not None for r in runtime)
    summary = f"{total} display lines in {len(view.segments)} source(s)"
    if known:
        default = sum(runtime)
        summary += f"; default runtime {default:.1f} s ({default / 60:.1f} min)"
    parts.append(f"{summary}\n{_segment_index_line(project)}")
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
        current_order=state.order,
        has_plan=bool(state.plan),
        max_keep_lines=_max_keep_lines(project.ctx.cfg["director"]),
    )


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
        order = edit.order if edit.order is not None else state.order
        plan = edit.plan if edit.plan is not None else state.plan
    text = edit_state(project.view, project.transcripts, ops, order=order, plan=plan)
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
    """Write a validated *edit*: every ``{stem}_director.json``, and
    ``order.json``/``plan.md`` when the edit carries them.  Returns the paths."""
    ctx = project.ctx
    by_index = to_source(project.view, edit)
    written: list[Path] = []
    for seg in project.view.segments:
        _write_director_ops(ctx, seg.stem, by_index.get(seg.index, []))
        written.append(project.director_dir / f"{seg.stem}_director.json")
    if edit.order is not None:
        segments = order_segments(project.view, edit.order)
        if segments is None:  # pragma: no cover - validated
            raise PipelineError("order does not map onto the sources")
        _write_director_order(ctx, normalise(segments, project.line_counts))
        written.append(project.director_dir / "order.json")
    if edit.plan is not None:
        _write_director_plan(ctx, edit.plan)
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
    if not cfg["guided_edit"].get("enabled", False):
        print(
            "warning: guided_edit.enabled is false in this config, so guided_edit will copy "
            "_edits.txt through WITHOUT applying these ops. Set guided_edit.enabled: true "
            "(cut/timelapse/overlay/keep need no LLM; an edit op uses guided_edit's model).",
            file=sys.stderr,
        )
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
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser(
        "write",
        parents=[common],
        help="Validate an edit and write the director files (all or nothing)",
        description=(
            "Validate the edit; on any problem print them all and write nothing "
            "(exit 1). Otherwise write {stem}_director.json for every source, and "
            "director/order.json and director/plan.md when the edit carries them."
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
