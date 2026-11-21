"""Quiet exit when the reader of stdout went away (``view | head``)."""

from __future__ import annotations

import os
import sys


def silence_stdout() -> None:
    """Point stdout at ``os.devnull`` so the interpreter's final flush is silent.

    The recipe from the Python docs for ``BrokenPipeError``: without it the
    flush at exit raises again and prints "Exception ignored ... BrokenPipeError".
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass
