"""Tests for Stage 2 rule-based filters."""

from __future__ import annotations

from nagare_clip.stage2.rule_filter import remove_midstream_closing


class TestRemoveMidstreamClosing:
    def test_marks_closing_in_middle(self):
        lines = [
            "こんにちは",
            "ご視聴ありがとうございました",
            "今日は天気がいいですね",
            "さようなら",
        ]
        result = remove_midstream_closing(lines)
        assert result == [
            "こんにちは",
            "{{ご視聴ありがとうございました->}}",
            "今日は天気がいいですね",
            "さようなら",
        ]

    def test_keeps_closing_at_end(self):
        lines = [
            "こんにちは",
            "今日は天気がいいですね",
            "ご視聴ありがとうございました",
        ]
        result = remove_midstream_closing(lines)
        assert result == lines

    def test_keeps_closing_at_end_with_trailing_empty(self):
        lines = [
            "こんにちは",
            "ご視聴ありがとうございました",
            "今日は天気がいいですね",
            "ご視聴ありがとうございました",
            "",
        ]
        result = remove_midstream_closing(lines)
        assert result == [
            "こんにちは",
            "{{ご視聴ありがとうございました->}}",
            "今日は天気がいいですね",
            "ご視聴ありがとうございました",
            "",
        ]

    def test_no_closing_lines(self):
        lines = ["こんにちは", "今日は天気がいいですね"]
        result = remove_midstream_closing(lines)
        assert result == lines

    def test_empty_input(self):
        assert remove_midstream_closing([]) == []

    def test_single_line_with_closing(self):
        lines = ["ご視聴ありがとうございました"]
        result = remove_midstream_closing(lines)
        assert result == lines

    def test_closing_embedded_in_longer_line_in_middle(self):
        lines = [
            "こんにちは",
            "それではご視聴ありがとうございました。また来週",
            "今日は天気がいいですね",
        ]
        result = remove_midstream_closing(lines)
        assert result == [
            "こんにちは",
            "それでは{{ご視聴ありがとうございました->}}。また来週",
            "今日は天気がいいですね",
        ]

    def test_multiple_closings_in_middle(self):
        lines = [
            "こんにちは",
            "ご視聴ありがとうございました",
            "今日は天気がいいですね",
            "ご視聴ありがとうございました",
            "さようなら",
        ]
        result = remove_midstream_closing(lines)
        assert result == [
            "こんにちは",
            "{{ご視聴ありがとうございました->}}",
            "今日は天気がいいですね",
            "{{ご視聴ありがとうございました->}}",
            "さようなら",
        ]
