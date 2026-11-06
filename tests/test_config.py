"""Tests for the centralised config module."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from nagare_clip.config import (
    DEFAULTS,
    deep_merge,
    generate_example_yaml,
    get_effective_config,
    load_config,
)


def _leaf_paths(d: dict[str, Any], prefix: str = "") -> Iterator[str]:
    """Yield the dotted path of every non-dict leaf in *d*."""
    for key, value in d.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _leaf_paths(value, path + ".")
        else:
            yield path


def _top_section_lines(text: str, top: str) -> list[str]:
    """Lines of the ``top:`` top-level section (until the next column-0 key or EOF)."""
    lines = text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(top)}\s*:", ln)),
        None,
    )
    if start is None:
        return []
    end = next(
        (j for j in range(start + 1, len(lines)) if re.match(r"^\w+\s*:", lines[j])),
        len(lines),
    )
    return lines[start:end]


def test_example_file_matches_generator():
    """config.example.yml must be exactly what the generator emits (regenerate
    with `make config-example` to fix drift)."""
    on_disk = Path("config.example.yml").read_text(encoding="utf-8")
    assert generate_example_yaml() == on_disk


def test_a_section_of_only_commented_keys_renders_as_an_empty_mapping():
    """`blender.render:` and `project:` have no uncommented keys at all. A bare
    heading followed by comments parses as null and then fails validation, which
    is why the example file could not be loaded as a config as it stood."""
    text = generate_example_yaml()
    for line in ("render: {}", "project: {}"):
        assert re.search(rf"^\s*{re.escape(line)}\s*$", text, re.M), text
    data = yaml.safe_load(text)
    assert data["blender"]["render"] == {}
    assert data["project"] == {}
    # The whole example must load as a config, unedited.
    get_effective_config(None, data)


def test_generated_example_is_valid_yaml_covering_all_defaults():
    """Every DEFAULTS leaf appears in the generated example as a real key or a
    commented `# leaf:` line in its own top-level section."""
    text = generate_example_yaml()
    data = yaml.safe_load(text) or {}

    def has_real(path: str) -> bool:
        cur: Any = data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        return True

    for path in _leaf_paths(DEFAULTS):
        if has_real(path):
            continue
        top, leaf = path.split(".")[0], path.split(".")[-1]
        block = _top_section_lines(text, top)
        assert any(re.match(rf"^\s*#\s*{re.escape(leaf)}\s*:", ln) for ln in block), (
            f"DEFAULTS leaf {path!r} missing from generated example section {top!r}"
        )


class TestLoadConfig:
    def test_returns_empty_for_none(self):
        assert load_config(None) == {}

    def test_reads_yaml_file(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": 2.0}}))
        result = load_config(cfg_file)
        assert result == {"intervals": {"silence_threshold": 2.0}}

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yml")

    def test_empty_file_returns_empty(self, tmp_path: Path):
        cfg_file = tmp_path / "empty.yml"
        cfg_file.write_text("")
        assert load_config(cfg_file) == {}

    def test_non_dict_yaml_returns_empty(self, tmp_path: Path):
        cfg_file = tmp_path / "list.yml"
        cfg_file.write_text("- a\n- b\n")
        assert load_config(cfg_file) == {}


class TestDeepMerge:
    def test_basic(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        assert deep_merge(base, override) == {"a": 1, "b": 3, "c": 4}

    def test_preserves_unset_keys(self):
        base = {"a": 1, "b": {"x": 10, "y": 20}}
        override = {"b": {"x": 99}}
        result = deep_merge(base, override)
        assert result == {"a": 1, "b": {"x": 99, "y": 20}}

    def test_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"c": 99}}}
        result = deep_merge(base, override)
        assert result["a"]["b"]["c"] == 99
        assert result["a"]["b"]["d"] == 2

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        deep_merge(base, override)
        assert base["a"]["b"] == 1

    def test_override_replaces_non_dict_with_dict(self):
        base = {"a": 1}
        override = {"a": {"nested": True}}
        result = deep_merge(base, override)
        assert result == {"a": {"nested": True}}


class TestGetEffectiveConfig:
    def test_defaults_only(self):
        cfg = get_effective_config(None)
        assert cfg == DEFAULTS
        # Verify it's a copy, not the same object
        assert cfg is not DEFAULTS

    def test_config_overrides_defaults(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": 2.5}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["intervals"]["silence_threshold"] == 2.5
        # Other defaults intact
        assert cfg["intervals"]["min_keep"] == 1.0
        assert cfg["intervals"]["caption"]["max_bunsetu"] == 12

    def test_cli_overrides_config(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": 2.5}}))
        cli = {"intervals": {"silence_threshold": 3.0}}
        cfg = get_effective_config(cfg_file, cli)
        assert cfg["intervals"]["silence_threshold"] == 3.0

    def test_full_precedence(self, tmp_path: Path):
        """CLI > config > defaults."""
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "intervals": {
                        "silence_threshold": 2.5,
                        "min_keep": 0.5,
                    }
                }
            )
        )
        cli = {"intervals": {"silence_threshold": 3.0}}
        cfg = get_effective_config(cfg_file, cli)
        # CLI wins
        assert cfg["intervals"]["silence_threshold"] == 3.0
        # Config wins over default
        assert cfg["intervals"]["min_keep"] == 0.5
        # Default remains
        assert cfg["intervals"]["keep_pre_margin"] == 1.0

    def test_partial_config(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"blender": {"proxy_size": 50}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["proxy_size"] == 50
        # All other sections still have defaults
        assert cfg["intervals"]["silence_threshold"] == 1.5
        assert cfg["general"]["log_level"] == "INFO"
        assert cfg["blender"]["caption_style"]["font_size"] == 50

    def test_nested_caption_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"caption": {"max_bunsetu": 20}}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["intervals"]["caption"]["max_bunsetu"] == 20
        # Other caption defaults intact
        assert cfg["intervals"]["caption"]["max_duration"] == 4.0

    def test_render_defaults_to_empty(self):
        """Nothing under `render:` means nothing is written to the scene, so a
        project that says nothing keeps the source-derived fps/resolution."""
        cfg = get_effective_config(None)
        assert cfg["blender"]["render"] == {}

    def test_render_forwards_arbitrary_nested_keys(self, tmp_path: Path):
        """The section is an open pass-through to Blender's render RNA: keys it
        has never heard of must survive validation as plain dicts/scalars."""
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "blender": {
                        "render": {
                            "resolution_x": 1920,
                            "filepath": "//../renders/final.mp4",
                            "ffmpeg": {"codec": "H264", "audio_codec": "AAC"},
                        }
                    }
                }
            )
        )
        cfg = get_effective_config(cfg_file)
        render = cfg["blender"]["render"]
        assert render["resolution_x"] == 1920
        assert render["filepath"] == "//../renders/final.mp4"
        assert render["ffmpeg"] == {"codec": "H264", "audio_codec": "AAC"}

    def test_default_fps_is_gone(self, tmp_path: Path):
        """Folded into `render.fps`. A leftover key must fail loudly rather than
        be silently ignored -- it would read as a working fps setting."""
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"blender": {"default_fps": 24.0}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_caption_style_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"blender": {"caption_style": {"font_size": 72}}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["caption_style"]["font_size"] == 72
        assert cfg["blender"]["caption_style"]["alignment_x"] == "CENTER"

    def test_unknown_top_level_section_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"custom_section": {"key": "value"}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_typo_leaf_key_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        # 'silence_threshld' is a typo of silence_threshold
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshld": 2.0}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_wrong_type_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": "not-a-number"}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_pipeline_to_stage_valid(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"pipeline": {"to_stage": "intervals"}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["pipeline"]["to_stage"] == "intervals"

    def test_pipeline_stage_defaults_are_names(self):
        cfg = get_effective_config(None)
        assert cfg["pipeline"]["from_stage"] == "audio_silence"
        assert cfg["pipeline"]["to_stage"] == "render"

    def test_retry_defaults_present(self):
        cfg = get_effective_config(None)
        s2 = cfg["text_filter"]
        assert s2["retry_on_invalid"] is True
        assert s2["retry_min_batch_size"] == 1

    def test_text_filter_keywords_default_empty(self):
        cfg = get_effective_config(None)
        assert cfg["text_filter"]["keywords"] == []
        assert "summary_llm" not in cfg["text_filter"]

    def test_text_filter_keywords_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"text_filter": {"keywords": ["Kubernetes"]}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["text_filter"]["keywords"] == ["Kubernetes"]
        # Other text_filter defaults intact
        assert cfg["text_filter"]["batch_size"] == 10

    def test_removed_summary_llm_section_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"text_filter": {"summary_llm": {"enabled": True}}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_general_image_markup_default_is_html(self):
        cfg = get_effective_config(None)
        assert cfg["general"]["image_markup"] == "html"

    def test_general_image_markup_rejects_unknown_value(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"general": {"image_markup": "rst"}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_gap_context_context_lines_default_is_one(self):
        cfg = get_effective_config(None)
        assert cfg["gap_context"]["context_lines"] == 1

    def test_gap_context_context_lines_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"gap_context": {"context_lines": 4}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["gap_context"]["context_lines"] == 4

    def test_gap_context_context_lines_rejects_a_negative(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"gap_context": {"context_lines": -1}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)


def test_blender_style_allows_extra_keys(tmp_path):
    """caption_style / speed_mark are open-ended Blender TextStrip pass-throughs:
    a `font` path and an arbitrary RNA attr must survive validation."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(
        yaml.dump(
            {
                "blender": {
                    "caption_style": {"font": "/abs/font.ttf", "shadow_blur": 0.5},
                    "speed_mark": {"box_margin": 0.05},
                }
            }
        )
    )
    cfg = get_effective_config(cfg_file)
    assert cfg["blender"]["caption_style"]["font"] == "/abs/font.ttf"
    assert cfg["blender"]["caption_style"]["shadow_blur"] == 0.5
    assert cfg["blender"]["speed_mark"]["box_margin"] == 0.05


