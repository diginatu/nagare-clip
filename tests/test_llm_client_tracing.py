import pytest

from nagare_clip import llm_client


def test_with_trace_meta_builds_reserved_entry():
    cfg = {"model": "m", "temperature": 0.1}
    out = llm_client.with_trace_meta(cfg, stage="director", unit="myvideo")
    assert out["_trace"] == {
        "generation_name": "director/myvideo",
        "tags": ["stage:director", "stem:myvideo"],
    }
    # original cfg is untouched and other keys preserved
    assert "_trace" not in cfg
    assert out["model"] == "m" and out["temperature"] == 0.1


def test_with_trace_meta_extra_tags_and_empty_stage():
    out = llm_client.with_trace_meta({}, stage="", unit="overall", extra_tags=["x"])
    assert out["_trace"]["generation_name"] == "overall"
    assert out["_trace"]["tags"] == ["stage:", "stem:overall", "x"]


@pytest.fixture
def lf_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.delenv("NAGARE_LANGFUSE", raising=False)


def test_tracing_enabled_when_keys_present(lf_keys):
    assert llm_client._tracing_enabled() is True


def test_tracing_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert llm_client._tracing_enabled() is False


def test_tracing_disabled_by_flag(lf_keys, monkeypatch):
    monkeypatch.setenv("NAGARE_LANGFUSE", "0")
    assert llm_client._tracing_enabled() is False


import litellm


@pytest.fixture
def reset_tracing(monkeypatch):
    monkeypatch.setattr(llm_client, "_TRACING_INITIALIZED", False)
    monkeypatch.setattr(litellm, "callbacks", [])
    yield


def test_ensure_tracing_registers_callback_once(lf_keys, reset_tracing, monkeypatch):
    registered = []
    monkeypatch.setattr(llm_client.atexit, "register", lambda fn: registered.append(fn))
    assert llm_client._ensure_tracing() is True
    assert llm_client._ensure_tracing() is True  # second call is a no-op
    assert litellm.callbacks.count("langfuse_otel") == 1
    assert llm_client.flush_traces in registered  # exactly one atexit hook
    assert len(registered) == 1


def test_ensure_tracing_noop_when_disabled(reset_tracing, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert llm_client._ensure_tracing() is False
    assert litellm.callbacks == []


def test_flush_traces_calls_force_flush(monkeypatch):
    calls = []

    class FakeProvider:
        def force_flush(self):
            calls.append(True)

    import opentelemetry.trace as ot
    monkeypatch.setattr(ot, "get_tracer_provider", lambda: FakeProvider())
    llm_client.flush_traces()
    assert calls == [True]


def test_flush_traces_swallows_missing_force_flush(monkeypatch):
    import opentelemetry.trace as ot
    monkeypatch.setattr(ot, "get_tracer_provider", lambda: object())
    llm_client.flush_traces()  # must not raise


class _FakeMsg:
    content = "ok"


class _FakeChoice:
    message = _FakeMsg()


class _FakeResp:
    choices = [_FakeChoice()]


def _capture_completion(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResp()

    monkeypatch.setattr(litellm, "completion", fake_completion)
    return captured


def test_call_llm_passes_metadata_when_enabled(lf_keys, reset_tracing, monkeypatch):
    monkeypatch.setattr(llm_client.atexit, "register", lambda fn: None)
    monkeypatch.setenv("NAGARE_RUN_ID", "run-123")
    captured = _capture_completion(monkeypatch)

    cfg = llm_client.with_trace_meta(
        {"provider": "openai", "model": "gpt-4o-mini"}, stage="director", unit="vid"
    )
    out = llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)

    assert out == "ok"
    assert captured["metadata"] == {
        "generation_name": "director/vid",
        "tags": ["stage:director", "stem:vid"],
        "session_id": "run-123",
    }
    assert "_trace" not in captured  # never leaks to the provider call


def test_call_llm_omits_session_when_no_run_id(lf_keys, reset_tracing, monkeypatch):
    monkeypatch.setattr(llm_client.atexit, "register", lambda fn: None)
    monkeypatch.delenv("NAGARE_RUN_ID", raising=False)
    captured = _capture_completion(monkeypatch)

    cfg = llm_client.with_trace_meta({"provider": "openai", "model": "m"}, stage="plan", unit="plan")
    llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)

    assert "session_id" not in captured["metadata"]
    assert captured["metadata"]["generation_name"] == "plan/plan"


def test_call_llm_no_metadata_when_disabled(reset_tracing, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    captured = _capture_completion(monkeypatch)

    cfg = llm_client.with_trace_meta({"provider": "openai", "model": "m"}, stage="plan", unit="p")
    llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)

    assert "metadata" not in captured       # regression: identical to pre-tracing
    assert "_trace" not in captured
    assert litellm.callbacks == []          # callbacks untouched when disabled
