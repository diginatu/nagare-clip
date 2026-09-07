"""blender.render: config keys forwarded 1:1 to Blender's render RNA.

No ``bpy`` here -- ``apply_render_settings`` only ever does getattr/setattr, so
the whole contract (recursion into sub-structs, unknown keys, bad values) is
exercised against fakes whose ``__slots__`` reject an unknown attribute exactly
the way Blender's RNA does.
"""

from __future__ import annotations

import logging

import pytest

from nagare_clip.blender.render_settings import apply_render_settings


class FakeFFmpeg:
    __slots__ = ("codec", "format", "audio_codec", "audio_bitrate")


class FakeImageSettings:
    __slots__ = ("file_format",)


class FakeRender:
    __slots__ = (
        "fps",
        "fps_base",
        "resolution_x",
        "resolution_y",
        "filepath",
        "image_settings",
        "ffmpeg",
    )

    def __init__(self) -> None:
        self.fps = 30
        self.fps_base = 1.001
        self.resolution_x = 640
        self.resolution_y = 360
        self.filepath = "/tmp/out"
        self.image_settings = FakeImageSettings()
        self.ffmpeg = FakeFFmpeg()


class TestFlatKeys:
    def test_scalar_keys_land_on_the_render_object(self):
        render = FakeRender()
        apply_render_settings(
            render, {"resolution_x": 1920, "resolution_y": 1080, "filepath": "//../out.mp4"}
        )
        assert (render.resolution_x, render.resolution_y) == (1920, 1080)
        assert render.filepath == "//../out.mp4"

    def test_empty_settings_write_nothing(self):
        render = FakeRender()
        apply_render_settings(render, {})
        assert (render.resolution_x, render.resolution_y) == (640, 360)
        assert (render.fps, render.fps_base) == (30, 1.001)
        assert render.filepath == "/tmp/out"


class TestNestedStructs:
    def test_a_dict_value_recurses_into_the_sub_struct(self):
        render = FakeRender()
        apply_render_settings(
            render,
            {
                "image_settings": {"file_format": "FFMPEG"},
                "ffmpeg": {"format": "MPEG4", "codec": "H264", "audio_codec": "AAC"},
            },
        )
        assert render.image_settings.file_format == "FFMPEG"
        assert (render.ffmpeg.format, render.ffmpeg.codec) == ("MPEG4", "H264")
        assert render.ffmpeg.audio_codec == "AAC"

    def test_an_unknown_sub_key_is_skipped_and_its_siblings_still_land(self, caplog):
        render = FakeRender()
        with caplog.at_level(logging.WARNING):
            apply_render_settings(render, {"ffmpeg": {"nosuch": 1, "codec": "H264"}})
        assert render.ffmpeg.codec == "H264"
        assert "ffmpeg.nosuch" in caplog.text

    def test_an_unknown_sub_struct_is_skipped(self, caplog):
        render = FakeRender()
        with caplog.at_level(logging.WARNING):
            apply_render_settings(render, {"nosuch": {"a": 1}, "resolution_x": 1920})
        assert render.resolution_x == 1920
        assert "nosuch" in caplog.text


class TestUnknownAndInvalid:
    def test_an_unknown_key_is_logged_and_skipped(self, caplog):
        render = FakeRender()
        with caplog.at_level(logging.WARNING):
            apply_render_settings(render, {"nosuch_key": 1, "resolution_x": 1920})
        assert render.resolution_x == 1920
        assert "nosuch_key" in caplog.text

    def test_a_bad_value_is_a_hard_error(self):
        """An unknown KEY is a typo in a setting nobody relies on; a rejected
        VALUE means the codec/container is not what the config asked for, and
        rendering with a silently different one is worse than failing."""

        class Strict:
            @property
            def codec(self):  # pragma: no cover - setter is the point
                return None

            @codec.setter
            def codec(self, value):
                raise TypeError("enum 'NOPE' not found")

        with pytest.raises(TypeError):
            apply_render_settings(Strict(), {"codec": "NOPE"})


class TestFpsBase:
    def test_fps_alone_resets_fps_base(self):
        """fps_base carries the source's 1.001 (29.97) pulldown. Leaving it in
        place would make `fps: 30` yield an effective 29.97 -- an override that
        does not override."""
        render = FakeRender()
        apply_render_settings(render, {"fps": 60})
        assert render.fps == 60
        assert render.fps_base == 1.0

    def test_an_explicit_fps_base_is_left_alone(self):
        render = FakeRender()
        apply_render_settings(render, {"fps": 30000, "fps_base": 1001.0})
        assert (render.fps, render.fps_base) == (30000, 1001.0)

    def test_fps_base_is_untouched_when_fps_is_not_given(self):
        render = FakeRender()
        apply_render_settings(render, {"resolution_x": 1920})
        assert render.fps_base == 1.001

    def test_a_nested_fps_does_not_reach_for_a_nested_fps_base(self, caplog):
        """The reset is a top-level rule. No render sub-struct has an fps_base,
        so a stray nested `fps` must degrade to a warning, not a crash."""
        render = FakeRender()
        with caplog.at_level(logging.WARNING):
            apply_render_settings(render, {"ffmpeg": {"fps": 60}})
        assert render.fps_base == 1.001
        assert "ffmpeg.fps" in caplog.text
