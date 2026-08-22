"""plan_dialogue/history.md: a forgiving markdown conversation log."""

from __future__ import annotations

from datetime import datetime

from nagare_clip.plan.dialogue import (
    DialogueTurn,
    active_turns,
    append_divider,
    append_reply_slot,
    append_turn,
    format_divider,
    has_unanswered_human,
    parse_history,
    read_active_history,
    read_history,
    render_history,
)


class TestParse:
    def test_splits_role_headings_in_order(self):
        text = "## human\n\nsplit part 7\n\n## plan\n\nsplit it\n"
        assert parse_history(text) == [
            DialogueTurn("human", "split part 7"),
            DialogueTurn("plan", "split it"),
        ]

    def test_leading_text_is_a_human_turn(self):
        # Someone opens the file and types; that must still be heard.
        assert parse_history("just do it\n") == [DialogueTurn("human", "just do it")]

    def test_html_comments_are_stripped(self):
        text = "<!-- how to write a turn -->\n## human\n\nhi\n"
        assert parse_history(text) == [DialogueTurn("human", "hi")]

    def test_unknown_heading_stays_inside_the_turn(self):
        text = "## human\n\n### notes\n\nkeep this\n"
        assert parse_history(text) == [DialogueTurn("human", "### notes\n\nkeep this")]

    def test_heading_may_carry_extra_words(self):
        assert parse_history("## human (2026-08-22)\nhi\n") == [DialogueTurn("human", "hi")]

    def test_blank_turns_dropped(self):
        assert parse_history("## human\n\n## plan\n\nok\n") == [DialogueTurn("plan", "ok")]

    def test_empty_text(self):
        assert parse_history("") == []
        assert parse_history("   \n\n") == []


