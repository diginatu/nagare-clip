"""Unit tests for the proxy builder: Blender 5.2's proxy file, made by ffmpeg.

Blender 5.2 writes ``<source dir>/BL_proxy/<source name>/proxy_<N>.avi`` --
an MP4 container holding H.264 despite the extension, no audio, no B-frames,
a keyframe every 10 frames, the source's rotation kept as metadata.  These
tests pin that contract; ``test_proxy_matches_blender.py`` compares a real
build against Blender's own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nagare_clip.blender import proxy as px


def test_proxy_path_sits_beside_the_source():
    src = Path("/media/clips/talk.mp4")
    assert px.proxy_path(src, 25) == Path("/media/clips/BL_proxy/talk.mp4/proxy_25.avi")
    assert px.proxy_path(src, 100) == Path("/media/clips/BL_proxy/talk.mp4/proxy_100.avi")


@pytest.mark.parametrize(
    ("w", "h", "size", "expected"),
    [
        (1920, 1080, 100, (1920, 1080)),
        (1920, 1080, 25, (480, 270)),
        # Measured on Blender 5.2.2 with a 1918x1078 source.
        (1918, 1078, 100, (1918, 1078)),
        (1918, 1078, 75, (1438, 808)),
        (1918, 1078, 50, (960, 540)),
        (1918, 1078, 25, (480, 270)),
    ],
)
def test_proxy_dims_match_blender(w, h, size, expected):
    assert px.proxy_dims(w, h, size) == expected


def test_proxy_size_must_be_one_blender_builds():
    with pytest.raises(ValueError):
        px.proxy_path(Path("a.mp4"), 33)


def test_is_fresh(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    proxy = px.proxy_path(src, 100)
    assert not px.is_fresh(src, proxy)  # missing
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"p")
    os.utime(src, (1000, 1000))
    os.utime(proxy, (2000, 2000))
    assert px.is_fresh(src, proxy)
    os.utime(src, (3000, 3000))  # source replaced after the proxy was made
    assert not px.is_fresh(src, proxy)


def test_parse_probe():
    assert px.parse_probe("1918,1078\n") == (1918, 1078)
    with pytest.raises(ValueError):
        px.parse_probe("")


def test_probe_cmd_reads_first_video_stream():
    cmd = px.probe_cmd(Path("/m/a.mp4"))
    assert cmd[0] == "ffprobe"
    assert "v:0" in cmd
    assert cmd[-1] == "/m/a.mp4"


def _common_shape(cmd, out):
    assert cmd[0] == "ffmpeg"
    assert "-nostdin" in cmd
    # Blender keeps the frame unrotated and carries the rotation as metadata.
    assert "-noautorotate" in cmd
    assert cmd[cmd.index("-g") + 1] == "10"
    assert cmd[cmd.index("-bf") + 1] == "0"
    assert "-an" in cmd
    # Keep the source's timestamps exactly (phones record variable frame rate).
    assert cmd[cmd.index("-enc_time_base") + 1] == "demux"
    assert "-copyts" in cmd  # ...including a first frame that is not at 0
    # The file is named .avi; the container must be forced.
    assert cmd[cmd.index("-f") + 1] == "mp4"
    assert cmd[-1] == str(out)


OUT = Path("/m/BL_proxy/a.mp4/proxy_50.part.avi")


def test_encode_cmd_default_decodes_on_gpu_encodes_like_blender():
    """Measured best: NVDEC + x264 with Blender's settings -- the same file as
    Blender's (same size, quality and decode cost), 1.4x faster to build."""
    cmd = px.encode_cmd(Path("/m/a.mp4"), OUT, 960, 540, decode="cuda", encoder="libx264")
    _common_shape(cmd, OUT)
    assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
    assert cmd[cmd.index("-hwaccel_output_format") + 1] == "cuda"
    assert cmd.index("-hwaccel") < cmd.index("-i")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("scale_cuda=960:540")
    assert "hwdownload" in vf  # x264 encodes from system memory
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-tune") + 1] == "fastdecode"


def test_encode_cmd_cpu_libx264_is_blender_on_the_cpu():
    cmd = px.encode_cmd(Path("/m/a.mp4"), OUT, 960, 540, decode="cpu", encoder="libx264")
    _common_shape(cmd, OUT)
    assert "-hwaccel" not in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("scale=960:540")
    assert "cuda" not in vf
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-tune") + 1] == "fastdecode"
    assert cmd[cmd.index("-preset") + 1] == "veryfast"
    assert cmd[cmd.index("-crf") + 1] == "25"


def test_encode_cmd_nvenc_stays_on_the_gpu():
    cmd = px.encode_cmd(Path("/m/a.mp4"), OUT, 960, 540, decode="cuda", encoder="h264_nvenc")
    _common_shape(cmd, OUT)
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "scale_cuda=960:540:format=yuv420p"  # no download: NVENC reads CUDA frames
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    # The frames are CUDA surfaces: a -pix_fmt would demand a CPU conversion.
    assert "-pix_fmt" not in cmd
    # Measured: cq 34 matches x264 crf 25's quality; CAVLC decodes cheaper.
    assert cmd[cmd.index("-cq") + 1] == "34"
    assert cmd[cmd.index("-b:v") + 1] == "0"
    assert cmd[cmd.index("-coder") + 1] == "cavlc"


