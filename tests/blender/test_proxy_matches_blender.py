"""A proxy made by ffmpeg must look, to Blender, like the one Blender makes.

Blender (5.2) builds its own proxy of a rotated test clip; ``build_proxy``
builds one of the same clip with CPU decode + libx264 (the default encoder;
CI has no GPU for the NVDEC half); every stream property Blender's reader could depend on must agree.
Requires blender, ffmpeg and ffprobe on PATH; skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nagare_clip.blender import proxy as px

TOOLS = [shutil.which(t) for t in ("blender", "ffmpeg", "ffprobe")]
SCRIPT = Path(__file__).parent / "blender_build_proxy.py"

pytestmark = pytest.mark.skipif(None in TOOLS, reason="blender/ffmpeg/ffprobe not on PATH")


def _clip(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=638x358:rate=30000/1001:duration=2",
            "-f", "lavfi", "-i", "sine=d=2",
            "-c:v", "libx264", "-bf", "2", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path.with_suffix(".tmp.mp4")),
        ],
        check=True,
    )  # fmt: skip
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-display_rotation", "90",
            "-i", str(path.with_suffix(".tmp.mp4")), "-c", "copy", str(path),
        ],
        check=True,
    )  # fmt: skip


def _vfr_clip(path: Path) -> None:
    """A variable-frame-rate clip, as phones record: uneven frame spacing."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=60:duration=2",
            "-vf", "settb=1/90000,setpts='(N/60+if(mod(N,7),0,0.004)+if(mod(N,11),0,-0.003))/TB'",
            "-fps_mode", "passthrough", "-enc_time_base", "1/90000",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )  # fmt: skip


def _pts(path: Path) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "packet=pts_time", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return sorted(out.split(), key=float)


def _x264_options(path: Path) -> dict:
    """The x264 settings the encoder wrote into the stream (its SEI text),
    minus the ones that depend on the machine (threads)."""
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-c", "copy", "-f", "h264", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    text = raw.split(b"options: ", 1)[1].split(b"\x00", 1)[0].decode("ascii", "replace")
    opts = dict(kv.split("=", 1) for kv in text.split() if "=" in kv)
    for machine in ("threads", "lookahead_threads", "sliced_threads"):
        opts.pop(machine, None)
    return opts


def _shape(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,profile,pix_fmt,width,height,"
            "r_frame_rate,has_b_frames,nb_read_frames:stream_side_data=rotation",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    (stream,) = data["streams"]  # exactly one stream: no audio
    stream.pop("index", None)
    return {"format": data["format"]["format_name"], **stream}


def _keyframes(path: Path) -> list[int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "frame=key_frame", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return [i for i, k in enumerate(out.split()) if k.startswith("1")]


@pytest.mark.parametrize("size", [100, 25])
def test_ffmpeg_proxy_matches_blender(tmp_path, size):
    blender_dir = tmp_path / "blender"
    ours_dir = tmp_path / "ours"
    blender_dir.mkdir()
    ours_dir.mkdir()
    _clip(blender_dir / "clip.mp4")
    shutil.copy(blender_dir / "clip.mp4", ours_dir / "clip.mp4")

    subprocess.run(
        ["blender", "-b", "--factory-startup", "--python-exit-code", "1",
         "--python", str(SCRIPT), "--", str(blender_dir / "clip.mp4"), str(size)],
        check=True, capture_output=True,
    )  # fmt: skip
    theirs = px.proxy_path(blender_dir / "clip.mp4", size)
    assert theirs.is_file(), "Blender wrote no proxy where proxy_path() says it lives"

    def run(cmd):
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def probe(cmd):
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    result = px.build_proxy(
        ours_dir / "clip.mp4", size, decode="cpu", encoder="libx264", run=run, probe=probe
    )
    assert result.status == "built", result.message
    ours = px.proxy_path(ours_dir / "clip.mp4", size)

    assert _shape(ours) == _shape(theirs)
    assert _keyframes(ours) == _keyframes(theirs)
    # Same encoder settings, so the proxy decodes as cheaply as Blender's
    # (cabac=0 and no deblocking: x264's fastdecode tune).
    assert _x264_options(ours) == _x264_options(theirs)


def test_ffmpeg_proxy_keeps_variable_frame_timing(tmp_path):
    """Blender keeps every source timestamp; a proxy retimed to a fixed rate
    shows a neighbouring frame wherever the spacing was uneven."""
    for d in ("blender", "ours"):
        (tmp_path / d).mkdir()
    _vfr_clip(tmp_path / "blender" / "clip.mp4")
    shutil.copy(tmp_path / "blender" / "clip.mp4", tmp_path / "ours" / "clip.mp4")
    subprocess.run(
        ["blender", "-b", "--factory-startup", "--python-exit-code", "1",
         "--python", str(SCRIPT), "--", str(tmp_path / "blender" / "clip.mp4"), "100"],
        check=True, capture_output=True,
    )  # fmt: skip

    def run(cmd):
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def probe(cmd):
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    src = tmp_path / "ours" / "clip.mp4"
    assert (
        px.build_proxy(src, 100, decode="cpu", encoder="libx264", run=run, probe=probe).status
        == "built"
    )
    theirs = _pts(px.proxy_path(tmp_path / "blender" / "clip.mp4", 100))
    assert len({round(float(b) - float(a), 4) for a, b in zip(theirs, theirs[1:])}) > 1
    assert _pts(px.proxy_path(src, 100)) == theirs
