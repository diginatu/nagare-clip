"""The declarative stage registry: one Stage entry per pipeline stage.

Adapters translate the PipelineContext into each stage's typed run()
function (or external command), print the same progress lines the bash
orchestrator echoed, and own the per-stage LLM-report recorder lifecycle
(clear once before the stem loop, rebuild the index after).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.blender.frames import ordered_sources
from nagare_clip.blender.warnings_file import WARNINGS_FILENAME
from nagare_clip.cut_report.report import build_cut_report
from nagare_clip.director.context import Neighbour, PriorEdits
from nagare_clip.director.director_llm import (
    DirectorOp,
    collect_overlay_texts,
    ops_from_dict,
    ops_to_dict,
)
from nagare_clip.director.run import (
    SegmentInputs,
    run_director,
    run_director_conversation,
    silence_line_min,
    whole_video_reference,
)
from nagare_clip.gap_context.describe import GapFrames
from nagare_clip.gap_context.run import run_gap_context
from nagare_clip.gap_context.snapshot import (
    frame_relpath,
    frame_times,
    parse_ssim_stats,
    select_gaps,
    ssim_relpath,
)
from nagare_clip.guided_edit.run import run_guided_edit
from nagare_clip.intervals.manifest import build_manifest
from nagare_clip.intervals.op_times import OpTimes, resolve_op_times
from nagare_clip.intervals.run import run_intervals
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.order import (
    MANIFEST_NAME,
    Segment,
    TimelineSegment,
    identity_segments,
    normalise,
    read_manifest,
    segment_unit,
    validate_segments,
    write_manifest,
)
from nagare_clip.order_note import format_order_note
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.external import (
    build_blender_cmd,
    build_silencedetect_cmd,
    build_snapshot_batch_cmd,
    build_transcription_cmd,
    run_command,
    run_magick,
)
from nagare_clip.pipeline.runner import PipelineContext, Stage
from nagare_clip.pipeline.sources import SourceMedia, project_stems
from nagare_clip.plan.dialogue import history_path
from nagare_clip.plan.divergence import find_divergences, format_divergences
from nagare_clip.plan.plan_llm import order_from_dict, plan_from_dict
from nagare_clip.plan.run import run_plan
from nagare_clip.plan_revise.run import run_plan_revise
from nagare_clip.publish.run import run_publish
from nagare_clip.publish.thumbs import (
    ThumbCandidate,
    ThumbShot,
    cap_candidates,
    select_candidates,
)
from nagare_clip.publish.thumbs import (
    frame_relpath as thumb_relpath,
)
from nagare_clip.render.run import run_render
from nagare_clip.sentence_split.run import run_sentence_split
from nagare_clip.summary.run import run_summary
from nagare_clip.text_filter.run import run_text_filter
from nagare_clip.timing import segment_times

STAGE_NAMES = [
    "transcription",
    "audio_silence",
    "sentence_split",
    "gap_context",
    "summary",
    "text_filter",
    "plan",
    "plan_revise",
    "director",
    "guided_edit",
    "intervals",
    "blender",
    "publish",
    "render",
]


def _docker_env(ctx: PipelineContext) -> dict[str, str]:
    return {
        "INPUT_VIDEOS_DIR": str(ctx.input_videos_dir),
        "OUTPUT_DIR": str(ctx.output_dir),
    }


def _recorder(ctx: PipelineContext, stage: str):
    return recorder_from_config(stage, ctx.cfg, override_dir=str(ctx.llm_report_dir))


# --- transcription -----------------------------------------------------------


def _transcription_run(ctx: PipelineContext) -> None:
    rels = [s.relative for s in ctx.sources]
    print(f"[transcription] WhisperX: {' '.join(rels)}")
    run_command(
        build_transcription_cmd(ctx.project_root, rels, ctx.cfg),
        env_extra=_docker_env(ctx),
    )


def _transcription_required(ctx: PipelineContext) -> list[Path]:
    # {stem}.json/.txt are consumed only by sentence_split; later start points
    # read output/sentence_split/ instead (validated by that stage's entry).
    if ctx.from_index > STAGE_NAMES.index("sentence_split"):
        return []
    d = ctx.stage_dir("transcription")
    return [d / f"{s}{ext}" for s in ctx.stems for ext in (".json", ".txt")]


# --- audio_silence -----------------------------------------------------------


def _audio_silence_run(ctx: PipelineContext) -> None:
    a = ctx.cfg["audio_silence"]
    d = ctx.stage_dir("audio_silence")
    for src in ctx.sources:
        print(f"[audio_silence] Detection: {src.stem}")
        raw_path = None
        if a["enabled"]:
            raw_path = d / f"{src.stem}_silencedetect.log"
            run_command(
                build_silencedetect_cmd(
                    ctx.project_root, src.relative, a["noise"], a["min_silence"]
                ),
                env_extra=_docker_env(ctx),
                stderr_to=raw_path,
            )
        run_audio_silence(d / f"{src.stem}_cuts.txt", ctx.cfg, raw_path=raw_path)


def _audio_silence_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("audio_silence")
    return [d / f"{s}_cuts.txt" for s in ctx.stems]


# --- sentence_split ----------------------------------------------------------


def _sentence_split_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "sentence_split")
    rec.clear()
    try:
        tdir = ctx.stage_dir("transcription")
        odir = ctx.stage_dir("sentence_split")
        adir = ctx.stage_dir("audio_silence")
        for src in ctx.sources:
            print(f"[sentence_split] Sentence re-segmentation: {src.stem}")
            run_sentence_split(
                tdir / f"{src.stem}.json",
                tdir / f"{src.stem}.txt",
                odir / f"{src.stem}.json",
                odir / f"{src.stem}.txt",
                ctx.cfg,
                stem=src.stem,
                recorder=rec,
                cuts_txt=adir / f"{src.stem}_cuts.txt",
            )
    finally:
        rec.rebuild_index()


def _sentence_split_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("sentence_split")
    return [d / f"{s}{ext}" for s in ctx.stems for ext in (".json", ".txt")]


# --- gap_context ---------------------------------------------------------------


def _extract_gap_frames(
    ctx: PipelineContext,
    gaps_by_source: list[tuple[SourceMedia, list[tuple[float, float]]]],
) -> dict[str, list[GapFrames]]:
    """Snapshot every gap of every source in ONE docker container run.

    A `docker compose run` pays ~0.8s of container + nvidia-runtime init
    regardless of how little work it does; the actual ffmpeg snapshot is
    ~30ms. Running one container per FRAME (the original design) made
    container startup dominate: measured 2.47s for 3 frames as 3 separate
    containers vs. 0.85s for the same 3 frames in one container (byte-
    identical JPEGs either way) -- a 40-gap video (120 frames) was burning
    ~98s in pure container init. Batching the whole stage's extraction into
    a single call mirrors the transcription stage's "single Docker
    container for all source files" precedent.
    """
    width = ctx.cfg["gap_context"]["frame_width"]
    ssim_threshold = float(ctx.cfg["gap_context"].get("static_ssim", 0.0))
    d = ctx.stage_dir("gap_context")

    jobs: list[tuple[str, float, str]] = []
    ssim_jobs: list[tuple[str, str, str]] = []
    # Per stem: list of (start, end, [(t, rel, host_path), ...], ssim_hosts) so
    # the single batch result can be re-split back per source/gap afterward.
    # ssim_hosts holds one stats-file path per CONSECUTIVE frame pair (0..1
    # for a 3-frame gap, i.e. first-vs-mid and mid-vs-last) -- comparing only
    # the first and last frame would miss a pan-away-and-return, since the
    # middle frame (already extracted, already paid for) is the one that
    # would reveal the on-screen action.
    plan: dict[str, list[tuple[float, float, list[tuple[float, str, Path]], list[Path]]]] = {}
    for src, gaps in gaps_by_source:
        entries: list[tuple[float, float, list[tuple[float, str, Path]], list[Path]]] = []
        for start, end in gaps:
            frame_entries: list[tuple[float, str, Path]] = []
            for t in frame_times(start, end):
                rel = frame_relpath(src.stem, t)
                host_path = d / rel
                host_path.parent.mkdir(parents=True, exist_ok=True)
                jobs.append((src.relative, t, f"/output/gap_context/{rel}"))
                frame_entries.append((t, rel, host_path))
            ssim_hosts: list[Path] = []
            if ssim_threshold > 0.0 and len(frame_entries) >= 2:
                pairs = list(zip(frame_entries, frame_entries[1:], strict=False))
                for i, (a, b) in enumerate(pairs):
                    ssim_rel = ssim_relpath(src.stem, start, end, i)
                    ssim_host = d / ssim_rel
                    ssim_hosts.append(ssim_host)
                    ssim_jobs.append(
                        (
                            f"/output/gap_context/{a[1]}",
                            f"/output/gap_context/{b[1]}",
                            f"/output/gap_context/{ssim_rel}",
                        )
                    )
            entries.append((start, end, frame_entries, ssim_hosts))
        plan[src.stem] = entries

    if jobs:
        try:
            run_command(
                build_snapshot_batch_cmd(ctx.project_root, jobs, width, ssim_jobs=ssim_jobs),
                env_extra=_docker_env(ctx),
            )
        except Exception:  # noqa: BLE001 - a failed batch must never abort the run
            logging.warning("gap_context: batch frame extraction failed (%d job(s))", len(jobs))

    result: dict[str, list[GapFrames]] = {}
    for stem, entries in plan.items():
        out: list[GapFrames] = []
        for start, end, frame_entries, ssim_hosts in entries:
            frames: list[Path] = []
            relpaths: list[str] = []
            for t, rel, host_path in frame_entries:
                if not host_path.is_file():
                    logging.warning("gap_context: no frame written at %.3fs for %s", t, stem)
                    continue
                frames.append(host_path)
                relpaths.append(rel)
            if not frames:
                logging.warning(
                    "gap_context: no frames for gap %.1f-%.1f in %s; skipping", start, end, stem
                )
                continue
            # The gap is judged static only if it holds still THROUGHOUT, so
            # take the min across consecutive-pair scores (min(a,b) >= T is
            # exactly (a>=T) AND (b>=T)); a missing/unparseable stats file
            # (failed comparison) is simply excluded, never fatal.
            pair_scores = [
                score
                for ssim_host in ssim_hosts
                if ssim_host.is_file()
                for score in [parse_ssim_stats(ssim_host.read_text(encoding="utf-8"))]
                if score is not None
            ]
            ssim: float | None = min(pair_scores) if pair_scores else None
            out.append(GapFrames(start=start, end=end, frames=frames, relpaths=relpaths, ssim=ssim))
        result[stem] = out
    return result


def _gap_context_run(ctx: PipelineContext) -> None:
    g = ctx.cfg["gap_context"]
    rec = _recorder(ctx, "gap_context")
    rec.clear()
    try:
        adir = ctx.stage_dir("audio_silence")
        odir = ctx.stage_dir("gap_context")
        frames_by_stem: dict[str, list[GapFrames]] = {}
        if g["enabled"]:
            gaps_by_source = [
                (src, select_gaps(read_cuts(adir / f"{src.stem}_cuts.txt"), g["min_gap"]))
                for src in ctx.sources
            ]
            frames_by_stem = _extract_gap_frames(ctx, gaps_by_source)
        for src in ctx.sources:
            print(f"[gap_context] Silent-gap visual context: {src.stem}")
            run_gap_context(
                frames_by_stem.get(src.stem, []),
                odir / f"{src.stem}_gaps.json",
                ctx.cfg,
                stem=src.stem,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _gap_context_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("gap_context")
    return [d / f"{s}_gaps.json" for s in ctx.stems]


# --- summary -----------------------------------------------------------------


def _summary_run(ctx: PipelineContext) -> None:
    print("[summary] Project-wide summaries")
    rec = _recorder(ctx, "summary")
    rec.clear()
    try:
        run_summary(
            [ctx.stage_dir("sentence_split") / f"{s}.txt" for s in ctx.stems],
            ctx.stage_dir("summary") / "summary.json",
            ctx.cfg,
            json_paths=[ctx.stage_dir("sentence_split") / f"{s}.json" for s in ctx.stems],
            gaps_paths=[ctx.stage_dir("gap_context") / f"{s}_gaps.json" for s in ctx.stems],
            cuts_paths=[ctx.stage_dir("audio_silence") / f"{s}_cuts.txt" for s in ctx.stems],
            recorder=rec,
        )
    finally:
        rec.rebuild_index()


def _summary_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("summary") / "summary.json"]


# --- text_filter -------------------------------------------------------------


def _text_filter_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "text_filter")
    rec.clear()
    try:
        sdir = ctx.stage_dir("sentence_split")
        odir = ctx.stage_dir("text_filter")
        for src in ctx.sources:
            print(f"[text_filter] Text editing checkpoint: {src.stem}")
            run_text_filter(
                sdir / f"{src.stem}.txt",
                odir / f"{src.stem}_edits.txt",
                ctx.cfg,
                summary_json=ctx.stage_dir("summary") / "summary.json",
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _text_filter_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("text_filter")
    return [d / f"{s}_edits.txt" for s in ctx.stems]


# --- plan --------------------------------------------------------------------


def _plan_run(ctx: PipelineContext) -> None:
    print("[plan] Cross-video rough directions")
    rec = _recorder(ctx, "plan")
    rec.clear()
    try:
        run_plan(
            ctx.stage_dir("summary") / "summary.json",
            ctx.stage_dir("plan") / "plan.json",
            ctx.cfg,
            history=history_path(ctx.output_dir),
            revised=_revised_plan_json(ctx),
            recorder=rec,
            line_counts=_line_counts(ctx),
        )
    finally:
        rec.rebuild_index()


def _plan_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("plan") / "plan.json"]


# --- plan_revise -------------------------------------------------------------


def _revised_plan_json(ctx: PipelineContext) -> Path:
    return ctx.stage_dir("plan_revise") / "plan.json"


def _effective_plan_json(ctx: PipelineContext) -> Path:
    """The plan the downstream stages read: the revised one when it exists.

    ``plan_revise`` writes its own directory rather than into ``plan/``, so
    ``diff plan/plan.json plan_revise/plan.json`` is exactly the human's
    influence on the edit.  A ``plan`` re-run deletes the revised file, which is
    what makes this fall back rather than go stale.
    """
    revised = _revised_plan_json(ctx)
    return revised if revised.is_file() else ctx.stage_dir("plan") / "plan.json"


def _line_counts(ctx: PipelineContext) -> dict[str, int]:
    """Lines per source, for validating an order against the real transcripts.

    Read off ``text_filter/{stem}_edits.txt`` — the file the director slices, and
    the one ``intervals/check_edits.py`` already pins to the JSON segment count.
    Counted for the whole **project**, not this run, so ``--source`` validates
    the same order a full run would; an unreadable source is simply absent, and
    an order naming it is then rejected rather than half-applied.
    """
    d = ctx.stage_dir("text_filter")
    counts: dict[str, int] = {}
    for stem in project_stems(ctx.input_videos_dir) or ctx.stems:
        path = d / f"{stem}_edits.txt"
        try:
            counts[stem] = len(path.read_text(encoding="utf-8").splitlines())
        except OSError:
            logging.debug("order: no line count for %s (%s)", stem, path)
    return counts


def _resolve_order(ctx: PipelineContext) -> tuple[list[Segment], list[str]]:
    """The playback order plus the problems that made it fall back (if any)."""
    shooting = identity_segments(project_stems(ctx.input_videos_dir) or ctx.stems)
    plan_json = _effective_plan_json(ctx)
    if not plan_json.is_file():
        return shooting, []
    try:
        segments = order_from_dict(json.loads(plan_json.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        logging.warning("order: could not read %s: %s", plan_json, e)
        return shooting, []
    if not segments:
        return shooting, []

    counts = _line_counts(ctx)
    problems = validate_segments(segments, counts)
    if problems:
        logging.warning(
            "order: %s does not cover every line exactly once; falling back to shooting order (%s)",
            plan_json,
            "; ".join(problems),
        )
        return shooting, problems
    return normalise(segments, counts), []


ORDER_NOTE = "order.md"


def write_order_note(ctx: PipelineContext) -> None:
    """State a reorder plainly, or say that one was rejected (no LLM call).

    A reorder changes the shape of the finished video more than any other single
    decision, and the failure mode to avoid is a human noticing it only while
    watching the result.  Nothing is written when the resolved order IS shooting
    order — including a plan that states it explicitly, which is not a reorder.
    """
    note = ctx.llm_report_dir / "notes" / ORDER_NOTE
    try:
        segments, problems = _resolve_order(ctx)
        shooting = identity_segments(project_stems(ctx.input_videos_dir) or ctx.stems)
        text = format_order_note(segments, shooting, problems)
        if text:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(text, encoding="utf-8")
            print(f"[order] the finished video is not in shooting order: see {note}")
        elif note.is_file():
            note.unlink()
    except (OSError, ValueError) as e:
        logging.warning("order: could not write the order note: %s", e)


def _timeline_segments(ctx: PipelineContext) -> list[Segment]:
    """The finished video's playback order — the one point stages consult.

    This replaces ``project_stems()`` as what improvements 19 and 22 route
    "what plays before this" through.  ``project_stems`` itself stays: it is
    what produces the identity value, i.e. shooting order, which is what a
    missing, unparseable or invalid order degrades to.

    The order is validated against the real line counts every time, because
    ``plan.json`` is hand-editable and ``sentence_split``/``summary`` can re-run
    underneath it.  On any problem the fallback is shooting order for the
    **whole project** — never a partial repair, which would be a video with a
    scene silently moved.
    """
    return _resolve_order(ctx)[0]


def _plan_revise_run(ctx: PipelineContext) -> None:
    print("[plan_revise] Revising the plan with the human editor")
    rec = _recorder(ctx, "plan_revise")
    rec.clear()
    try:
        run_plan_revise(
            ctx.stage_dir("summary") / "summary.json",
            ctx.stage_dir("plan") / "plan.json",
            _revised_plan_json(ctx),
            ctx.cfg,
            history=history_path(ctx.output_dir),
            recorder=rec,
            line_counts=_line_counts(ctx),
        )
    finally:
        rec.rebuild_index()


# --- director ----------------------------------------------------------------


def _segment_ops(
    ctx: PipelineContext, segment: Segment, done: dict[Segment, list[DirectorOp]]
) -> list[DirectorOp]:
    """One earlier segment's ops: from this run when it ran, else off disk.

    A source this run is not processing still has its whole-source
    ``_director.json`` on disk from an earlier run; filtering it to the
    segment's line range is what makes "the captions shown before this point"
    exact rather than approximate.  ``num_lines=None`` skips the range check —
    those ops belong to another transcript.
    """
    if segment in done:
        return done[segment]
    path = ctx.stage_dir("director") / f"{segment.stem}_director.json"
    if not path.is_file():
        return []
    try:
        ops = ops_from_dict(json.loads(path.read_text(encoding="utf-8")), None)
    except (ValueError, OSError):
        logging.warning("director: could not read prior ops %s", path)
        return []
    if segment.lines is None:
        return ops
    first, last = segment.lines
    return [op for op in ops if first <= op.lines[0] <= last]


def _write_director_ops(ctx: PipelineContext, stem: str, ops: list[DirectorOp]) -> None:
    """One source's merged ops, line-sorted, written once."""
    path = ctx.stage_dir("director") / f"{stem}_director.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ops_to_dict(sorted(ops, key=lambda op: op.lines)), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    logging.info("director: wrote %s (%d operation(s))", path, len(ops))


