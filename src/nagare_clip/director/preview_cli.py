"""director-preview: play back existing ``{stem}_director.json`` files (no LLM).

    python -m nagare_clip.director.preview_cli --config nagare_config.yml
    ./scripts/director_preview.sh --config ... --director-dir /tmp/old/director

Read-only: every input is loaded the way the director stage loads it
(:func:`~nagare_clip.director.run.load_segment_transcript`, the resolved
playback order), one SEGMENT at a time, and the preview is printed to stdout.

The parser's drops are not in ``_director.json`` — the file holds only the ops
that survived.  They are recovered by re-parsing the recorded response in the
director's LLM report (``--report-dir``; by default the project's own
``llm_report/director`` when previewing the project's own director dir).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.director.director_llm import (
    _max_keep_lines,
    ops_from_dict,
    try_parse_director_response,
)
from nagare_clip.director.preview import SegmentPreview, preview_segment
from nagare_clip.director.run import SegmentInputs, load_segment_transcript
from nagare_clip.order import Segment, segment_label, segment_unit
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia
from nagare_clip.pipeline.stages import _timeline_segments

_RESPONSE = "### Response\n```\n"


def recorded_response(report: Path) -> str | None:
    """The last response recorded in one unit's director LLM report."""
    try:
        text = report.read_text(encoding="utf-8")
    except OSError:
        return None
    start = text.rfind(_RESPONSE)
    if start < 0:
        return None
    body = text[start + len(_RESPONSE) :]
    end = body.rfind("\n```")
    return body[:end] if end >= 0 else None


def _context(cfg: dict, stems: list[str]) -> PipelineContext:
    p = cfg["pipeline"]
    return PipelineContext(
        cfg=cfg,
        project_root=Path.cwd(),
        config_path=None,
        input_videos_dir=Path(p["input_videos_dir"]).resolve(),
        output_dir=Path(p["output_dir"]).resolve(),
        sources=[SourceMedia(Path(s), s, s) for s in stems],
        from_index=0,
        to_index=0,
    )


def _segment_preview(
    ctx: PipelineContext,
    segment: Segment,
    index: int,
    director_dir: Path,
    report_dir: Path | None,
    elsewhere: float | None = None,
) -> tuple[list[str], SegmentPreview]:
    """(notes, preview) for one segment."""
    stem = segment.stem
    edits = ctx.stage_dir("text_filter") / f"{stem}_edits.txt"
    transcript = load_segment_transcript(
        SegmentInputs(
            segment,
            edits,
            json_path=ctx.stage_dir("sentence_split") / f"{stem}.json",
            gaps=ctx.stage_dir("gap_context") / f"{stem}_gaps.json",
            cuts_txt=ctx.stage_dir("audio_silence") / f"{stem}_cuts.txt",
        )
    )
    first = transcript.first_line
    last = first + len(transcript.edit_lines) - 1
    num_lines = len(edits.read_text(encoding="utf-8").splitlines())
    notes: list[str] = []
    ops = []
    path = director_dir / f"{stem}_director.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        ops = [op for op in ops_from_dict(data, num_lines) if first <= op.lines[0] <= last]
    else:
        notes.append(f"(no {path.name} in {director_dir}: no ops)")
    drops: list[str] = []
    if report_dir is not None:
        report = report_dir / f"{segment_unit(segment)}.md"
        response = recorded_response(report)
        if response is None:
            notes.append(f"(no LLM report for {segment_unit(segment)}: parser drops unknown)")
        else:
            try_parse_director_response(
                response,
                num_lines=last,
                drops=drops,
                max_keep_lines=_max_keep_lines(ctx.cfg["director"]),
                first_line=first,
            )
    preview = preview_segment(
        transcript.edit_lines,
        ops,
        seg_times=transcript.seg_times,
        silences=transcript.silences,
        anchored_gaps=transcript.gaps,
        first_line=first,
        drops=drops,
        label=f"segment [{index}]",
        elsewhere_seconds=elsewhere,
    )
    return notes, preview


def preview_project(
    ctx: PipelineContext,
    director_dir: Path,
    *,
    report_dir: Path | None = None,
    only: set[str] | None = None,
) -> str:
    """Every segment's preview in playback order (those of *only*, if given)."""
    segments = _timeline_segments(ctx)
    first_pass = [
        _segment_preview(ctx, seg, i, director_dir, report_dir)[1]
        for i, seg in enumerate(segments, start=1)
    ]
    runtimes = [p.runtime_seconds for p in first_pass]
    known = all(r is not None for r in runtimes)
    total = sum(runtimes) if known else None
    out: list[str] = []
    for i, (seg, pv) in enumerate(zip(segments, first_pass), start=1):
        if only is not None and seg.stem not in only:
            continue
        elsewhere = total - pv.runtime_seconds if total is not None else None
        notes, pv = _segment_preview(ctx, seg, i, director_dir, report_dir, elsewhere)
        out.append("\n".join([f"=== [{i}] {segment_label(seg)} ==="] + notes + [pv.text]))
    if known:
        default = sum(p.default_seconds for p in first_pass)
        out.append(f"whole video: default {default:.1f} s → with these ops {total:.1f} s")
    return "\n\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="director-preview",
        description="Print what existing director ops will play (read-only, no LLM).",
    )
    parser.add_argument("--config", default=None, help="Path to YAML config file")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Only print this source's segments (stem or file name; repeatable)",
    )
    parser.add_argument(
        "--director-dir",
        default=None,
        dest="director_dir",
        help="Directory of {stem}_director.json files (default: the project's director/)",
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        dest="report_dir",
        help="Director LLM report dir to recover parser drops from "
        "(default: the project's llm_report/director, only with the default --director-dir)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)

    cfg = get_effective_config(Path(args.config).resolve() if args.config else None, {})
    output_dir = Path(cfg["pipeline"]["output_dir"]).resolve()
    director_dir = Path(args.director_dir) if args.director_dir else output_dir / "director"
    if args.report_dir:
        report_dir: Path | None = Path(args.report_dir)
    elif args.director_dir:
        report_dir = None
    else:
        report_dir = output_dir / "llm_report" / "director"
    stems = sorted(
        p.name.removesuffix("_director.json") for p in director_dir.glob("*_director.json")
    )
    only = {Path(s).stem for s in args.source} if args.source else None
    print(
        preview_project(_context(cfg, stems), director_dir, report_dir=report_dir, only=only),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
