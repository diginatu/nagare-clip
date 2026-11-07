import json

from nagare_clip.config import get_effective_config
from nagare_clip.transcription.run import asr_request, finish_transcription


def _vad(tmp_path, name, speech):
    p = tmp_path / name
    p.write_text(json.dumps({"duration": 20.0, "speech": speech}))
    return p


def test_asr_request_packs_regions(tmp_path):
    cfg = get_effective_config(None, {"transcription": {"max_chunk_seconds": 5.0}})
    vad = _vad(tmp_path, "a_vad.json", [[1.0, 2.0], [3.0, 4.0], [8.0, 9.0]])
    req = asr_request([(tmp_path / "a.mp4", vad, tmp_path / "a_haas.json")], cfg)
    assert req == {
        "model": "third-intelligence/Haas-ja-v1.0",
        "batch_size": 8,
        "max_chunk_seconds": 5.0,
        "sources": [
            {
                "path": str(tmp_path / "a.mp4"),
                "out": str(tmp_path / "a_haas.json"),
                "chunks": [[1.0, 4.0], [8.0, 9.0]],
            }
        ],
    }


def test_asr_request_no_speech_has_no_chunks(tmp_path):
    cfg = get_effective_config(None)
    vad = _vad(tmp_path, "a_vad.json", [])
    req = asr_request([(tmp_path / "a.mp4", vad, tmp_path / "o.json")], cfg)
    assert req["sources"][0]["chunks"] == []


def test_finish_transcription_writes_json_and_txt(tmp_path):
    vad = _vad(tmp_path, "v.json", [[1.0, 2.0], [3.0, 4.0]])
    asr = tmp_path / "h.json"
    asr.write_text(
        json.dumps(
            {
                "chunks": [[1.0, 4.0]],
                "tokens": [
                    {"text": "こん", "start": 1.0, "end": 1.4},
                    {"text": "ばんは", "start": 3.0, "end": 3.6},
                ],
            }
        )
    )
    out_json, out_txt = tmp_path / "a.json", tmp_path / "a.txt"
    finish_transcription(asr, vad, out_json, out_txt, 0.5)
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert [s["text"] for s in data["segments"]] == ["こん", "ばんは"]
    assert data["language"] == "ja"
    assert "こん" in out_json.read_text(encoding="utf-8")  # ensure_ascii=False
    assert out_txt.read_text(encoding="utf-8").splitlines() == ["こん", "ばんは"]


def test_asr_request_chunks_come_from_joined_regions(tmp_path):
    # 0.3 s gap < min_silence 0.5: one line, so one chunk start even with a tiny limit
    cfg = get_effective_config(None, {"transcription": {"max_chunk_seconds": 1.0}})
    vad = _vad(tmp_path, "a_vad.json", [[1.0, 2.0], [2.3, 3.0], [8.0, 9.0]])
    req = asr_request([(tmp_path / "a.mp4", vad, tmp_path / "o.json")], cfg)
    assert req["sources"][0]["chunks"] == [[1.0, 3.0], [8.0, 9.0]]


def _finish(tmp_path, second_start, min_silence):
    vad = _vad(tmp_path, "v.json", [[1.0, 2.0], [second_start, second_start + 1.0]])
    asr = tmp_path / "h.json"
    asr.write_text(
        json.dumps(
            {
                "chunks": [],
                "tokens": [
                    {"text": "結", "start": 1.0, "end": 1.4},
                    {"text": "構", "start": second_start, "end": second_start + 0.4},
                ],
            }
        )
    )
    out_json, out_txt = tmp_path / "a.json", tmp_path / "a.txt"
    finish_transcription(asr, vad, out_json, out_txt, min_silence)
    return json.loads(out_json.read_text(encoding="utf-8"))


def test_finish_joins_regions_closer_than_min_silence(tmp_path):
    data = _finish(tmp_path, 2.6, 0.7)
    assert [s["text"] for s in data["segments"]] == ["結構"]


def test_finish_keeps_regions_further_than_min_silence(tmp_path):
    data = _finish(tmp_path, 2.8, 0.7)
    assert [s["text"] for s in data["segments"]] == ["結", "構"]
