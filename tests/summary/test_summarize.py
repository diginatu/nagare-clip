"""Tests for the summary stage (segment + summarise, pure, no network)."""

from __future__ import annotations

from nagare_clip.summary.summarize import (
    PartSummary,
    ProjectSummary,
    build_summary,
    generate_project_summary,
    segment_video,
    summary_from_dict,
    summary_to_dict,
)


def _seq_llm(items, temps=None):
    """Fake call_llm yielding *items* in order; an Exception item is raised."""
    box = {"i": 0}

    def fake(_messages, cfg):
        if temps is not None:
            temps.append(cfg.get("temperature"))
        item = items[box["i"]]
        box["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    fake.calls = box
    return fake


class TestSegmentVideo:
    def test_parses_parts(self):
        resp = (
            '{"parts": ['
            '{"lines": [1, 2], "summary": "intro"},'
            '{"lines": [3, 4], "summary": "demo"}'
            '], "video_summary": "v"}'
        )
        parts, _, _ = segment_video(
            "vid", ["a", "b", "c", "d"], {"prompt": "P"}, call_llm=lambda m, c: resp
        )
        assert parts == [
            PartSummary(stem="vid", lines=(1, 2), summary="intro"),
            PartSummary(stem="vid", lines=(3, 4), summary="demo"),
        ]

    def test_strips_fence(self):
        resp = '```json\n{"parts": [{"lines": [1, 1], "summary": "x"}], "video_summary": "v"}\n```'
        parts, _, _ = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert parts == [PartSummary(stem="v", lines=(1, 1), summary="x")]

    def test_out_of_range_lines_dropped(self):
        resp = (
            '{"parts": [{"lines": [1, 9], "summary": "bad"},{"lines": [1, 2], "summary": "ok"}],'
            ' "video_summary": "v"}'
        )
        parts, _, _ = segment_video(
            "v", ["a", "b", "c"], {"prompt": "P"}, call_llm=lambda m, c: resp
        )
        assert parts == [PartSummary(stem="v", lines=(1, 2), summary="ok")]

    def test_empty_summary_dropped(self):
        resp = '{"parts": [{"lines": [1, 1], "summary": ""}], "video_summary": "v"}'
        parts, _, _ = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert parts == []

    def test_uses_clean_numbered_input(self):
        captured = {}

        def fake(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"parts": [{"lines": [1, 2], "summary": "s"}], "video_summary": "v"}'

        segment_video("v", ["あ", "い"], {"prompt": "P"}, call_llm=fake)
        assert captured["user"] == "1: あ\n2: い"

    def test_llm_error_returns_empty(self):
        def boom(m, c):
            raise ConnectionError("down")

        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=boom) == ([], [], "")

    def test_unparseable_returns_empty(self):
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: "junk") == (
            [],
            [],
            "",
        )

    def test_retries_then_succeeds(self):
        fake = _seq_llm(
            ["junk", '{"parts": [{"lines": [1, 1], "summary": "s"}], "video_summary": "v"}']
        )
        parts, _, _ = segment_video("v", ["a"], {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert fake.calls["i"] == 2
        assert parts[0].summary == "s"

    def test_returns_video_summary(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}],'
            ' "keywords": [], "video_summary": "whole video overview"}'
        )
        parts, keywords, vsum = segment_video(
            "v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp
        )
        assert parts == [PartSummary(stem="v", lines=(1, 1), summary="s")]
        assert vsum == "whole video overview"

    def test_missing_video_summary_is_hard_failure(self):
        # No video_summary at all -> retried, then degraded to empty.
        resp = '{"parts": [{"lines": [1, 1], "summary": "s"}], "keywords": []}'
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp) == (
            [],
            [],
            "",
        )

    def test_empty_video_summary_is_hard_failure(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}], "keywords": [], "video_summary": "   "}'
        )
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp) == (
            [],
            [],
            "",
        )

    def test_non_string_video_summary_is_hard_failure(self):
        resp = '{"parts": [{"lines": [1, 1], "summary": "s"}], "keywords": [], "video_summary": 5}'
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp) == (
            [],
            [],
            "",
        )