def _director_run(ctx: PipelineContext) -> None:
    """ONE conversation over the whole finished video.

    Every segment's transcript is loaded once and numbered once; the director
    is then asked for an approximate range per turn and answered with what its
    ops will play (:func:`~nagare_clip.director.run.run_director_conversation`).
    ``--source`` does not narrow it: one conversation owns the whole video, so
    every source's ``_director.json`` is rewritten whatever this run was asked
    to process.

    The ops accepted so far are written even when the conversation ends badly
    — the turn cap, or a turn that fails every retry — and only then does the
    stage fail: the user inspects them and continues by hand from
    ``guided_edit`` rather than losing the whole conversation.
    """
    rec = _recorder(ctx, "director")
    rec.clear()
    segments = _timeline_segments(ctx)
    director_dir = ctx.stage_dir("director")
    text_filter_dir = ctx.stage_dir("text_filter")
    stems = sorted({segment.stem for segment in segments})
    if set(ctx.stems) != set(stems):
        logging.info(
            "director: --source does not narrow the director; one conversation owns the "
            "whole video, so every source's _director.json is rewritten"
        )

    inputs = [
        SegmentInputs(
            segment,
            text_filter_dir / f"{segment.stem}_edits.txt",
            json_path=ctx.stage_dir("sentence_split") / f"{segment.stem}.json",
            gaps=ctx.stage_dir("gap_context") / f"{segment.stem}_gaps.json",
            cuts_txt=ctx.stage_dir("audio_silence") / f"{segment.stem}_cuts.txt",
            silence_line_min=silence_line_min(ctx.cfg["director"]),
        )
        for segment in segments
    ]

    # Nothing half-written survives: these are all about to be rewritten, and a
    # file from a previous run was built under a different segmentation anyway.
    for stem in stems:
        path = director_dir / f"{stem}_director.json"
        if path.is_file():
            path.unlink()

    print(f"[director] Edit operations: {len(segments)} segment(s) in one conversation")
    try:
        result = run_director_conversation(
            inputs,
            ctx.cfg,
            summary=ctx.stage_dir("summary") / "summary.json",
            plan=_effective_plan_json(ctx),
            recorder=rec,
        )
        # Written BEFORE the failure, never after it: the ops are what the
        # conversation is for, and a cap is not a reason to throw them away.
        for stem in stems:
            _write_director_ops(ctx, stem, result.ops.get(stem, []))
        if not result.ok:
            files = ", ".join(f"{stem}_director.json" for stem in stems)
            raise PipelineError(
                f"[director] {result.error}; reviewed through display line "
                f"{result.reviewed_through}. The ops accepted so far are written to "
                f"{director_dir} ({files}) — inspect them and continue by hand "
                "(--from-stage guided_edit)"
            )
        _write_divergence_note(ctx)
        write_order_note(ctx)
    finally:
        rec.rebuild_index()


