"""publish/describe_frames: one vision call per candidate still, once ever.

The director's labels say what it *thought* was happening at a moment; only
looking says what is legible in the frame. So each still is described once,
into publish/frames.json, and reused by the hash of its own bytes.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.publish.describe_frames import (
    FrameDescription,
    content_hash,
    describe_frames,
    frames_from_dict,
    frames_to_dict,
    load_frames,
)
from nagare_clip.publish.thumbs import ThumbShot

CFG = {"enabled": True, "prompt": "look at it", "max_retries": 2}


def _shot(path="frames/a/1.000.jpg", label="まさかの水漏れ発覚", time=1.0):
    return ThumbShot(stem="a", time=time, kind="overlay", label=label, path=path)


def _write_frame(publish_dir, rel, content=b"\xff\xd8jpegbytes"):
    p = publish_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses) or ["a person's back, centre; the left third is bare wall"]
        self.calls: list[list[dict]] = []

    def __call__(self, messages, cfg):
        self.calls.append(messages)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


# --- the hash ----------------------------------------------------------------


def test_the_hash_is_over_the_bytes_not_the_path(tmp_path):
    a = _write_frame(tmp_path, "frames/a/1.000.jpg", b"same")
    b = _write_frame(tmp_path, "frames/b/99.000.jpg", b"same")
    assert content_hash(a) == content_hash(b)


def test_different_bytes_at_the_same_path_hash_differently(tmp_path):
    p = _write_frame(tmp_path, "frames/a/1.000.jpg", b"before")
    first = content_hash(p)
    p.write_bytes(b"after")
    assert content_hash(p) != first


def test_an_unreadable_frame_has_no_hash(tmp_path):
    assert content_hash(tmp_path / "gone.jpg") == ""


# --- the artifact ------------------------------------------------------------


def test_frames_json_round_trips():
    entries = [
        FrameDescription(
            stem="a",
            source_time=55.66,
            kind="overlay",
            label="水漏れ",
            path="frames/a/55.660.jpg",
            hash="abc123",
            description="a puddle under the tank, lower right; upper left is plain wall",
        )
    ]
    assert frames_from_dict(frames_to_dict(entries)) == entries


@pytest.mark.parametrize(
    "data",
    [None, {}, {"frames": "no"}, {"frames": [None, 7, {"no": "path"}]}],
)
def test_a_malformed_frames_file_reads_as_empty_rather_than_raising(data):
    assert frames_from_dict(data) == []


def test_load_frames_of_a_missing_file_is_empty(tmp_path):
    assert load_frames(tmp_path / "frames.json") == []


def test_load_frames_reads_what_was_written(tmp_path):
    path = tmp_path / "frames.json"
    entries = [FrameDescription("a", 1.0, "overlay", "l", "frames/a/1.000.jpg", "h", "d")]
    path.write_text(json.dumps(frames_to_dict(entries)), encoding="utf-8")
    assert load_frames(path) == entries


# --- describing --------------------------------------------------------------


def test_every_candidate_is_described_once(tmp_path):
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"one")
    _write_frame(tmp_path, "frames/a/2.000.jpg", b"two")
    llm = FakeLLM("first frame", "second frame")
    got = describe_frames(
        [_shot("frames/a/1.000.jpg"), _shot("frames/a/2.000.jpg", time=2.0)],
        tmp_path,
        CFG,
        call_llm=llm,
    )
    assert len(llm.calls) == 2
    assert [e.description for e in got] == ["first frame", "second frame"]
    assert [e.path for e in got] == ["frames/a/1.000.jpg", "frames/a/2.000.jpg"]


def test_the_shortlist_fields_are_carried_into_the_entry(tmp_path):
    _write_frame(tmp_path, "frames/a/55.660.jpg")
    got = describe_frames(
        [_shot("frames/a/55.660.jpg", time=55.66)], tmp_path, CFG, call_llm=FakeLLM()
    )
    assert (got[0].stem, got[0].source_time, got[0].kind, got[0].label) == (
        "a",
        55.66,
        "overlay",
        "まさかの水漏れ発覚",
    )


def test_an_unchanged_frame_costs_no_call(tmp_path):
    """The expensive pass is paid once per edit of the film, not per attempt
    at a thumbnail."""
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"one")
    llm = FakeLLM()
    first = describe_frames([_shot()], tmp_path, CFG, call_llm=llm)
    again = describe_frames([_shot()], tmp_path, CFG, previous=first, call_llm=llm)
    assert len(llm.calls) == 1
    assert again[0].description == first[0].description


def test_a_re_extracted_frame_with_different_content_is_described_again(tmp_path):
    """Re-running director changes the shortlist; the same filename can hold a
    different picture, and the hash is what notices."""
    path = _write_frame(tmp_path, "frames/a/1.000.jpg", b"before")
    llm = FakeLLM("the old picture", "the new picture")
    first = describe_frames([_shot()], tmp_path, CFG, call_llm=llm)
    path.write_bytes(b"after")
    again = describe_frames([_shot()], tmp_path, CFG, previous=first, call_llm=llm)
    assert len(llm.calls) == 2
    assert again[0].description == "the new picture"
    assert again[0].hash != first[0].hash


def test_a_hand_edited_description_survives_a_re_run(tmp_path):
    """If you disagree with what the model saw, correct the prose and it sticks."""
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"one")
    llm = FakeLLM()
    first = describe_frames([_shot()], tmp_path, CFG, call_llm=llm)
    edited = [
        FrameDescription(**{**vars(first[0]), "description": "手書き: 実際は水漏れは映っていない"})
    ]
    again = describe_frames([_shot()], tmp_path, CFG, previous=edited, call_llm=llm)
    assert len(llm.calls) == 1
    assert again[0].description == "手書き: 実際は水漏れは映っていない"


def test_a_previous_entry_with_no_description_is_not_reuseable(tmp_path):
    """An entry left empty by a failed call must be retried, not cached."""
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"one")
    stale = [
        FrameDescription(
            "a",
            1.0,
            "overlay",
            "l",
            "frames/a/1.000.jpg",
            content_hash(tmp_path / "frames/a/1.000.jpg"),
            "",
        )
    ]
    llm = FakeLLM()
    got = describe_frames([_shot()], tmp_path, CFG, previous=stale, call_llm=llm)
    assert len(llm.calls) == 1
    assert got[0].description


def test_a_described_entry_wins_over_an_empty_one_with_the_same_hash(tmp_path):
    """Two shortlist entries can point at one picture -- an identical frame
    filed under two timestamps. The one that was actually described is the
    one worth keeping, whichever order they sit in."""
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"one")
    digest = content_hash(tmp_path / "frames/a/1.000.jpg")
    previous = [
        FrameDescription("a", 1.0, "overlay", "l", "frames/a/1.000.jpg", digest, ""),
        FrameDescription("a", 9.0, "keep", "l", "frames/a/9.000.jpg", digest, "the real one"),
    ]
    llm = FakeLLM()
    got = describe_frames([_shot()], tmp_path, CFG, previous=previous, call_llm=llm)
    assert llm.calls == []
    assert got[0].description == "the real one"


# --- the call itself ---------------------------------------------------------


def test_the_frame_is_sent_as_a_base64_data_uri_image_part(tmp_path):
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"\xff\xd8jpeg")
    llm = FakeLLM()
    describe_frames([_shot()], tmp_path, CFG, call_llm=llm)
    parts = llm.calls[0][1]["content"]
    images = [p for p in parts if p.get("type") == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_the_system_prompt_is_the_configured_one(tmp_path):
    _write_frame(tmp_path, "frames/a/1.000.jpg")
    llm = FakeLLM()
    describe_frames([_shot()], tmp_path, CFG, call_llm=llm)
    assert llm.calls[0][0] == {"role": "system", "content": "look at it"}


def test_the_directors_label_is_never_shown_to_the_vision_model(tmp_path):
    """A label is a claim about the moment; the description exists to catch a
    label whose frame does not match it, so showing it would only confirm."""
    _write_frame(tmp_path, "frames/a/1.000.jpg")
    llm = FakeLLM()
    describe_frames([_shot(label="まさかの水漏れ発覚")], tmp_path, CFG, call_llm=llm)
    flat = json.dumps(llm.calls[0], ensure_ascii=False)
    assert "水漏れ" not in flat


def test_whitespace_in_the_response_is_collapsed(tmp_path):
    _write_frame(tmp_path, "frames/a/1.000.jpg")
    llm = FakeLLM("  a puddle,\n\n  lower right  ")
    got = describe_frames([_shot()], tmp_path, CFG, call_llm=llm)
    assert got[0].description == "a puddle, lower right"


class FakeRecorder:
    """Captures exactly what would be written into the LLM report."""

    stage = "publish"

    def __init__(self):
        self.attempts: list[dict] = []

    def begin(self, unit):
        pass

    def attempt(self, **kwargs):
        self.attempts.append(kwargs)

    def flush_unit(self, unit, **kwargs):
        pass


def test_the_report_records_the_frame_path_never_the_base64_payload(tmp_path):
    """A 24-frame shortlist of inlined JPEGs would make the report unopenable,
    and the payload says nothing a path does not."""
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"\xff\xd8" + b"payloadbytes" * 40)
    rec = FakeRecorder()
    describe_frames([_shot()], tmp_path, CFG, call_llm=FakeLLM(), recorder=rec)
    recorded = json.dumps(rec.attempts[0]["messages"], ensure_ascii=False)
    assert "base64" not in recorded
    assert "frames/a/1.000.jpg" in recorded
    assert all(isinstance(m["content"], str) for m in rec.attempts[0]["messages"])


# --- degrading ---------------------------------------------------------------


def test_an_empty_response_is_retried_then_leaves_the_entry_undescribed(tmp_path):
    _write_frame(tmp_path, "frames/a/1.000.jpg")
    llm = FakeLLM("", "", "")
    got = describe_frames([_shot()], tmp_path, {**CFG, "max_retries": 2}, call_llm=llm)
    assert len(llm.calls) == 3
    assert got[0].description == ""
    assert got[0].hash  # still recorded, so the next run retries this frame


def test_a_failing_call_does_not_abort_the_rest(tmp_path):
    _write_frame(tmp_path, "frames/a/1.000.jpg", b"one")
    _write_frame(tmp_path, "frames/a/2.000.jpg", b"two")

    calls = {"n": 0}

    def boom(messages, cfg):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("no vision model")
        return "the second frame"

    got = describe_frames(
        [_shot("frames/a/1.000.jpg"), _shot("frames/a/2.000.jpg", time=2.0)],
        tmp_path,
        CFG,
        call_llm=boom,
    )
    assert [e.description for e in got] == ["", "the second frame"]


def test_a_frame_that_is_not_on_disk_is_dropped(tmp_path, caplog):
    got = describe_frames([_shot("frames/a/gone.jpg")], tmp_path, CFG, call_llm=FakeLLM())
    assert got == []
    assert "gone.jpg" in caplog.text


def test_disabled_records_the_shortlist_with_no_call(tmp_path):
    """Hashes and shortlist fields still land, so descriptions can be written
    by hand -- and turning the setting on later re-uses them."""
    _write_frame(tmp_path, "frames/a/1.000.jpg")
    llm = FakeLLM()
    got = describe_frames([_shot()], tmp_path, {**CFG, "enabled": False}, call_llm=llm)
    assert llm.calls == []
    assert [(e.path, e.description) for e in got] == [("frames/a/1.000.jpg", "")]
    assert got[0].hash
