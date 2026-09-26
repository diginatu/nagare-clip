"""The director as ONE conversation over the whole finished video.

Driven through the real stage with only the LLM faked, because what matters
here is the assembled conversation — what is cached, what each turn carries,
what reaches disk when it ends badly — not any one function's return value.

The project is the same shape as ``test_director_whole_video``'s: ``dev``
whole, then ``mix`` split into three stretches and reordered by the plan.  The
view is shooting order whatever the plan says — one ``[k]`` block per source —
and the plan's order is the conversation's starting order, not its shape.
"""

from __future__ import annotations

import json
import re

import pytest

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.config import get_effective_config
from nagare_clip.director import conversation
from nagare_clip.director import director_llm as dl
from nagare_clip.director.display import build_display_view
from nagare_clip.director.loop import PLAN_REQUEST
from nagare_clip.director.preview import STATE_HEADER
from nagare_clip.director.run import VIEW_HEADER, SegmentInputs, load_segment_transcript
from nagare_clip.llm_client import CACHEABLE_PREFIX_KEY
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineError, PipelineStop
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
    """The view the stage will build: every source whole, in shooting order."""
    segments = st.identity_segments(st.project_stems(ctx.input_videos_dir))
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
        # The planning turn is answered here for every scripted model: each
        # test's own script only has to know about ranges.
        if PLAN_REQUEST in messages[-1]["content"]:
            out = json.dumps({"plan": "PLAN"})
            replies.append(out)
            return out
        out = (reply or script)(messages, cfg)
        replies.append(out)
        return out

    monkeypatch.setattr(dl, "_call_llm", fake)
    next(s for s in st.STAGES if s.name == "director").run(ctx)
    return calls


