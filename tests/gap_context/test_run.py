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