def _director_run_segments(ctx: PipelineContext) -> None:
    """One LLM call per SEGMENT of the finished video — the path the
    conversation replaced.  Unwired (see :func:`_director_run`) and deleted
    with the rest of the per-segment path; its tests still drive it.

    The loop walks the whole project's order even when ``--source`` narrows the
    run, and calls only for the segments this run owns: narrowing changes what
    is processed, not what the finished video contains, so every segment keeps
    its real position and its real neighbours.

    A source's segments are merged in memory and written once, when its last
    segment in the order completes.  Every file this run is about to rewrite is
    deleted first, so a failure cannot leave a stale one standing as if it were
    current.
    """
    rec = _recorder(ctx, "director")
    rec.clear()
    segments = _timeline_segments(ctx)
    director_dir = ctx.stage_dir("director")
    text_filter_dir = ctx.stage_dir("text_filter")
    mine = set(ctx.stems)

    def _edits(stem: str) -> Path:
        return text_filter_dir / f"{stem}_edits.txt"

    def _neighbour(index: int, offset: int) -> Neighbour | None:
        n = index + offset
        if not 0 <= n < len(segments):
            return None
        return Neighbour(segments[n], _edits(segments[n].stem))

    def _inputs(segment: Segment) -> SegmentInputs:
        stem = segment.stem
        return SegmentInputs(
            segment,
            _edits(stem),
            json_path=ctx.stage_dir("sentence_split") / f"{stem}.json",
            gaps=ctx.stage_dir("gap_context") / f"{stem}_gaps.json",
            cuts_txt=ctx.stage_dir("audio_silence") / f"{stem}_cuts.txt",
            silence_line_min=silence_line_min(ctx.cfg["director"]),
        )

    # director.whole_project_context: the whole video's transcript is rendered
    # ONCE, before any call — it sits in the cached prefix, so every segment's
    # call must carry the very same string.
    whole = ctx.cfg["director"].get("whole_project_context") is True
    whole_video = whole_video_reference([_inputs(s) for s in segments]) if whole else ""

    # Nothing half-written survives: these are all about to be rewritten, and a
    # file from a previous run was built under a different segmentation anyway.
    for stem in mine:
        path = director_dir / f"{stem}_director.json"
        if path.is_file():
            path.unlink()

    # The last position in the order at which each source still has a segment,
    # so its file is written as soon as it is complete rather than at the end.
    last_index = {seg.stem: i for i, seg in enumerate(segments) if seg.stem in mine}
    done: dict[Segment, list[DirectorOp]] = {}
    pending: dict[str, list[DirectorOp]] = {stem: [] for stem in mine}
    failed = False
    try:
        for index, segment in enumerate(segments):
            if segment.stem not in mine:
                continue
            unit = segment_unit(segment)
            print(f"[director] Edit operations: {unit}")
            prior = collect_overlay_texts(
                [op for earlier in segments[:index] for op in _segment_ops(ctx, earlier, done)]
            )
            whole_kw = {}
            if whole:
                whole_kw = {
                    "whole_video": whole_video,
                    "prior_edits": [
                        PriorEdits(i + 1, earlier, _segment_ops(ctx, earlier, done))
                        for i, earlier in enumerate(segments[:index])
                    ],
                }
            inputs = _inputs(segment)
            result = run_director(
                inputs.edits,
                ctx.cfg,
                segment=segment,
                all_segments=segments,
                summary=ctx.stage_dir("summary") / "summary.json",
                plan=_effective_plan_json(ctx),
                json_path=inputs.json_path,
                gaps=inputs.gaps,
                cuts_txt=inputs.cuts_txt,
                prior_captions=prior,
                before=_neighbour(index, -1),
                after=_neighbour(index, 1),
                recorder=rec,
                **whole_kw,
            )
            if not result.ok:
                failed = True
                raise PipelineError(
                    f"[director] {unit} failed after every retry; "
                    f"{segment.stem}_director.json not written "
                    "(re-run with --source "
                    f"{segment.stem} --from-stage director --to-stage director)"
                )
            done[segment] = result.ops
            pending[segment.stem].extend(result.ops)
            if index == last_index.get(segment.stem):
                _write_director_ops(ctx, segment.stem, pending[segment.stem])
    finally:
        # A note describing ops that are not there is worse than no note.
        if not failed:
            _write_divergence_note(ctx)
            write_order_note(ctx)
        rec.rebuild_index()


