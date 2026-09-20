"""The director as ONE conversation over the whole finished video.

Driven through the real stage with only the LLM faked, because what matters
here is the assembled conversation — what is cached, what each turn carries,
what reaches disk when it ends badly — not any one function's return value.

The project is the same shape as ``test_director_whole_video``'s: ``dev``
whole, then ``mix`` split into three stretches and reordered, so one source
plays as three segments and a source line number is never a display number.
"""

from __future__ import annotations

import json
import re

import pytest

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.config import get_effective_config
from nagare_clip.director import director_llm as dl
from nagare_clip.director.display import build_display_view
from nagare_clip.director.preview import STATE_HEADER
from nagare_clip.director.run import VIEW_HEADER, SegmentInputs, load_segment_transcript
from nagare_clip.llm_client import CACHEABLE_PREFIX_KEY
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia

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


class _Rec:
    """A recorder that remembers what the stage asked it to write."""

    stage = "director"

    def __init__(self):
        self.units: list[str] = []
        self.attempts: list[dict] = []
        self.flushed: list[tuple[str, str]] = []

    def clear(self): ...
    def rebuild_index(self): ...

    def begin(self, unit):
        self.units.append(unit)

    def attempt(self, **kw):
        self.attempts.append(kw)

    def flush_unit(self, unit, **kw):
        self.flushed.append((unit, kw.get("outcome", "")))


def _ctx(tmp_path, stems=("mix", "dev"), **director):
    cfg = get_effective_config(None, {})
    cfg["director"].update({"enabled": True, "prompt": "P", "max_retries": 0, **director})
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


def _view(ctx):
    """The view the stage will build, for a test that needs its line count."""
    segments = st._timeline_segments(ctx)
    pairs = []
    for segment in segments:
        stem = segment.stem
        inputs = SegmentInputs(
            segment,
            ctx.stage_dir("text_filter") / f"{stem}_edits.txt",
            json_path=ctx.stage_dir("sentence_split") / f"{stem}.json",
            gaps=ctx.stage_dir("gap_context") / f"{stem}_gaps.json",
            cuts_txt=ctx.stage_dir("audio_silence") / f"{stem}_cuts.txt",
        )
        pairs.append((segment, load_segment_transcript(inputs)))
    return build_display_view(pairs)


_RANGE = re.compile(r"around lines (\d+) to (\d+)")


def _asked(user: str) -> tuple[int, int] | None:
    m = _RANGE.search(user)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _run(monkeypatch, ctx, reply=None, calls=None, replies=None):
    """Run the real director stage with a scripted model; return every call."""
    calls = [] if calls is None else calls
    replies = [] if replies is None else replies

    def script(messages, cfg):
        asked = _asked(messages[-1]["content"])
        if asked is None:
            return json.dumps({"done": True})
        first, last = asked
        return json.dumps(
            {
                "range": [first, last],
                "reviewed_through": last,
                "ops": [{"type": "cut", "lines": [first, first], "note": "x"}],
            }
        )

    def fake(messages, cfg):
        calls.append([dict(m) for m in messages])
        out = (reply or script)(messages, cfg)
        replies.append(out)
        return out

    monkeypatch.setattr(dl, "_call_llm", fake)
    next(s for s in st.STAGES if s.name == "director").run(ctx)
    return calls


HISTORY_LINE = re.compile(r"^(Review around lines \d+ to \d+\.|Every line has been reviewed\.)$")


