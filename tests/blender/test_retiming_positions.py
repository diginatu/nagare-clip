"""Regression test: retimed strips must land at their computed timeline
positions.

Blender 5.1's retiming operators corrupt ``content_start`` of placed strips
(and displace neighbours) once a scene contains several retimed strips at
large source-frame offsets — the shared ``retiming_keys`` C-pointer bug.
``place_strips()`` re-pins every strip's visible start after all operators
run (Phase D). This exercises the real Blender flow at a scale that triggers
the corruption and asserts the final positions are contiguous/correct.

Requires Blender + ffmpeg on PATH; skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BLENDER = shutil.which("blender")
SCRIPT = Path(__file__).parent / "blender_retiming_positions.py"

pytestmark = pytest.mark.skipif(BLENDER is None, reason="blender not on PATH")


@pytest.fixture(scope="module")
def long_video(tmp_path_factory) -> Path:
    """A 20-minute 60fps silent video so clips reach large frame offsets."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not on PATH")
    vid = tmp_path_factory.mktemp("media") / "long.mp4"
    subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=60:d=1200",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1200",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "600",
            "-c:a", "aac", "-b:a", "64k",
            str(vid),
        ],
        check=True,
        capture_output=True,
    )
    return vid


@pytest.fixture(scope="module")
def retiming_result(long_video, tmp_path_factory) -> dict:
    out_json = tmp_path_factory.mktemp("blender") / "result.json"
    result = subprocess.run(
        [
            BLENDER, "--background", "--factory-startup", "--python", str(SCRIPT),
            "--", str(long_video), str(out_json),
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Blender exited with code {result.returncode}\n"
            f"--- stdout ---\n{result.stdout[-2000:]}\n"
            f"--- stderr ---\n{result.stderr[-2000:]}"
        )
    return json.loads(out_json.read_text())


def test_scenario_contains_multiple_speed_strips(retiming_result):
    """Guard the scenario itself: it must keep enough retimed strips to
    trigger the corruption (otherwise the test cannot catch a regression)."""
    sped = [s for s in retiming_result["strips"] if s["speed_factor"] != 1.0]
    assert len(sped) >= 3, f"scenario lost its speed strips: {sped}"


def test_every_strip_at_expected_position(retiming_result):
    """Each placed strip's visible start must equal the position
    build_timeline_map() computed for it. Without Phase D the retiming
    operators leave many strips shifted by their un-retimed length."""
    misplaced = [
        s for s in retiming_result["strips"]
        if s["actual_start"] != s["expected_start"]
    ]
    assert not misplaced, (
        "strips not at computed positions: "
        + ", ".join(
            f"{s['name']} exp={s['expected_start']} act={s['actual_start']}"
            for s in misplaced[:10]
        )
    )