class TestReadWrite:
    def test_read_missing_or_none(self, tmp_path):
        assert read_history(None) == []
        assert read_history(tmp_path / "nope.md") == []

    def test_append_creates_file_and_round_trips(self, tmp_path):
        path = tmp_path / "plan_dialogue" / "history.md"
        append_turn(path, "human", "split part 7")
        append_turn(path, "plan", "done")
        assert read_history(path) == [
            DialogueTurn("human", "split part 7"),
            DialogueTurn("plan", "done"),
        ]

    def test_append_keeps_hand_written_text(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text("## human\n\ntyped by hand\n", encoding="utf-8")
        append_turn(path, "plan", "heard")
        assert read_history(path) == [
            DialogueTurn("human", "typed by hand"),
            DialogueTurn("plan", "heard"),
        ]


def test_render_history_labels_each_turn():
    out = render_history([DialogueTurn("human", "a"), DialogueTurn("plan", "b")])
    assert out == "## human\n\na\n\n## plan\n\nb"


class TestCli:
    def test_appends_a_human_turn(self, tmp_path, capsys):
        from nagare_clip.plan.dialogue import main

        assert main(["--output-dir", str(tmp_path), "part 7 is mixed"]) == 0
        assert read_history(tmp_path / "plan_dialogue" / "history.md") == [
            DialogueTurn("human", "part 7 is mixed")
        ]
        assert "history.md" in capsys.readouterr().out

    def test_joins_several_words(self, tmp_path):
        from nagare_clip.plan.dialogue import main

        main(["--output-dir", str(tmp_path), "part", "7", "is", "mixed"])
        assert read_history(tmp_path / "plan_dialogue" / "history.md") == [
            DialogueTurn("human", "part 7 is mixed")
        ]

    def test_reads_stdin_when_no_text_given(self, tmp_path, monkeypatch):
        import io

        from nagare_clip.plan.dialogue import main

        monkeypatch.setattr("sys.stdin", io.StringIO("from a pipe\n"))
        assert main(["--output-dir", str(tmp_path)]) == 0
        assert read_history(tmp_path / "plan_dialogue" / "history.md") == [
            DialogueTurn("human", "from a pipe")
        ]

    def test_empty_text_writes_nothing(self, tmp_path, monkeypatch):
        import io

        from nagare_clip.plan.dialogue import main

        monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
        assert main(["--output-dir", str(tmp_path)]) == 1
        assert not (tmp_path / "plan_dialogue" / "history.md").exists()


class TestDivider:
    """A `plan` re-run divides the conversation instead of clearing it."""

    def test_active_turns_are_those_after_the_last_divider(self):
        text = (
            "## human\n\nold instruction\n\n"
            "## plan\n\nold reply\n\n" + format_divider() + "\n\n## human\n\nnew instruction\n"
        )
        assert parse_history(text) == [
            DialogueTurn("human", "old instruction"),
            DialogueTurn("plan", "old reply"),
            DialogueTurn("human", "new instruction"),
        ]
        assert active_turns(text) == [DialogueTurn("human", "new instruction")]

    def test_only_the_last_divider_counts(self):
        text = f"## human\n\na\n\n{format_divider()}\n\n## human\n\nb\n\n{format_divider()}\n"
        assert active_turns(text) == []

    def test_no_divider_means_every_turn_is_active(self):
        text = "## human\n\na\n"
        assert active_turns(text) == parse_history(text)

    def test_divider_records_when_it_happened(self):
        line = format_divider(datetime(2026, 8, 22, 19, 4))
        assert line.startswith("---") and line.endswith("---")
        assert "2026-08-22T19:04" in line
        assert "no longer apply" in line

    def test_append_divider_retires_what_came_before(self, tmp_path):
        path = tmp_path / "plan_dialogue" / "history.md"
        append_turn(path, "human", "a")
        append_turn(path, "plan", "b")
        append_divider(path)
        text = path.read_text(encoding="utf-8")
        assert active_turns(text) == []
        assert parse_history(text) == [DialogueTurn("human", "a"), DialogueTurn("plan", "b")]
        # the divider alone: plan appends its account of the new plan below it
        assert text.rstrip().endswith("---")

    def test_append_divider_on_an_empty_history_writes_nothing_new(self, tmp_path):
        path = tmp_path / "plan_dialogue" / "history.md"
        append_divider(path)
        assert path.is_file()  # created, so a human can find where to reply
        first = path.read_text(encoding="utf-8")
        append_divider(path)
        # nothing was said since the last divider: no second divider piles up
        assert path.read_text(encoding="utf-8") == first
        assert first.count("no longer apply") <= 1

    def test_append_divider_after_a_turn_divides_again(self, tmp_path):
        path = tmp_path / "plan_dialogue" / "history.md"
        append_turn(path, "human", "a")
        append_divider(path)
        append_turn(path, "human", "b")
        append_divider(path)
        assert path.read_text(encoding="utf-8").count("no longer apply") == 2
        assert active_turns(path.read_text(encoding="utf-8")) == []

    def test_read_active_history_reads_the_file(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text(f"## human\n\na\n\n{format_divider()}\n\n## human\n\nb\n", encoding="utf-8")
        assert read_active_history(path) == [DialogueTurn("human", "b")]
        assert read_active_history(tmp_path / "nope.md") == []
        assert read_active_history(None) == []


class TestUnansweredHuman:
    def test_last_turn_human_is_unanswered(self):
        assert has_unanswered_human([DialogueTurn("human", "a")])
        assert has_unanswered_human([DialogueTurn("plan", "a"), DialogueTurn("human", "b")])

    def test_answered_or_empty_is_not(self):
        assert not has_unanswered_human([])
        assert not has_unanswered_human([DialogueTurn("human", "a"), DialogueTurn("plan", "b")])


class TestReplySlot:
    """The file must end somewhere a human can type, whatever was appended last."""

    def test_appends_a_human_heading(self, tmp_path):
        path = tmp_path / "history.md"
        append_turn(path, "plan", "here is the plan")
        append_reply_slot(path)
        text = path.read_text(encoding="utf-8")
        assert text.rstrip().endswith("## human")
        # an empty heading is not a turn, so it does not fire plan_revise
        assert active_turns(text) == [DialogueTurn("plan", "here is the plan")]
        assert not has_unanswered_human(active_turns(text))

    def test_does_not_pile_up(self, tmp_path):
        path = tmp_path / "history.md"
        append_turn(path, "plan", "x")
        append_reply_slot(path)
        first = path.read_text(encoding="utf-8")
        append_reply_slot(path)
        assert path.read_text(encoding="utf-8") == first

    def test_creates_the_file(self, tmp_path):
        path = tmp_path / "plan_dialogue" / "history.md"
        append_reply_slot(path)
        assert path.is_file() and path.read_text(encoding="utf-8").rstrip().endswith("## human")

    def test_a_written_turn_still_reads_after_the_slot(self, tmp_path):
        path = tmp_path / "history.md"
        append_turn(path, "plan", "x")
        append_reply_slot(path)
        append_turn(path, "human", "split part 2")
        assert has_unanswered_human(active_turns(path.read_text(encoding="utf-8")))
