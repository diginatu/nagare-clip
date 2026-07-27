import json

from nagare_clip.gap_context.describe import GapFrames
from nagare_clip.gap_context.run import run_gap_context

BASE_CFG = {
    "gap_context": {
        "enabled": True,
        "prompt": "P",
        "max_retries": 0,
        "temperature": 0.2,
        "model": "m",
    }
}


def _frames(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"jpeg")
        out.append(p)
    return out


def test_disabled_writes_an_empty_gap_list(tmp_path):
    out = tmp_path / "a_gaps.json"
    run_gap_context([], out, {"gap_context": {"enabled": False}}, stem="a")
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}


def test_disabled_ignores_a_nonempty_gap_frames_list_and_makes_zero_llm_calls(
    tmp_path, monkeypatch
):
    """Regression: test_disabled_writes_an_empty_gap_list above only ever
    passes gap_frames=[], so it never actually exercises the enabled==False
    branch in run_gap_context -- a caller handed a non-empty GapFrames list
    with enabled: false must still get {"gaps": []} and make ZERO LLM calls.

    Uses a call COUNTER, not a fake that signals via raising: describe_gap's
    broad ``except Exception`` would swallow a raise and this test would be
    unable to fail no matter what the gate does.
    """
    import nagare_clip.gap_context.run as run_mod

    calls: list[None] = []

    def counting_llm(messages, cfg):
        calls.append(None)
        return "should never be called"

    monkeypatch.setattr(run_mod, "_call_llm", counting_llm)
    gf = GapFrames(
        start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["frames/a/10.200.jpg"]
    )
    out = tmp_path / "a_gaps.json"
    run_gap_context([gf], out, {"gap_context": {"enabled": False}}, stem="a")
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}
    assert calls == [], f"expected zero LLM calls, got {len(calls)}"


def test_writes_a_described_gap(tmp_path, monkeypatch):
    import nagare_clip.gap_context.run as run_mod

    monkeypatch.setattr(run_mod, "_call_llm", lambda messages, cfg: "画面でビルドが走っている")
    gf = GapFrames(
        start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["frames/a/10.200.jpg"]
    )
    out = tmp_path / "a_gaps.json"
    run_gap_context([gf], out, BASE_CFG, stem="a")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {
        "gaps": [
            {
                "start": 10.0,
                "end": 20.0,
                "frames": ["frames/a/10.200.jpg"],
                "description": "画面でビルドが走っている",
                "static": False,
            }
        ]
    }


def test_a_failing_gap_is_dropped_not_fatal(tmp_path, monkeypatch):
    import nagare_clip.gap_context.run as run_mod

    def boom(messages, cfg):
        raise ConnectionError("down")

    monkeypatch.setattr(run_mod, "_call_llm", boom)
    gf = GapFrames(start=1.0, end=9.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    out = tmp_path / "a_gaps.json"
    run_gap_context([gf], out, BASE_CFG, stem="a")
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}


