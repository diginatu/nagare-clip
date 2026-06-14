"""Unit tests for the LiteLLM-backed unified LLM client."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nagare_clip import llm_client


def _fake_response(content: str):
    """Mimic the litellm.completion return shape we read from."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


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
    _, m = _call({"provider": "ollama_chat", "model": "x",
                  "api_base": "http://host:9999"})
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


def test_thinking_level_maps_to_reasoning_effort():
    _, m = _call({"provider": "openai", "model": "x", "thinking": "high"})
    assert m.call_args.kwargs["reasoning_effort"] == "high"


def test_thinking_true_maps_to_low():
    _, m = _call({"provider": "openai", "model": "x", "thinking": True})
    assert m.call_args.kwargs["reasoning_effort"] == "low"


def test_thinking_false_omits_reasoning_effort():
    _, m = _call({"provider": "openai", "model": "x", "thinking": False})
    assert "reasoning_effort" not in m.call_args.kwargs


def test_api_key_forwarded_when_set():
    _, m = _call({"provider": "openai", "model": "x", "api_key": "sk-123"})
    assert m.call_args.kwargs["api_key"] == "sk-123"


def test_litellm_error_wrapped_as_connection_error():
    with patch("nagare_clip.llm_client.litellm.completion", side_effect=ValueError("boom")):
        with pytest.raises(ConnectionError):
            llm_client.call_llm([{"role": "user", "content": "hi"}],
                                {"provider": "openai", "model": "x"})
