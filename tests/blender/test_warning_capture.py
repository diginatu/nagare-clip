"""Blender's own warnings, lifted out of its startup noise.

The clamp/overlap notices are the only sign that a requested interval did not
fit, and they print into the same stream as Blender's unrelated bl_pkg/cattrs
extension tracebacks the operator is told to ignore.  The stage writes them to
a file so the finished-cut report can surface them.
"""

from __future__ import annotations

import json
import logging

import pytest

from nagare_clip.blender.warnings_file import WARNINGS_FILENAME, capture_warnings, write_warnings


@pytest.fixture
def verbose_root():
    """The Blender stage runs at INFO by default, so INFO records really do
    reach the handler -- its own level is what filters them, not the root's."""
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.setLevel(previous)


class TestCapture:
    def test_only_warning_and_above_are_kept(self, verbose_root):
        with capture_warnings() as captured:
            logging.debug("Strip 3: frame_start=1")
            logging.info("Source 1/7: a.mp4")
            logging.warning("Strip 109: interval clamped to clip duration")
            logging.error("something worse")
        assert captured == [
            "Strip 109: interval clamped to clip duration",
            "something worse",
        ]

    def test_arguments_are_formatted_into_the_message(self):
        with capture_warnings() as captured:
            logging.warning("Strip %d: clamped to %d frames", 109, 23524)
        assert captured == ["Strip 109: clamped to 23524 frames"]

    def test_the_handler_is_removed_afterwards(self):
        with capture_warnings() as captured:
            pass
        logging.warning("after")
        assert captured == []


class TestWriteWarnings:
    def test_writes_the_json_contract_next_to_the_blend(self, tmp_path):
        write_warnings(tmp_path, ["a", "b"])
        data = json.loads((tmp_path / WARNINGS_FILENAME).read_text(encoding="utf-8"))
        assert data == {"warnings": ["a", "b"]}

    def test_an_empty_list_still_writes_so_a_stale_file_cannot_survive(self, tmp_path):
        (tmp_path / WARNINGS_FILENAME).write_text('{"warnings": ["old"]}', encoding="utf-8")
        write_warnings(tmp_path, [])
        data = json.loads((tmp_path / WARNINGS_FILENAME).read_text(encoding="utf-8"))
        assert data == {"warnings": []}

    def test_a_write_failure_never_fails_the_stage(self, tmp_path):
        write_warnings(tmp_path / "nope" / "deeper", ["a"])  # parent missing -> created
        assert (tmp_path / "nope" / "deeper" / WARNINGS_FILENAME).is_file()


class TestCliWiring:
    """main() records what the build logged, beside the .blend it wrote."""

    def _main(self, monkeypatch, blend, build):
        import sys
        from unittest.mock import MagicMock

        sys.modules.setdefault("bpy", MagicMock())
        from nagare_clip.blender import blender_cli

        monkeypatch.setattr(
            sys,
            "argv",
            ["blender", "--", "--source", "a.mp4", "--intervals", "a.json", "--output", str(blend)],
        )
        monkeypatch.setattr(blender_cli, "_build", build)
        return blender_cli

    def test_warnings_from_the_build_land_beside_the_blend(self, tmp_path, monkeypatch):
        blend = tmp_path / "out" / "a_edited.blend"
        cli = self._main(monkeypatch, blend, lambda args: logging.warning("Strip 109: clamped"))
        cli.main()
        data = json.loads((blend.parent / WARNINGS_FILENAME).read_text(encoding="utf-8"))
        assert data == {"warnings": ["Strip 109: clamped"]}

    def test_a_failed_build_still_leaves_the_warnings_that_preceded_it(self, tmp_path, monkeypatch):
        blend = tmp_path / "out" / "a_edited.blend"

        def boom(args):
            logging.warning("Strip 109: clamped")
            raise RuntimeError("blender died")

        cli = self._main(monkeypatch, blend, boom)
        with pytest.raises(RuntimeError):
            cli.main()
        data = json.loads((blend.parent / WARNINGS_FILENAME).read_text(encoding="utf-8"))
        assert data == {"warnings": ["Strip 109: clamped"]}
