"""Tests for the centralised config module."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nagare_clip.config import DEFAULTS, deep_merge, get_effective_config, load_config


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
        cfg_file.write_text(
            yaml.dump({"intervals": {"caption": {"max_bunsetu": 20}}})
        )
        cfg = get_effective_config(cfg_file)
        assert cfg["intervals"]["caption"]["max_bunsetu"] == 20
        # Other caption defaults intact
        assert cfg["intervals"]["caption"]["max_duration"] == 4.0

    def test_caption_style_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(
            yaml.dump({"blender": {"caption_style": {"font_size": 72}}})
        )
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["caption_style"]["font_size"] == 72
        assert cfg["blender"]["caption_style"]["alignment_x"] == "CENTER"

    def test_unknown_keys_preserved(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"custom_section": {"key": "value"}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["custom_section"]["key"] == "value"
        # Defaults still present
        assert "intervals" in cfg

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

    def test_summary_llm_defaults_present(self):
        cfg = get_effective_config(None)
        slm = cfg["text_filter"]["summary_llm"]
        assert slm["enabled"] is False
        assert "model" in slm
        assert "api_base" in slm
        assert "prompt" in slm
        assert slm["response_format"] == "json"

    def test_summary_llm_config_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(
            yaml.dump({"text_filter": {"summary_llm": {"model": "gemma3:27b"}}})
        )
        cfg = get_effective_config(cfg_file)
        assert cfg["text_filter"]["summary_llm"]["model"] == "gemma3:27b"
        # Other summary_llm defaults intact
        assert cfg["text_filter"]["summary_llm"]["enabled"] is False
        # Other text_filter defaults intact
        assert cfg["text_filter"]["batch_size"] == 10
