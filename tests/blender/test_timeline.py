"""Integration tests for blender timeline strip placement.

Tests run place_strips() inside a real Blender process via
``blender --background --python`` and assert on the resulting strip data.
A single Blender invocation is shared across all tests via a module-scoped fixture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BLENDER = shutil.which("blender")
SCRIPT = Path(__file__).parent / "blender_place_strips.py"

pytestmark = pytest.mark.skipif(BLENDER is None, reason="blender not on PATH")


@pytest.fixture(scope="module")
def test_video(tmp_path_factory) -> Path:
    """Generate a 10-second silent test video with ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not on PATH")
    vid = tmp_path_factory.mktemp("media") / "test.mp4"
    subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=10",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "10",
            "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
            "-c:a", "aac", "-b:a", "64k",
            str(vid),
        ],
        check=True,
        capture_output=True,
    )
    return vid


@pytest.fixture(scope="module")
def blender_result(test_video, tmp_path_factory) -> dict:
    """Run the Blender-side script once and return parsed JSON result."""
    out_json = tmp_path_factory.mktemp("blender") / "result.json"
    result = subprocess.run(
        [
            BLENDER, "--background", "--factory-startup", "--python", str(SCRIPT),
            "--", str(test_video), str(out_json),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Blender exited with code {result.returncode}\n"
            f"--- stdout ---\n{result.stdout[-2000:]}\n"
            f"--- stderr ---\n{result.stderr[-2000:]}"
        )
    return json.loads(out_json.read_text())


def test_place_strips_channels(blender_result):
    """All video strips on channel 1, all sound strips on channel 2."""
    video_strips = [s for s in blender_result["strips"] if s["type"] == "MOVIE"]
    sound_strips = [s for s in blender_result["strips"] if s["type"] == "SOUND"]

    assert len(video_strips) == 4
    assert len(sound_strips) == 4

    for s in video_strips:
        assert s["channel"] == 1, f"{s['name']} on channel {s['channel']}, expected 1"
    for s in sound_strips:
        assert s["channel"] == 2, f"{s['name']} on channel {s['channel']}, expected 2"


def test_place_strips_not_muted(blender_result):
    """Output strips are not muted."""
    for s in blender_result["strips"]:
        assert not s["mute"], f"{s['name']} should not be muted"


def test_place_strips_templates_deleted(blender_result):
    """Template strips should be cleaned up."""
    names = [s["name"] for s in blender_result["strips"]]
    assert not any("tmpl" in n for n in names), f"Template strips remain: {names}"


def test_place_strips_cursor_and_offsets(blender_result):
    """Cursor advances correctly; sped-up interval shortens its timeline contribution."""
    fps = blender_result["effective_fps"]
    fps_int = round(fps)

    # 3 unsped 1-second intervals + 1 sped-up 1-second interval (speed=2.0)
    expected_total_frames = 3 * fps_int + max(1, round(fps_int / 2.0))
    assert blender_result["cursor"] == 1 + expected_total_frames

    video_strips = sorted(
        [s for s in blender_result["strips"] if s["type"] == "MOVIE"],
        key=lambda s: s["name"],
    )
    assert video_strips[0]["frame_offset_start"] == 0
    assert video_strips[1]["frame_offset_start"] == round(2.0 * fps)
    assert video_strips[2]["frame_offset_start"] == round(4.0 * fps)
    assert video_strips[3]["frame_offset_start"] == round(6.0 * fps)


def test_place_strips_strip_count(blender_result):
    """4 video + 4 sound = 8 strips total (retiming, no Speed Control effects)."""
    assert blender_result["strip_count"] == 8


def test_place_strips_no_speed_effects(blender_result):
    """Retiming replaces Speed Control effect strips — none should be present."""
    speed_strips = [s for s in blender_result["strips"] if s["type"] == "SPEED"]
    assert len(speed_strips) == 0


def test_place_strips_retiming_duration_halved(blender_result):
    """Sped-up video strip duration = round(keep_frames / speed) via retiming."""
    movie_strips = sorted(
        [s for s in blender_result["strips"] if s["type"] == "MOVIE"],
        key=lambda s: s["name"],
    )
    sped_strip = movie_strips[3]  # keep_0004: source [6,7] speed_factor=2.0
    fps_int = round(blender_result["effective_fps"])
    adjusted = max(1, round(fps_int / 2.0))
    assert sped_strip["frame_final_duration"] == adjusted


def test_place_strips_retiming_sound_duration_halved(blender_result):
    """Sped-up sound strip duration also halved via retiming (no muting needed)."""
    sound_strips = sorted(
        [s for s in blender_result["strips"] if s["type"] == "SOUND"],
        key=lambda s: s["name"],
    )
    sped_sound = sound_strips[3]  # keep_0004_audio: speed_factor=2.0
    fps_int = round(blender_result["effective_fps"])
    adjusted = max(1, round(fps_int / 2.0))
    assert sped_sound["frame_final_duration"] == adjusted


def test_place_strips_unsped_sound_not_muted(blender_result):
    """Sound strips in non-sped intervals are not muted."""
    sound_by_name = {s["name"]: s for s in blender_result["strips"] if s["type"] == "SOUND"}
    for name, s in sound_by_name.items():
        if name != "keep_0004_audio":
            assert not s["mute"], f"{name} should not be muted"


def test_place_strips_sound_pitch_correction_preserved(blender_result):
    """Source sound strip in sped interval keeps pitch_correction=True (Blender auto-pitch)."""
    sound_by_name = {s["name"]: s for s in blender_result["strips"] if s["type"] == "SOUND"}
    sped_sound = sound_by_name.get("keep_0004_audio")
    assert sped_sound is not None, "keep_0004_audio not found in strips"
    assert sped_sound["pitch_correction"] is True
