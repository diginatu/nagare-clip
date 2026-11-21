"""Suite-wide fixtures."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """A test that runs a CLI ``main()`` installs root handlers bound to its
    captured stream; once that stream closes, a later test's logging errors."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)
