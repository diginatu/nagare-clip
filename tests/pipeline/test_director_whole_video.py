"""``director.whole_project_context``: every segment's call sees the whole video.

Driven through the real director stage with only the LLM faked, because the
properties that matter here are about the assembled REQUEST — what is cached,
what differs between calls, and whether two views of one transcript agree.
"""

from __future__ import annotations

import json
import re

import pytest

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.config import get_effective_config
from nagare_clip.director import director_llm as dl
from nagare_clip.llm_client import CACHEABLE_PREFIX_KEY
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


class _NullRec:
    stage = "director"

    def clear(self): ...
    def begin(self, unit): ...
    def attempt(self, **kw): ...
    def flush_unit(self, unit, **kw): ...
    def rebuild_index(self): ...


#: Per-line (start, end) seconds.  mix line 5 holds a 3 s audio_silence cut and
#: a vision-described gap sits between mix lines 1 and 2, so both the
#: speech/silence split and the gap annotation are exercised.
TIMES = {
    "dev": [(0.0, 2.0), (3.0, 7.0), (8.0, 9.5), (10.0, 13.0)],
    "mix": [
        (0.0, 4.0),
        (10.0, 12.0),
        (12.5, 15.0),
        (16.0, 18.0),
        (19.0, 29.0),
        (30.0, 31.0),
        (32.0, 35.0),
        (36.0, 38.0),
        (39.0, 41.0),
    ],
}


