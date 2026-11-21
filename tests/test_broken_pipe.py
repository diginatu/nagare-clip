"""`view | head` closes the pipe: no traceback, return 1 (both tool CLIs)."""

from __future__ import annotations

import pytest

from nagare_clip.director.tools import cli as director_cli
from nagare_clip.publish.tools import cli as publish_cli


class ClosedPipe:
    """A stdout whose reader went away."""

    def write(self, _text):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError

    def fileno(self):
        raise OSError("no fd")


@pytest.mark.parametrize("cli", [publish_cli, director_cli])
def test_a_closed_pipe_returns_1_without_a_traceback(cli, monkeypatch, capsys):
    def boom(project, args):
        print("some output")
        return 0

    monkeypatch.setattr(cli, "load_project", lambda cfg: object())
    monkeypatch.setattr(cli, "_load_cfg", lambda paths: {})
    monkeypatch.setattr(cli, "cmd_view", boom)
    parser = cli.build_parser()
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    for action in parser._subparsers._group_actions:
        action.choices["view"].set_defaults(func=boom)
    monkeypatch.setattr("sys.stdout", ClosedPipe())
    assert cli.main(["view"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "BrokenPipe" not in err