def _write_divergence_note(ctx: PipelineContext) -> None:
    """Record where the ops that landed argue with plan.json (no LLM call).

    Written into the LLM report's notes/ dir so it survives later stages'
    index rebuilds.  Best-effort: every input is optional and a failure here
    must never fail the director stage.
    """
    note = ctx.llm_report_dir / "notes" / "plan_divergence.md"
    try:
        plan_json = _effective_plan_json(ctx)
        directions = (
            plan_from_dict(json.loads(plan_json.read_text(encoding="utf-8")))
            if plan_json.is_file()
            else []
        )
        ops_by_stem = {}
        for stem in ctx.stems:
            path = ctx.stage_dir("director") / f"{stem}_director.json"
            if path.is_file():
                ops_by_stem[stem] = ops_from_dict(
                    json.loads(path.read_text(encoding="utf-8")), None
                )
        text = format_divergences(find_divergences(directions, ops_by_stem))
        if text:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(text, encoding="utf-8")
            print(f"[director] plan/director divergence: see {note}")
        elif note.is_file():
            note.unlink()
    except (OSError, ValueError) as e:
        logging.warning("director: could not write the divergence note: %s", e)


def _director_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("director")
    return [d / f"{s}_director.json" for s in ctx.stems]


# --- guided_edit -------------------------------------------------------------