def test_caption_style_new_defaults_present():
    cfg = get_effective_config(None)
    cs = cfg["blender"]["caption_style"]
    assert cs["use_shadow"] is True
    assert cs["wrap_width"] == 0.90


def test_intervals_unknown_key_rejected(tmp_path):
    """A non-open-ended section still rejects unknown keys."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"blender": {"caption": {"bogus": 1}}}))
    # blender has no 'caption' key at all -> rejected
    with pytest.raises(ValidationError):
        get_effective_config(cfg_file)


def test_speed_mark_defaults_present():
    cfg = get_effective_config(None, {})
    sm = cfg["blender"]["speed_mark"]
    assert sm["enabled"] is True
    assert sm["template"] == "x{factor}"
    assert sm["font_size"] == 35
    assert sm["alignment_x"] == "RIGHT"
    assert sm["anchor_y"] == "TOP"
    assert sm["location_x"] == 0.95
    assert sm["location_y"] == 0.95


def test_guided_edit_has_no_settings():
    assert "guided_edit" not in get_effective_config(None, {})


@pytest.mark.parametrize("value", [{}, None])
def test_an_empty_guided_edit_section_is_accepted(tmp_path, value):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"guided_edit": value}))
    assert "guided_edit" not in get_effective_config(cfg_file)


def test_every_removed_guided_edit_key_fails_naming_its_path(tmp_path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"guided_edit": {"enabled": True, "model": "x"}}))
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    msg = str(e.value)
    assert "`guided_edit.enabled`" in msg and "`guided_edit.model`" in msg
    assert "removed" in msg
    assert "extra" not in msg.lower()


def test_llm_report_defaults():
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    assert cfg["general"]["llm_report"] is True
    assert cfg["general"]["llm_report_dir"] == "output/llm_report"


def test_llm_sections_default_provider_and_empty_api_base():
    from nagare_clip.config import DEFAULTS

    sections = [
        DEFAULTS["text_filter"],
        DEFAULTS["summary"],
    ]
    for sec in sections:
        assert sec["provider"] == "ollama_chat"
        assert sec["api_base"] == ""


def test_general_langfuse_defaults_true():
    cfg = get_effective_config(None, {})
    assert cfg["general"]["langfuse"] is True


def test_sentence_split_defaults_present():
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    sp = cfg["sentence_split"]
    assert sp["enabled"] is False
    assert sp["window_segments"] == 200
    assert sp["max_retries"] == 2
    assert sp["response_format"] == "json"
    assert isinstance(sp["prompt"], str) and sp["prompt"]


def test_section_named_env_var_does_not_override_default(monkeypatch):
    """A top-level section name as an env var must NOT leak into config
    (no environment config source; precedence is CLI > file > defaults only)."""
    monkeypatch.setenv("SUMMARY", '{"model":"LEAKED"}')
    cfg = get_effective_config(None)
    assert cfg["summary"]["model"] == "gpt-oss:120b"


def test_scalar_env_var_colliding_with_section_does_not_crash(monkeypatch):
    """A non-JSON env var whose name collides with a section must not make
    get_effective_config raise."""
    monkeypatch.setenv("SUMMARY", "short")
    cfg = get_effective_config(None)  # must not raise
    assert cfg["summary"]["enabled"] is False


def test_gap_context_defaults():
    cfg = get_effective_config(None, {})
    g = cfg["gap_context"]
    assert g["enabled"] is False
    assert g["min_gap"] == 3.0
    assert g["frame_width"] == 960
    assert g["max_retries"] == 2
    assert g["provider"] == "ollama_chat"
    assert g["prompt"]


def test_gap_context_rejects_unknown_key():
    with pytest.raises(ValidationError):
        get_effective_config(None, {"gap_context": {"nonesuch": 1}})


def test_gap_context_prompt_documents_the_markers_describe_parses():
    # describe.describe_gap parses a leading STATIC:/ACTION: marker off the
    # vision reply; the default prompt must instruct the model to emit them.
    cfg = get_effective_config(None, {})
    prompt = cfg["gap_context"]["prompt"]
    assert "STATIC:" in prompt
    assert "ACTION:" in prompt


def test_director_keeps_only_what_the_tools_read():
    assert set(get_effective_config(None, {})["director"]) == {"max_keep_lines", "silence_line_min"}


@pytest.mark.parametrize(
    "key",
    [
        "enabled",
        "provider",
        "api_base",
        "model",
        "api_key",
        "temperature",
        "reasoning_effort",
        "timeout",
        "response_format",
        "max_retries",
        "retry_temp_step",
        "retry_temp_cap",
        "chunk_lines",
        "pause_after_plan",
        "prompt",
    ],
)
def test_every_removed_director_key_fails_naming_its_path(tmp_path, key):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"director": {key: 1, "max_keep_lines": 4}}))
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    msg = str(e.value)
    assert f"`director.{key}`" in msg
    assert "removed" in msg and "nagare-director" in msg
    assert "extra" not in msg.lower()


def test_removed_keys_are_all_named_at_once(tmp_path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(
        yaml.dump({"director": {"enabled": True, "model": "x"}, "guided_edit": {"enabled": True}})
    )
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    msg = str(e.value)
    for path in ("director.enabled", "director.model", "guided_edit.enabled"):
        assert f"`{path}`" in msg


def test_director_max_keep_lines_default():
    cfg = get_effective_config(None, {})
    assert cfg["director"]["max_keep_lines"] == 8


def test_text_filter_prompt_repeated_phrase_example_is_valid_patch_syntax():
    """Real-run failure mode: told to de-duplicate repeated phrases but shown
    no marker example, the filter LLM rewrites the line bare and the safety
    check drops it.  The default prompt must state the rule AND show a worked
    marker example — and the example must round-trip through the real patch
    applier."""
    from nagare_clip.text_filter.llm_filter import apply_patches_to_lines

    cfg = get_effective_config(None, {})
    prompt = cfg["text_filter"]["prompt"]
    assert "repeated" in prompt
    assert "映ってる映ってるね" in prompt  # example input line
    assert "{{映ってる->}}映ってるね" in prompt  # example output line
    assert apply_patches_to_lines(["{{映ってる->}}映ってるね"]) == ["映ってるね"]


def test_publish_describe_frames_defaults():
    cfg = get_effective_config(None, {})["publish"]["describe_frames"]
    assert cfg["enabled"] is False  # a vision model has to be configured first
    assert cfg["provider"] == "ollama_chat"
    assert cfg["max_retries"] == 2
    assert cfg["prompt"]


def test_the_frame_description_prompt_asks_for_prose_and_the_empty_regions():
    """It is read by a model and by a human; neither needs a schema."""
    prompt = get_effective_config(None, {})["publish"]["describe_frames"]["prompt"].lower()
    assert "json" not in prompt or "no json" in prompt
    assert "empty" in prompt
    assert "legible" in prompt


def test_the_frame_description_prompt_forbids_writing_a_headline():
    """Writing copy is the copy call's job; this one only looks. A vision model
    handed a frame will happily caption it unless told not to."""
    prompt = get_effective_config(None, {})["publish"]["describe_frames"]["prompt"].lower()
    assert "do not write a headline" in prompt


def test_image_markup_is_general_not_per_stage():
    """It is a property of the VIEWER, not of a stage: two copies is one for a
    human to keep in sync, and one project with two markdown files disagreeing."""
    cfg = get_effective_config(None, {})
    assert cfg["general"]["image_markup"] == "html"
    assert "image_markup" not in cfg["publish"]
    assert "image_markup" not in cfg["render"]


@pytest.mark.parametrize("section", ["publish", "render"])
def test_a_per_stage_image_markup_is_rejected_rather_than_ignored(tmp_path, section):
    path = tmp_path / "c.yml"
    path.write_text(f"{section}:\n  image_markup: markdown\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})


def test_publish_pairing_defaults():
    cfg = get_effective_config(None, {})["publish"]["pairing"]
    assert cfg["enabled"] is True  # same text model as the copy call; no new dependency
    # unset = inherit publish's, because it runs on publish's model and must be
    # sampled the way that model requires
    assert cfg["temperature"] is None
    assert cfg["max_retries"] is None
    assert cfg["retry_temp_step"] is None and cfg["retry_temp_cap"] is None
    assert cfg["prompt"]


def test_the_pairing_prompt_owns_the_look_vocabulary_the_copy_prompt_lost():
    prompt = get_effective_config(None, {})["publish"]["pairing"]["prompt"].lower()
    for word in ("fill", "stroke", "strokewidth", "pointsize", "gravity", "offset"):
        assert word in prompt


def test_the_pairing_prompt_asks_for_an_index_never_a_path():
    """A path is a string a model can invent; an index is bounded."""
    prompt = get_effective_config(None, {})["publish"]["pairing"]["prompt"].lower()
    assert "index" in prompt
    assert ".jpg" not in prompt and "path" not in prompt


def test_the_pairing_prompts_own_json_example_parses():
    from nagare_clip.publish.pairing import try_parse_pairing_response

    prompt = get_effective_config(None, {})["publish"]["pairing"]["prompt"]
    start = prompt.index("{", prompt.index("JSON shape"))
    depth, end = 0, start
    for i, ch in enumerate(prompt[start:], start=start):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            end = i + 1
            break
    got = try_parse_pairing_response(prompt[start:end], num_sets=4, num_frames=24)
    assert got is not None and got[1].frame is not None
    assert got[1].style and got[1].lines


def test_render_defaults():
    cfg = get_effective_config(None, {})
    render = cfg["render"]
    assert render["enabled"] is True
    assert (render["width"], render["height"]) == (1280, 720)
    assert render["line_gap"] == 12
    assert render["fonts"] == {}


def test_render_fonts_come_from_the_file(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        'render:\n  fonts:\n    hook: "Noto-Serif-CJK-JP-Black"\n',
        encoding="utf-8",
    )
    cfg = get_effective_config(path, {})
    assert cfg["render"]["fonts"] == {"hook": "Noto-Serif-CJK-JP-Black"}


def test_an_unknown_render_key_is_rejected(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("render:\n  colour: red\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})


def test_a_background_key_is_rejected_rather_than_ignored(tmp_path):
    """One project-wide background is gone; a set names its own in publish.json."""
    path = tmp_path / "c.yml"
    path.write_text("render:\n  background: frames/a/1.jpg\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})


def test_a_config_still_carrying_publish_thumbnail_fails_loudly(tmp_path):
    """No shim: the canvas size must have exactly one place to look."""
    path = tmp_path / "c.yml"
    path.write_text("publish:\n  thumbnail:\n    width: 1920\n", encoding="utf-8")
    with pytest.raises(ValidationError) as e:
        get_effective_config(path, {})
    assert "thumbnail" in str(e.value)


def _display_view(n: int = 100):
    """A flat one-segment view, enough for the loop to read a reply against."""
    from nagare_clip.director.display import DisplayLine, DisplaySegment, DisplayView

    return DisplayView(
        lines=[
            DisplayLine(number=i, segment=1, stem="x", source_line=i, is_silence=False, text="t")
            for i in range(1, n + 1)
        ],
        segments=[DisplaySegment(index=1, stem="x", label="x", first=1, last=n)],
    )


# ---------------------------------------------------------------------------
# reasoning_effort: passed straight to LiteLLM; the old `thinking` key is gone
# ---------------------------------------------------------------------------

_LLM_SECTIONS = [
    ("sentence_split",),
    ("text_filter",),
    ("summary",),
    ("gap_context",),
    ("publish",),
    ("publish", "describe_frames"),
]


def _section(cfg: dict, path: tuple[str, ...]) -> dict:
    for key in path:
        cfg = cfg[key]
    return cfg


def _nest(path: tuple[str, ...], leaf: dict) -> dict:
    for key in reversed(path):
        leaf = {key: leaf}
    return leaf


@pytest.mark.parametrize("path", _LLM_SECTIONS, ids=".".join)
def test_reasoning_effort_defaults_to_unset(path):
    section = _section(DEFAULTS, path)
    assert section["reasoning_effort"] is None
    assert "thinking" not in section


@pytest.mark.parametrize("path", _LLM_SECTIONS, ids=".".join)
def test_reasoning_effort_value_is_kept_verbatim(tmp_path: Path, path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump(_nest(path, {"reasoning_effort": "xhigh"})))
    assert _section(get_effective_config(cfg_file), path)["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("path", _LLM_SECTIONS, ids=".".join)
def test_old_thinking_key_fails_naming_the_key_and_its_replacement(tmp_path: Path, path):
    """Not a silent alias: `thinking: false` used to be translated, and
    `reasoning_effort` is not -- so a leftover key must stop the run and say
    which key and what replaced it, not be read as a generic typo."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump(_nest(path, {"thinking": False})))
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    msg = str(e.value)
    assert ".".join((*path, "thinking")) in msg
    assert "reasoning_effort" in msg
    assert "LiteLLM" in msg
    assert "extra" not in msg.lower()


