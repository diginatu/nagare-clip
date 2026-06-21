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