def _guided_edit_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "guided_edit")
    rec.clear()
    try:
        for src in ctx.sources:
            print(f"[guided_edit] Applying director ops: {src.stem}")
            run_guided_edit(
                ctx.stage_dir("text_filter") / f"{src.stem}_edits.txt",
                ctx.stage_dir("director") / f"{src.stem}_director.json",
                ctx.stage_dir("guided_edit") / f"{src.stem}_edits.txt",
                ctx.cfg,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _guided_edit_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("guided_edit")
    return [d / f"{s}_edits.txt" for s in ctx.stems]


# --- intervals ---------------------------------------------------------------


def _silence_op_times(ctx: PipelineContext, stem: str) -> OpTimes | None:
    """Time ranges for this source's ops that address a silence (``"n~"``).

    Those carry no marker in ``_edits.txt`` — there are no words in a silence
    to wrap — so they are resolved here and handed to ``run_intervals``.
    ``None`` when there is no director output to read, which is the state a
    project without the director stage enabled is in.
    """
    director_json = ctx.stage_dir("director") / f"{stem}_director.json"
    json_path = ctx.stage_dir("sentence_split") / f"{stem}.json"
    if not director_json.is_file() or not json_path.is_file():
        return None
    try:
        ops = ops_from_dict(json.loads(director_json.read_text(encoding="utf-8")), None)
        whisperx = json.loads(json_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logging.warning("intervals: could not read %s for silence refs", director_json)
        return None
    return resolve_op_times(ops, whisperx)


def _intervals_run(ctx: PipelineContext) -> None:
    for src in ctx.sources:
        print(f"[intervals] Patch application + keep intervals: {src.stem}")
        run_intervals(
            ctx.stage_dir("guided_edit") / f"{src.stem}_edits.txt",
            ctx.stage_dir("sentence_split") / f"{src.stem}.json",
            ctx.stage_dir("intervals") / f"{src.stem}_intervals.json",
            ctx.cfg,
            cuts_txt=ctx.stage_dir("audio_silence") / f"{src.stem}_cuts.txt",
            extra=_silence_op_times(ctx, src.stem),
        )
    write_order_note(ctx)
    _write_manifest(ctx)
    write_cut_report(ctx)


def _manifest_path(ctx: PipelineContext) -> Path:
    return ctx.stage_dir("intervals") / MANIFEST_NAME


def _intervals_json(ctx: PipelineContext, stem: str) -> dict | None:
    path = ctx.stage_dir("intervals") / f"{stem}_intervals.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logging.warning("order: could not read %s: %s", path, e)
        return None


def _write_manifest(ctx: PipelineContext) -> list[TimelineSegment]:
    """Resolve the order into source seconds and write ``intervals/timeline.json``.

    This stage is the single conversion point between the two coordinate
    systems: the plan is the authority on the order up to here, the manifest is
    the authority after it, and nothing downstream re-derives a time from a line
    number.  An order that cannot be resolved degrades the whole manifest to
    shooting order rather than resolving some of it.
    """
    segments = _timeline_segments(ctx)
    stems = [s.stem for s in identity_segments(project_stems(ctx.input_videos_dir) or ctx.stems)]
    durations: dict[str, float] = {}
    for stem in stems:
        data = _intervals_json(ctx, stem)
        if data is not None and isinstance(data.get("duration_sec"), (int, float)):
            durations[stem] = float(data["duration_sec"])

    # Only a split source needs its line times; shooting order needs none.
    times: dict[str, list] = {}
    for stem in {seg.stem for seg in segments if seg.lines is not None}:
        path = ctx.stage_dir("sentence_split") / f"{stem}.json"
        try:
            times[stem] = segment_times(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logging.warning("order: could not read %s for the segment boundaries", path)

    entries = build_manifest(segments, times, durations)
    if not entries and segments:
        entries = build_manifest(identity_segments(stems), times, durations)
    write_manifest(_manifest_path(ctx), entries)
    logging.info("order: wrote %s (%d segment(s))", _manifest_path(ctx), len(entries))
    return entries


def _ordered_sources(ctx: PipelineContext) -> list[tuple[str, dict]]:
    """The finished video as ``(stem, sliced intervals data)``, in playback order.

    Filtered to the sources this run processes, which is what makes
    ``--source X`` build a ``.blend`` of X alone while every other stage still
    knows the whole project's order.  A missing manifest (a project built before
    the order existed) degrades to shooting order.
    """
    entries = read_manifest(_manifest_path(ctx))
    if not entries:
        entries = [
            TimelineSegment(seg.stem, 0.0, float("inf"))
            for seg in identity_segments(project_stems(ctx.input_videos_dir) or ctx.stems)
        ]
    only = set(ctx.stems)
    entries = [e for e in entries if e.stem in only]
    data_by_stem: dict[str, dict] = {}
    for stem in {e.stem for e in entries}:
        data = _intervals_json(ctx, stem)
        if data is not None:
            data_by_stem[stem] = data
    return ordered_sources(entries, data_by_stem)


CUT_REPORT_NOTE = "cut_report.md"


def write_cut_report(ctx: PipelineContext) -> None:
    """Measure the finished cut and write the LLM report's note (no LLM call).

    Called after `intervals` (which owns every number but Blender's own
    warnings) and again after `blender`, which adds them.  Like the
    plan/director divergence note it lives in the report's notes/ dir so it
    survives a later stage's rebuild of index.md.  Best-effort throughout: a
    failure here must never fail the stage that produced the cut.
    """
    note = ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE
    try:
        sources = _ordered_sources(ctx)
        text = (
            build_cut_report(
                sources,
                ctx.cfg,
                blender_warnings_path=ctx.stage_dir("blender") / WARNINGS_FILENAME,
            )
            if sources
            else ""
        )
        if text:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(text, encoding="utf-8")
            print(f"[cut_report] finished-cut metrics: see {note}")
        elif note.is_file():
            note.unlink()
    except (OSError, ValueError) as e:
        logging.warning("cut_report: could not write the finished-cut note: %s", e)


def _intervals_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("intervals")
    return [d / f"{s}_intervals.json" for s in ctx.stems]


# --- blender -----------------------------------------------------------------


def _blender_run(ctx: PipelineContext) -> None:
    print("[blender] VSE project generation")
    # Named after the first source in SHOOTING order, so a reorder does not
    # rename the project file.
    output_blend = ctx.stage_dir("blender") / f"{ctx.stems[0]}_edited.blend"
    intervals_paths = [ctx.stage_dir("intervals") / f"{s}_intervals.json" for s in ctx.stems]
    manifest = _manifest_path(ctx)
    run_command(
        build_blender_cmd(
            ctx.project_root,
            [s.abs_path for s in ctx.sources],
            intervals_paths,
            output_blend,
            ctx.config_path,
            ctx.log_file,
            manifest=manifest if manifest.is_file() else None,
        )
    )
    # Re-written now that Blender has recorded its own clamp/overlap warnings.
    write_cut_report(ctx)


# --- publish -----------------------------------------------------------------


def _director_ops(ctx: PipelineContext, stem: str) -> tuple[list[DirectorOp], list]:
    """This source's director ops plus its segment times (both may be empty).

    Every input is optional here: the publish stage runs last, so the director
    may have been disabled or its output removed, and the shortlist simply
    stays empty rather than failing the run.
    """
    seg_times: list = []
    json_path = ctx.stage_dir("sentence_split") / f"{stem}.json"
    if json_path.is_file():
        try:
            seg_times = segment_times(json.loads(json_path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            logging.warning("publish: could not read %s", json_path)
    ops: list[DirectorOp] = []
    director_path = ctx.stage_dir("director") / f"{stem}_director.json"
    if director_path.is_file():
        try:
            ops = ops_from_dict(
                json.loads(director_path.read_text(encoding="utf-8")), len(seg_times)
            )
        except (ValueError, OSError):
            logging.warning("publish: could not read %s", director_path)
    return ops, seg_times


def _extract_thumb_frames(
    ctx: PipelineContext, candidates: list[ThumbCandidate]
) -> list[ThumbShot]:
    """Snapshot every candidate moment in ONE whisperx container.

    Same reasoning as gap_context's batch: a `docker compose run` costs ~0.8s
    of container init against ~30ms of actual ffmpeg work per still, so the
    whole shortlist rides in a single call.
    """
    if not candidates:
        return []
    width = ctx.cfg["publish"]["frame_width"]
    d = ctx.stage_dir("publish")
    relative_by_stem = {s.stem: s.relative for s in ctx.sources}

    jobs: list[tuple[str, float, str]] = []
    planned: list[tuple[ThumbCandidate, str, Path]] = []
    for cand in candidates:
        relative = relative_by_stem.get(cand.stem)
        if relative is None:
            continue
        rel = thumb_relpath(cand.stem, cand.time)
        host_path = d / rel
        host_path.parent.mkdir(parents=True, exist_ok=True)
        jobs.append((relative, cand.time, f"/output/publish/{rel}"))
        planned.append((cand, rel, host_path))

    if jobs:
        try:
            run_command(
                build_snapshot_batch_cmd(ctx.project_root, jobs, width),
                env_extra=_docker_env(ctx),
            )
        except Exception:  # noqa: BLE001 - a failed batch must never abort the run
            logging.warning("publish: batch frame extraction failed (%d job(s))", len(jobs))

    shots: list[ThumbShot] = []
    for cand, rel, host_path in planned:
        if not host_path.is_file():
            logging.warning(
                "publish: no still written at %.3fs for %s; dropped from the shortlist",
                cand.time,
                cand.stem,
            )
            continue
        shots.append(
            ThumbShot(stem=cand.stem, time=cand.time, kind=cand.kind, label=cand.label, path=rel)
        )
    return shots


def _publish_run(ctx: PipelineContext) -> None:
    print("[publish] Title, description, chapters, thumbnail material")
    rec = _recorder(ctx, "publish")
    rec.clear()
    try:
        thumbs: list[ThumbShot] = []
        overlay_texts: dict[str, list[str]] = {}
        if ctx.cfg["publish"]["enabled"]:
            candidates: list[ThumbCandidate] = []
            for src in ctx.sources:
                ops, seg_times = _director_ops(ctx, src.stem)
                candidates += select_candidates(src.stem, ops, seg_times)
                texts = collect_overlay_texts(ops)
                if texts:
                    overlay_texts[src.stem] = texts
            thumbs = _extract_thumb_frames(
                ctx, cap_candidates(candidates, ctx.cfg["publish"]["max_frames"])
            )
        d = ctx.stage_dir("publish")
        run_publish(
            ctx.stage_dir("summary") / "summary.json",
            d / "publish.json",
            ctx.cfg,
            ordered=_ordered_sources(ctx),
            plan_json=_effective_plan_json(ctx),
            overlay_texts=overlay_texts,
            thumbs=thumbs,
            frames_json=d / "frames.json",
            markdown=d / "publish.md",
            recorder=rec,
        )
    finally:
        rec.rebuild_index()


def _publish_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("publish") / "publish.json"]


# --- render ------------------------------------------------------------------


def _render_run(ctx: PipelineContext) -> None:
    print("[render] Thumbnails from publish.json (no LLM call)")
    run_render(
        ctx.stage_dir("publish") / "publish.json",
        ctx.stage_dir("render") / "render.json",
        ctx.cfg,
        markdown=ctx.stage_dir("render") / "render.md",
        run=run_magick,
    )


def _render_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("render") / "render.json"]


STAGES = [
    Stage("transcription", _transcription_run, _transcription_required),
    Stage("audio_silence", _audio_silence_run, _audio_silence_required),
    Stage("sentence_split", _sentence_split_run, _sentence_split_required),
    Stage("gap_context", _gap_context_run, _gap_context_required),
    Stage("summary", _summary_run, _summary_required),
    Stage("text_filter", _text_filter_run, _text_filter_required),
    Stage("plan", _plan_run, _plan_required),
    Stage("plan_revise", _plan_revise_run),
    Stage("director", _director_run, _director_required),
    Stage("guided_edit", _guided_edit_run, _guided_edit_required),
    Stage("intervals", _intervals_run, _intervals_required),
    Stage("blender", _blender_run),
    Stage("publish", _publish_run, _publish_required),
    Stage("render", _render_run, _render_required),
]
