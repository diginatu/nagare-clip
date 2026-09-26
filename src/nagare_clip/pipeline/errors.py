"""Pipeline orchestration errors."""

from __future__ import annotations


class PipelineError(Exception):
    """User-facing orchestration failure; the CLI prints it and exits 1."""


class PipelineStop(Exception):
    """A deliberate stop that is not a failure; the CLI prints it and exits 0.

    Raised when a stage has done its part and wants a person to look before
    the pipeline goes on (the director's ``pause_after_plan``).
    """
