"""Whisper hints shown to the text_filter LLM."""

from __future__ import annotations

from nagare_clip.text_filter.llm_filter import HINT_INSTRUCTIONS, filter_transcript

LINES = ["one", "two", "three", "four"]
HEADER = "Reference (second recognizer, may be wrong):"


def _run(hints=..., cfg_extra=None, respond=None):
    calls: list[list[dict]] = []

    def fake(messages, cfg):
        calls.append(messages)
        if respond:
            return respond(len(calls), messages)
        return "\n".join(f"{i}: {t}" for i, t in enumerate(LINES, 1))

    cfg = {"prompt": "BASE", "batch_size": 10, **(cfg_extra or {})}
    kw = {} if hints is ... else {"hints": hints}
    filter_transcript(LINES, cfg, call_llm=fake, **kw)
    return calls


def test_no_hints_messages_unchanged():
    base = _run()
    assert _run(hints=None) == base
    assert _run(hints=[None] * 4) == base
    assert HEADER not in base[0][1]["content"]
    assert base[0][0]["content"] == "BASE"


def test_hint_block_lists_only_differing_lines():
    calls = _run(hints=[None, "TWO", None, "FOUR"])
    user = calls[0][1]["content"]
    assert user.endswith("\n\n" + HEADER + "\n2: TWO\n4: FOUR")
    assert user.startswith("1: one\n2: two")


def test_empty_hint_renders_none():
    user = _run(hints=[None, "", None, None])[0][1]["content"]
    assert user.endswith(HEADER + "\n2: (none)")


def test_retry_subbatch_carries_own_hints():
    def respond(n, messages):
        if n == 1:  # omit lines 3-4 -> halving retry of those lines
            return "1: one\n2: two"
        return "3: three\n4: four"

    calls = _run(
        hints=["A", None, None, "D"],
        cfg_extra={"batch_size": 4, "retry_min_batch_size": 1},
        respond=respond,
    )
    assert len(calls) == 2  # first call, then lines 3-4 as one halved batch
    retry = calls[1][1]["content"]
    assert retry.startswith("3: three")
    assert "1: A" not in retry
    assert retry.endswith(HEADER + "\n4: D")


def test_system_prompt_gets_instructions_once():
    calls = _run(
        hints=[None, "TWO", None, None],
        cfg_extra={"batch_size": 2},
    )
    assert len(calls) == 2
    for c in calls:
        assert c[0]["content"] == "BASE" + "\n\n" + HINT_INSTRUCTIONS
        assert c[0]["content"].count(HINT_INSTRUCTIONS) == 1


def test_echoed_reference_block_is_ignored():
    def respond(n, messages):
        return "1: one\n2: {{two->TWO}}\n3: three\n4: four\n\n" + HEADER + "\n2: TWO\n4: FOUR"

    out = filter_transcript(
        LINES,
        {"prompt": "BASE", "batch_size": 10},
        call_llm=lambda m, c: respond(1, m),
        hints=[None, "TWO", None, "FOUR"],
    )
    assert out == ["one", "{{two->TWO}}", "three", "four"]


def test_instructions_forbid_repeating_reference():
    assert "never repeat the reference block" in HINT_INSTRUCTIONS
