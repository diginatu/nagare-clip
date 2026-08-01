"""publish stage: title/description/chapters/thumbnail material after blender.

Everything needed to publish the cut already exists in the pipeline —
``summary.json`` (whole-video, per-video and per-part summaries), ``plan.json``
(the editorial shape), the ``project:`` brief, the director's ops and the
intervals stage's keep spans. This stage collects them into one reviewable
file pair and leaves the upload (and the thumbnail compositing) to the human.

When ``publish.enabled`` is false (default) it writes an empty artifact and
makes no LLM or Docker call, exactly like every other LLM stage.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.brief import apply_brief
from nagare_clip.director.director_llm import ops_from_dict
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.publish import publish_llm
from nagare_clip.publish.chapters import apply_titles, build_chapters, chapter_issues
from nagare_clip.publish.render import empty_publish_data, publish_to_dict, render_markdown
from nagare_clip.publish.thumbs import ThumbCandidate, limit_candidates, select_candidates
from nagare_clip.publish.timeline import build_edit_map, total_duration
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_times


def _load_json(path: Path | None) -> dict:
    """Read a JSON dict; a missing/unreadable file degrades to ``{}``."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logging.warning("publish: could not read %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def run_publish(
    stems: list[str],
    output_json: Path,
    output_md: Path,
    cfg: dict,
    *,
    intervals_paths: list[Path],
    summary_json: Path | None = None,
    plan_json: Path | None = None,
    director_paths: list[Path] | None = None,
    json_paths: list[Path] | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> list[ThumbCandidate]:
    """Write ``publish.json`` + ``publish.md``; return the frame shortlist.

    The candidate frames are returned rather than extracted here: pulling
    stills goes through ffmpeg in the whisperx image, which is the pipeline
    adapter's job (no stage runs Docker itself).

    *stems*, *intervals_paths*, *director_paths* and *json_paths* are parallel
    lists in timeline order — the same order the blender stage concatenates.
    """
    publish_cfg = cfg["publish"]
    enabled = publish_cfg.get("enabled", False)

    if not enabled:
        logging.info("publish: disabled, writing empty publish material")
        data = empty_publish_data()
        candidates: list[ThumbCandidate] = []
    else:
        placed = build_edit_map(
            [(stem, _load_json(path)) for stem, path in zip(stems, intervals_paths)]
        )
        duration = total_duration(placed)
        project: ProjectSummary = summary_from_dict(_load_json(summary_json))
        directions = plan_from_dict(_load_json(plan_json))

        chapters = build_chapters(
            project.parts,
            placed,
            duration,
            min_chapter=publish_cfg.get("min_chapter", 10.0),
        )
        logging.info(
            "publish: %d chapter(s) from %d part(s) over %.1fs of finished video",
            len(chapters),
            len(project.parts),
            duration,
        )

        candidates = []
        overlays: list[str] = []
        directors = director_paths or [None] * len(stems)
        jsons = json_paths or [None] * len(stems)
        for i, stem in enumerate(stems):
            seg_times = segment_times(_load_json(jsons[i]))
            # Without the transcript JSON there is nothing to bound line ranges
            # against; the ops are still worth loading for their overlay text,
            # which needs no timing (an unbounded range maps to no moment).
            ops = ops_from_dict(_load_json(directors[i]), num_lines=len(seg_times) or 10**9)
            overlays += [op.text for op in ops if op.type == "overlay" and op.text]
            candidates += select_candidates(ops, seg_times, placed, stem)
        candidates = limit_candidates(candidates, publish_cfg.get("max_frames", 12))

        copy = publish_llm.generate_publish_copy(
            project,
            chapters,
            apply_brief(publish_cfg, cfg),
            directions=directions,
            overlays=overlays,
            duration=duration,
            recorder=recorder,
        )
        chapters = apply_titles(chapters, copy.chapter_titles)
        issues = chapter_issues(
            chapters, duration, min_chapter=publish_cfg.get("min_chapter", 10.0)
        )
        if issues:
            logging.info("publish: chapter list will not qualify: %s", "; ".join(issues))
        data = publish_to_dict(copy, chapters, issues, candidates, duration)

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(render_markdown(data, enabled=enabled), encoding="utf-8")
    logging.info("publish: wrote %s and %s", output_json, output_md)
    return candidates
