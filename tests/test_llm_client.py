"""Unit tests for the LiteLLM-backed unified LLM client."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nagare_clip import llm_client


def _fake_response(content: str):
    """Mimic the litellm.completion return shape we read from."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _call(cfg):
    with patch("nagare_clip.llm_client.litellm.completion") as m:
        m.return_value = _fake_response("RESULT")
        out = llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)
    return out, m


def test_returns_message_content():
    out, _ = _call({"provider": "openai", "model": "gpt-4o"})
    assert out == "RESULT"


def test_provider_model_composition():
    _, m = _call({"provider": "openai", "model": "gpt-4o"})
    assert m.call_args.kwargs["model"] == "openai/gpt-4o"


def test_default_provider_is_ollama_chat():
    _, m = _call({"model": "qwen3.5:4b"})
    assert m.call_args.kwargs["model"] == "ollama_chat/qwen3.5:4b"


def test_empty_ollama_api_base_falls_back_to_localhost():
    _, m = _call({"provider": "ollama_chat", "model": "x", "api_base": ""})
    assert m.call_args.kwargs["api_base"] == "http://localhost:11434"


def test_explicit_api_base_is_forwarded():
    _, m = _call({"provider": "ollama_chat", "model": "x", "api_base": "http://host:9999"})
    assert m.call_args.kwargs["api_base"] == "http://host:9999"


def test_cloud_provider_with_empty_api_base_passes_none():
    _, m = _call({"provider": "openai", "model": "gpt-4o", "api_base": ""})
    assert "api_base" not in m.call_args.kwargs


def test_temperature_forwarded_when_set():
    _, m = _call({"provider": "openai", "model": "x", "temperature": 0.2})
    assert m.call_args.kwargs["temperature"] == 0.2


def test_temperature_omitted_lets_provider_default_apply():
    _, m = _call({"provider": "openai", "model": "x"})
    assert "temperature" not in m.call_args.kwargs


def test_response_format_json_translated():
    _, m = _call({"provider": "openai", "model": "x", "response_format": "json"})
    assert m.call_args.kwargs["response_format"] == {"type": "json_object"}


def test_no_response_format_key_when_unset():
    _, m = _call({"provider": "openai", "model": "x"})
    assert "response_format" not in m.call_args.kwargs


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "ollama_chat"])
@pytest.mark.parametrize("effort", ["none", "low", "high", "xhigh"])
def test_reasoning_effort_is_passed_to_litellm_unchanged(provider, effort):
    """The config value IS LiteLLM's `reasoning_effort` -- no translation, no
    provider special case. What a value means for a model is LiteLLM's (and
    the provider's) business, which the user can look up there."""
    _, m = _call({"provider": provider, "model": "x", "reasoning_effort": effort})
    assert m.call_args.kwargs["reasoning_effort"] == effort
    assert "thinking" not in m.call_args.kwargs


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama_chat"])
@pytest.mark.parametrize("cfg_extra", [{}, {"reasoning_effort": None}])
def test_unset_reasoning_effort_sends_nothing(provider, cfg_extra):
    _, m = _call({"provider": provider, "model": "x", **cfg_extra})
    assert "reasoning_effort" not in m.call_args.kwargs
    assert "thinking" not in m.call_args.kwargs


def test_api_key_forwarded_when_set():
    _, m = _call({"provider": "openai", "model": "x", "api_key": "sk-123"})
    assert m.call_args.kwargs["api_key"] == "sk-123"


def test_trailing_slash_stripped_from_explicit_api_base():
    _, m = _call({"provider": "ollama_chat", "model": "x", "api_base": "http://localhost:11434/"})
    assert m.call_args.kwargs["api_base"] == "http://localhost:11434"


def test_litellm_error_wrapped_as_connection_error():
    with patch("nagare_clip.llm_client.litellm.completion", side_effect=ValueError("boom")):
        with pytest.raises(ConnectionError):
            llm_client.call_llm(
                [{"role": "user", "content": "hi"}], {"provider": "openai", "model": "x"}
            )


# --------------------------------------------------------------------------
# Usage recording (Task 1)
# --------------------------------------------------------------------------


def _usage_response(content: str, usage):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
    )


def _call_with(response, cfg):
    with patch("nagare_clip.llm_client.litellm.completion") as m:
        m.return_value = response
        out = llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)
    return out, m