class TestSegmentVideoKeywords:
    def test_parses_keywords_stripped(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}],'
            ' "keywords": [" Kubernetes ", "PostgreSQL"], "video_summary": "v"}'
        )
        parts, keywords, _ = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert [p.summary for p in parts] == ["s"]
        assert keywords == ["Kubernetes", "PostgreSQL"]

    def test_missing_keywords_is_empty_list(self):
        resp = '{"parts": [{"lines": [1, 1], "summary": "s"}], "video_summary": "v"}'
        _, keywords, _ = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert keywords == []

    def test_malformed_keyword_entries_dropped(self):
        resp = '{"parts": [], "keywords": ["ok", 42, "", null], "video_summary": "v"}'
        _, keywords, _ = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert keywords == ["ok"]

    def test_keywords_non_list_is_empty(self):
        resp = '{"parts": [], "keywords": "not a list", "video_summary": "v"}'
        _, keywords, _ = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert keywords == []


class TestProjectKeywords:
    def test_build_summary_collects_keywords_by_stem_omitting_empty(self):
        responses = iter(
            [
                '{"parts": [{"lines": [1, 1], "summary": "sa"}], "keywords": ["KWA"],'
                ' "video_summary": "v"}',
                '{"parts": [{"lines": [1, 1], "summary": "sb"}], "keywords": [],'
                ' "video_summary": "v"}',
                '{"summary": "all"}',
            ]
        )
        ps = build_summary(
            [("a", ["x"]), ("b", ["y"])], {"prompt": "P"}, call_llm=lambda m, c: next(responses)
        )
        assert ps.keywords == {"a": ["KWA"]}

    def test_to_dict_includes_keywords(self):
        ps = ProjectSummary(summary="s", parts=[], keywords={"a": ["K"]})
        assert summary_to_dict(ps)["keywords"] == {"a": ["K"]}

    def test_to_dict_empty_keywords_present(self):
        assert summary_to_dict(ProjectSummary(summary="", parts=[]))["keywords"] == {}

    def test_from_dict_round_trip_keywords(self):
        ps = ProjectSummary(
            summary="s", parts=[PartSummary("a", (1, 2), "x")], keywords={"a": ["K"]}
        )
        assert summary_from_dict(summary_to_dict(ps)).keywords == {"a": ["K"]}

    def test_from_dict_missing_keywords_is_empty(self):
        assert summary_from_dict({"summary": "s", "parts": []}).keywords == {}

    def test_from_dict_malformed_keywords_dropped(self):
        data = {
            "summary": "s",
            "parts": [],
            "keywords": {"a": ["ok", ""], "b": "not-a-list", "c": []},
        }
        assert summary_from_dict(data).keywords == {"a": ["ok"]}


class TestVideoSummaries:
    def test_build_summary_collects_video_summaries(self):
        responses = iter(
            [
                '{"parts": [{"lines": [1, 1], "summary": "p"}],'
                ' "keywords": [], "video_summary": "vid-A overview"}',
                '{"summary": "project"}',  # reduce
            ]
        )
        project = build_summary(
            [("A", ["a"])],
            {"prompt": "P", "overall_prompt": "O"},
            call_llm=lambda m, c: next(responses),
        )
        assert project.video_summaries == {"A": "vid-A overview"}

    def test_to_dict_includes_video_summaries(self):
        ps = ProjectSummary(
            summary="s",
            parts=[PartSummary(stem="A", lines=(1, 1), summary="p")],
            video_summaries={"A": "overview"},
        )
        d = summary_to_dict(ps)
        assert d["video_summaries"] == {"A": "overview"}

    def test_from_dict_reads_video_summaries(self):
        d = {
            "summary": "s",
            "parts": [{"stem": "A", "lines": [1, 1], "summary": "p"}],
            "video_summaries": {"A": "overview", "B": 5, "": "x"},
        }
        ps = summary_from_dict(d)
        assert ps.video_summaries == {"A": "overview"}

    def test_from_dict_old_file_without_video_summaries(self):
        d = {"summary": "s", "parts": [{"stem": "A", "lines": [1, 1], "summary": "p"}]}
        ps = summary_from_dict(d)
        assert ps.video_summaries == {}

    def test_round_trip(self):
        ps = ProjectSummary(
            summary="s",
            parts=[PartSummary(stem="A", lines=(1, 2), summary="p")],
            keywords={"A": ["kw"]},
            video_summaries={"A": "overview"},
        )
        assert summary_from_dict(summary_to_dict(ps)).video_summaries == {"A": "overview"}


