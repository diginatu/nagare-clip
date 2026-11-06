"""The faster-whisper worker: pure helpers and the no-GPU paths."""

import ast
import json
from pathlib import Path

import pytest

from nagare_clip.whisper import worker


def test_group_by_chunk_strips_spaces():
    out = worker.group_by_chunk([(0, 30), (30, 60)], [(0.5, "はい いつも"), (31.0, "水位 が")])
    assert out == ["はいいつも", "水位が"]


def test_group_by_chunk_split_long_chunk():
    out = worker.group_by_chunk([(0, 60)], [(1.0, "あ い"), (31.0, "う　え\n")])
    assert out == ["あいうえ"]


def test_group_by_chunk_empty_chunk():
    out = worker.group_by_chunk([(0, 30), (30, 60)], [(31.0, "はい")])
    assert out == ["", "はい"]


def test_group_by_chunk_nearest_chunk_when_in_none():
    out = worker.group_by_chunk([(0, 10), (20, 30)], [(11.0, "a"), (19.0, "b")])
    assert out == ["a", "b"]


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
                "batch_size": 8,
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