def _ctx(tmp_path, stems=("mix", "dev"), whole=None):
    cfg = get_effective_config(None, {})
    cfg["director"].update({"enabled": True, "prompt": "P", "max_retries": 0})
    if whole is not None:
        cfg["director"]["whole_project_context"] = whole
    sources = [
        SourceMedia(abs_path=tmp_path / f"{s}.mp4", stem=s, relative=f"{s}.mp4") for s in stems
    ]
    return PipelineContext(
        cfg=cfg,
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=sources,
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    """dev (4 lines) whole, then mix (9 lines) split into three and reordered."""
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    out = tmp_path / "out"
    (tmp_path / "in").mkdir()
    for sub in ("text_filter", "sentence_split", "audio_silence", "gap_context", "plan"):
        (out / sub).mkdir(parents=True)
    for stem, times in TIMES.items():
        (tmp_path / "in" / f"{stem}.mp4").touch()
        (out / "text_filter" / f"{stem}_edits.txt").write_text(
            "\n".join(f"{stem}の{i}行目" for i in range(1, len(times) + 1)) + "\n",
            encoding="utf-8",
        )
        (out / "sentence_split" / f"{stem}.json").write_text(
            json.dumps({"segments": [{"start": s, "end": e} for s, e in times]}),
            encoding="utf-8",
        )
    write_cuts(out / "audio_silence" / "mix_cuts.txt", [(22.0, 25.0)])
    (out / "gap_context" / "mix_gaps.json").write_text(
        json.dumps(
            {"gaps": [{"start": 4.0, "end": 10.0, "frames": [], "description": "バルブを外す"}]}
        ),
        encoding="utf-8",
    )
    (out / "plan" / "plan.json").write_text(
        json.dumps(
            {
                "directions": [],
                "order": [
                    {"stem": "dev"},
                    {"stem": "mix", "lines": [4, 6]},
                    {"stem": "mix", "lines": [1, 3]},
                    {"stem": "mix", "lines": [7, 9]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _first_line(user: str) -> int:
    return int(re.search(r"^(\d+):", user, re.MULTILINE).group(1))


def _calls(monkeypatch, ctx):
    """Run the real director stage; return each call's messages, in order."""
    calls = []

    def fake(messages, cfg):
        calls.append(messages)
        first = _first_line(messages[1]["content"])
        return json.dumps(
            {
                "ops": [
                    {
                        "type": "overlay",
                        "lines": [first, first],
                        "text": f"字幕{first}",
                        "duration": 2,
                    },
                    {
                        "type": "timelapse",
                        "lines": [first + 1, first + 2],
                        "factor": 6,
                        "text": "早送り",
                    },
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(dl, "_call_llm", fake)
    next(s for s in st.STAGES if s.name == "director").run(ctx)
    return calls


class TestFlagOffIsUnchanged:
    """Pinned against the requests the code built before the flag existed."""

    def test_the_default_is_off(self):
        assert get_effective_config(None, {})["director"]["whole_project_context"] is False

    def test_every_request_is_byte_identical_to_before(self, project, monkeypatch):
        calls = _calls(monkeypatch, _ctx(project))
        assert [m[0]["content"] for m in calls] == PINNED_SYSTEM
        assert [m[1]["content"] for m in calls] == PINNED_USER
        assert all(m[0][CACHEABLE_PREFIX_KEY] == PINNED_PREFIX for m in calls)

    def test_explicit_false_is_the_same_as_absent(self, project, monkeypatch):
        calls = _calls(monkeypatch, _ctx(project, whole=False))
        assert [m[0]["content"] for m in calls] == PINNED_SYSTEM
        assert [m[1]["content"] for m in calls] == PINNED_USER


def _on(monkeypatch, project, **kw):
    return _calls(monkeypatch, _ctx(project, whole=True, **kw))


def _prefix(call) -> str:
    return call[0][CACHEABLE_PREFIX_KEY]


def _varying(call) -> str:
    """The part of the system prompt after the cache breakpoint."""
    system, prefix = call[0]["content"], _prefix(call)
    assert system.startswith(prefix)
    return system[len(prefix) :]


def _section(reference: str, k: int) -> list[str]:
    """Segment *k*'s chunk of the reference block, header line first."""
    chunk = next(c for c in reference.split("\n\n") if c.startswith(f"[{k}] "))
    return chunk.split("\n")


class TestTheReferenceIsCached:
    def test_the_prefix_is_byte_identical_across_every_segment(self, project, monkeypatch):
        calls = _on(monkeypatch, project)
        assert len(calls) == 4
        assert len({_prefix(c) for c in calls}) == 1
        assert "[1]1: devの1行目" in _prefix(calls[0])

    def test_it_sits_after_the_keep_note_inside_the_prefix(self, project, monkeypatch):
        from nagare_clip.director.context import WHOLE_VIDEO_NOTE

        prefix = _prefix(_on(monkeypatch, project)[1])
        assert prefix.startswith(PINNED_PREFIX + "\n\n" + WHOLE_VIDEO_NOTE + "\n\n[1] dev")
        assert "[4]9: mixの9行目" in prefix

    def test_the_overview_stays_after_the_breakpoint(self, project, monkeypatch):
        call = _on(monkeypatch, project)[1]
        assert "Project context (all videos):" not in _prefix(call)
        assert _varying(call).startswith("\n\nProject context (all videos):")


class TestTheReferenceRendering:
    def test_each_segment_reads_as_its_own_editable_transcript(self, project, monkeypatch):
        # Same renderer for both views: brackets, speech/silence split and gap
        # annotations must agree line for line; only the [k] qualifier differs.
        calls = _on(monkeypatch, project)
        for k, call in enumerate(calls, start=1):
            body = _section(_prefix(call), k)[1:]
            unqualified = [re.sub(rf"^\[{k}\](\d+):", r"\1:", line) for line in body]
            editable = call[1]["content"].split("\n", 1)[1]
            assert "\n".join(unqualified) == editable

    def test_numbered_lines_are_segment_qualified(self, project, monkeypatch):
        prefix = _prefix(_on(monkeypatch, project)[0])
        assert _section(prefix, 3)[1:] == [
            "[3]1: mixの1行目  [4.0s, gap 6.0s]",
            "    [silent gap: バルブを外す]",
            "[3]2: mixの2行目  [2.0s, gap 0.5s]",
            "[3]3: mixの3行目  [2.5s]",
        ]

    def test_no_line_in_the_reference_carries_a_bare_number(self, project, monkeypatch):
        prefix = _prefix(_on(monkeypatch, project)[0])
        reference = prefix[len(PINNED_PREFIX) :]
        assert not re.search(r"^\d+:", reference, re.MULTILINE)

    def test_each_segment_is_headed_by_its_label_and_default_runtime(self, project, monkeypatch):
        prefix = _prefix(_on(monkeypatch, project)[0])
        # dev 2+4+1.5+3 = 10.5 s; mix[4-6] 2+(10-3)+1 = 10 s (the cut silence
        # does not play); mix[1-3] 8.5 s; mix[7-9] 7 s.
        assert [_section(prefix, k)[0] for k in (1, 2, 3, 4)] == [
            "[1] dev — default runtime 0.2 min",
            "[2] mix [4-6] — default runtime 0.2 min",
            "[3] mix [1-3] — default runtime 0.1 min",
            "[4] mix [7-9] — default runtime 0.1 min",
        ]

    def test_the_block_ends_with_the_whole_videos_runtime(self, project, monkeypatch):
        prefix = _prefix(_on(monkeypatch, project)[0])
        assert prefix.endswith("\n\nWhole video — default runtime 0.6 min")


class TestEarlierSegmentsEdits:
    def test_the_caption_list_is_replaced_by_the_earlier_segments_ops(self, project, monkeypatch):
        calls = _on(monkeypatch, project)
        varying = _varying(calls[3])
        assert "Captions already shown" not in calls[3][0]["content"]
        assert (
            "\n\nEdits already made to the segments playing earlier:\n"
            "[1] dev: overlay [1]1 「字幕1」; timelapse [1]2-[1]3 x6 「早送り」\n"
            "[2] mix [4-6]: overlay [2]4 「字幕4」; timelapse [2]5-[2]6 x6 「早送り」\n"
            "[3] mix [1-3]: overlay [3]1 「字幕1」; timelapse [3]2-[3]3 x6 「早送り」\n"
        ) in varying

    def test_the_first_segment_gets_no_such_block(self, project, monkeypatch):
        assert "Edits already made" not in _on(monkeypatch, project)[0][0]["content"]

    def test_only_segments_playing_earlier_are_listed(self, project, monkeypatch):
        varying = _varying(_on(monkeypatch, project)[1])
        assert "[1] dev: overlay" in varying
        assert "[2] mix" not in varying.split("Edits already made", 1)[1].split("\n\n")[0]

    def test_an_earlier_segment_not_in_this_run_is_read_off_disk(self, project, monkeypatch):
        d = project / "out" / "director"
        d.mkdir(parents=True, exist_ok=True)
        (d / "dev_director.json").write_text(
            json.dumps({"ops": [{"type": "cut", "lines": [2, 4], "note": "長い"}]}),
            encoding="utf-8",
        )
        calls = _on(monkeypatch, project, stems=("mix",))
        assert "\n[1] dev: cut [1]2-[1]4\n" in _varying(calls[0])


class TestTheUserMessage:
    def test_it_names_its_segment_then_is_unchanged(self, project, monkeypatch):
        calls = _on(monkeypatch, project)
        assert [c[1]["content"] for c in calls] == [
            f"Edit segment [{k}] — its lines below are the ones your ops address:\n{pinned}"
            for k, pinned in enumerate(PINNED_USER, start=1)
        ]

    def test_the_ops_still_land(self, project, monkeypatch):
        _on(monkeypatch, project)
        data = json.loads(
            (project / "out" / "director" / "mix_director.json").read_text(encoding="utf-8")
        )
        assert [op["lines"] for op in data["ops"]] == [
            [1, 1],
            [2, 3],
            [4, 4],
            [5, 6],
            [7, 7],
            [8, 9],
        ]


#: Captured from the director stage BEFORE ``whole_project_context`` existed.
PINNED_SYSTEM: list[str] = [
    'P\n\nA "keep" op may span at most 8 line(s); a wider one is rejected and has no effect — to hold a longer event, emit consecutive keeps that each stay within the cap. A "timelapse" op needs no keep of its own; it protects its whole range by itself.\n\nProject context (all videos):\nAll segments below are concatenated into ONE finished video in this order; you are editing only segment 1 of them.\nThis segment ("dev") — segment 1 of 4, the FIRST in the finished timeline:\n\nEarlier in the finished video (already edited):\n- (none)\nLater in the finished video:\n- 2. mix [4-6]\n- 3. mix [1-3]\n- 4. mix [7-9]\n\nYour footage is NOT a standalone episode — it plays inside one longer finished video, and the lines below are what the viewer hears immediately before and after it. An opening greeting or a closing sign-off in your footage is addressing an audience that is already mid-video. They are NOT part of your transcript and carry no numbering — every op you emit refers to your own numbered lines only.\nImmediately AFTER this segment (mix [4-6], its first lines):\n- mixの4行目\n- mixの5行目\n- mixの6行目',
    'P\n\nA "keep" op may span at most 8 line(s); a wider one is rejected and has no effect — to hold a longer event, emit consecutive keeps that each stay within the cap. A "timelapse" op needs no keep of its own; it protects its whole range by itself.\n\nProject context (all videos):\nAll segments below are concatenated into ONE finished video in this order; you are editing only segment 2 of them.\nThis segment ("mix [4-6]") — segment 2 of 4:\n\nEarlier in the finished video (already edited):\n- 1. dev\nLater in the finished video:\n- 3. mix [1-3]\n- 4. mix [7-9]\n\nCaptions already shown earlier in the finished video:\n- 字幕1\n- 早送り\n\nYour footage is NOT a standalone episode — it plays inside one longer finished video, and the lines below are what the viewer hears immediately before and after it. An opening greeting or a closing sign-off in your footage is addressing an audience that is already mid-video. They are NOT part of your transcript and carry no numbering — every op you emit refers to your own numbered lines only.\nImmediately BEFORE this segment in the finished video (dev, its last lines):\n- devの2行目\n- devの3行目\n- devの4行目\nImmediately AFTER this segment (mix [1-3], its first lines):\n- mixの1行目\n- mixの2行目\n- mixの3行目',
    'P\n\nA "keep" op may span at most 8 line(s); a wider one is rejected and has no effect — to hold a longer event, emit consecutive keeps that each stay within the cap. A "timelapse" op needs no keep of its own; it protects its whole range by itself.\n\nProject context (all videos):\nAll segments below are concatenated into ONE finished video in this order; you are editing only segment 3 of them.\nThis segment ("mix [1-3]") — segment 3 of 4:\n\nEarlier in the finished video (already edited):\n- 1. dev\n- 2. mix [4-6]\nLater in the finished video:\n- 4. mix [7-9]\n\nCaptions already shown earlier in the finished video:\n- 字幕1\n- 早送り\n- 字幕4\n\nYour footage is NOT a standalone episode — it plays inside one longer finished video, and the lines below are what the viewer hears immediately before and after it. An opening greeting or a closing sign-off in your footage is addressing an audience that is already mid-video. They are NOT part of your transcript and carry no numbering — every op you emit refers to your own numbered lines only.\nImmediately BEFORE this segment in the finished video (mix [4-6], its last lines):\n- mixの4行目\n- mixの5行目\n- mixの6行目\nImmediately AFTER this segment (mix [7-9], its first lines):\n- mixの7行目\n- mixの8行目\n- mixの9行目',
    'P\n\nA "keep" op may span at most 8 line(s); a wider one is rejected and has no effect — to hold a longer event, emit consecutive keeps that each stay within the cap. A "timelapse" op needs no keep of its own; it protects its whole range by itself.\n\nProject context (all videos):\nAll segments below are concatenated into ONE finished video in this order; you are editing only segment 4 of them.\nThis segment ("mix [7-9]") — segment 4 of 4, the LAST in the finished timeline:\n\nEarlier in the finished video (already edited):\n- 1. dev\n- 2. mix [4-6]\n- 3. mix [1-3]\nLater in the finished video:\n- (none)\n\nCaptions already shown earlier in the finished video:\n- 字幕1\n- 早送り\n- 字幕4\n\nYour footage is NOT a standalone episode — it plays inside one longer finished video, and the lines below are what the viewer hears immediately before and after it. An opening greeting or a closing sign-off in your footage is addressing an audience that is already mid-video. They are NOT part of your transcript and carry no numbering — every op you emit refers to your own numbered lines only.\nImmediately BEFORE this segment in the finished video (mix [1-3], its last lines):\n- mixの1行目\n- mixの2行目\n- mixの3行目',
]
PINNED_USER: list[str] = [
    "1: devの1行目  [2.0s, gap 1.0s]\n2: devの2行目  [4.0s, gap 1.0s]\n3: devの3行目  [1.5s, gap 0.5s]\n4: devの4行目  [3.0s]",
    "4: mixの4行目  [2.0s, gap 1.0s]\n5: mixの5行目  [7.0s speech, 3.0s silence, gap 1.0s]\n6: mixの6行目  [1.0s]",
    "1: mixの1行目  [4.0s, gap 6.0s]\n    [silent gap: バルブを外す]\n2: mixの2行目  [2.0s, gap 0.5s]\n3: mixの3行目  [2.5s]",
    "7: mixの7行目  [3.0s, gap 1.0s]\n8: mixの8行目  [2.0s, gap 1.0s]\n9: mixの9行目  [2.0s]",
]
PINNED_PREFIX = 'P\n\nA "keep" op may span at most 8 line(s); a wider one is rejected and has no effect — to hold a longer event, emit consecutive keeps that each stay within the cap. A "timelapse" op needs no keep of its own; it protects its whole range by itself.'