class TestGenerateProjectSummary:
    def test_parses_overall_summary(self):
        parts = [PartSummary("v", (1, 2), "intro")]
        out = generate_project_summary(
            parts, {"overall_prompt": "P"}, call_llm=lambda m, c: '{"summary": "all"}'
        )
        assert out == "all"

    def test_empty_parts_returns_empty_without_call(self):
        called = {"n": 0}

        def fake(m, c):
            called["n"] += 1
            return '{"summary": "x"}'

        assert generate_project_summary([], {"overall_prompt": "P"}, call_llm=fake) == ""
        assert called["n"] == 0

    def test_llm_failure_returns_empty(self):
        parts = [PartSummary("v", (1, 1), "s")]

        def boom(m, c):
            raise ConnectionError("x")

        assert generate_project_summary(parts, {"overall_prompt": "P"}, call_llm=boom) == ""


class TestOverallInputFormat:
    def test_groups_parts_by_video_with_summary_header(self):
        captured = {}

        def fake(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"summary": "ok"}'

        parts = [
            PartSummary(stem="A", lines=(1, 12), summary="a1"),
            PartSummary(stem="A", lines=(13, 20), summary="a2"),
            PartSummary(stem="B", lines=(1, 5), summary="b1"),
        ]
        generate_project_summary(
            parts,
            {"overall_prompt": "O"},
            call_llm=fake,
            video_summaries={"A": "video A overview", "B": "video B overview"},
        )
        assert captured["user"] == (
            "## A — video A overview\n"
            "1: [1-12] — a1\n"
            "2: [13-20] — a2\n"
            "## B — video B overview\n"
            "3: [1-5] — b1"
        )


class TestBuildSummary:
    def test_map_then_reduce(self):
        # 2 videos -> segment each, then 1 overall call.
        seq = _seq_llm(
            [
                '{"parts": [{"lines": [1, 1], "summary": "a-intro"}], "video_summary": "v"}',
                '{"parts": [{"lines": [1, 2], "summary": "b-body"}], "video_summary": "v"}',
                '{"summary": "whole project"}',
            ]
        )
        ps = build_summary([("a", ["x"]), ("b", ["y", "z"])], {"prompt": "P"}, call_llm=seq)
        assert ps.summary == "whole project"
        assert ps.parts == [
            PartSummary(stem="a", lines=(1, 1), summary="a-intro"),
            PartSummary(stem="b", lines=(1, 2), summary="b-body"),
        ]


import yaml as _yaml

from nagare_clip.llm_report import Recorder


def _outcome(tmp_path, unit):
    text = (tmp_path / "summary" / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)["outcome"]


