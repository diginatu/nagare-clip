"""GiNZA bunsetsu extraction for the sentence_split stage (lazy import)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    import spacy


def load_nlp() -> "spacy.language.Language":
    import spacy

    return spacy.load("ja_ginza")


def bunsetsu_units(text: str, nlp: "spacy.language.Language") -> List[Tuple[int, int, str]]:
    """Return ``(start_char, end_char, surface)`` for every bunsetsu in ``text``."""
    import ginza

    doc = nlp(text)
    return [(sp.start_char, sp.end_char, sp.text) for sp in ginza.bunsetu_spans(doc)]
