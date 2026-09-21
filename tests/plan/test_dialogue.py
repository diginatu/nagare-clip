"""plan_dialogue/history.md: a forgiving markdown conversation log."""

from __future__ import annotations

from datetime import datetime

from nagare_clip.plan.dialogue import (
    FILE_HEADER,
    DialogueTurn,
    active_turns,
    append_divider,
    append_reply_slot,
    append_turn,
    ensure_history,
    format_divider,
    has_unanswered_human,
    parse_history,
    read_active_history,
    read_history,
    refresh_header,
    render_history,
    unanswered_turns,
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


# The header is advice, and the advice changed: improvement 23 made
# "re-run plan to apply a turn" actively wrong.  A project whose history.md
# predates that change must not keep telling its human the old thing.
STALE_HEADER = (
    "<!--\n"
    "plan_dialogue/history.md — the plan_revise stage's conversation with you.\n"
    "\n"
    "Append a turn under a '## human' heading and re-run\n"
    "  ./scripts/run_pipeline.sh --from-stage plan --to-stage plan\n"
    "-->\n"
)


class TestHeaderRefresh:
    def test_stale_header_is_replaced_on_next_write(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text(f"{STALE_HEADER}\n## human\n\nkeep this\n", encoding="utf-8")

        append_turn(path, "plan", "heard")

        text = path.read_text(encoding="utf-8")
        assert FILE_HEADER.strip() in text
        assert "--from-stage plan --to-stage plan" not in text
        # The turns themselves are untouched and not duplicated.
        assert read_history(path) == [
            DialogueTurn("human", "keep this"),
            DialogueTurn("plan", "heard"),
        ]
        assert text.count("keep this") == 1

    def test_refresh_is_idempotent(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text(f"{STALE_HEADER}\n## human\n\nkeep this\n", encoding="utf-8")
        append_turn(path, "plan", "heard")
        first = path.read_text(encoding="utf-8")

        refresh_header(path)
        refresh_header(path)

        assert path.read_text(encoding="utf-8") == first
        assert text_has_one_header(first)

    def test_ensure_history_refreshes_an_existing_file(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text(f"{STALE_HEADER}\n## human\n\nkeep this\n", encoding="utf-8")

        ensure_history(path)

        assert FILE_HEADER.strip() in path.read_text(encoding="utf-8")

    def test_file_without_a_leading_comment_is_left_alone(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text("## human\n\ntyped by hand\n", encoding="utf-8")

        refresh_header(path)

        assert path.read_text(encoding="utf-8") == "## human\n\ntyped by hand\n"

    def test_a_later_comment_is_not_mistaken_for_the_header(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text("## human\n\n<!-- a note -->\nhi\n", encoding="utf-8")

        refresh_header(path)

        assert path.read_text(encoding="utf-8") == "## human\n\n<!-- a note -->\nhi\n"

    def test_missing_or_unreadable_file_does_not_raise(self, tmp_path):
        refresh_header(tmp_path / "nope.md")
        refresh_header(tmp_path)  # a directory: OSError on read


def text_has_one_header(text: str) -> bool:
    return text.count("plan_dialogue/history.md — the plan_revise stage's conversation") == 1


class TestUnansweredTurns:
    """What a ``plan`` re-run would retire, counted for the guard's message."""

    def _write(self, tmp_path, text):
        path = tmp_path / "history.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_all_active_turns_when_the_human_spoke_last(self, tmp_path):
        # The divider retires everything below the previous one, not only the
        # unanswered turn -- so that is what the count has to be.
        path = self._write(
            tmp_path,
            "## human\n\nold\n\n"
            "--- plan re-ran 2026-08-24T01:56 — turns above this line no longer apply ---\n\n"
            "## plan\n\naccount\n\n## human\n\nsplit part 21\n",
        )
        assert unanswered_turns(path) == [
            DialogueTurn("plan", "account"),
            DialogueTurn("human", "split part 21"),
        ]

    def test_empty_when_the_plan_answered_last(self, tmp_path):
        path = self._write(tmp_path, "## human\n\nsplit\n\n## plan\n\ndone\n")
        assert unanswered_turns(path) == []

    def test_empty_when_the_reply_slot_is_still_blank(self, tmp_path):
        # plan leaves a bare '## human' heading to type under; an empty heading
        # is not a turn and must not look like an unapplied instruction.
        path = self._write(tmp_path, "## plan\n\naccount\n\n## human\n")
        assert unanswered_turns(path) == []

    def test_empty_when_the_file_does_not_exist(self, tmp_path):
        assert unanswered_turns(tmp_path / "nope.md") == []

    def test_empty_for_no_path(self):
        assert unanswered_turns(None) == []

    def test_header_comment_is_not_a_turn(self, tmp_path):
        path = self._write(tmp_path, FILE_HEADER + "\n## plan\n\naccount\n")
        assert unanswered_turns(path) == []
