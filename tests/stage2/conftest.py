"""Shared test fixtures for Stage 2 tests."""

from unittest.mock import MagicMock


def make_tagger(morpheme_lists):
    """
    morpheme_lists: list of lists of surface strings, one per tagger call.
    """

    call_iter = iter(morpheme_lists)

    def side_effect(text):
        surfaces = next(call_iter)
        return [MagicMock(surface=surface) for surface in surfaces]

    return MagicMock(side_effect=side_effect)
