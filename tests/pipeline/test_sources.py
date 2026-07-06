"""Tests for pipeline source discovery, resolution, and staging."""

from pathlib import Path

import pytest

from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.sources import (
    SourceMedia,
    discover_sources,
    resolve_cli_sources,
    stage_sources,
)


def test_discover_sources_sorted_and_filtered(tmp_path):
    (tmp_path / "b.mp4").touch()
    (tmp_path / "a.MKV").touch()  # case-insensitive extension
    (tmp_path / "notes.txt").touch()  # ignored extension
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.mp4").touch()  # not top-level: ignored
    found = discover_sources(tmp_path)
    assert [p.name for p in found] == ["a.MKV", "b.mp4"]


def test_discover_sources_empty_raises(tmp_path):
    with pytest.raises(PipelineError, match="No video files found"):
        discover_sources(tmp_path)


def test_resolve_cli_sources_bare_name_under_input_dir(tmp_path):
    (tmp_path / "clip.mp4").touch()
    assert resolve_cli_sources(["clip.mp4"], tmp_path) == [tmp_path / "clip.mp4"]


def test_resolve_cli_sources_path_used_as_is(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "clip.mp4").touch()
    assert resolve_cli_sources([str(other / "clip.mp4")], tmp_path) == [other / "clip.mp4"]


def test_resolve_cli_sources_missing_raises(tmp_path):
    with pytest.raises(PipelineError, match="Source file not found"):
        resolve_cli_sources(["nope.mp4"], tmp_path)


def test_stage_sources_inside_dir_no_copy(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"x")
    sources, cleanup = stage_sources([tmp_path / "clip.mp4"], tmp_path)
    assert cleanup == []
    assert sources == [
        SourceMedia(
            abs_path=(tmp_path / "clip.mp4").resolve(), stem="clip", relative="clip.mp4"
        )
    ]


def test_stage_sources_outside_dir_copies_but_keeps_original_abs(tmp_path):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    outside = tmp_path / "clip.mp4"
    outside.write_bytes(b"x")
    sources, cleanup = stage_sources([outside], input_dir)
    assert cleanup == [input_dir / "clip.mp4"]
    assert (input_dir / "clip.mp4").read_bytes() == b"x"
    # Blender must reference the ORIGINAL media path, not the staging copy.
    assert sources[0].abs_path == outside.resolve()
    assert sources[0].relative == "clip.mp4"
    assert sources[0].stem == "clip"
