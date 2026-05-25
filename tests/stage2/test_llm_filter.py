"""Tests for Stage 2 LLM filter."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from nagare_clip.stage2.llm_filter import (
    _apply_patches,
    _batch_lines,
    _call_llm,
    _format_batch,
    _parse_response,
    _validate_patches,
    apply_patches_to_lines,
    filter_transcript,
)


class TestBatchLines:
    def test_single_batch(self):
        lines = ["a", "b", "c"]
        batches = _batch_lines(lines, 10)
        assert len(batches) == 1
        assert batches[0] == [(0, "a"), (1, "b"), (2, "c")]

    def test_multiple_batches(self):
        lines = ["a", "b", "c", "d", "e"]
        batches = _batch_lines(lines, 2)
        assert len(batches) == 3
        assert batches[0] == [(0, "a"), (1, "b")]
        assert batches[1] == [(2, "c"), (3, "d")]
        assert batches[2] == [(4, "e")]

    def test_empty(self):
        assert _batch_lines([], 5) == []


class TestFormatBatch:
    def test_basic(self):
        batch = [(0, "hello"), (1, "world")]
        result = _format_batch(batch)
        assert result == "1: hello\n2: world"

    def test_preserves_content(self):
        batch = [(4, "line five")]
        result = _format_batch(batch)
        assert result == "5: line five"


class TestApplyPatches:
    def test_correction(self):
        result = _apply_patches(
            "{{急->今日}}はいい天気ですね",
            "急はいい天気ですね",
        )
        assert result == "今日はいい天気ですね"

    def test_deletion(self):
        result = _apply_patches(
            "{{えーと->}}はい",
            "えーとはい",
        )
        assert result == "はい"

    def test_whole_line_delete(self):
        result = _apply_patches(
            "{{(雑音)->}}",
            "(雑音)",
        )
        assert result == ""

    def test_multiple_patches(self):
        result = _apply_patches(
            "{{えーと->}}回り始めるようになっ{{てた->ていた}}と思います",
            "えーと回り始めるようになってたと思います",
        )
        assert result == "回り始めるようになっていたと思います"

    def test_no_patches_returns_text(self):
        result = _apply_patches(
            "これは修正不要です",
            "これは修正不要です",
        )
        assert result == "これは修正不要です"

    def test_old_not_in_original_returns_none(self):
        result = _apply_patches(
            "{{存在しない->修正}}テスト",
            "テスト",
        )
        assert result is None

    def test_empty_old_is_valid(self):
        # Empty old = insertion (always valid since "" is in any string)
        result = _apply_patches(
            "{{->追加}}テスト",
            "テスト",
        )
        assert result == "追加テスト"


class TestValidatePatches:
    def test_valid_patches(self):
        assert _validate_patches("{{急->今日}}はいい天気ですね", "急はいい天気ですね")

    def test_no_patches(self):
        assert _validate_patches("plain text", "plain text")

    def test_changed_text_without_markers_rejected(self):
        """LLM changed text without {{old->new}} markers should be rejected."""
        assert not _validate_patches("changed text", "original text")

    def test_text_changed_outside_markers_rejected(self):
        """LLM silently changed text outside markers should be rejected."""
        # Original: "どうだろうないけるかな", LLM removed "な" outside markers
        assert not _validate_patches(
            "どうだろう{{いけるかな->}}",
            "どうだろうないけるかな",
        )

    def test_valid_markers_with_unchanged_surrounding_text(self):
        """Markers valid and surrounding text unchanged should pass."""
        assert _validate_patches(
            "どうだろうな{{いけるかな->}}",
            "どうだろうないけるかな",
        )

    def test_invalid_old(self):
        assert not _validate_patches("{{存在しない->修正}}テスト", "テスト")


class TestParseResponse:
    def test_basic_parse_preserves_markers(self):
        batch = [(0, "あのー今日は"), (1, "えーとはい")]
        response = "1: {{あのー->}}今日は\n2: {{えーと->}}はい"
        result = _parse_response(response, batch)
        assert result[0] == "{{あのー->}}今日は"
        assert result[1] == "{{えーと->}}はい"

    def test_missing_line_skipped(self):
        batch = [(0, "line one"), (1, "line two")]
        response = "1: line one"
        result = _parse_response(response, batch)
        assert 1 not in result

    def test_no_numbered_lines_returns_empty(self):
        batch = [(0, "test")]
        response = "This is not a valid response"
        result = _parse_response(response, batch)
        assert result == {}

    def test_unchanged_lines_returned(self):
        batch = [(0, "unchanged")]
        response = "1: unchanged"
        result = _parse_response(response, batch)
        assert result[0] == "unchanged"

    def test_noop_markers_stripped(self):
        """Markers where old == new (no actual change) should be stripped."""
        batch = [(0, "テストはいって感じ")]
        response = "1: テスト{{はいって->はいって}}感じ"
        result = _parse_response(response, batch)
        assert result[0] == "テストはいって感じ"

    def test_noop_markers_stripped_mixed(self):
        """Mix of real and no-op markers: only no-op ones are stripped."""
        batch = [(0, "こいつが2メートルはいって感じ")]
        response = "1: {{こいつが->は}}2メートル{{はいって->はいって}}感じ"
        result = _parse_response(response, batch)
        assert result[0] == "{{こいつが->は}}2メートルはいって感じ"

    def test_invalid_patch_rejected(self):
        batch = [(0, "テスト")]
        response = "1: {{存在しない->修正}}テスト"
        result = _parse_response(response, batch)
        assert 0 not in result

    def test_changed_text_without_markers_rejected(self):
        """LLM changed text without markers should be rejected in parse."""
        batch = [(0, "original text")]
        response = "1: different text"
        result = _parse_response(response, batch)
        assert 0 not in result


class TestApplyPatchesToLines:
    def test_applies_patches(self):
        lines = ["{{えーと->}}今日は", "plain line"]
        result = apply_patches_to_lines(lines)
        assert result == ["今日は", "plain line"]

    def test_multiple_patches_per_line(self):
        lines = ["{{えーと->}}回り始めるようになっ{{てた->ていた}}と思います"]
        result = apply_patches_to_lines(lines)
        assert result == ["回り始めるようになっていたと思います"]

    def test_no_patches(self):
        lines = ["clean line"]
        result = apply_patches_to_lines(lines)
        assert result == ["clean line"]

    def test_empty_input(self):
        assert apply_patches_to_lines([]) == []


class TestFilterTranscript:
    def test_empty_input(self):
        assert filter_transcript([], {}) == []

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_successful_filter_preserves_markers(self, mock_llm):
        mock_llm.return_value = "1: {{えーと->}}今日は\n2: line two"
        lines = ["えーと今日は", "line two"]
        cfg = {"batch_size": 10, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert result[0] == "{{えーと->}}今日は"
        assert result[1] == "line two"

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_api_failure_keeps_originals(self, mock_llm):
        mock_llm.side_effect = ConnectionError("timeout")
        lines = ["original line"]
        cfg = {"batch_size": 10, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert result == ["original line"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_garbled_response_keeps_originals(self, mock_llm):
        mock_llm.return_value = "garbled nonsense without line numbers"
        lines = ["original"]
        cfg = {"batch_size": 10, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert result == ["original"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_added_text_preserves_markers(self, mock_llm):
        """LLM may add helpful text — markers are preserved."""
        mock_llm.return_value = "1: {{短い->短い文を長い説明に変える}}テスト"
        lines = ["短いテスト"]
        cfg = {"batch_size": 10, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert result[0] == "{{短い->短い文を長い説明に変える}}テスト"


class TestRetryOnInvalid:
    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_no_retry_when_all_succeed(self, mock_llm):
        mock_llm.return_value = "1: {{a->A}}\n2: {{b->B}}\n3: {{c->C}}\n4: {{d->D}}"
        lines = ["a", "b", "c", "d"]
        cfg = {"batch_size": 4, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 1
        assert result == ["{{a->A}}", "{{b->B}}", "{{c->C}}", "{{d->D}}"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_halving_retry_on_validation_failure(self, mock_llm):
        """Line 2 changed outside markers on call #1; retry with just line 2 succeeds."""
        mock_llm.side_effect = [
            "1: {{a->A}}\n2: B_wrong\n3: {{c->C}}\n4: {{d->D}}",
            "2: {{b->B}}",
        ]
        lines = ["a", "b", "c", "d"]
        cfg = {"batch_size": 4, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 2
        retry_user_content = mock_llm.call_args_list[1][0][0][1]["content"]
        assert retry_user_content == "2: b"
        assert result == ["{{a->A}}", "{{b->B}}", "{{c->C}}", "{{d->D}}"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_recursive_shrink_to_size_one(self, mock_llm):
        """4→2→1: all fail at 4, half succeed at 2, remaining succeed only at 1."""
        mock_llm.side_effect = [
            "1: A_wrong\n2: B_wrong\n3: C_wrong\n4: D_wrong",
            "1: {{a->A}}\n2: {{b->B}}",
            "3: C_wrong\n4: D_wrong",
            "3: {{c->C}}",
            "4: {{d->D}}",
        ]
        lines = ["a", "b", "c", "d"]
        cfg = {"batch_size": 4, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 5
        assert result == ["{{a->A}}", "{{b->B}}", "{{c->C}}", "{{d->D}}"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_floor_honored(self, mock_llm):
        """retry_min_batch_size=2 stops shrinking at 2 even if size-2 batch still fails."""
        mock_llm.side_effect = [
            "1: A_wrong\n2: B_wrong\n3: C_wrong\n4: D_wrong",
            "1: A_wrong\n2: B_wrong",
            "3: C_wrong\n4: D_wrong",
        ]
        lines = ["a", "b", "c", "d"]
        cfg = {"batch_size": 4, "prompt": "fix", "retry_min_batch_size": 2}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 3
        assert result == ["a", "b", "c", "d"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_disabled_skips_retry(self, mock_llm):
        mock_llm.return_value = "1: A_wrong\n2: B_wrong"
        lines = ["a", "b"]
        cfg = {"batch_size": 2, "prompt": "fix", "retry_on_invalid": False}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 1
        assert result == ["a", "b"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_retry_on_missing_line(self, mock_llm):
        """LLM omits line 2 entirely; retry with just that line succeeds."""
        mock_llm.side_effect = [
            "1: {{a->A}}",
            "2: {{b->B}}",
        ]
        lines = ["a", "b"]
        cfg = {"batch_size": 2, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 2
        assert result == ["{{a->A}}", "{{b->B}}"]

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_exception_during_retry_keeps_partial_progress(self, mock_llm):
        """Retry sub-call raises; lines that passed call #1 stay corrected."""
        mock_llm.side_effect = [
            "1: {{a->A}}\n2: B_wrong\n3: {{c->C}}\n4: D_wrong",
            ConnectionError("timeout"),
        ]
        lines = ["a", "b", "c", "d"]
        cfg = {"batch_size": 4, "prompt": "fix"}
        result = filter_transcript(lines, cfg)
        assert mock_llm.call_count == 2
        assert result == ["{{a->A}}", "b", "{{c->C}}", "d"]


_FILTER_LOGGER = "nagare_clip.stage2.llm_filter"


class TestRetryStats:
    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_all_succeed_per_size_log(self, mock_llm, caplog):
        """All lines succeed at initial size: per-size log emitted, no 'total' retry line."""
        mock_llm.return_value = "1: {{a->A}}\n2: {{b->B}}"
        lines = ["a", "b"]
        cfg = {"batch_size": 2, "prompt": "fix"}
        with caplog.at_level(logging.INFO, logger=_FILTER_LOGGER):
            filter_transcript(lines, cfg)
        msgs = [r.message for r in caplog.records if "LLM filter" in r.message]
        assert any("batch_size=2" in m and "2/2" in m and "(retry)" not in m for m in msgs)
        assert not any("retries saved" in m for m in msgs)

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_retry_saved_count(self, mock_llm, caplog):
        """Line 2 fails at batch=2, succeeds at batch=1: retry-saved=1 in total log."""
        mock_llm.side_effect = [
            "1: {{a->A}}\n2: B_wrong",
            "2: {{b->B}}",
        ]
        lines = ["a", "b"]
        cfg = {"batch_size": 2, "prompt": "fix"}
        with caplog.at_level(logging.INFO, logger=_FILTER_LOGGER):
            filter_transcript(lines, cfg)
        msgs = [r.message for r in caplog.records if "LLM filter" in r.message]
        assert any("batch_size=2" in m and "1/2" in m and "(retry)" not in m for m in msgs)
        assert any("batch_size=1 (retry)" in m and "1/1" in m for m in msgs)
        assert any("retries saved 1/1" in m for m in msgs)

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_retry_all_fail(self, mock_llm, caplog):
        """Retry also fails: retries saved 0/N in total log."""
        mock_llm.side_effect = [
            "1: B_wrong\n2: C_wrong",
            "1: B_wrong",
            "2: C_wrong",
        ]
        lines = ["a", "b"]
        cfg = {"batch_size": 2, "prompt": "fix"}
        with caplog.at_level(logging.INFO, logger=_FILTER_LOGGER):
            filter_transcript(lines, cfg)
        msgs = [r.message for r in caplog.records if "LLM filter" in r.message]
        assert any("retries saved 0/2" in m for m in msgs)

    @patch("nagare_clip.stage2.llm_filter._call_llm")
    def test_exception_path_counted(self, mock_llm, caplog):
        """Exception during LLM call: lines counted as attempted with 0 succeeded."""
        mock_llm.side_effect = ConnectionError("timeout")
        lines = ["a", "b"]
        cfg = {"batch_size": 2, "prompt": "fix"}
        with caplog.at_level(logging.INFO, logger=_FILTER_LOGGER):
            filter_transcript(lines, cfg)
        msgs = [r.message for r in caplog.records if "LLM filter" in r.message]
        assert any("batch_size=2" in m and "0/2" in m for m in msgs)

    def test_empty_input_no_stats_log(self, caplog):
        """Empty input: no stats log emitted."""
        with caplog.at_level(logging.INFO, logger=_FILTER_LOGGER):
            filter_transcript([], {})
        assert not any("LLM filter" in r.message for r in caplog.records)


def _make_urlopen_mock(content: str) -> MagicMock:
    resp_body = json.dumps({"message": {"content": content}, "done": True}).encode(
        "utf-8"
    )
    mock_resp = MagicMock()
    mock_resp.read.return_value = resp_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestCallLlmThinking:
    @patch("urllib.request.urlopen")
    def test_think_true_when_thinking_true(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock("1: ok")
        cfg = {"thinking": True, "model": "test-model", "api_base": "http://localhost"}
        _call_llm([{"role": "user", "content": "hi"}], cfg)
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body.get("think") is True

    @patch("urllib.request.urlopen")
    def test_think_false_when_thinking_false(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock("1: ok")
        cfg = {"thinking": False, "model": "test-model", "api_base": "http://localhost"}
        _call_llm([{"role": "user", "content": "hi"}], cfg)
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body.get("think") is False

    @patch("urllib.request.urlopen")
    def test_think_false_by_default(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock("1: ok")
        cfg = {"model": "test-model", "api_base": "http://localhost"}
        _call_llm([{"role": "user", "content": "hi"}], cfg)
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body.get("think") is False

    @patch("urllib.request.urlopen")
    def test_think_string_level(self, mock_urlopen):
        """thinking accepts string levels like 'low' for models that support it."""
        mock_urlopen.return_value = _make_urlopen_mock("1: ok")
        cfg = {"thinking": "low", "model": "test-model", "api_base": "http://localhost"}
        _call_llm([{"role": "user", "content": "hi"}], cfg)
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body.get("think") == "low"


class TestCallLlmResponseFormat:
    @patch("urllib.request.urlopen")
    def test_format_json_when_response_format_set(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock('{"key": "value"}')
        cfg = {
            "response_format": "json",
            "model": "test-model",
            "api_base": "http://localhost",
        }
        _call_llm([{"role": "user", "content": "hi"}], cfg)
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body.get("format") == "json"

    @patch("urllib.request.urlopen")
    def test_no_format_when_response_format_not_set(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock("1: ok")
        cfg = {"model": "test-model", "api_base": "http://localhost"}
        _call_llm([{"role": "user", "content": "hi"}], cfg)
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert "format" not in body