def test_encode_cmd_nvenc_from_cpu_frames():
    cmd = px.encode_cmd(Path("/m/a.mp4"), OUT, 960, 540, decode="cpu", encoder="h264_nvenc")
    _common_shape(cmd, OUT)
    assert "-hwaccel" not in cmd
    assert cmd[cmd.index("-vf") + 1].startswith("scale=960:540")


@pytest.mark.parametrize(("decode", "encoder"), [("cuda", "mjpeg"), ("vaapi", "libx264")])
def test_encode_cmd_rejects_unknown_choices(decode, encoder):
    with pytest.raises(ValueError):
        px.encode_cmd(Path("a.mp4"), Path("o.avi"), 2, 2, decode=decode, encoder=encoder)


# --- build_proxy: orchestration with injected runners ------------------------


def _mode(cmd):
    return ("cuda" if "-hwaccel" in cmd else "cpu", cmd[cmd.index("-c:v") + 1])


class _Runner:
    def __init__(self, fail=(), size="1920,1080\n"):
        self.calls: list[list[str]] = []
        self.fail = set(fail)
        self.size = size

    def probe(self, cmd):
        self.calls.append(cmd)
        return self.size

    def run(self, cmd):
        self.calls.append(cmd)
        if _mode(cmd) in self.fail:
            Path(cmd[-1]).write_bytes(b"half")  # a failed encode leaves debris
            raise subprocess.CalledProcessError(1, cmd, stderr="CUDA_ERROR_NO_DEVICE")
        Path(cmd[-1]).write_bytes(b"proxy")

    def modes(self):
        return [_mode(c) for c in self.calls if "-c:v" in c]


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"video")
    return src


def _build(source, size=100, decode="cuda", encoder="libx264", r=None, probe=None):
    r = r or _Runner()
    return px.build_proxy(
        source, size, decode=decode, encoder=encoder, run=r.run, probe=probe or r.probe
    )


def test_build_proxy_writes_the_file(source):
    r = _Runner()
    result = _build(source, 50, r=r)
    proxy = px.proxy_path(source, 50)
    assert result.status == "built"
    assert result.method == "cuda+libx264"
    assert proxy.read_bytes() == b"proxy"
    assert r.modes() == [("cuda", "libx264")]
    enc = r.calls[-1]
    assert "scale_cuda=960:540" in enc[enc.index("-vf") + 1]
    # Written under a temporary name, then renamed: Blender never sees half a file.
    assert Path(enc[-1]) != proxy
    assert list(proxy.parent.iterdir()) == [proxy]


def test_build_proxy_skips_a_fresh_proxy(source):
    proxy = px.proxy_path(source, 100)
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"old")
    os.utime(source, (1000, 1000))
    r = _Runner()
    assert _build(source, r=r).status == "fresh"
    assert r.calls == []
    assert proxy.read_bytes() == b"old"


def test_build_proxy_rebuilds_a_stale_proxy(source):
    proxy = px.proxy_path(source, 100)
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"old")
    os.utime(proxy, (1000, 1000))
    assert _build(source).status == "built"
    assert proxy.read_bytes() == b"proxy"


@pytest.mark.parametrize(
    ("decode", "encoder"), [("cuda", "libx264"), ("cuda", "h264_nvenc"), ("cpu", "h264_nvenc")]
)
def test_build_proxy_falls_back_to_cpu_libx264(source, decode, encoder):
    r = _Runner(fail={(decode, encoder)})
    result = _build(source, decode=decode, encoder=encoder, r=r)
    assert result.status == "built"
    assert result.method == "cpu+libx264"
    assert r.modes() == [(decode, encoder), ("cpu", "libx264")]
    assert "CUDA_ERROR_NO_DEVICE" in result.message
    proxy = px.proxy_path(source, 100)
    assert list(proxy.parent.iterdir()) == [proxy]


def test_build_proxy_gives_up_without_leaving_debris(source):
    r = _Runner(fail={("cuda", "libx264"), ("cpu", "libx264")})
    result = _build(source, r=r)
    assert result.status == "failed"
    proxy = px.proxy_path(source, 100)
    assert not proxy.exists()
    assert list(proxy.parent.iterdir()) == []


def test_build_proxy_cpu_libx264_does_not_retry_itself(source):
    r = _Runner(fail={("cpu", "libx264")})
    assert _build(source, decode="cpu", r=r).status == "failed"
    assert r.modes() == [("cpu", "libx264")]


def test_build_proxy_missing_source(tmp_path):
    r = _Runner()
    assert _build(tmp_path / "gone.mp4", r=r).status == "failed"
    assert r.calls == []


def test_build_proxy_unprobeable_source(source):
    def probe(cmd):
        raise subprocess.CalledProcessError(1, cmd, stderr="Invalid data")

    r = _Runner()
    result = _build(source, r=r, probe=probe)
    assert result.status == "failed"
    assert "Invalid data" in result.message
    assert r.modes() == []


def test_build_proxy_message_keeps_the_cause(source):
    """ffmpeg's last line is a generic summary; the cause sits above it."""

    def run(cmd):
        raise subprocess.CalledProcessError(
            1,
            cmd,
            stderr="Impossible to convert between the formats\n"
            "Error reinitializing filters!\n"
            "Nothing was written into output file\n",
        )

    r = _Runner()
    result = px.build_proxy(source, 100, decode="cpu", encoder="libx264", run=run, probe=r.probe)
    assert "Impossible to convert" in result.message