class TestTheMessages:
    """Four lines a turn, so the conversation really has several of them.

    Each turn sends: the system message, then every earlier ask trimmed to one
    line with the reply it got, then ONE user message carrying the complete
    current edit and the live request.
    """

    def test_one_system_message_byte_identical_on_every_turn(self, project, monkeypatch):
        calls = _run(monkeypatch, _ctx(project, chunk_lines=4))
        assert len(calls) > 2
        assert len({c[0]["content"] for c in calls}) == 1
        assert all(c[0]["role"] == "system" for c in calls)

    def test_the_whole_system_message_is_the_cacheable_prefix(self, project, monkeypatch):
        calls = _run(monkeypatch, _ctx(project, chunk_lines=4))
        assert all(c[0][CACHEABLE_PREFIX_KEY] == c[0]["content"] for c in calls)

    def test_it_carries_the_prompt_the_keep_note_and_the_whole_video(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        system = _run(monkeypatch, ctx)[0][0]["content"]
        assert system.startswith("P\n")
        assert "keep" in system  # the keep-limit note
        assert system.rstrip().endswith(_view(ctx).render().rstrip())

    def test_the_turns_alternate_user_and_assistant(self, project, monkeypatch):
        last = _run(monkeypatch, _ctx(project, chunk_lines=4))[-1]
        roles = [m["role"] for m in last[1:]]
        assert roles == ["user", "assistant"] * (len(roles) // 2) + ["user"]

    def test_nothing_but_role_and_content_goes_over_the_wire(self, project, monkeypatch):
        # Plain chat: no tool definitions, no tool_choice, no tool messages.
        for call in _run(monkeypatch, _ctx(project, chunk_lines=4)):
            for message in call[1:]:
                assert set(message) == {"role", "content"}

    def test_the_models_own_replies_stay_verbatim_and_in_order(self, project, monkeypatch):
        replies: list[str] = []
        calls = _run(monkeypatch, _ctx(project, chunk_lines=4), replies=replies)
        said = [m["content"] for m in calls[-1] if m["role"] == "assistant"]
        assert said == replies[: len(calls) - 1]
        assert len(said) >= 2

    def test_only_the_newest_user_message_carries_the_state(self, project, monkeypatch):
        calls = _run(monkeypatch, _ctx(project, chunk_lines=4))
        last = calls[-1]
        assert last[-1]["content"].count(STATE_HEADER) == 1
        assert sum(m["content"].count(STATE_HEADER) for m in last) == 1
        assert sum(m["content"].count("Playback of these ops") for m in last[:-1]) == 0

    def test_an_earlier_ask_is_trimmed_to_one_line_naming_its_range(self, project, monkeypatch):
        calls = _run(monkeypatch, _ctx(project, chunk_lines=4))
        earlier = [m["content"] for m in calls[-1][1:-1] if m["role"] == "user"]
        assert len(earlier) >= 2
        assert all(HISTORY_LINE.match(text) for text in earlier)

    def test_the_newest_user_message_is_the_state_then_the_request(self, project, monkeypatch):
        newest = _run(monkeypatch, _ctx(project, chunk_lines=4))[1][-1]["content"]
        assert "Reviewed through line" in newest
        assert newest.index(STATE_HEADER) < newest.index("Reviewed through line")

    def test_the_very_first_turn_already_carries_the_whole_state(self, project, monkeypatch):
        first = _run(monkeypatch, _ctx(project, chunk_lines=4))[0]
        assert len(first) == 2
        assert first[-1]["content"].startswith(STATE_HEADER)
        assert "(no ops: every line plays its default)" in first[-1]["content"]

    def test_the_state_covers_every_segment_on_every_turn(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        calls = _run(monkeypatch, ctx)
        segments = len(_view(ctx).segments)
        assert segments == 4
        for call in calls:
            newest = call[-1]["content"]
            for index in range(1, segments + 1):
                assert f"segment [{index}] " in newest

    def test_the_state_carries_the_whole_video_runtime_and_the_captions(self, project, monkeypatch):
        newest = _run(monkeypatch, _ctx(project, chunk_lines=4))[-1][-1]["content"]
        assert "whole video so far" in newest
        assert "Captions in playback order" in newest

    def test_the_state_prices_what_the_model_just_sent(self, project, monkeypatch):
        second = _run(monkeypatch, _ctx(project, chunk_lines=4))[1][-1]["content"]
        assert "cut [1,1]" in second
        assert "whole video so far" in second

    def test_an_op_note_survives_into_the_next_turns_state(self, project, monkeypatch):
        second = _run(monkeypatch, _ctx(project, chunk_lines=4))[1][-1]["content"]
        assert "note: x" in second


class TestTheOpsThatReachDisk:
    def test_every_source_is_written_in_source_coordinates(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        _run(monkeypatch, ctx)
        for stem in ("dev", "mix"):
            path = ctx.stage_dir("director") / f"{stem}_director.json"
            ops = json.loads(path.read_text(encoding="utf-8"))["ops"]
            assert ops
            for op in ops:
                assert 1 <= op["lines"][0] <= len(TIMES[stem])

    def test_source_no_longer_narrows_the_director(self, project, monkeypatch):
        # Only dev is being processed, but one conversation owns the whole
        # video: mix is REVIEWED (it is in the transcript the model reads, all
        # three of its segments) and its ops are written, not blanked.
        ctx = _ctx(project, stems=("dev",), chunk_lines=4)
        calls = _run(monkeypatch, ctx)
        assert calls[0][0]["content"].count("] mix") == 3
        path = ctx.stage_dir("director") / "mix_director.json"
        assert json.loads(path.read_text(encoding="utf-8"))["ops"]


def _run_failing(monkeypatch, ctx, reply):
    """Run the stage expecting it to fail; return (calls, the error message)."""
    calls: list[list[dict]] = []
    with pytest.raises(PipelineError) as e:
        _run(monkeypatch, ctx, reply=reply, calls=calls)
    return calls, str(e.value)


class TestTheCap:
    def _silent(self, messages, cfg):
        """A model that reviews nothing: every turn is a legal no-op reply."""
        first, last = _asked(messages[-1]["content"]) or (1, 1)
        return json.dumps({"range": [first, last], "reviewed_through": 1, "ops": []})

    def test_it_is_two_turns_per_chunk(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        lines = len(_view(ctx).lines)
        calls, _error = _run_failing(monkeypatch, ctx, self._silent)
        assert len(calls) == -(-lines // 4) * 2

    def test_the_ops_are_written_before_it_raises(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        _run_failing(monkeypatch, ctx, self._one_op)
        for stem in ("dev", "mix"):
            path = ctx.stage_dir("director") / f"{stem}_director.json"
            assert path.is_file()
        ops = json.loads(
            (ctx.stage_dir("director") / "dev_director.json").read_text(encoding="utf-8")
        )["ops"]
        assert ops == [{"type": "cut", "lines": [1, 1], "note": "x"}]

    def _one_op(self, messages, cfg):
        """One op on the first turn, then no progress at all.

        Later turns must not own line 1 again — a reply owns its range, so
        re-sending [1, 1] with no ops would delete the very op under test.
        """
        first, _last = _asked(messages[-1]["content"]) or (1, 1)
        if first == 1:
            return json.dumps(
                {
                    "range": [1, 1],
                    "reviewed_through": 1,
                    "ops": [{"type": "cut", "lines": [1, 1], "note": "x"}],
                }
            )
        return json.dumps({"range": [2, 2], "reviewed_through": 1, "ops": []})

    def test_the_error_names_the_last_reviewed_line_and_the_files(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        _calls, message = _run_failing(monkeypatch, ctx, self._silent)
        assert "line 1" in message
        assert "dev_director.json" in message and "mix_director.json" in message


class TestATurnThatNeverParses:
    def _garbage(self, messages, cfg):
        return "sorry, no JSON today"

    def test_it_writes_what_was_accepted_then_raises(self, project, monkeypatch):
        ctx = _ctx(project)
        _run_failing(monkeypatch, ctx, self._garbage)
        assert (ctx.stage_dir("director") / "dev_director.json").is_file()
        assert (ctx.stage_dir("director") / "mix_director.json").is_file()


class TestRecording:
    def test_one_unit_and_one_attempt_per_turn(self, project, monkeypatch):
        rec = _Rec()
        monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: rec)
        calls = _run(monkeypatch, _ctx(project, chunk_lines=4))
        assert rec.units == ["director"]
        llm = [a for a in rec.attempts if not a.get("deterministic")]
        assert len(llm) == len(calls)
        assert [a["unit"] for a in llm] == ["director"] * len(calls)

    def test_a_turn_records_only_what_it_added(self, project, monkeypatch):
        rec = _Rec()
        monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: rec)
        _run(monkeypatch, _ctx(project, chunk_lines=4))
        llm = [a for a in rec.attempts if not a.get("deterministic")]
        # Not the growing transcript: one message, this turn's request.
        assert all(len(a["messages"]) == 1 for a in llm)
        assert all(a["messages"][0]["role"] == "user" for a in llm)
        assert llm[1]["response"] is not None


# --- the project context in the cached prefix ---------------------------------


ORDER = [
    {"stem": "dev"},
    {"stem": "mix", "lines": [4, 6]},
    {"stem": "mix", "lines": [1, 3]},
    {"stem": "mix", "lines": [7, 9]},
]


@pytest.fixture
def planned(project):
    """The same project, with a summary and a plan that has real directions."""
    out = project / "out"
    (out / "summary").mkdir(parents=True, exist_ok=True)
    (out / "summary" / "summary.json").write_text(
        json.dumps(
            {
                "summary": "ポンプの修理",
                "parts": [{"stem": "mix", "lines": [1, 9], "summary": "作業"}],
                "video_summaries": {"mix": "mixの全体"},
            }
        ),
        encoding="utf-8",
    )
    (out / "plan" / "plan.json").write_text(
        json.dumps(
            {
                "order": ORDER,
                "directions": [
                    {"stem": "mix", "lines": [4, 6], "direction": "keep the fitting"},
                    {"stem": "dev", "lines": [1, 4], "direction": "trim the preamble"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return project


class TestTheProjectContext:
    """The plan's directions and the project summary, which the conversation
    dropped when it stopped calling ``build_director_context``.

    Without them the model edits a transcript with no brief at all -- and
    without the section-boundaries sentence that travels with them, a measured
    run copied plan boundaries into 19 of 56 op starts and into all three
    timelapses.
    """

    def test_the_overall_summary_reaches_the_model(self, planned, monkeypatch):
        system = _run(monkeypatch, _ctx(planned, chunk_lines=4))[0][0]["content"]
        assert "Overall: ポンプの修理" in system

    def test_a_direction_arrives_in_display_numbers(self, planned, monkeypatch):
        ctx = _ctx(planned, chunk_lines=4)
        system = _run(monkeypatch, ctx)[0][0]["content"]
        view = _view(ctx)
        # mix's source lines 4-6 play SECOND, so they are not display 4-6.
        first = view.from_source(2, 4)
        last = view.from_source(2, 6)
        assert (first, last) != (4, 6)
        assert f"- lines {first}-{last}: keep the fitting" in system
        assert "lines 4-6: keep the fitting" not in system

    def test_the_boundary_note_travels_with_them(self, planned, monkeypatch):
        system = _run(monkeypatch, _ctx(planned, chunk_lines=4))[0][0]["content"]
        marker = "section boundaries, not op boundaries"
        assert marker in system
        assert system.index(marker) < system.index("- lines ")

    def test_it_sits_between_the_prompt_and_the_transcript(self, planned, monkeypatch):
        ctx = _ctx(planned, chunk_lines=4)
        system = _run(monkeypatch, ctx)[0][0]["content"]
        assert system.startswith("P\n")
        assert system.rstrip().endswith(_view(ctx).render().rstrip())
        assert system.index("Overall: ") < system.index(VIEW_HEADER)

    def test_it_is_in_the_cacheable_prefix_and_never_changes(self, planned, monkeypatch):
        calls = _run(monkeypatch, _ctx(planned, chunk_lines=4))
        assert len(calls) > 2
        assert all("Overall: ポンプの修理" in c[0][CACHEABLE_PREFIX_KEY] for c in calls)
        assert len({c[0]["content"] for c in calls}) == 1

    def test_the_project_brief_precedes_it(self, planned, monkeypatch):
        """The editorial brief is appended to the PROMPT, so it lands above the
        project context, which is above the transcript.

        Inherited from the per-segment path, where the brief had to precede the
        summary/plan overview block for the same reason."""
        ctx = _ctx(planned, chunk_lines=4)
        ctx.cfg["project"] = {"audience": "DIY viewers"}
        system = _run(monkeypatch, ctx)[0][0]["content"]
        assert "- Audience: DIY viewers" in system
        assert system.index("- Audience: DIY viewers") < system.index("Overall: ")

    def test_the_revised_plan_wins(self, planned, monkeypatch):
        revised = planned / "out" / "plan_revise"
        revised.mkdir(parents=True, exist_ok=True)
        (revised / "plan.json").write_text(
            json.dumps(
                {
                    "order": ORDER,
                    "directions": [
                        {"stem": "mix", "lines": [4, 6], "direction": "人間が直した指示"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        system = _run(monkeypatch, _ctx(planned, chunk_lines=4))[0][0]["content"]
        assert "人間が直した指示" in system
        assert "keep the fitting" not in system
