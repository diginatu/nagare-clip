"""Pipeline orchestration errors."""

from __future__ import annotations


class PipelineError(Exception):
    """User-facing orchestration failure; the CLI prints it and exits 1."""
