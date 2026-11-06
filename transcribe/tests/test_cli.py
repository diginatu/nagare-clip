import json

import pytest
from nagare_transcribe import __main__ as cli
from nagare_transcribe import asr


def test_cli_asr_source_without_chunks_skips_model(tmp_path, monkeypatch):
    def boom(name):
        raise AssertionError("model must not load")

    monkeypatch.setattr(asr, "load_model", boom)
    out = tmp_path / "o.json"
    req = tmp_path / "r.json"
    req.write_text(
        json.dumps(
            {
                "model": "m",
                "batch_size": 4,
                "max_chunk_seconds": 30,
                "sources": [{"path": "x.mp4", "out": str(out), "chunks": []}],
            }
        )
    )
    cli.main(["asr", str(req)])
    assert json.loads(out.read_text()) == {"chunks": [], "tokens": []}


def test_cli_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.main([])
