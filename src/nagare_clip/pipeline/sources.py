"""Source-video discovery, resolution, and staging for the orchestrator."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.pipeline.errors import PipelineError

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm")


@dataclass(frozen=True)
class SourceMedia:
    """One source video: original absolute path, stem, and path relative to
    the input dir (the path Docker sees)."""

    abs_path: Path
    stem: str
    relative: str


def discover_sources(input_dir: Path) -> list[Path]:
    """All video files directly inside *input_dir*, sorted by name."""
    found = [
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not found:
        raise PipelineError(f"No video files found in: {input_dir}")
    return sorted(found)


def resolve_cli_sources(cli_sources: list[str], input_dir: Path) -> list[Path]:
    """Resolve explicit --source values; bare names live under *input_dir*."""
    paths: list[Path] = []
    for src in cli_sources:
        p = Path(src) if "/" in src else input_dir / src
        if not p.is_file():
            raise PipelineError(f"Source file not found: {p}")
        paths.append(p)
    return paths


def stage_sources(
    paths: list[Path], input_dir: Path
) -> tuple[list[SourceMedia], list[Path]]:
    """Make every source reachable inside *input_dir* for Docker.

    Sources outside the dir are copied in (returned in the cleanup list);
    ``abs_path`` always stays the original file so Blender references the
    original media in place.
    """
    abs_input = input_dir.resolve()
    sources: list[SourceMedia] = []
    cleanup: list[Path] = []
    for p in paths:
        ap = p.resolve()
        if ap.is_relative_to(abs_input):
            relative = str(ap.relative_to(abs_input))
        else:
            dest = input_dir / p.name
            shutil.copyfile(p, dest)
            cleanup.append(dest)
            relative = p.name
        sources.append(SourceMedia(abs_path=ap, stem=p.stem, relative=relative))
    return sources, cleanup
