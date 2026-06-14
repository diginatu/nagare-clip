"""Tests for Stage 2 summary LLM module."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nagare_clip.stage2.summary_llm import (
    SummaryResult,
    build_enhanced_prompt,
    generate_summary,
    parse_summary_response,
)


class TestParseSummaryResponse:
    def test_valid_json(self):
        response = json.dumps(
            {"summary": "動画の概要", "keywords": ["Kubernetes", "PostgreSQL"]}
        )
        result = parse_summary_response(response)
        assert result is not None
        assert result.summary == "動画の概要"
        assert result.keywords == ["Kubernetes", "PostgreSQL"]

    def test_missing_summary_returns_none(self):
        response = json.dumps({"keywords": ["word1"]})
        assert parse_summary_response(response) is None

    def test_missing_keywords_returns_none(self):
        response = json.dumps({"summary": "概要"})
        assert parse_summary_response(response) is None

    def test_empty_string_returns_none(self):
        assert parse_summary_response("") is None

    def test_invalid_json_returns_none(self):
        assert parse_summary_response("not json at all") is None

    def test_non_dict_json_returns_none(self):
        assert parse_summary_response(json.dumps(["a", "b"])) is None

    def test_keywords_whitespace_trimmed(self):
        response = json.dumps(
            {"summary": "概要", "keywords": [" word1 ", "  word2  "]}
        )
        result = parse_summary_response(response)
        assert result is not None
        assert result.keywords == ["word1", "word2"]

    def test_empty_keywords_list(self):
        response = json.dumps({"summary": "概要", "keywords": []})
        result = parse_summary_response(response)
        assert result is not None
        assert result.keywords == []

    def test_extra_fields_ignored(self):
        response = json.dumps(
            {"summary": "概要", "keywords": ["w1"], "extra": "ignored"}
        )
        result = parse_summary_response(response)
        assert result is not None
        assert result.summary == "概要"
        assert result.keywords == ["w1"]

    def test_keywords_non_list_returns_none(self):
        response = json.dumps({"summary": "概要", "keywords": "not a list"})
        assert parse_summary_response(response) is None

    def test_summary_non_string_returns_none(self):
        response = json.dumps({"summary": 123, "keywords": ["w1"]})
        assert parse_summary_response(response) is None

    def test_markdown_json_fence_stripped(self):
        payload = json.dumps({"summary": "概要", "keywords": ["w1"]})
        response = f"```json\n{payload}\n```"
        result = parse_summary_response(response)
        assert result is not None
        assert result.summary == "概要"
        assert result.keywords == ["w1"]

    def test_markdown_plain_fence_stripped(self):
        payload = json.dumps({"summary": "概要", "keywords": ["w1"]})
        response = f"```\n{payload}\n```"
        result = parse_summary_response(response)
        assert result is not None
        assert result.summary == "概要"

    def test_fence_with_surrounding_whitespace(self):
        payload = json.dumps({"summary": "概要", "keywords": ["w1"]})
        response = f"  \n```json\n{payload}\n```  \n"
        result = parse_summary_response(response)
        assert result is not None
        assert result.summary == "概要"


class TestBuildEnhancedPrompt:
    def test_appends_summary_and_keywords(self):
        base = "Fix errors."
        summary = SummaryResult(summary="プログラミング解説", keywords=["Kubernetes", "PostgreSQL"])
        result = build_enhanced_prompt(base, summary)
        assert result.startswith("Fix errors.")
        assert "プログラミング解説" in result
        assert "Kubernetes" in result
        assert "PostgreSQL" in result

    def test_empty_keywords_list(self):
        base = "Fix errors."
        summary = SummaryResult(summary="概要のみ", keywords=[])
        result = build_enhanced_prompt(base, summary)
        assert "概要のみ" in result
        assert result.startswith("Fix errors.")

    def test_preserves_base_prompt(self):
        base = "Line 1\nLine 2\nLine 3"
        summary = SummaryResult(summary="概要", keywords=["w1"])
        result = build_enhanced_prompt(base, summary)
        assert result.startswith(base)

    def test_empty_summary_with_keywords_omits_summary_line(self):
        base = "Fix errors."
        summary = SummaryResult(summary="", keywords=["Foo", "Bar"])
        result = build_enhanced_prompt(base, summary)
        assert "Foo" in result
        assert "Bar" in result
        assert "Summary:" not in result

    def test_empty_summary_no_keywords_returns_base(self):
        base = "Fix errors."
        summary = SummaryResult(summary="", keywords=[])
        result = build_enhanced_prompt(base, summary)
        assert result == base


import yaml as _yaml

from nagare_clip.llm_report import Recorder


class TestSummaryLLMRecorder:
    def test_records_summary_call(self, tmp_path):
        rec = Recorder("text_filter", tmp_path, enabled=True)

        def fake(_messages, _cfg):
            return '{"summary":"s","keywords":["k"]}'

        out = generate_summary("some text", {"model": "m"}, call_llm=fake, recorder=rec)
        assert out is not None
        text = (tmp_path / "text_filter" / "summary_llm.md").read_text(encoding="utf-8")
        _, fm, _ = text.split("---", 2)
        assert _yaml.safe_load(fm)["outcome"] == "ok"


class TestGenerateSummary:
    @patch("nagare_clip.stage2.summary_llm._call_llm")
    def test_successful_summary(self, mock_llm):
        mock_llm.return_value = json.dumps(
            {"summary": "テスト動画の概要", "keywords": ["WebSocket", "API"]}
        )
        cfg = {
            "api_base": "http://localhost:11434",
            "model": "test",
            "prompt": "Analyze transcript",
            "response_format": "json",
        }
        result = generate_summary("テスト文章", cfg)
        assert result is not None
        assert result.summary == "テスト動画の概要"
        assert result.keywords == ["WebSocket", "API"]

    @patch("nagare_clip.stage2.summary_llm._call_llm")
    def test_llm_failure_returns_none(self, mock_llm):
        mock_llm.side_effect = ConnectionError("timeout")
        cfg = {"api_base": "http://localhost:11434", "model": "test", "prompt": "p"}
        result = generate_summary("テスト", cfg)
        assert result is None

    @patch("nagare_clip.stage2.summary_llm._call_llm")
    def test_unparseable_response_returns_none(self, mock_llm):
        mock_llm.return_value = "not valid json"
        cfg = {"api_base": "http://localhost:11434", "model": "test", "prompt": "p"}
        result = generate_summary("テスト", cfg)
        assert result is None

    @patch("nagare_clip.stage2.summary_llm._call_llm")
    def test_passes_correct_messages(self, mock_llm):
        mock_llm.return_value = json.dumps(
            {"summary": "s", "keywords": ["k"]}
        )
        cfg = {
            "api_base": "http://localhost:11434",
            "model": "test",
            "prompt": "Analyze this",
        }
        generate_summary("line1\nline2", cfg)
        messages = mock_llm.call_args[0][0]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Analyze this"
        assert messages[1]["role"] == "user"
        assert "line1\nline2" in messages[1]["content"]

    @patch("nagare_clip.stage2.summary_llm._call_llm")
    def test_empty_text_returns_none(self, mock_llm):
        cfg = {"api_base": "http://localhost:11434", "model": "test", "prompt": "p"}
        result = generate_summary("", cfg)
        assert result is None
        mock_llm.assert_not_called()
