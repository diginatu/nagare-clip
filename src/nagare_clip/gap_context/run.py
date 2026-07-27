"""gap_context stage: describe the long silent gaps of one video.

Frame extraction (ffmpeg in the whisperx image) happens in the pipeline
adapter, which hands the extracted ``GapFrames`` here.  When
``gap_context.enabled`` is false (default) an empty gap list is written, so the
summary/director prompts stay byte-identical and the pipeline behaves exactly
as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nagare_clip.gap_context.describe import GapFrames, describe_gap
from nagare_clip.gap_context.gaps import Gap, gaps_to_dict
from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_report import NULL_RECORDER, OK, Recorder
from nagare_clip.timing import segment_times


def _neighbour_lines(
    gf: GapFrames, segments: list[dict[str, Any]], seg_times, count: int
) -> tuple[list[str], list[str]]:
    """Up to *count* lines ending at/before the gap and starting at/after it.

    Both lists are in transcript order, so the line nearest the gap is last in
    *before* and first in *after*.
    """
    before: list[str] = []
    after: list[str] = []
    if count <= 0:
        return before, after
    for (start, end), seg in zip(seg_times, segments, strict=False):
        text = seg.get("text", "") if isinstance(seg, dict) else ""
        if not isinstance(text, str) or not text.strip():
            continue
        if end is not None and end <= gf.start + 0.01:
            before.append(text.strip())
        elif len(after) < count and start is not None and start >= gf.end - 0.01:
            after.append(text.strip())
    return before[-count:], after


def run_gap_context(
    gap_frames: list[GapFrames],
    output: Path,
    cfg: dict,
    *,
    stem: str,
    json_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    gc_cfg = cfg["gap_context"]

    gaps: list[Gap] = []
    if not gc_cfg.get("enabled", False):
        logging.info("gap_context: disabled, writing empty gap list")
    else:
        segments: list[dict[str, Any]] = []
        seg_times: list = []
        if json_path and Path(json_path).is_file():
            try:
                data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                segments = data.get("segments", []) or []
                seg_times = segment_times(data)
            except (ValueError, OSError):
                logging.warning("gap_context: could not read --json %s", json_path)
        context_lines = gc_cfg.get("context_lines", 1)
        threshold = float(gc_cfg.get("static_ssim", 0.0))
        logging.info("gap_context: describing %d gap(s) for %s", len(gap_frames), stem)
        for i, gf in enumerate(gap_frames):
            if threshold > 0.0 and gf.ssim is not None and gf.ssim >= threshold:
                unit = f"{stem}_gap{i + 1:02d}"
                recorder.begin(unit)
                recorder.flush_unit(
                    unit, outcome=OK, reason=f"static prefilter (ssim {gf.ssim:.4f})"
                )
                gaps.append(
                    Gap(
                        start=gf.start,
                        end=gf.end,
                        frames=gf.relpaths,
                        description=(
                            f"static (prefilter: frames nearly identical, ssim {gf.ssim:.3f})"
                        ),
                        static=True,
                    )
                )
                continue
            before: list[str] = []
            after: list[str] = []
            if segments and seg_times:
                before, after = _neighbour_lines(gf, segments, seg_times, context_lines)
            gap = describe_gap(
                gf,
                gc_cfg,
                unit=f"{stem}_gap{i + 1:02d}",
                before=before,
                after=after,
                call_llm=_call_llm,
                recorder=recorder,
            )
            if gap is not None:
                gaps.append(gap)
        logging.info("gap_context: %d gap(s) described for %s", len(gaps), stem)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(gaps_to_dict(gaps), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("gap_context: wrote %s", output)
