"""Build Blender's VSE proxy files with ffmpeg, before Blender runs (no bpy).

Blender builds a proxy only when a person selects strips and presses Rebuild
Proxy, and its builder encodes on the CPU.  The file it writes is an ordinary
video, so the pipeline writes the same file itself -- decoding on the GPU
(NVDEC) and encoding with Blender's own x264 settings by default -- and
Blender, finding it where it looks, simply uses it.

What Blender 5.2 writes (measured, and pinned by
``tests/blender/test_proxy_matches_blender.py`` against a real build):

- ``<source dir>/BL_proxy/<source file name>/proxy_<N>.avi`` for the default
  per-strip storage, wherever the ``.blend`` lives;
- an **MP4** container holding **H.264** (High, yuv420p) despite the ``.avi``
  name, no B-frames, a keyframe every 10 frames, no audio;
- the source's frame rate and exact timestamps (a variable-rate phone clip
  stays variable), and the frame left **unrotated** with
  the source's rotation carried as metadata;
- ``N`` percent of the source size, ``int(W * N / 100)`` rounded up to even.

Blender 5.x has no timecode index files, so the video file is all there is.
Subprocesses are injected (``run``/``probe``) so the pipeline keeps every
process boundary in ``pipeline.external``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SIZES = (25, 50, 75, 100)
DECODERS = ("cuda", "cpu")
ENCODERS = ("libx264", "h264_nvenc")
# The last resort when a GPU attempt fails: Blender's own builder, on the CPU.
FALLBACK = ("cpu", "libx264")
GOP = 10
# Blender's default proxy quality (50) encodes at x264 crf 25.
X264_CRF = 25
# NVENC's -cq giving crf 25's quality (SSIM 0.971 on a 1080p60 HEVC clip).
NVENC_CQ = 34


def _check_size(size: int) -> None:
    if size not in SIZES:
        raise ValueError(f"proxy size must be one of {SIZES}, got {size!r}")


def proxy_path(source: Path, size: int) -> Path:
    """Where Blender looks for *source*'s proxy at *size* percent."""
    _check_size(size)
    return source.parent / "BL_proxy" / source.name / f"proxy_{size}.avi"


def _part_path(proxy: Path) -> Path:
    return proxy.with_name(f"{proxy.stem}.nagare_part{proxy.suffix}")


def proxy_dims(width: int, height: int, size: int) -> tuple[int, int]:
    """The proxy frame size Blender uses: truncated, then rounded up to even."""
    _check_size(size)

    def one(v: int) -> int:
        n = v * size // 100
        return n + (n & 1)

    return one(width), one(height)


def is_fresh(source: Path, proxy: Path) -> bool:
    """True when *proxy* exists and is not older than *source*."""
    try:
        return proxy.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def probe_cmd(source: Path) -> list[str]:
    """ffprobe: the coded width,height of the first video stream."""
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0",
        str(source),
    ]


def parse_probe(stdout: str) -> tuple[int, int]:
    first = stdout.strip().splitlines()[0] if stdout.strip() else ""
    parts = first.split(",")
    if len(parts) < 2:
        raise ValueError(f"no video size in ffprobe output: {stdout!r}")
    return int(parts[0]), int(parts[1])


