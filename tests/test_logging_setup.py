"""Tests for nagare_clip.logging_setup."""

from __future__ import annotations

import logging
import os
import tempfile

import pytest

from nagare_clip.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def reset_root_logger():
    """Remove all handlers from the root logger before and after each test."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.level = original_level


def _our_handlers(root: logging.Logger) -> list:
    """Return only the handlers added by setup_logging (exclude pytest internals)."""
    return [h for h in root.handlers if not h.__class__.__name__ == "LogCaptureHandler"]


def test_console_only():
    setup_logging("INFO")
    root = logging.getLogger()
    assert root.level == logging.INFO
    handlers = _our_handlers(root)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], logging.FileHandler)


def test_with_log_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        log_path = f.name
    try:
        setup_logging("DEBUG", log_path)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

        handler_types = [type(h) for h in _our_handlers(root)]
        assert logging.StreamHandler in handler_types
        assert logging.FileHandler in handler_types
        assert len(_our_handlers(root)) == 2

        # Emit a log record
        logging.getLogger("test").info("hello from test")

        # Flush and close file handler
        for h in root.handlers:
            h.flush()

        content = open(log_path, encoding="utf-8").read()
        assert "hello from test" in content
        # File format should include timestamp
        assert "INFO" in content
    finally:
        os.unlink(log_path)


def test_file_format_has_timestamp():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        log_path = f.name
    try:
        setup_logging("INFO", log_path)
        logging.getLogger("ts_test").info("timestamp check")
        for h in logging.getLogger().handlers:
            h.flush()
        content = open(log_path, encoding="utf-8").read()
        # Timestamp format: YYYY-MM-DD HH:MM:SS,mmm
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", content)
    finally:
        os.unlink(log_path)


def test_console_format_no_timestamp():
    import io

    stream = io.StringIO()
    setup_logging("INFO")
    root = logging.getLogger()
    # Replace the StreamHandler's stream with our StringIO
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.stream = stream
    logging.getLogger("fmt_test").info("format check")
    output = stream.getvalue()
    assert "format check" in output
    # Console should NOT have a timestamp (no year-like pattern at start)
    import re

    assert not re.match(r"\d{4}-\d{2}-\d{2}", output.strip())


def test_append_mode():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        log_path = f.name
    try:
        # First call
        setup_logging("INFO", log_path)
        logging.getLogger("app1").info("first message")
        for h in logging.getLogger().handlers:
            h.flush()
            h.close()
        logging.getLogger().handlers.clear()

        # Second call — should append, not overwrite
        setup_logging("INFO", log_path)
        logging.getLogger("app2").info("second message")
        for h in logging.getLogger().handlers:
            h.flush()

        content = open(log_path, encoding="utf-8").read()
        assert "first message" in content
        assert "second message" in content
    finally:
        os.unlink(log_path)


def test_empty_log_file_string_treated_as_no_file():
    """Empty string for log_file should behave like None (console only)."""
    setup_logging("WARNING", "")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    file_handlers = [h for h in _our_handlers(root) if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 0