def test_old_thinking_key_reports_every_occurrence(tmp_path: Path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(
        yaml.dump({"director": {"thinking": True}, "summary": {"thinking": "high"}})
    )
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    assert "director.thinking" in str(e.value)
    assert "summary.thinking" in str(e.value)


@pytest.mark.parametrize("section", ["plan", "plan_revise"])
def test_a_removed_stage_section_fails_naming_what_replaced_it(tmp_path: Path, section):
    """The plan stages are gone; a config that still configures them must say
    so and point at the director, not fail with a generic "extra inputs"."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({section: {"enabled": True}}))
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    msg = str(e.value)
    assert f"`{section}:`" in msg and "director" in msg
    assert "extra" not in msg.lower()


def test_config_rejects_removed_audio_silence_keys(tmp_path):
    f = tmp_path / "c.yml"
    f.write_text(yaml.safe_dump({"audio_silence": {"noise": -30, "enabled": False}}))
    with pytest.raises(ValueError) as e:
        get_effective_config(f, {})
    assert "audio_silence.noise" in str(e.value) and "audio_silence.enabled" in str(e.value)


def test_config_rejects_old_transcription_keys(tmp_path):
    f = tmp_path / "c.yml"
    f.write_text("transcription:\n  compute_type: int8\n  language: ja\n  align_model: x\n")
    with pytest.raises(Exception) as e:
        get_effective_config(f)
    msg = str(e.value)
    for k in ("compute_type", "language", "align_model"):
        assert f"`transcription.{k}`" in msg


def test_transcription_defaults():
    t = get_effective_config(None)["transcription"]
    assert t == {
        "model": "third-intelligence/Haas-ja-v1.0",
        "batch_size": 8,
        "max_chunk_seconds": 30.0,
    }


def test_window_segments_default_200():
    assert get_effective_config(None)["sentence_split"]["window_segments"] == 200


def test_whisper_config_defaults():
    w = get_effective_config(None, {})["whisper"]
    assert w == {"enabled": False, "model": "large-v3", "batch_size": 8, "beam_size": 5}
