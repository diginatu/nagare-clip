from types import SimpleNamespace

import numpy as np
from nagare_transcribe import asr


def _hyp(entries):
    return SimpleNamespace(timestamp={"char": entries})


def test_tokens_drop_empty_and_add_offset():
    h = _hyp(
        [
            {"char": [""], "start": 0.0, "end": 0.08},
            {"char": ["▁は"], "start": 0.96, "end": 1.28},
            {"char": ["んです"], "start": 1.28, "end": 1.36},
        ]
    )
    assert asr.tokens_from_hypothesis(h, 92.81) == [
        {"text": "は", "start": 93.77, "end": 94.09},
        {"text": "んです", "start": 94.09, "end": 94.17},
    ]


def test_tokens_strip_inner_whitespace():
    h = _hyp([{"char": ["Third ", "▁Intelligence"], "start": 0.0, "end": 1.0}])
    assert asr.tokens_from_hypothesis(h, 0.0)[0]["text"] == "ThirdIntelligence"


def test_split_long_chunks_offsets_subchunks():
    calls = []

    def splitter(audio, *, chunk_seconds):
        calls.append((len(audio), chunk_seconds))
        return [(0, 480000), (480000, 1088000)]

    audio = np.zeros(16000 * 100, np.float32)
    out = asr.split_long_chunks(audio, [(0.0, 10.0), (12.0, 80.0)], 30, splitter=splitter)
    assert out == [(0.0, 10.0), (12.0, 42.0), (42.0, 80.0)]
    assert calls == [(68 * 16000, 30)]


def test_transcribe_chunks_passes_timestamps_and_offsets():
    seen = {}

    class Model:
        def transcribe(self, slices, **kw):
            seen["lens"] = [len(s) for s in slices]
            seen["kw"] = kw
            return [_hyp([{"char": ["あ"], "start": 0.5, "end": 0.6}]) for _ in slices]

    audio = np.zeros(16000 * 20, np.float32)
    toks = asr.transcribe_chunks(Model(), audio, [(0.0, 2.0), (10.0, 13.0)], 8)
    assert seen["kw"]["timestamps"] is True and seen["kw"]["batch_size"] == 8
    assert seen["lens"] == [32000, 48000]
    assert [t["start"] for t in toks] == [0.5, 10.5]


def test_transcribe_chunks_rejects_hypothesis_count_mismatch():
    import pytest

    class Model:
        def transcribe(self, slices, **kw):
            return [_hyp([{"char": ["あ"], "start": 0.0, "end": 0.1}]) for _ in slices[:-1]]

    audio = np.zeros(16000 * 10, np.float32)
    with pytest.raises(ValueError):
        asr.transcribe_chunks(Model(), audio, [(0.0, 1.0), (2.0, 3.0)], 2)