def encode_cmd(
    source: Path, out: Path, width: int, height: int, *, decode: str, encoder: str
) -> list[str]:
    """ffmpeg command writing *source*'s proxy to *out*.

    *decode* ``cuda`` decodes and scales on the GPU (NVDEC, ``scale_cuda``);
    ``cpu`` does both on the CPU.  *encoder* ``libx264`` uses Blender's own
    settings (veryfast, fastdecode, crf 25) and is the default because the
    file is then Blender's, cheap to scrub; ``h264_nvenc`` builds faster but
    decodes ~1.5x slower (no way to drop deblocking), measured on a 1080p60
    HEVC phone clip -- see docs/stages/blender.md.  ``-noautorotate`` matches
    Blender, which stores the frame unrotated and keeps the rotation metadata
    (it also keeps the GPU path free of a CPU-only transpose).
    """
    if decode == "cuda":
        hwaccel = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = f"scale_cuda={width}:{height}:format=yuv420p"
        if encoder != "h264_nvenc":
            vf += ",hwdownload,format=yuv420p"
    elif decode == "cpu":
        hwaccel = []
        vf = f"scale={width}:{height}:flags=bilinear,format=yuv420p"
    else:
        raise ValueError(f"proxy decode must be one of {DECODERS}, got {decode!r}")
    if encoder == "libx264":
        codec = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            # Blender's own settings (cabac=0, no deblocking): cheap to decode,
            # which is what a proxy is for.
            "-tune",
            "fastdecode",
            "-crf",
            str(X264_CRF),
            "-pix_fmt",
            "yuv420p",
        ]
    elif encoder == "h264_nvenc":
        # No -pix_fmt: with cuda decode the frames are CUDA surfaces whose sw
        # format scale_cuda already set, and naming one asks for a CPU
        # conversion that cannot happen. cq 34 measured to match crf 25's
        # quality; CAVLC (Blender's cabac=0) decodes cheaper.
        codec = [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-rc",
            "vbr",
            "-cq",
            str(NVENC_CQ),
            "-b:v",
            "0",
            "-coder",
            "cavlc",
        ]
    else:
        raise ValueError(f"proxy encoder must be one of {ENCODERS}, got {encoder!r}")
    return [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-noautorotate",
        # Keep the source's timestamps as they are, a first frame after 0 included.
        "-copyts",
        *hwaccel,
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        vf,
        *codec,
        "-profile:v",
        "high",
        "-g",
        str(GOP),
        "-bf",
        "0",
        "-fps_mode",
        "passthrough",
        # Encode in the source's time base: the default (1/frame rate) snaps
        # a variable-rate phone clip's timestamps to a grid, as Blender does not.
        "-enc_time_base",
        "demux",
        "-f",
        "mp4",
        str(out),
    ]


@dataclass(frozen=True)
class ProxyResult:
    """What ``build_proxy`` did: ``built``, ``fresh`` (left alone) or ``failed``."""

    source: Path
    proxy: Path
    status: str
    method: str = ""  # "<decode>+<encoder>" that built it
    message: str = ""


def _err(exc: BaseException) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    lines = [ln.strip() for ln in (stderr or str(exc)).splitlines() if ln.strip()]
    # ffmpeg ends with a generic summary; the cause is a line or two above it.
    return " | ".join(lines[-4:]) if lines else type(exc).__name__


def build_proxy(
    source: Path,
    size: int,
    *,
    decode: str,
    encoder: str,
    run: Callable[[list[str]], object],
    probe: Callable[[list[str]], str],
) -> ProxyResult:
    """Write *source*'s ``proxy_<size>.avi`` unless a fresh one exists.

    Never raises for a media problem: the proxy only makes the VSE preview
    smoother, and Blender works without one.  A failing *decode*/*encoder*
    is retried once on the CPU with ``libx264``; the file is written under a
    temporary name and renamed, so Blender never finds half a proxy.
    """
    proxy = proxy_path(source, size)
    if not source.is_file():
        return ProxyResult(source, proxy, "failed", message="source not found")
    if is_fresh(source, proxy):
        return ProxyResult(source, proxy, "fresh")
    try:
        width, height = parse_probe(probe(probe_cmd(source)))
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return ProxyResult(source, proxy, "failed", message=f"ffprobe: {_err(exc)}")
    w, h = proxy_dims(width, height, size)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(proxy)
    attempts = [(decode, encoder)]
    if attempts[0] != FALLBACK:
        attempts.append(FALLBACK)
    messages: list[str] = []
    for dec, enc in attempts:
        method = f"{dec}+{enc}"
        try:
            run(encode_cmd(source, part, w, h, decode=dec, encoder=enc))
        except (subprocess.CalledProcessError, OSError) as exc:
            part.unlink(missing_ok=True)
            messages.append(f"{method}: {_err(exc)}")
            logging.warning("proxy %s with %s failed: %s", source.name, method, messages[-1])
            continue
        part.replace(proxy)
        return ProxyResult(source, proxy, "built", method, "; ".join(messages))
    return ProxyResult(source, proxy, "failed", message="; ".join(messages))
