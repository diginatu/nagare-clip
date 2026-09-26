"""``director/conversation.md``: entries, the done mark, and what the API sees."""

from __future__ import annotations

from nagare_clip.director.conversation import (
    DIRECTOR,
    DONE,
    EDITOR,
    GUIDE,
    Entry,
    is_done,
    load,
    main,
    messages,
    parse,
    render,
    reviewed_through,
    say,
)

LOG = [
    Entry(GUIDE, "Write the plan."),
    Entry(DIRECTOR, '{"plan": "p"}'),
    Entry(GUIDE, "Review around lines 1 to 8."),
    Entry(DIRECTOR, '{"range": [1, 8], "reviewed_through": 8, "ops": []}'),
    Entry(DONE),
]


class TestTheFile:
    def test_it_round_trips(self):
        assert parse(render(LOG)) == LOG

    def test_it_is_readable_markdown(self):
        assert render([Entry(GUIDE, "x"), Entry(DONE)]) == "## guide\nx\n\n## done\n"

    def test_text_before_any_heading_is_the_editor(self):
        assert parse("残して\n\n## guide\nx") == [Entry(EDITOR, "残して"), Entry(GUIDE, "x")]

    def test_an_unknown_heading_is_the_editor(self):
        assert parse("## human\n残して") == [Entry(EDITOR, "残して")]

    def test_headings_are_case_insensitive(self):
        assert parse("## Editor\nx") == [Entry(EDITOR, "x")]

    def test_blank_entries_are_dropped_but_the_done_mark_is_not(self):
        assert parse("## editor\n\n## done\n") == [Entry(DONE)]

    def test_a_missing_file_is_an_empty_conversation(self, tmp_path):
        assert load(tmp_path / "none.md") == []


class TestState:
    def test_the_done_mark_anywhere_is_done(self):
        assert is_done(LOG)
        assert is_done([Entry(DONE), Entry(EDITOR, "x")])
        assert not is_done(LOG[:-1])

    def test_progress_is_the_furthest_reviewed_through(self):
        assert reviewed_through(LOG) == 8
        assert reviewed_through([Entry(DIRECTOR, "not json")]) == 0


class TestMessages:
    def test_the_director_is_the_assistant_and_the_rest_is_labelled(self):
        assert messages(LOG) == [
            {"role": "user", "content": "Guide: Write the plan."},
            {"role": "assistant", "content": '{"plan": "p"}'},
            {"role": "user", "content": "Guide: Review around lines 1 to 8."},
            {
                "role": "assistant",
                "content": '{"range": [1, 8], "reviewed_through": 8, "ops": []}',
            },
        ]

    def test_an_editor_entry_joins_the_live_guidance_in_one_user_message(self):
        entries = [*LOG[:-1], Entry(EDITOR, "残して")]
        out = messages(entries, live="Every line has been reviewed.")
        assert out[-1] == {
            "role": "user",
            "content": "Editor: 残して\n\nGuide: Every line has been reviewed.",
        }
        roles = [m["role"] for m in out]
        assert all(a != b for a, b in zip(roles, roles[1:])), "roles must alternate"


class TestSay:
    def test_it_removes_the_mark_and_appends_an_editor_entry(self, tmp_path):
        path = tmp_path / "conversation.md"
        path.write_text(render(LOG), encoding="utf-8")
        say(path, " 残して ")
        entries = load(path)
        assert not is_done(entries)
        assert entries[-1] == Entry(EDITOR, "残して")
        assert entries[:-1] == LOG[:-1]

    def test_the_cli_writes_under_the_output_dir(self, tmp_path, capsys):
        assert main(["--output-dir", str(tmp_path), "残して"]) == 0
        assert load(tmp_path / "director" / "conversation.md") == [Entry(EDITOR, "残して")]

    def test_the_cli_refuses_an_empty_entry(self, tmp_path, monkeypatch):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert main(["--output-dir", str(tmp_path)]) == 1