HISTORY_LINE = re.compile(
    r"^Guide: (Write the plan\.|Review around lines \d+ to \d+\.|Every line has been reviewed\.)$"
)


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
        assert first[-1]["content"].startswith(f"Guide: {STATE_HEADER}")
        assert "(no ops: every line plays its default)" in first[-1]["content"]

    def test_the_state_covers_every_segment_on_every_turn(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4)
        calls = _run(monkeypatch, ctx)
        segments = len(_view(ctx).segments)
        assert segments == 2
        for call in calls:
            newest = call[-1]["content"]
            for index in range(1, segments + 1):
                assert f"segment [{index}] " in newest

    def test_the_state_carries_the_whole_video_runtime_and_the_captions(self, project, monkeypatch):
        newest = _run(monkeypatch, _ctx(project, chunk_lines=4))[-1][-1]["content"]
        assert "whole video so far" in newest
        assert "Captions in playback order" in newest

    def test_the_state_prices_what_the_model_just_sent(self, project, monkeypatch):
        second = _run(monkeypatch, _ctx(project, chunk_lines=4))[2][-1]["content"]
        assert "cut [1,1]" in second
        assert "whole video so far" in second

    def test_an_op_note_survives_into_the_next_turns_state(self, project, monkeypatch):
        second = _run(monkeypatch, _ctx(project, chunk_lines=4))[2][-1]["content"]
        assert "note: x" in second

    def test_a_refusal_reaches_the_model_above_the_state(self, project, monkeypatch):
        """A refusal is about the reply just sent, not about the edit, so it
        leads — and it is the only thing that tells the model its `done` (or
        its join-crossing op) was not taken."""
        seen = {"done": False}

        def script(messages, cfg):
            if not seen["done"]:
                seen["done"] = True
                return json.dumps({"done": True})
            asked = _asked(messages[-1]["content"])
            if asked is None:
                return json.dumps({"done": True})
            return json.dumps({"range": list(asked), "reviewed_through": asked[1], "ops": []})

        calls = _run(monkeypatch, _ctx(project, chunk_lines=4), reply=script)
        second = calls[2][-1]["content"]
        assert "not done: lines 1-" in second
        assert second.index("not done") < second.index(STATE_HEADER)

    def test_a_parser_drop_reaches_the_next_turns_state(self, project, monkeypatch):
        def script(messages, cfg):
            asked = _asked(messages[-1]["content"])
            if asked is None:
                return json.dumps({"done": True})
            first, last = asked
            return json.dumps(
                {
                    "range": [first, last],
                    "reviewed_through": last,
                    "ops": [{"type": "sparkle", "lines": [first, first], "note": "x"}],
                }
            )

        second = _run(monkeypatch, _ctx(project, chunk_lines=4), reply=script)[2][-1]["content"]
        assert "dropped by the parser (no effect):" in second


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
        # video: mix is REVIEWED (it is in the transcript the model reads) and
        # its ops are written, not blanked.
        ctx = _ctx(project, stems=("dev",), chunk_lines=4)
        calls = _run(monkeypatch, ctx)
        assert calls[0][0]["content"].count("] mix") == 1
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
        assert len(calls) == -(-lines // 4) * 2 + 1  # + the planning turn

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

    def test_the_error_names_the_last_reviewed_line_and_says_to_run_again(
        self, project, monkeypatch
    ):
        ctx = _ctx(project, chunk_lines=4)
        _calls, message = _run_failing(monkeypatch, ctx, self._silent)
        assert "line 1" in message
        assert "run the director again to continue" in message


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
    """The summary stage's facts, in the cached prefix: the overall summary and
    each source's whole-video summary.  The plan stage's directions are not
    shown — the director's first turn writes its own plan."""

    def test_the_overall_summary_reaches_the_model(self, planned, monkeypatch):
        system = _run(monkeypatch, _ctx(planned, chunk_lines=4))[0][0]["content"]
        assert "Overall: ポンプの修理" in system

    def test_the_plan_stages_directions_are_not_shown(self, planned, monkeypatch):
        """The director writes its own plan; the plan stage's directions —
        the least-informed opinion — no longer frame it."""
        system = _run(monkeypatch, _ctx(planned, chunk_lines=4))[0][0]["content"]
        assert "keep the fitting" not in system
        assert "trim the preamble" not in system
        assert "section boundaries, not op boundaries" not in system

    def test_each_sources_summary_reaches_the_model(self, planned, monkeypatch):
        system = _run(monkeypatch, _ctx(planned, chunk_lines=4))[0][0]["content"]
        assert "[2] mix: mixの全体" in system

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


def test_the_stage_prices_brackets_with_the_projects_intervals_settings(project, monkeypatch):
    # A line's "Ys silence" is what intervals drops, so the director must be
    # handed the SAME intervals: section the intervals stage will run with --
    # not the defaults, which on a real project differ in every margin.
    from nagare_clip.director.run import ConversationResult

    ctx = _ctx(project)
    ctx.cfg["intervals"]["keep_pre_margin"] = 0.123
    seen = []

    def fake(inputs, cfg, **kwargs):
        seen.extend(inputs)
        return ConversationResult(ops={})

    monkeypatch.setattr(st, "run_director_conversation", fake)
    st._director_run(ctx)
    assert seen
    assert all(i.intervals_cfg["keep_pre_margin"] == 0.123 for i in seen)


def test_every_turn_is_traced_as_the_director(project, monkeypatch):
    """Langfuse groups calls by the ``_trace`` the stage threads into cfg; a
    turn sent without it lands ungrouped and unnamed."""
    seen: list[dict] = []

    def reply(messages, cfg):
        seen.append(cfg)
        asked = _asked(messages[-1]["content"])
        if asked is None:
            return json.dumps({"done": True})
        return json.dumps({"range": list(asked), "reviewed_through": asked[1], "ops": []})

    _run(monkeypatch, _ctx(project), reply=reply)
    assert seen
    assert all(c["_trace"]["generation_name"] == "director/director" for c in seen)


def _answer(messages):
    """A usable reply to whatever the newest message asks."""
    asked = _asked(messages[-1]["content"])
    if asked is None:
        return json.dumps({"done": True})
    return json.dumps({"range": list(asked), "reviewed_through": asked[1], "ops": []})


class TestRetryWithinATurn:
    """A turn's call is retried (``director.max_retries``) on a failed call or
    an unusable reply; only when every attempt fails does the stage fail."""

    def _flaky(self, first):
        """Reply *first* (raise it if it is an exception) once, then answer."""
        seen: list[tuple[list[dict], dict]] = []

        def reply(messages, cfg):
            seen.append((messages, cfg))
            if len(seen) == 1:
                if isinstance(first, Exception):
                    raise first
                return first
            return _answer(messages)

        return reply, seen

    def test_a_failed_call_is_retried_with_the_same_ask(self, project, monkeypatch):
        reply, seen = self._flaky(ConnectionError("down"))
        _run(monkeypatch, _ctx(project, max_retries=2), reply=reply)
        assert seen[1][0] == seen[0][0]

    def test_an_unusable_reply_is_retried_with_its_error_attached(self, project, monkeypatch):
        reply, seen = self._flaky("not json")
        _run(monkeypatch, _ctx(project, max_retries=2), reply=reply)
        first, retry = seen[0][0][-1]["content"], seen[1][0][-1]["content"]
        assert retry.startswith(first)
        assert "That reply could not be used:" in retry[len(first) :]

    def test_a_retry_nudges_the_temperature_up(self, project, monkeypatch):
        reply, seen = self._flaky(ConnectionError("down"))
        ctx = _ctx(project, max_retries=2, temperature=0.2, retry_temp_step=0.3)
        _run(monkeypatch, ctx, reply=reply)
        assert seen[0][1]["temperature"] == pytest.approx(0.2)
        assert seen[1][1]["temperature"] == pytest.approx(0.5)

    def test_every_attempt_failing_fails_the_stage(self, project, monkeypatch):
        calls = []

        def boom(messages, cfg):
            raise ConnectionError("down")

        with pytest.raises(PipelineError, match="failed after all 3 attempt"):
            _run(monkeypatch, _ctx(project, max_retries=2), reply=boom, calls=calls)
        assert len(calls) == 1 + 3  # the plan, then one turn's three attempts


class TestTheOrder:
    """The director decides the order, seeded with the plan's, and writes it to
    ``director/order.json`` — which every later reader then prefers."""

    def _order_json(self, ctx):
        return json.loads((ctx.stage_dir("director") / "order.json").read_text(encoding="utf-8"))

    def test_the_plans_order_passes_through_when_the_model_never_sends_one(
        self, project, monkeypatch
    ):
        ctx = _ctx(project, chunk_lines=40)
        _run(monkeypatch, ctx)
        assert self._order_json(ctx)["order"] == [
            {"stem": "dev"},
            {"stem": "mix", "lines": [4, 6]},
            {"stem": "mix", "lines": [1, 3]},
            {"stem": "mix", "lines": [7, 9]},
        ]

    def test_the_seed_reaches_the_model_as_its_starting_order(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40)
        first = _run(monkeypatch, ctx)[0][-1]["content"]
        assert "THE VIDEO AS IT PLAYS" in first

    def test_the_models_order_wins(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40)
        total = len(_view(ctx).lines)

        def script(messages, cfg):
            if _asked(messages[-1]["content"]) is None:
                return json.dumps({"done": True})
            return json.dumps(
                {"range": [1, total], "reviewed_through": total, "ops": [], "order": [[1, total]]}
            )

        _run(monkeypatch, ctx, reply=script)
        assert self._order_json(ctx)["order"] == [{"stem": "dev"}, {"stem": "mix"}]
        assert st._timeline_segments(ctx) == [st.Segment("dev"), st.Segment("mix")]

    def test_a_disabled_director_still_writes_the_seed(self, project, monkeypatch):
        ctx = _ctx(project, enabled=False)
        _run(monkeypatch, ctx)
        assert len(self._order_json(ctx)["order"]) == 4

    def test_the_directors_order_beats_the_plans(self, project):
        ctx = _ctx(project)
        (ctx.stage_dir("director")).mkdir(parents=True, exist_ok=True)
        (ctx.stage_dir("director") / "order.json").write_text(
            json.dumps({"order": [{"stem": "mix"}, {"stem": "dev"}]}), encoding="utf-8"
        )
        assert st._timeline_segments(ctx) == [st.Segment("mix"), st.Segment("dev")]
        # ...but never for the director's own seed.
        assert len(st._resolve_order(ctx, director=False)[0]) == 4


def test_the_view_header_says_shooting_order(project, monkeypatch):
    # The plan reorders this project, and the transcript is still shooting
    # order: a header claiming playback order would be a lie the model reads.
    system = _run(monkeypatch, _ctx(project, chunk_lines=40))[0][0]["content"]
    assert "shooting order" in system[system.index(VIEW_HEADER) :][:200]
    assert "playback order under one numbering" not in system


class TestTheDirectory:
    """The directory is the state: a run is one step over it."""

    def _director(self, ctx):
        return ctx.stage_dir("director")

    def test_a_finished_run_leaves_a_done_mark_and_every_state_file(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40)
        _run(monkeypatch, ctx)
        d = self._director(ctx)
        for name in ("plan.md", "order.json", "dev_director.json", "conversation.md"):
            assert (d / name).is_file(), name
        entries = conversation.load(d / "conversation.md")
        assert conversation.is_done(entries)
        assert entries[0] == conversation.Entry(conversation.GUIDE, "Write the plan.")

    def test_a_done_directory_makes_no_call_and_touches_no_file(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40)
        _run(monkeypatch, ctx)
        d = self._director(ctx)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
        assert _run(monkeypatch, ctx) == []
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
        assert after == before

    def test_an_editor_entry_resumes_the_conversation(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40)
        _run(monkeypatch, ctx)
        conversation.say(self._director(ctx) / "conversation.md", "冒頭は残して")
        calls = _run(monkeypatch, ctx)
        assert len(calls) == 1  # all reviewed already: one turn, the model says done
        newest = calls[0][-1]["content"]
        assert newest.startswith("Editor: 冒頭は残して\n\nGuide: ")
        assert "Every line" in newest
        # the earlier turns are the history it reads
        assert [m["role"] for m in calls[0][1:3]] == ["user", "assistant"]

    def test_a_hand_edited_plan_is_the_plan_in_force(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40)
        _run(monkeypatch, ctx)
        d = self._director(ctx)
        (d / "plan.md").write_text("# The director's plan\n\n人が書いた方針\n", encoding="utf-8")
        conversation.say(d / "conversation.md", "方針を直した")
        newest = _run(monkeypatch, ctx)[0][-1]["content"]
        assert "YOUR PLAN (in force" in newest and "人が書いた方針" in newest

    def test_a_failed_run_keeps_its_turns_and_the_next_run_continues(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=4, max_retries=0)
        total = len(_view(ctx).lines)
        broken = {"on": True}

        def script(messages, cfg):
            asked = _asked(messages[-1]["content"])
            if asked is None:
                return json.dumps({"done": True})
            first, last = asked
            if first > 4 and broken["on"]:
                return "not json"
            return json.dumps(
                {"range": [first, last], "reviewed_through": last, "ops": [_cut_op(first)]}
            )

        with pytest.raises(PipelineError, match="run the director again"):
            _run(monkeypatch, ctx, reply=script)
        d = self._director(ctx)
        entries = conversation.load(d / "conversation.md")
        assert conversation.reviewed_through(entries) == 4
        assert not conversation.is_done(entries)
        broken["on"] = False
        calls = _run(monkeypatch, ctx, reply=script)
        assert "around lines 5 to" in calls[0][-1]["content"]  # no plan turn again
        assert conversation.reviewed_through(conversation.load(d / "conversation.md")) == total

    def test_pause_after_plan_stops_cleanly_with_the_mark_written(self, project, monkeypatch):
        ctx = _ctx(project, chunk_lines=40, pause_after_plan=True)
        calls: list = []
        with pytest.raises(PipelineStop, match="plan.md"):
            _run(monkeypatch, ctx, calls=calls)
        assert len(calls) == 1
        d = self._director(ctx)
        assert (d / "plan.md").read_text(encoding="utf-8").endswith("PLAN\n")
        assert conversation.is_done(conversation.load(d / "conversation.md"))
        # Deleting the mark continues, and the plan already exists: no pause again.
        conversation.say(d / "conversation.md", "この方針でいい")
        _run(monkeypatch, ctx)
        assert conversation.is_done(conversation.load(d / "conversation.md"))


def _cut_op(line):
    return {"type": "cut", "lines": [line, line], "note": "x"}