class TestSummaryRecorder:
    def test_segment_records_ok(self, tmp_path):
        rec = Recorder("summary", tmp_path, enabled=True)
        resp = '{"parts":[{"lines":[1,2],"summary":"s"}], "video_summary": "v"}'

        def fake(_m, _c):
            return resp

        parts, _, _ = segment_video(
            "vid", ["a", "b"], {"max_retries": 0}, call_llm=fake, recorder=rec
        )
        assert len(parts) == 1
        assert _outcome(tmp_path, "vid") == "ok"

    def test_segment_records_dropped_items(self, tmp_path):
        rec = Recorder("summary", tmp_path, enabled=True)
        resp = (
            '{"parts":[{"lines":[1,2],"summary":"s"},{"lines":[9,9],"summary":"x"}],'
            ' "video_summary": "v"}'
        )

        def fake(_m, _c):
            return resp

        parts, _, _ = segment_video(
            "vid", ["a", "b"], {"max_retries": 0}, call_llm=fake, recorder=rec
        )
        assert len(parts) == 1
        assert _outcome(tmp_path, "vid") == "dropped-items"

    def test_overall_records_ok(self, tmp_path):
        rec = Recorder("summary", tmp_path, enabled=True)

        def fake(_m, _c):
            return '{"summary":"all"}'

        parts = [PartSummary(stem="v", lines=(1, 2), summary="p")]
        out = generate_project_summary(parts, {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert out == "all"
        assert _outcome(tmp_path, "overall") == "ok"


class TestRoundTrip:
    def test_to_from_dict(self):
        ps = ProjectSummary(
            summary="all",
            parts=[
                PartSummary("a", (1, 4), "intro"),
                PartSummary("b", (2, 9), "body"),
            ],
        )
        d = summary_to_dict(ps)
        assert d == {
            "summary": "all",
            "parts": [
                {"stem": "a", "lines": [1, 4], "summary": "intro"},
                {"stem": "b", "lines": [2, 9], "summary": "body"},
            ],
            "keywords": {},
            "video_summaries": {},
        }
        assert summary_from_dict(d) == ps

    def test_from_dict_tolerates_garbage(self):
        assert summary_from_dict("nope") == ProjectSummary(summary="", parts=[])
        assert summary_from_dict({}) == ProjectSummary(summary="", parts=[])


class TestPartTimes:
    def test_build_summary_attaches_part_times(self):
        resp = '{"parts": [{"lines": [1, 2], "summary": "intro"}], "video_summary": "v"}'
        overall = '{"summary": "S"}'
        seg_times = {"v": [(1.0, 3.0), (4.0, 6.5)]}
        project = build_summary(
            [("v", ["あ", "い"])],
            {"prompt": "p", "overall_prompt": "o"},
            call_llm=_seq_llm([resp, overall]),
            seg_times_by_stem=seg_times,
        )
        p = project.parts[0]
        assert p.start == 1.0 and p.end == 6.5

    def test_build_summary_without_seg_times_leaves_none(self):
        resp = '{"parts": [{"lines": [1, 2], "summary": "intro"}], "video_summary": "v"}'
        overall = '{"summary": "S"}'
        project = build_summary(
            [("v", ["あ", "い"])],
            {"prompt": "p", "overall_prompt": "o"},
            call_llm=_seq_llm([resp, overall]),
        )
        assert project.parts[0].start is None
        assert project.parts[0].end is None

    def test_to_dict_omits_none_times(self):
        ps = ProjectSummary(
            summary="S",
            parts=[PartSummary(stem="v", lines=(1, 2), summary="x")],
        )
        d = summary_to_dict(ps)
        assert "start" not in d["parts"][0]
        assert "end" not in d["parts"][0]

    def test_to_dict_includes_times_when_set(self):
        ps = ProjectSummary(
            summary="S",
            parts=[PartSummary(stem="v", lines=(1, 2), summary="x", start=1.0, end=6.5)],
        )
        d = summary_to_dict(ps)
        assert d["parts"][0]["start"] == 1.0
        assert d["parts"][0]["end"] == 6.5

    def test_from_dict_round_trip_times(self):
        data = {
            "summary": "S",
            "parts": [{"stem": "v", "lines": [1, 2], "summary": "x", "start": 1.0, "end": 6.5}],
        }
        ps = summary_from_dict(data)
        assert ps.parts[0].start == 1.0 and ps.parts[0].end == 6.5

    def test_from_dict_missing_times_are_none(self):
        data = {"summary": "S", "parts": [{"stem": "v", "lines": [1, 2], "summary": "x"}]}
        ps = summary_from_dict(data)
        assert ps.parts[0].start is None and ps.parts[0].end is None
