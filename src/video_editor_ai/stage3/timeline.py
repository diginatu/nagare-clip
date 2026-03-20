"""Timeline computation and VSE strip placement."""

from __future__ import annotations

import logging

import bpy


def sec_to_frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def build_timeline_map(
    keep_intervals: list,
    effective_fps: float,
    source_fps: float,
) -> list:
    """
    Returns a list of dicts, each describing one placed keep interval:
      src_start, src_end: original source seconds
      tl_start: first frame on the output timeline (1-based)
      tl_end:   last frame (exclusive) on the output timeline
    This must mirror the strip placement loop exactly.
    """
    mapping = []
    cursor = 1
    for interval in keep_intervals:
        start_sec = float(interval["start"])
        end_sec = float(interval["end"])
        if end_sec <= start_sec:
            continue
        src_start_frame = max(0, sec_to_frames(start_sec, effective_fps))
        src_end_frame = max(src_start_frame + 1, sec_to_frames(end_sec, effective_fps))
        # Mirror the clamping logic from the strip loop
        # full_duration is not available here, so use src_end_frame as
        # an upper bound — clamping only matters at the very end of the
        # source clip and will not affect most captions
        keep_frame_count = src_end_frame - src_start_frame
        mapping.append(
            {
                "src_start": start_sec,
                "src_end": end_sec,
                "tl_start": cursor,
                "tl_end": cursor + keep_frame_count,
            }
        )
        cursor += keep_frame_count
    return mapping


def place_strips(
    keep_intervals: list,
    source_path: str,
    sequence_collection: object,
    effective_fps: float,
) -> int:
    """Place video+audio strip pairs on the timeline.

    Returns the final timeline cursor position (one past the last frame).
    """
    timeline_cursor = 1

    for idx, interval in enumerate(keep_intervals, start=1):
        start_sec = float(interval["start"])
        end_sec = float(interval["end"])
        if end_sec <= start_sec:
            continue

        src_start_frame = max(0, sec_to_frames(start_sec, effective_fps))
        src_end_frame = max(src_start_frame + 1, sec_to_frames(end_sec, effective_fps))

        logging.info(
            "Strip %d: source %.3fs-%.3fs -> frames %d-%d",
            idx,
            start_sec,
            end_sec,
            src_start_frame,
            src_end_frame,
        )

        strip = sequence_collection.new_movie(
            name=f"keep_{idx:04d}",
            filepath=source_path,
            channel=1,
            frame_start=timeline_cursor,
        )

        full_duration = max(1, int(strip.frame_duration))
        bounded_start = min(src_start_frame, full_duration - 1)
        bounded_end = min(max(src_end_frame, bounded_start + 1), full_duration)
        keep_frame_count = bounded_end - bounded_start

        if bounded_start != src_start_frame or bounded_end != src_end_frame:
            logging.warning(
                "Strip %d: interval clamped to clip duration (%d frames). "
                "Requested frames %d-%d, applied %d-%d",
                idx,
                full_duration,
                src_start_frame,
                src_end_frame,
                bounded_start,
                bounded_end,
            )

        strip.frame_start = timeline_cursor - bounded_start
        strip.frame_offset_start = bounded_start
        strip.frame_offset_end = full_duration - bounded_end
        strip.channel = 1

        sound_strip = sequence_collection.new_sound(
            name=f"keep_{idx:04d}_audio",
            filepath=source_path,
            channel=2,
            frame_start=timeline_cursor - bounded_start,
        )
        sound_full_duration = max(1, int(sound_strip.frame_duration))
        sound_strip.frame_offset_start = bounded_start
        sound_strip.frame_offset_end = sound_full_duration - (
            bounded_start + keep_frame_count
        )
        if sound_strip.frame_offset_end < 0:
            sound_strip.frame_offset_end = 0

        # Deselect all strips, then select only this video+audio pair
        for s in sequence_collection:
            s.select = False
        strip.select = True
        sound_strip.select = True

        # Connect requires a sequencer area context -- build a temporary override
        sequencer_area = None
        window = None
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "SEQUENCE_EDITOR":
                    sequencer_area = area
                    break
            if sequencer_area:
                break

        if sequencer_area is not None and window is not None:
            with bpy.context.temp_override(
                window=window,
                area=sequencer_area,
                region=sequencer_area.regions[-1],
            ):
                bpy.ops.sequencer.connect(toggle=False)
        else:
            logging.warning(
                "Strip %d: no SEQUENCE_EDITOR area found, skipping connect.", idx
            )

        if strip.frame_final_duration != keep_frame_count:
            logging.warning(
                "Strip %d: frame_final_duration=%d differs from keep_frame_count=%d",
                idx,
                strip.frame_final_duration,
                keep_frame_count,
            )

        logging.info(
            "Strip %d: frame_start=%d frame_offset_start=%d frame_offset_end=%d "
            "keep_frames=%d timeline_cursor=%d",
            idx,
            timeline_cursor - bounded_start,
            bounded_start,
            full_duration - bounded_end,
            keep_frame_count,
            timeline_cursor,
        )

        timeline_cursor += keep_frame_count

    return timeline_cursor


def place_captions(
    captions: list,
    tl_map: list,
    effective_fps: float,
    sequence_collection: object,
    *,
    caption_style: dict | None = None,
) -> None:
    """Place text caption strips on channel 3 of the timeline."""
    for cap in captions:
        cap_src_start = float(cap["start"])
        cap_src_end = float(cap["end"])
        text = cap.get("text", "").strip()
        if not text:
            continue

        tl_start = None
        tl_end = None
        length = None
        for entry in tl_map:
            if cap_src_start < entry["src_end"] and cap_src_end > entry["src_start"]:
                clamped_start = max(cap_src_start, entry["src_start"])
                clamped_end = min(cap_src_end, entry["src_end"])
                offset_start = sec_to_frames(
                    clamped_start - entry["src_start"], effective_fps
                )
                duration_sec = clamped_end - clamped_start
                length = max(1, sec_to_frames(duration_sec, effective_fps))
                tl_start = entry["tl_start"] + offset_start
                tl_end = tl_start + length
                break

        if tl_start is None or tl_end is None or length is None or tl_end <= tl_start:
            logging.warning(
                "Caption skipped (no matching keep interval): %r", text[:60]
            )
            continue
        text_strip = sequence_collection.new_effect(
            name=f"cap_{cap_src_start:.3f}",
            type="TEXT",
            channel=3,
            frame_start=tl_start,
            length=length,
        )
        style = caption_style or {}
        text_strip.text = text
        text_strip.font_size = style.get("font_size", 50)
        text_strip.alignment_x = style.get("alignment_x", "CENTER")
        text_strip.anchor_y = style.get("anchor_y", "BOTTOM")
        text_strip.location[0] = style.get("location_x", 0.5)
        text_strip.location[1] = style.get("location_y", 0.05)
        text_strip.use_shadow = style.get("use_shadow", True)
        logging.info(
            "Caption '%s': timeline frames %d-%d",
            text[:40],
            tl_start,
            tl_end,
        )
