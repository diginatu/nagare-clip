"""Tests for the filter-LLM prompt-context builder."""

from __future__ import annotations

from nagare_clip.text_filter.context import build_enhanced_prompt


class TestBuildEnhancedPrompt:
    def test_appends_summary_and_keywords(self):
        result = build_enhanced_prompt(
            "Fix errors.", ["プログラミング解説"], ["Kubernetes", "PostgreSQL"]
        )
        assert result.startswith("Fix errors.")
        assert "プログラミング解説" in result
        assert "Kubernetes" in result
        assert "PostgreSQL" in result

    def test_single_summary_on_one_line(self):
        result = build_enhanced_prompt("Base.", ["概要のみ"], [])
        assert "Summary: 概要のみ" in result

    def test_multiple_summaries_bulleted(self):
        result = build_enhanced_prompt("Base.", ["part one", "part two"], [])
        assert "Summary:" in result
        assert "- part one" in result
        assert "- part two" in result

    def test_preserves_base_prompt(self):
        base = "Line 1\nLine 2\nLine 3"
        assert build_enhanced_prompt(base, ["概要"], ["w1"]).startswith(base)

    def test_keywords_only_omits_summary_line(self):
        result = build_enhanced_prompt("Fix errors.", [], ["Foo", "Bar"])
        assert "Foo" in result
        assert "Bar" in result
        assert "Summary:" not in result

    def test_no_context_returns_base_unchanged(self):
        assert build_enhanced_prompt("Fix errors.", [], []) == "Fix errors."

    def test_blank_entries_ignored(self):
        assert build_enhanced_prompt("Base.", [""], [""]) == "Base."
