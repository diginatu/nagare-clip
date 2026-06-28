import pytest

ginza = pytest.importorskip("ginza")
spacy = pytest.importorskip("spacy")


@pytest.fixture(scope="module")
def nlp():
    return spacy.load("ja_ginza")


def test_bunsetsu_units_offsets_match_surface(nlp):
    from nagare_clip.sentence_split.nlp import bunsetsu_units
    text = "今日は水槽の水を替えました"
    units = bunsetsu_units(text, nlp)
    assert units, "expected at least one bunsetsu"
    for start, end, surface in units:
        assert 0 <= start < end <= len(text)
        assert text[start:end] == surface
    # bunsetsu are ordered and non-overlapping
    for (_, e0, _), (s1, _, _) in zip(units, units[1:]):
        assert s1 >= e0
