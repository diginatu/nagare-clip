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


def _byte_entries():
    return [
        {"char": ["今"], "start": 10.0, "end": 10.16},
        {"char": ["�"], "start": 10.16, "end": 10.24},
        {"char": ["�"], "start": 10.24, "end": 10.32},
        {"char": ["�"], "start": 10.32, "end": 10.4},
        {"char": ["れた"], "start": 10.4, "end": 10.56},
    ]


_BYTE_PIECES = ["今", "<0xE5>", "<0x91>", "<0x86>", "れた"]


def test_byte_fallback_run_becomes_one_token():
    h = _hyp(_byte_entries())
    toks = asr.tokens_from_hypothesis(h, 1.0, _BYTE_PIECES)
    assert toks == [
        {"text": "今", "start": 11.0, "end": 11.16},
        {"text": "呆", "start": 11.16, "end": 11.4},
        {"text": "れた", "start": 11.4, "end": 11.56},
    ]


def test_pieces_none_keeps_replacement_tokens():
    toks = asr.tokens_from_hypothesis(_hyp(_byte_entries()), 0.0)
    assert [t["text"] for t in toks] == ["今", "�", "�", "�", "れた"]


def test_pieces_length_mismatch_keeps_replacement_tokens(capsys):
    toks = asr.tokens_from_hypothesis(_hyp(_byte_entries()), 0.0, _BYTE_PIECES[:-1])
    assert [t["text"] for t in toks] == ["今", "�", "�", "�", "れた"]
    assert capsys.readouterr().err.strip() != ""


def test_incomplete_byte_run_is_replacement_char():
    h = _hyp([{"char": ["�"], "start": 0.0, "end": 0.08}])
    assert asr.tokens_from_hypothesis(h, 0.0, ["<0xE5>"]) == [
        {"text": "�", "start": 0.0, "end": 0.08}
    ]


def test_adjacent_multibyte_chars_join_in_one_token():
    raw = "呆呆".encode()
    entries = [{"char": ["�"], "start": i * 0.08, "end": (i + 1) * 0.08} for i in range(len(raw))]
    pieces = [f"<0x{b:02X}>" for b in raw]
    toks = asr.tokens_from_hypothesis(_hyp(entries), 0.0, pieces)
    assert toks == [{"text": "呆呆", "start": 0.0, "end": 0.48}]


def test_transcribe_chunks_passes_tokenizer_pieces():
    seen = {}

    class Tok:
        def ids_to_tokens(self, ids):
            seen["ids"] = ids
            return list(_BYTE_PIECES)

    class Model:
        tokenizer = Tok()

        def transcribe(self, slices, **kw):
            h = _hyp(_byte_entries())
            h.y_sequence = [np.int64(i) for i in range(5)]
            return [h]

    audio = np.zeros(16000 * 5, np.float32)
    toks = asr.transcribe_chunks(Model(), audio, [(0.0, 2.0)], 1)
    assert [t["text"] for t in toks] == ["今", "呆", "れた"]
    assert seen["ids"] == [0, 1, 2, 3, 4]
    assert all(type(i) is int for i in seen["ids"])


def test_transcribe_chunks_without_tokenizer_passes_none():
    class Model:
        def transcribe(self, slices, **kw):
            return [_hyp(_byte_entries())]

    audio = np.zeros(16000 * 5, np.float32)
    toks = asr.transcribe_chunks(Model(), audio, [(0.0, 2.0)], 1)
    assert [t["text"] for t in toks] == ["今", "�", "�", "�", "れた"]
