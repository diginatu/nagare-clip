"""Shared logging configuration for all pipeline stages."""

from __future__ import annotations

import logging


def setup_logging(level: str, log_file: str | None = None) -> None:
    """Configure the root logger with a console handler and optional file handler.

    Console format: ``LEVEL: message`` (unchanged from previous basicConfig calls).
    File format:    ``YYYY-MM-DD HH:MM:SS,mmm LEVEL: message`` (with timestamp).
    Logs are appended to *log_file* when provided; pass ``None`` or ``""`` for
    console-only output.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        root.addHandler(fh)
