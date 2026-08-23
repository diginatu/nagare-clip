"""The declarative stage registry: one Stage entry per pipeline stage.

Adapters translate the PipelineContext into each stage's typed run()
function (or external command), print the same progress lines the bash
orchestrator echoed, and own the per-stage LLM-report recorder lifecycle
(clear once before the stem loop, rebuild the index after).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.blender.warnings_file import WARNINGS_FILENAME
from nagare_clip.cut_report.report import build_cut_report
from nagare_clip.director.director_llm import DirectorOp, collect_overlay_texts, ops_from_dict
from nagare_clip.director.run import run_director
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
from nagare_clip.intervals.run import run_intervals
from nagare_clip.llm_report import recorder_from_config
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
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.plan.run import run_plan
from nagare_clip.plan_revise.run import run_plan_revise
from nagare_clip.publish.run import run_publish
from nagare_clip.publish.thumbnail import ThumbRender, render_sets
from nagare_clip.publish.thumbs import (
    ThumbCandidate,
    ThumbShot,
    cap_candidates,
    select_candidates,
)
from nagare_clip.publish.thumbs import (
    frame_relpath as thumb_relpath,
)
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
        )
    finally:
        rec.rebuild_index()


# --- director ----------------------------------------------------------------


def _director_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "director")
    rec.clear()
    # The concatenation order of the whole project, not just of this run: a
    # `--source` re-run still edits one slice of the same finished video, and the
    # director is told which slice.  Falls back to the processed sources when the
    # input dir yields nothing readable.
    timeline_stems = project_stems(ctx.input_videos_dir) or ctx.stems
    director_dir = ctx.stage_dir("director")
    text_filter_dir = ctx.stage_dir("text_filter")

    def _neighbour(index: int, offset: int) -> Path | None:
        """The transcript of the video playing right before/after this one.

        ``text_filter`` has already run for every source by the time the director
        starts, so both sides can be read off disk — including the one that plays
        *after* this video, whose ops do not exist yet.
        """
        n = index + offset
        if index < 0 or not 0 <= n < len(timeline_stems):
            return None
        return text_filter_dir / f"{timeline_stems[n]}_edits.txt"

    try:
        for src in ctx.sources:
            print(f"[director] Edit operations: {src.stem}")
            # Every video playing earlier already has its ops on disk (this loop
            # writes them in order), so their captions can be read back.
            index = timeline_stems.index(src.stem) if src.stem in timeline_stems else -1
            earlier = timeline_stems[:index] if index >= 0 else []
            run_director(
                text_filter_dir / f"{src.stem}_edits.txt",
                ctx.stage_dir("director") / f"{src.stem}_director.json",
                ctx.cfg,
                summary=ctx.stage_dir("summary") / "summary.json",
                plan=_effective_plan_json(ctx),
                stem=src.stem,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                gaps=ctx.stage_dir("gap_context") / f"{src.stem}_gaps.json",
                cuts_txt=ctx.stage_dir("audio_silence") / f"{src.stem}_cuts.txt",
                all_stems=timeline_stems,
                prior_director_paths=[director_dir / f"{s}_director.json" for s in earlier],
                before_edits=_neighbour(index, -1),
                after_edits=_neighbour(index, 1),
                recorder=rec,
            )
    finally:
        _write_divergence_note(ctx)
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


def _intervals_run(ctx: PipelineContext) -> None:
    for src in ctx.sources:
        print(f"[intervals] Patch application + keep intervals: {src.stem}")
        run_intervals(
            ctx.stage_dir("guided_edit") / f"{src.stem}_edits.txt",
            ctx.stage_dir("sentence_split") / f"{src.stem}.json",
            ctx.stage_dir("intervals") / f"{src.stem}_intervals.json",
            ctx.cfg,
            cuts_txt=ctx.stage_dir("audio_silence") / f"{src.stem}_cuts.txt",
        )
    write_cut_report(ctx)


CUT_REPORT_NOTE = "cut_report.md"


def _load_intervals(ctx: PipelineContext) -> list[tuple[str, dict]]:
    """Every source's intervals JSON, in the order blender concatenates them.

    Each file is optional: the report is a courtesy, not a gate, so a source
    whose JSON is missing or unreadable is skipped with a warning rather than
    failing the stage that just succeeded.
    """
    out: list[tuple[str, dict]] = []
    for stem in ctx.stems:
        path = ctx.stage_dir("intervals") / f"{stem}_intervals.json"
        if not path.is_file():
            continue
        try:
            out.append((stem, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as e:
            logging.warning("cut_report: could not read %s: %s", path, e)
    return out


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
        sources = _load_intervals(ctx)
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
    output_blend = ctx.stage_dir("blender") / f"{ctx.stems[0]}_edited.blend"
    intervals_paths = [ctx.stage_dir("intervals") / f"{s}_intervals.json" for s in ctx.stems]
    run_command(
        build_blender_cmd(
            ctx.project_root,
            [s.abs_path for s in ctx.sources],
            intervals_paths,
            output_blend,
            ctx.config_path,
            ctx.log_file,
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


def _render_thumbnails(
    ctx: PipelineContext, sets: Sequence[Any], thumbs: Sequence[ThumbShot]
) -> list[ThumbRender]:
    """Composite each copy set over the chosen still (host ImageMagick)."""
    return render_sets(
        sets, thumbs, ctx.cfg["publish"]["thumbnail"], ctx.stage_dir("publish"), run_magick
    )


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
            stems=ctx.stems,
            intervals_paths=[ctx.stage_dir("intervals") / f"{s}_intervals.json" for s in ctx.stems],
            plan_json=_effective_plan_json(ctx),
            overlay_texts=overlay_texts,
            thumbs=thumbs,
            markdown=d / "publish.md",
            recorder=rec,
            render=lambda sets, shots: _render_thumbnails(ctx, sets, shots),
        )
    finally:
        rec.rebuild_index()


def _publish_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("publish") / "publish.json"]


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
]