class TestUsageCapture:
    def test_plain_token_counts_are_captured(self):
        usage = SimpleNamespace(prompt_tokens=120, completion_tokens=7, total_tokens=127)
        _call_with(_usage_response("R", usage), {"provider": "openai", "model": "x"})
        assert llm_client.take_last_usage() == {
            "prompt_tokens": 120,
            "completion_tokens": 7,
            "total_tokens": 127,
        }

    def test_anthropic_cache_counters_come_from_prompt_tokens_details(self):
        """LiteLLM normalises Anthropic's cache_read_input_tokens /
        cache_creation_input_tokens onto prompt_tokens_details as
        cached_tokens / cache_creation_tokens (litellm/types/utils.py Usage)."""
        usage = SimpleNamespace(
            prompt_tokens=4000,
            completion_tokens=50,
            total_tokens=4050,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3796, cache_creation_tokens=0),
        )
        _call_with(
            _usage_response("R", usage), {"provider": "anthropic", "model": "claude-sonnet-5"}
        )
        got = llm_client.take_last_usage()
        assert got["cache_read_input_tokens"] == 3796
        assert got["cache_creation_input_tokens"] == 0

    def test_raw_anthropic_field_names_are_accepted(self):
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=1,
            total_tokens=11,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=4,
        )
        _call_with(_usage_response("R", usage), {"provider": "anthropic", "model": "m"})
        got = llm_client.take_last_usage()
        assert got["cache_read_input_tokens"] == 5
        assert got["cache_creation_input_tokens"] == 4

    def test_real_litellm_usage_object_is_understood(self):
        """Pin the field names against litellm's own Usage class, not a fake.

        LiteLLM's Anthropic handler builds Usage(...) with the raw
        cache_read_input_tokens / cache_creation_input_tokens kwargs, and the
        constructor mirrors them onto prompt_tokens_details as cached_tokens /
        cache_creation_tokens; if that mapping ever moves, this fails.
        """
        from litellm.types.utils import Usage

        usage = Usage(
            prompt_tokens=4000,
            completion_tokens=50,
            total_tokens=4050,
            cache_read_input_tokens=3796,
            cache_creation_input_tokens=11,
        )
        _call_with(
            _usage_response("R", usage), {"provider": "anthropic", "model": "claude-sonnet-5"}
        )
        got = llm_client.take_last_usage()
        assert got["prompt_tokens"] == 4000
        assert got["completion_tokens"] == 50
        assert got["cache_read_input_tokens"] == 3796
        assert got["cache_creation_input_tokens"] == 11

    def test_reasoning_tokens_captured(self):
        usage = SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=200,
            total_tokens=201,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=180),
        )
        _call_with(_usage_response("R", usage), {"provider": "anthropic", "model": "m"})
        assert llm_client.take_last_usage()["reasoning_tokens"] == 180

    def test_missing_usage_is_not_an_error(self):
        """A provider (or a test fake) that omits usage must not break the call."""
        out, _ = _call_with(_fake_response("RESULT"), {"provider": "ollama_chat", "model": "x"})
        assert out == "RESULT"
        assert llm_client.take_last_usage() is None

    def test_usage_is_consumed_once(self):
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        _call_with(_usage_response("R", usage), {"provider": "openai", "model": "x"})
        assert llm_client.take_last_usage() is not None
        assert llm_client.take_last_usage() is None

    def test_failed_call_clears_stale_usage(self):
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        _call_with(_usage_response("R", usage), {"provider": "openai", "model": "x"})
        with patch("nagare_clip.llm_client.litellm.completion", side_effect=ValueError("boom")):
            with pytest.raises(ConnectionError):
                llm_client.call_llm([{"role": "user", "content": "hi"}], {"model": "x"})
        assert llm_client.take_last_usage() is None


# --------------------------------------------------------------------------
# Anthropic prompt caching (Task 2)
# --------------------------------------------------------------------------

