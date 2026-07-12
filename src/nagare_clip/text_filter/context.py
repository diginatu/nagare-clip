"""Filter-LLM prompt context: inject summary-stage summaries/keywords into the prompt."""

from __future__ import annotations


def build_enhanced_prompt(base_prompt: str, summaries: list[str], keywords: list[str]) -> str:
    """Append this video's part summaries and keywords to the filter LLM's base prompt."""
    summaries = [s for s in summaries if s]
    keywords = [k for k in keywords if k]
    if not summaries and not keywords:
        return base_prompt
    parts = [base_prompt, "", "Context about this transcript:"]
    if len(summaries) == 1:
        parts.append(f"Summary: {summaries[0]}")
    elif summaries:
        parts.append("Summary:")
        parts.extend(f"- {s}" for s in summaries)
    if keywords:
        parts.append(f"Keywords (correct spellings): {', '.join(keywords)}")
        parts.append("When you see words that sound similar to these keywords, correct them.")
    return "\n".join(parts)
