"""The .blend's real length vs the timeline the publish tools compute (no LLM)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from nagare_clip.publish.blend_check import (
    MISMATCH_SECONDS,
    BlendRange,
    blend_info_cmd,
    mismatch_warning,
    parse_blend_info,
    read_blend_range,
)


def test_seconds_counts_frame_end_inclusive():
    # the real case: 60 fps, frames 0..100518 -> the rendered mp4's 1675.328 s
    assert BlendRange(60, 1.0, 0, 100518).seconds == pytest.approx(100519 / 60)


def test_seconds_honours_fps_base():
    assert BlendRange(30000, 1001.0, 1, 300).seconds == pytest.approx(300 * 1001 / 30000)


def test_cmd_opens_the_blend_headless_without_user_settings():
    cmd = blend_info_cmd(Path("/x/a_edited.blend"))
    assert cmd[0] == "blender"
    assert "--background" in cmd and "--factory-startup" in cmd
    assert "/x/a_edited.blend" in cmd
    assert "--python-expr" in cmd


def test_parse_finds_the_marked_line_among_blenders_noise():
    out = (
        "Blender 5.2.2 LTS\nRead blend: x\n"
        'NAGARE_BLEND_INFO {"fps": 60, "fps_base": 1.0, "frame_start": 0, "frame_end": 99}\n'
        "Blender quit\n"
    )
    assert parse_blend_info(out) == BlendRange(60, 1.0, 0, 99)


@pytest.mark.parametrize(
    "out",
    [
        "Blender quit\n",
        'NAGARE_BLEND_INFO {"fps": 0, "fps_base": 1.0, "frame_start": 0, "frame_end": 9}\n',
        'NAGARE_BLEND_INFO {"fps": 60}\n',
        "NAGARE_BLEND_INFO not json\n",
    ],
)
def test_parse_refuses_what_it_cannot_measure(out):
    with pytest.raises(ValueError):
        parse_blend_info(out)


def test_read_runs_the_command_and_parses():
    seen = []

    def run(cmd):
        seen.append(cmd)
        return 'NAGARE_BLEND_INFO {"fps": 25, "fps_base": 1, "frame_start": 1, "frame_end": 250}\n'

    rng = read_blend_range(Path("b.blend"), run=run)
    assert rng.seconds == pytest.approx(10.0)
    assert seen == [blend_info_cmd(Path("b.blend"))]


def test_no_warning_below_the_threshold():
    assert MISMATCH_SECONDS == 1.0
    assert mismatch_warning(100.0, 100.0 + 0.99, Path("b.blend")) is None
    assert mismatch_warning(100.0, 100.0 - 0.99, Path("b.blend")) is None


def test_warns_at_the_threshold_either_way():
    assert mismatch_warning(100.0, 101.0, Path("b.blend")) is not None
    assert mismatch_warning(100.0, 99.0, Path("b.blend")) is not None


def test_warning_states_both_lengths_the_difference_the_risk_and_the_fix():
    text = mismatch_warning(1699.4, 100519 / 60, Path("/v/out/blender/a_edited.blend"))
    assert text is not None
    for part in ("28:19", "1699.4 s", "27:55", "1675.3 s", "0:24", "24.1 s", "shorter"):
        assert part in text, part
    assert "/v/out/blender/a_edited.blend" in text
    assert "chapter" in text and "thumbnail" in text
    assert "director" in text and "blender" in text
    assert "rendered video" in text


def test_warning_says_longer_when_the_blend_grew():
    text = mismatch_warning(60.0, 75.5, Path("b.blend"))
    assert text is not None and "longer" in text and "0:15" in text and "15.5 s" in text


@pytest.mark.skipif(shutil.which("blender") is None, reason="blender not on PATH")
def test_reads_a_real_blend(tmp_path):
    blend = tmp_path / "t.blend"
    script = (
        "import bpy; s = bpy.context.scene; s.render.fps = 30; s.render.fps_base = 1.0; "
        "s.frame_start = 0; s.frame_end = 299; "
        f"bpy.ops.wm.save_as_mainfile(filepath={str(blend)!r}, compress=True)"
    )
    subprocess.run(
        ["blender", "--background", "--factory-startup", "--python-expr", script],
        check=True,
        capture_output=True,
    )
    rng = read_blend_range(blend)
    assert rng == BlendRange(30, 1.0, 0, 299)
    assert rng.seconds == pytest.approx(10.0)
