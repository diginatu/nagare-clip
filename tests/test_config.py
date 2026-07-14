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
        cfg_file.write_text(yaml.dump({"blender": {"default_fps": 24.0}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["default_fps"] == 24.0
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
        assert cfg["pipeline"]["from_stage"] == "transcription"
        assert cfg["pipeline"]["to_stage"] == "blender"

    def test_transcription_language_default(self):
        cfg = get_effective_config(None)
        assert cfg["transcription"]["language"] == "ja"

    def test_transcription_language_config_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"transcription": {"language": "en"}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["transcription"]["language"] == "en"
        # Other transcription defaults intact
        assert cfg["transcription"]["compute_type"] == "float16"

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


def test_director_defaults_present():
    cfg = get_effective_config(None, {})
    d = cfg["director"]
    assert d["enabled"] is False
    assert d["provider"] == "ollama_chat"
    assert d["api_base"] == ""
    assert "model" in d
    assert d["response_format"] == "json"
    assert isinstance(d["prompt"], str) and d["prompt"]


def test_director_prompt_documents_speed_does_not_keep_silence():
    """The director prompt must tell the LLM that <speed> drops internal
    silences and that a `keep` over the same span preserves them (so the
    director can express keep+speed)."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    speed_line = next(ln for ln in prompt.splitlines() if ln.startswith("- speed:"))
    assert "keep" in speed_line.lower()
    assert "silence" in speed_line.lower() or "pause" in speed_line.lower()


@pytest.mark.parametrize("stage", ["director", "plan"])
def test_prompt_documents_duration_and_gap_bracket(stage):
    """The director/plan inputs carry a `[4.2s, gap 0.8s]` bracket per line/part
    (rendered by timing.format_dur_gap).  The prompt must explain that notation,
    and the example it shows must be exactly what the renderer emits — otherwise
    the LLM is told to read a format we never produce."""
    from nagare_clip.timing import format_dur_gap

    prompt = get_effective_config(None, {})[stage]["prompt"]
    assert format_dur_gap(4.2, 0.8) in prompt  # "[4.2s, gap 0.8s]"
    assert format_dur_gap(4.2, None) in prompt  # "[4.2s]" — last line/part, no gap
    lowered = prompt.lower()
    assert "duration" in lowered
    assert "gap" in lowered


def test_guided_edit_defaults_present():
    cfg = get_effective_config(None, {})
    g = cfg["guided_edit"]
    assert g["enabled"] is False
    assert g["provider"] == "ollama_chat"
    assert g["api_base"] == ""
    assert "model" in g
    assert isinstance(g["prompt"], str) and g["prompt"]


def test_director_override_keeps_other_defaults(tmp_path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"director": {"enabled": True, "model": "gpt-oss:120b"}}))
    cfg = get_effective_config(cfg_file)
    assert cfg["director"]["enabled"] is True
    assert cfg["director"]["model"] == "gpt-oss:120b"
    assert cfg["director"]["temperature"] == 0.2


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
        DEFAULTS["plan"],
        DEFAULTS["director"],
        DEFAULTS["guided_edit"],
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
    assert sp["window_segments"] == 20
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
    monkeypatch.setenv("PLAN", "short")
    cfg = get_effective_config(None)  # must not raise
    assert cfg["plan"]["enabled"] is False


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


def test_director_prompt_documents_gap_annotations():
    # The director must be told what the `[silent gap ...]` annotation line means
    # and that a `keep` op spanning the adjacent lines rescues the moment.
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    assert "[silent gap" in prompt
    assert "keep" in prompt


def test_director_prompt_gap_example_matches_the_real_formatter():
    """The DIRECTOR_PROMPT's documented example line must be exactly what
    gap_context.context.annotate_numbered_transcript renders for the
    corresponding Gap -- pins the doc example to the real formatter so a
    rendering change (indent, decimal places, wording) fails loudly here
    instead of silently going stale in the prompt."""
    from nagare_clip.gap_context.context import annotate_numbered_transcript
    from nagare_clip.gap_context.gaps import Gap

    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]

    gap = Gap(start=0.0, end=12.4, frames=[], description="a build runs and logs scroll past")
    rendered = annotate_numbered_transcript("1: x", [(1, gap)])
    annotation_line = rendered.split("\n")[1]

    assert annotation_line == "    [silent gap 12.4s: a build runs and logs scroll past]"
    assert annotation_line in prompt
