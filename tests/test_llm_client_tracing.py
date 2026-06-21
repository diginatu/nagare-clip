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
