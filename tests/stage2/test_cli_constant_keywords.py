"""Tests for constant keywords injection in stage2 CLI."""

from __future__ import annotations

from unittest.mock import patch

from nagare_clip.stage2 import cli as stage2_cli
from nagare_clip.stage2.summary_llm import SummaryResult


def _s2_config(
    summary_llm_enabled: bool = False,
    constant_keywords: list | None = None,
) -> dict:
    return {
        "use_llm": True,
        "api_base": "http://localhost:11434",
        "model": "test",
        "api_key": "",
        "batch_size": 10,
        "timeout": 60,
        "retry_on_invalid": False,
        "retry_min_batch_size": 1,
        "prompt": "Base prompt.",
        "temperature": 0.1,
        "thinking": False,
        "summary_llm": {
            "enabled": summary_llm_enabled,
            "keywords": constant_keywords if constant_keywords is not None else [],
            "api_base": "http://localhost:11434",
            "model": "test",
            "api_key": "",
            "temperature": 0.3,
            "thinking": False,
            "timeout": 120,
            "response_format": "json",
            "prompt": "Summarize.",
        },
    }


def _run(tmp_path, s2_config, summary_result=None, lines=None):
    """Run CLI main() and return the filter_cfg passed to filter_transcript."""
    if lines is None:
        lines = ["test line"]

    txt = tmp_path / "test.txt"
    txt.write_text("\n".join(lines))
    output = tmp_path / "test_edits.txt"

    captured: dict = {}

    def mock_filter(lines, cfg, **kwargs):
        captured.update(cfg)
        return lines

    config = {
        "general": {"log_level": "WARNING", "log_file": ""},
        "text_filter": s2_config,
    }

    with (
        patch("nagare_clip.stage2.cli.get_effective_config", return_value=config),
        patch("nagare_clip.stage2.cli.filter_transcript", side_effect=mock_filter),
        patch("nagare_clip.stage2.cli.generate_summary", return_value=summary_result),
        patch("nagare_clip.stage2.cli.setup_logging"),
        patch("sys.argv", ["cli", "--txt", str(txt), "--output-txt", str(output)]),
    ):
        stage2_cli.main()

    return captured


class TestConstantKeywords:
    def test_constant_keywords_injected_when_summary_llm_disabled(self, tmp_path):
        s2 = _s2_config(summary_llm_enabled=False, constant_keywords=["TestWord"])
        cfg = _run(tmp_path, s2)
        assert "TestWord" in cfg.get("prompt", "")

    def test_no_keywords_prompt_unchanged_when_summary_llm_disabled(self, tmp_path):
        s2 = _s2_config(summary_llm_enabled=False, constant_keywords=[])
        cfg = _run(tmp_path, s2)
        assert cfg.get("prompt") == "Base prompt."

    def test_constant_keywords_merged_with_dynamic_when_summary_llm_enabled(self, tmp_path):
        s2 = _s2_config(summary_llm_enabled=True, constant_keywords=["Constant"])
        summary_result = SummaryResult(summary="a summary", keywords=["Dynamic"])
        cfg = _run(tmp_path, s2, summary_result=summary_result)
        prompt = cfg.get("prompt", "")
        assert "Constant" in prompt
        assert "Dynamic" in prompt

    def test_constant_only_merged_when_summary_has_none(self, tmp_path):
        """Constant keywords remain when summary LLM returns no keywords."""
        s2 = _s2_config(summary_llm_enabled=True, constant_keywords=["Const"])
        summary_result = SummaryResult(summary="summary text", keywords=[])
        cfg = _run(tmp_path, s2, summary_result=summary_result)
        assert "Const" in cfg.get("prompt", "")
