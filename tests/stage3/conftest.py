"""Shared test fixtures for Stage 2 tests."""

from unittest.mock import MagicMock


def make_span(text: str, start_char: int, end_char: int) -> MagicMock:
    """Create a mock spaCy Span with the attributes used by build_bunsetu_times."""
    span = MagicMock()
    span.text = text
    span.start_char = start_char
    span.end_char = end_char
    return span


def make_nlp(bunsetu_lists: list[list[str]]) -> MagicMock:
    """Return a mock spaCy Language whose __call__ produces a mock Doc.

    ``bunsetu_lists`` is a list of lists, one per nlp() call.  Each inner list
    contains the surface strings of the bunsetsu spans for that call, in order.
    The mock patches ``ginza.bunsetu_spans`` via the ``bunsetu.py`` module so
    that it returns the correct spans for each doc.

    Because ``build_bunsetu_times`` calls ``ginza.bunsetu_spans(doc)`` inside
    the function, we store the expected spans on each mock Doc object and then
    patch ``ginza.bunsetu_spans`` to return them.
    """
    call_iter = iter(bunsetu_lists)

    def nlp_side_effect(text: str) -> MagicMock:
        surfaces = next(call_iter)
        doc = MagicMock()
        doc._text = text
        # Build spans with correct character offsets from the given surfaces.
        spans = []
        char_offset = 0
        for surface in surfaces:
            start_char = text.find(surface, char_offset)
            if start_char == -1:
                # Fallback: use the running cursor (shouldn't happen in tests).
                start_char = char_offset
            end_char = start_char + len(surface)
            spans.append(make_span(surface, start_char, end_char))
            char_offset = end_char
        doc._bunsetu_spans = spans
        return doc

    mock_nlp = MagicMock(side_effect=nlp_side_effect)
    return mock_nlp
