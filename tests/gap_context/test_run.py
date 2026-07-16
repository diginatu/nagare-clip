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
