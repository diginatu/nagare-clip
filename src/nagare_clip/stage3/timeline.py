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
    start_cursor: int = 1,
) -> list:
    """
    Returns a list of dicts, each describing one placed keep interval:
      src_start, src_end: original source seconds
      tl_start: first frame on the output timeline (1-based)
      tl_end:   last frame (exclusive) on the output timeline
    This must mirror the strip placement loop exactly.
    """
    mapping = []
    cursor = start_cursor
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


def _get_sequencer_context():
    """Find a SEQUENCE_EDITOR area and return (window, area) or (None, None)."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "SEQUENCE_EDITOR":
                return window, area
    return None, None


def _sequencer_op(window, area, op_func, **kwargs):
    """Run a bpy.ops.sequencer operation with a temp_override context.

    Returns True if the operation ran, False if no SEQUENCE_EDITOR area.
    """
    if area is None or window is None:
        return False
    with bpy.context.temp_override(
        window=window, area=area, region=area.regions[-1],
    ):
        op_func(**kwargs)
    return True


def _deselect_all(sequence_collection):
    """Deselect every strip in the collection."""
    for s in sequence_collection:
        s.select = False


def place_strips(
    keep_intervals: list,
    source_path: str,
    sequence_collection: object,
    effective_fps: float,
    start_cursor: int = 1,
    idx_offset: int = 0,
    source_num: int | None = None,
) -> int:
    """Place video+audio strip pairs on the timeline.

    Uses a template-duplicate pattern: the source file is opened only once
    (as a muted template pair on high channels), then duplicated per interval.
    This avoids opening a new FFmpeg decoder per interval and keeps memory
    usage constant regardless of interval count.

    Returns the final timeline cursor position (one past the last frame).
    """
    src_tag = f"[src {source_num}] " if source_num is not None else ""
    timeline_cursor = start_cursor

    # --- Phase A: create template strips (opened once, reused via duplicate) ---
    TEMPLATE_VIDEO_CH = 10
    TEMPLATE_SOUND_CH = 11

    tmpl_video = sequence_collection.new_movie(
        name="_tmpl_video",
        filepath=source_path,
        channel=TEMPLATE_VIDEO_CH,
        frame_start=1,
    )
    tmpl_video.mute = True
    full_duration = max(1, int(tmpl_video.frame_duration))

    tmpl_sound = sequence_collection.new_sound(
        name="_tmpl_sound",
        filepath=source_path,
        channel=TEMPLATE_SOUND_CH,
        frame_start=1,
    )
    tmpl_sound.mute = True
    sound_full_duration = max(1, int(tmpl_sound.frame_duration))

    logging.info(
        "%sTemplate strips created: video %d frames, sound %d frames",
        src_tag,
        full_duration,
        sound_full_duration,
    )

    window, sequencer_area = _get_sequencer_context()

    # --- Phase B: duplicate templates for each keep interval ---
    for idx, interval in enumerate(keep_intervals, start=1 + idx_offset):
        start_sec = float(interval["start"])
        end_sec = float(interval["end"])
        if end_sec <= start_sec:
            continue

        src_start_frame = max(0, sec_to_frames(start_sec, effective_fps))
        src_end_frame = max(src_start_frame + 1, sec_to_frames(end_sec, effective_fps))

        logging.info(
            "%sStrip %d: source %.3fs-%.3fs -> frames %d-%d",
            src_tag,
            idx,
            start_sec,
            end_sec,
            src_start_frame,
            src_end_frame,
        )

        bounded_start = min(src_start_frame, full_duration - 1)
        bounded_end = min(max(src_end_frame, bounded_start + 1), full_duration)
        keep_frame_count = bounded_end - bounded_start

        if bounded_start != src_start_frame or bounded_end != src_end_frame:
            logging.warning(
                "%sStrip %d: interval clamped to clip duration (%d frames). "
                "Requested frames %d-%d, applied %d-%d",
                src_tag,
                idx,
                full_duration,
                src_start_frame,
                src_end_frame,
                bounded_start,
                bounded_end,
            )

        # Deselect all, then select only templates
        _deselect_all(sequence_collection)
        tmpl_video.select = True
        tmpl_sound.select = True

        if not _sequencer_op(window, sequencer_area, bpy.ops.sequencer.duplicate):
            logging.warning(
                "%sStrip %d: no SEQUENCE_EDITOR area found, cannot duplicate.",
                src_tag, idx
            )
            continue

        # Find the newly duplicated strips (duplicate deselects originals)
        new_video = None
        new_sound = None
        for s in sequence_collection:
            if not s.select:
                continue
            if s.type == "MOVIE" and new_video is None:
                new_video = s
            elif s.type == "SOUND" and new_sound is None:
                new_sound = s

        if new_video is None or new_sound is None:
            logging.warning(
                "%sStrip %d: duplicate did not produce expected strips, skipping.",
                src_tag, idx
            )
            continue

        # Configure the duplicated video strip
        # Set frame position and offsets before channel so Blender sees the
        # trimmed range and does not reject channel 1 due to overlap.
        new_video.name = f"keep_{idx:04d}"
        new_video.mute = False
        new_video.frame_start = timeline_cursor - bounded_start
        new_video.frame_offset_start = bounded_start
        new_video.frame_offset_end = full_duration - bounded_end
        new_video.channel = 1

        # Configure the duplicated sound strip
        new_sound.name = f"keep_{idx:04d}_audio"
        new_sound.mute = False
        new_sound.frame_start = timeline_cursor - bounded_start
        new_sound.frame_offset_start = bounded_start
        new_sound.frame_offset_end = sound_full_duration - (
            bounded_start + keep_frame_count
        )
        if new_sound.frame_offset_end < 0:
            new_sound.frame_offset_end = 0
        new_sound.channel = 2

        # Connect the video+audio pair
        _deselect_all(sequence_collection)
        new_video.select = True
        new_sound.select = True
        _sequencer_op(window, sequencer_area, bpy.ops.sequencer.connect, toggle=False)

        if new_video.frame_final_duration != keep_frame_count:
            logging.warning(
                "%sStrip %d: frame_final_duration=%d differs from keep_frame_count=%d",
                src_tag,
                idx,
                new_video.frame_final_duration,
                keep_frame_count,
            )

        logging.info(
            "%sStrip %d: frame_start=%d frame_offset_start=%d frame_offset_end=%d "
            "keep_frames=%d timeline_cursor=%d",
            src_tag,
            idx,
            timeline_cursor - bounded_start,
            bounded_start,
            full_duration - bounded_end,
            keep_frame_count,
            timeline_cursor,
        )

        timeline_cursor += keep_frame_count

    # --- Phase C: delete template strips ---
    _deselect_all(sequence_collection)
    tmpl_video.select = True
    tmpl_sound.select = True

    if _sequencer_op(window, sequencer_area, bpy.ops.sequencer.delete):
        logging.info("%sTemplate strips deleted.", src_tag)
    else:
        logging.warning(
            "%sCould not delete template strips: no SEQUENCE_EDITOR area.", src_tag
        )

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
