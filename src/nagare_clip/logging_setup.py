"""Shared logging configuration for all pipeline stages."""

from __future__ import annotations

import logging

_NOISE_SNIPPETS = ("Proxy Server is not installed",)

# LiteLLM logs one multi-line INFO banner per completion() call plus a
# harmless proxy/OTel warning (with traceback) at import; both drown
# pipeline.log. Keep them only when the user asked for DEBUG.
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx")


class _DropLiteLLMNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in _NOISE_SNIPPETS)


def _quiet_litellm(root_level: int) -> None:
    if root_level <= logging.DEBUG:
        return
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    lg = logging.getLogger("LiteLLM")
    if not any(isinstance(f, _DropLiteLLMNoise) for f in lg.filters):
        lg.addFilter(_DropLiteLLMNoise())


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

    _quiet_litellm(root.level)