def test_neighbour_lines_are_passed_to_the_llm(tmp_path, monkeypatch):
    import nagare_clip.gap_context.run as run_mod

    seen = {}

    def fake(messages, cfg):
        seen["text"] = messages[1]["content"][0]["text"]
        return "d"

    monkeypatch.setattr(run_mod, "_call_llm", fake)
    jp = tmp_path / "a.json"
    jp.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 10.0, "text": "ここでビルドします"},
                    {"start": 20.0, "end": 25.0, "text": "できました"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gf = GapFrames(start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    run_gap_context([gf], tmp_path / "a_gaps.json", BASE_CFG, stem="a", json_path=jp)
    assert "ここでビルドします" in seen["text"]
    assert "できました" in seen["text"]


def test_neighbour_lines_picks_last_before_and_first_after(tmp_path, monkeypatch):
    """With multiple segments on each side of the gap, _neighbour_lines picks
    the immediately-preceding and immediately-following ones, not distant ones."""
    import nagare_clip.gap_context.run as run_mod

    seen = {}

    def fake(messages, cfg):
        seen["text"] = messages[1]["content"][0]["text"]
        return "d"

    monkeypatch.setattr(run_mod, "_call_llm", fake)
    jp = tmp_path / "b.json"
    jp.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "text": "古い文1"},
                    {"start": 5.0, "end": 8.0, "text": "古い文2"},
                    {"start": 8.0, "end": 10.0, "text": "直前の文"},  # LAST before gap
                    {"start": 20.0, "end": 22.0, "text": "直後の文"},  # FIRST after gap
                    {"start": 22.0, "end": 25.0, "text": "新しい文"},
                    {"start": 25.0, "end": 30.0, "text": "もっと新しい文"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gf = GapFrames(start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    run_gap_context([gf], tmp_path / "b_gaps.json", BASE_CFG, stem="b", json_path=jp)
    # Ensure exactly the immediate neighbors are in the prompt
    assert "直前の文" in seen["text"]
    assert "直後の文" in seen["text"]
    # Ensure the distant ones are NOT in the prompt
    assert "古い文1" not in seen["text"]
    assert "古い文2" not in seen["text"]
    assert "新しい文" not in seen["text"]
    assert "もっと新しい文" not in seen["text"]


def test_neighbour_lines_degrades_on_malformed_segments(tmp_path, monkeypatch):
    """_neighbour_lines handles None start/end, non-dict entries, and non-string
    text gracefully, still picking valid neighbours and not raising."""
    import nagare_clip.gap_context.run as run_mod

    seen = {}

    def fake(messages, cfg):
        seen["text"] = messages[1]["content"][0]["text"]
        return "d"

    monkeypatch.setattr(run_mod, "_call_llm", fake)
    jp = tmp_path / "c.json"
    jp.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "text": "有効1"},
                    {"start": 5.0, "end": None, "text": "Noneを持つ"},  # None end
                    "これは辞書ではない",  # Non-dict
                    {"start": 8.0, "end": 10.0, "text": None},  # None text
                    {"start": 8.5, "end": 9.9, "text": "直前の文"},  # Valid neighbor before
                    {"start": 20.0, "end": 22.0, "text": 123},  # Non-string text
                    {"start": 20.1, "end": 21.0, "text": "直後の文"},  # Valid neighbor after
                    None,  # None segment
                    {"start": 25.0, "end": 30.0, "text": "有効2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gf = GapFrames(start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    # Should not raise despite malformed segments
    run_gap_context([gf], tmp_path / "c_gaps.json", BASE_CFG, stem="c", json_path=jp)
    # Should still extract the valid neighbors
    assert "直前の文" in seen["text"]
    assert "直後の文" in seen["text"]


def _run_with_context_lines(tmp_path, monkeypatch, stem, context_lines):
    """Run one gap over a 6-segment fixture and return the LLM's header text."""
    import nagare_clip.gap_context.run as run_mod

    seen = {}

    def fake(messages, cfg):
        seen["text"] = messages[1]["content"][0]["text"]
        return "d"

    monkeypatch.setattr(run_mod, "_call_llm", fake)
    jp = tmp_path / f"{stem}.json"
    jp.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "text": "前3"},
                    {"start": 5.0, "end": 8.0, "text": "前2"},
                    {"start": 8.0, "end": 10.0, "text": "前1"},
                    {"start": 20.0, "end": 22.0, "text": "後1"},
                    {"start": 22.0, "end": 25.0, "text": "後2"},
                    {"start": 25.0, "end": 30.0, "text": "後3"},
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = {"gap_context": {**BASE_CFG["gap_context"], "context_lines": context_lines}}
    gf = GapFrames(start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    run_gap_context([gf], tmp_path / f"{stem}_gaps.json", cfg, stem=stem, json_path=jp)
    return seen["text"]


def test_context_lines_collects_that_many_neighbours_on_each_side(tmp_path, monkeypatch):
    text = _run_with_context_lines(tmp_path, monkeypatch, "n2", 2)
    assert "前2" in text and "前1" in text
    assert "後1" in text and "後2" in text
    # The third line out on each side is beyond context_lines: 2
    assert "前3" not in text
    assert "後3" not in text


def test_context_lines_keeps_the_neighbours_in_chronological_order(tmp_path, monkeypatch):
    text = _run_with_context_lines(tmp_path, monkeypatch, "n3", 3)
    assert text.index("前3") < text.index("前2") < text.index("前1")
    assert text.index("後1") < text.index("後2") < text.index("後3")


def test_context_lines_beyond_the_available_segments_is_not_an_error(tmp_path, monkeypatch):
    text = _run_with_context_lines(tmp_path, monkeypatch, "n9", 9)
    assert "前3" in text and "後3" in text


def test_context_lines_zero_omits_the_neighbour_lines_entirely(tmp_path, monkeypatch):
    text = _run_with_context_lines(tmp_path, monkeypatch, "n0", 0)
    for t in ("前1", "前2", "前3", "後1", "後2", "後3"):
        assert t not in text


def test_context_lines_defaults_to_one_when_absent_from_cfg(tmp_path, monkeypatch):
    """An older/hand-written cfg without the key keeps the previous behaviour."""
    import nagare_clip.gap_context.run as run_mod

    seen = {}

    def fake(messages, cfg):
        seen["text"] = messages[1]["content"][0]["text"]
        return "d"

    monkeypatch.setattr(run_mod, "_call_llm", fake)
    jp = tmp_path / "d.json"
    jp.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 5.0, "end": 8.0, "text": "前2"},
                    {"start": 8.0, "end": 10.0, "text": "前1"},
                    {"start": 20.0, "end": 22.0, "text": "後1"},
                    {"start": 22.0, "end": 25.0, "text": "後2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gf = GapFrames(start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    assert "context_lines" not in BASE_CFG["gap_context"]
    run_gap_context([gf], tmp_path / "d_gaps.json", BASE_CFG, stem="d", json_path=jp)
    assert "前1" in seen["text"] and "後1" in seen["text"]
    assert "前2" not in seen["text"] and "後2" not in seen["text"]


def test_static_ssim_prefilter_skips_vision_call(tmp_path, monkeypatch):
    """A gap whose frames scored >= static_ssim is written as static:true
    without any LLM call; a below-threshold gap still calls the LLM."""
    import nagare_clip.gap_context.run as run_mod

    calls: list[None] = []

    def counting_llm(messages, cfg):
        calls.append(None)
        return "ACTION: something is happening on screen"

    monkeypatch.setattr(run_mod, "_call_llm", counting_llm)

    static_gf = GapFrames(
        start=10.0,
        end=20.0,
        frames=_frames(tmp_path, "static_first.jpg", "static_last.jpg"),
        relpaths=["frames/a/10.200.jpg", "frames/a/19.800.jpg"],
        ssim=0.999,
    )
    action_gf = GapFrames(
        start=30.0,
        end=40.0,
        frames=_frames(tmp_path, "action_first.jpg", "action_last.jpg"),
        relpaths=["frames/a/30.200.jpg", "frames/a/39.800.jpg"],
        ssim=0.5,
    )
    cfg = {"gap_context": {**BASE_CFG["gap_context"], "static_ssim": 0.99}}
    out = tmp_path / "a_gaps.json"
    run_gap_context([static_gf, action_gf], out, cfg, stem="a")

    assert len(calls) == 1, f"expected exactly one LLM call, got {len(calls)}"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["gaps"]) == 2
    prefiltered = next(g for g in data["gaps"] if g["start"] == 10.0)
    described = next(g for g in data["gaps"] if g["start"] == 30.0)
    assert prefiltered["static"] is True
    assert "prefilter" in prefiltered["description"]
    assert described["static"] is False