# ~2.8k tokens: above Sonnet 5's 1024 minimum, below Opus 4.6's / an unknown
# model's 4096, so it exercises the per-model minimum table.
LONG_SYSTEM = "cacheable prefix sentence number one. " * 400
# ~5.6k tokens: above EVERY documented minimum, so only the provider gate can
# keep it unmarked.
HUGE_SYSTEM = "cacheable prefix sentence number one. " * 800
# The user turn is long too, so only the role check - never the length gate -
# can be what keeps the varying suffix out of the cached prefix.
LONG_USER = "the varying tail sentence number two. " * 400
SHORT_SYSTEM = "too short to cache"


def _msgs(system: str, user: str = LONG_USER):
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _sent_messages(messages, cfg):
    with patch("nagare_clip.llm_client.litellm.completion") as m:
        m.return_value = _fake_response("R")
        llm_client.call_llm(messages, cfg)
    return m.call_args.kwargs["messages"]


class TestAnthropicPromptCaching:
    def test_system_message_gets_a_cache_control_breakpoint(self):
        sent = _sent_messages(
            _msgs(LONG_SYSTEM), {"provider": "anthropic", "model": "claude-sonnet-5"}
        )
        assert sent[0]["content"] == [
            {
                "type": "text",
                "text": LONG_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_user_message_is_never_marked(self):
        sent = _sent_messages(
            _msgs(LONG_SYSTEM), {"provider": "anthropic", "model": "claude-sonnet-5"}
        )
        assert sent[1] == {"role": "user", "content": LONG_USER}

    def test_short_prefix_is_left_alone(self):
        sent = _sent_messages(
            _msgs(SHORT_SYSTEM), {"provider": "anthropic", "model": "claude-sonnet-5"}
        )
        assert sent == _msgs(SHORT_SYSTEM)

    def test_model_minimum_is_honoured(self):
        """Opus 4.6 needs a 4096-token prefix; the same prompt that caches on
        Sonnet 5 (1024 minimum) must not be marked there."""
        assert llm_client.min_cacheable_tokens("claude-sonnet-5") == 1024
        assert llm_client.min_cacheable_tokens("claude-opus-5") == 512
        assert llm_client.min_cacheable_tokens("claude-opus-4-6") == 4096
        assert llm_client.min_cacheable_tokens("claude-opus-4-7") == 2048
        assert llm_client.min_cacheable_tokens("some-unknown-model") == 4096
        sent = _sent_messages(
            _msgs(LONG_SYSTEM), {"provider": "anthropic", "model": "claude-opus-4-6"}
        )
        assert sent == _msgs(LONG_SYSTEM)

    def test_only_one_breakpoint_for_several_system_messages(self):
        messages = [
            {"role": "system", "content": LONG_SYSTEM},
            {"role": "system", "content": LONG_SYSTEM},
            {"role": "user", "content": "u"},
        ]
        sent = _sent_messages(messages, {"provider": "anthropic", "model": "claude-sonnet-5"})
        assert sent[0] == {"role": "system", "content": LONG_SYSTEM}
        assert isinstance(sent[1]["content"], list)
        marked = sum(
            1
            for m in sent
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if "cache_control" in b
        )
        assert marked == 1

    def test_no_system_message_is_a_noop(self):
        messages = [{"role": "user", "content": LONG_SYSTEM}]
        sent = _sent_messages(messages, {"provider": "anthropic", "model": "claude-sonnet-5"})
        assert sent == messages

    def test_callers_messages_are_not_mutated(self):
        messages = _msgs(LONG_SYSTEM)
        before = [dict(m) for m in messages]
        _sent_messages(messages, {"provider": "anthropic", "model": "claude-sonnet-5"})
        assert messages == before

    @pytest.mark.parametrize("provider", ["ollama_chat", "openai", "gemini"])
    def test_other_providers_send_byte_identical_messages(self, provider):
        # HUGE_SYSTEM clears every model minimum, so a mark here could only be
        # prevented by the provider gate.
        messages = _msgs(HUGE_SYSTEM)
        sent = _sent_messages(messages, {"provider": provider, "model": "whatever"})
        assert sent == messages
        assert "cache_control" not in repr(sent)

    def test_default_provider_is_untouched(self):
        messages = _msgs(HUGE_SYSTEM)
        sent = _sent_messages(messages, {"model": "qwen3.5:4b"})
        assert sent == messages

    def test_system_content_already_in_block_form_is_left_alone(self):
        """A caller that already built content blocks owns its own breakpoints;
        overwriting the list would drop whatever it put there."""
        blocks = [{"type": "text", "text": HUGE_SYSTEM}]
        messages = [
            {"role": "system", "content": blocks},
            {"role": "user", "content": LONG_USER},
        ]
        sent = _sent_messages(messages, {"provider": "anthropic", "model": "claude-sonnet-5"})
        assert sent == messages


# Per-call context appended after a stable prefix -- the director's shape: the
# same prompt + brief + keep note on every call, then a segment-specific block.
VARYING_TAIL = "\n\nThis segment is number seven of nine. " * 50


class TestCacheablePrefix:
    """A system message whose tail differs per call must be cached only up to
    where it stops being identical. Marking the whole message puts the
    breakpoint after the varying tail, so no two calls share a prefix: every
    call pays the 1.25x cache-WRITE premium and none ever reads. That was the
    first cut of this feature against the director, whose 9 system messages are
    all distinct beyond a shared 8.7k-character head."""

    CFG = {"provider": "anthropic", "model": "claude-sonnet-5"}

    def _system(self, prefix=LONG_SYSTEM, tail=VARYING_TAIL):
        return {"role": "system", "content": prefix + tail, "cacheable_prefix": prefix}

    def test_breakpoint_lands_at_the_end_of_the_declared_prefix(self):
        sent = _sent_messages([self._system(), {"role": "user", "content": "u"}], self.CFG)
        assert sent[0]["content"] == [
            {"type": "text", "text": LONG_SYSTEM, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": VARYING_TAIL},
        ]

    def test_the_sent_text_is_exactly_the_original_content(self):
        # Splitting into blocks must not add, drop or reorder a single byte.
        sent = _sent_messages([self._system(), {"role": "user", "content": "u"}], self.CFG)
        assert "".join(b["text"] for b in sent[0]["content"]) == LONG_SYSTEM + VARYING_TAIL

    def test_two_calls_with_different_tails_share_a_byte_identical_cached_block(self):
        a = _sent_messages(
            [self._system(tail="\n\nsegment A"), {"role": "user", "content": "u"}], self.CFG
        )
        b = _sent_messages(
            [self._system(tail="\n\nsegment B"), {"role": "user", "content": "u"}], self.CFG
        )
        assert a[0]["content"][0] == b[0]["content"][0]
        assert a[0]["content"][1] != b[0]["content"][1]

    def test_a_prefix_that_does_not_match_places_no_breakpoint(self):
        # A wrong breakpoint costs the write premium for nothing; none is safer.
        # The declared prefix is long enough to clear every minimum, so only
        # the startswith check -- not the length gate -- can be what refuses it.
        msg = {
            "role": "system",
            "content": LONG_SYSTEM + VARYING_TAIL,
            "cacheable_prefix": HUGE_SYSTEM,
        }
        sent = _sent_messages([msg, {"role": "user", "content": "u"}], self.CFG)
        assert sent[0] == {"role": "system", "content": LONG_SYSTEM + VARYING_TAIL}

    def test_the_minimum_is_measured_on_the_prefix_not_the_whole_message(self):
        # A short prefix does not become cacheable because its tail is long.
        msg = self._system(prefix=SHORT_SYSTEM, tail=HUGE_SYSTEM)
        sent = _sent_messages([msg, {"role": "user", "content": "u"}], self.CFG)
        assert sent[0] == {"role": "system", "content": SHORT_SYSTEM + HUGE_SYSTEM}

    @pytest.mark.parametrize("provider", ["anthropic", "ollama_chat", "openai", "gemini"])
    def test_the_hint_never_reaches_the_provider(self, provider):
        # An unknown message key is at best ignored and at worst a 400.
        sent = _sent_messages(
            [self._system(), {"role": "user", "content": "u"}],
            {"provider": provider, "model": "claude-sonnet-5"},
        )
        assert all("cacheable_prefix" not in m for m in sent)

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_callers_messages_keep_the_hint(self, provider):
        # The stages reuse the same list across retries; stripping it in place
        # would silently disable caching from attempt 2 onward.  openai matters
        # most here: nothing upstream copies the dicts before the strip.
        messages = [self._system(), {"role": "user", "content": "u"}]
        _sent_messages(messages, {"provider": provider, "model": "claude-sonnet-5"})
        assert messages[0]["cacheable_prefix"] == LONG_SYSTEM
