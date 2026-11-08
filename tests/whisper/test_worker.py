"""The faster-whisper worker: pure helpers and the no-GPU paths."""

import ast
import json
from pathlib import Path

import pytest

from nagare_clip.whisper import worker


class FakeSeg:
    def __init__(self, text):
        self.text = text


class FakeModel:
    """Returns text keyed by the slice length, so attribution is checkable."""

    def __init__(self, by_len):
        self.by_len = by_len
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append((len(audio), kw))
        return iter([FakeSeg(t) for t in self.by_len[len(audio)]]), None


def test_decode_chunks_one_call_per_chunk_attributed_by_construction():
    # two short chunks close together: a batched pipeline would merge them
    chunks = [(0.0, 2.0), (2.5, 4.0), (10.0, 10.5)]
    model = FakeModel({32000: ["はい いつも", "\n"], 24000: ["水位 が"], 8000: []})
    audio = [0.0] * (16000 * 11)
    texts = worker.decode_chunks(model, audio, chunks, beam_size=5)
    assert texts == ["はいいつも", "水位が", ""]
    assert [n for n, _ in model.calls] == [32000, 24000, 8000]
    kw = model.calls[0][1]
    assert kw == {
        "vad_filter": False,
        "language": "ja",
        "beam_size": 5,
        "condition_on_previous_text": False,
        "without_timestamps": True,
    }


def test_decode_chunks_warns_over_30s(capsys):
    model = FakeModel({16000 * 31: ["あ"]})
    worker.decode_chunks(model, [0.0] * (16000 * 40), [(1.0, 32.0)], beam_size=5)
    err = capsys.readouterr().err
    assert "31" in err and "30" in err


def test_worker_has_no_batched_pipeline():
    src = Path(worker.__file__).read_text(encoding="utf-8")
    assert "BatchedInferencePipeline(" not in src
    assert "import BatchedInferencePipeline" not in src


def test_find_model_globs(tmp_path):
    for rev in ("zzz", "aaa"):
        snap = tmp_path / "models--Systran--faster-whisper-large-v3" / "snapshots" / rev
        snap.mkdir(parents=True)
    got = worker.find_model("large-v3", root=str(tmp_path))
    assert got.endswith("snapshots/aaa")


def test_find_model_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="faster-whisper-large-v3"):
        worker.find_model("large-v3", root=str(tmp_path))


def test_main_no_chunks_skips_model(tmp_path, monkeypatch):
    def boom(model):
        raise AssertionError("model must not load")

    monkeypatch.setattr(worker, "load_model", boom)
    out = tmp_path / "out.json"
    req = tmp_path / "req.json"
    req.write_text(
        json.dumps(
            {
                "model": "large-v3",
                "beam_size": 5,
                "sources": [{"path": "/x.mp4", "out": str(out), "chunks": []}],
            }
        )
    )
    worker.main([str(req)])
    assert json.loads(out.read_text()) == {"chunks": [], "texts": []}


def test_worker_never_imports_nagare_clip():
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(n.split(".")[0] == "nagare_clip" for n in names)


def test_main_chowns_output_to_nagare_owner(tmp_path, monkeypatch):
    """The container is root: the output goes back to $NAGARE_OWNER, the
    same variable `pipeline.external` sets (the worker cannot import it)."""
    import os

    from nagare_clip.pipeline.external import OWNER_ENV

    assert OWNER_ENV == "NAGARE_OWNER"
    calls = []
    monkeypatch.setattr(worker.os, "chown", lambda p, u, g: calls.append((p, u, g)))
    monkeypatch.setenv(OWNER_ENV, "1234:5678")
    out = tmp_path / "out.json"
    req = tmp_path / "req.json"
    req.write_text(
        json.dumps(
            {
                "model": "large-v3",
                "beam_size": 5,
                "sources": [{"path": "/x.mp4", "out": str(out), "chunks": []}],
            }
        )
    )
    worker.main([str(req)])
    assert calls == [(str(out), 1234, 5678)]
    calls.clear()
    monkeypatch.delenv(OWNER_ENV)
    worker.main([str(req)])
    assert calls == []
    assert os.path.exists(out)
