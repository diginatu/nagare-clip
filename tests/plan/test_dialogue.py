"""plan_dialogue/history.md: a forgiving markdown conversation log."""

from __future__ import annotations

from nagare_clip.plan.dialogue import (
    DialogueTurn,
    append_turn,
    parse_history,
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
