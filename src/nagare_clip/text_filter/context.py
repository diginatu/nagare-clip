"""Filter-LLM prompt context: inject summary-stage summaries/keywords into the prompt."""

from __future__ import annotations


def build_enhanced_prompt(
    base_prompt: str,
    summaries: list[str],
    keywords: list[str],
    video_summary: str = "",
) -> str:
    """Append this video's whole-video summary, part summaries and keywords to the base prompt."""
    summaries = [s for s in summaries if s]
    keywords = [k for k in keywords if k]
    video_summary = video_summary.strip()
    if not summaries and not keywords and not video_summary:
        return base_prompt
    parts = [base_prompt, "", "Context about this transcript:"]
    if video_summary:
        parts.append(f"Video summary: {video_summary}")
    if len(summaries) == 1:
        parts.append(f"Summary: {summaries[0]}")
    elif summaries:
        parts.append("Summary:")
        parts.extend(f"- {s}" for s in summaries)
    if keywords:
        parts.append(f"Keywords (correct spellings): {', '.join(keywords)}")
        parts.append("When you see words that sound similar to these keywords, correct them.")
    return "\n".join(parts)
