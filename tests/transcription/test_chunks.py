from nagare_clip.transcription.chunks import pack_regions


def test_pack_merges_until_limit():
    assert pack_regions([(0, 5), (6, 20), (21, 31), (32, 40)], 30) == [(0, 20), (21, 40)]


def test_pack_long_region_alone():
    assert pack_regions([(0, 5), (6, 50), (51, 52)], 30) == [(0, 5), (6, 50), (51, 52)]


def test_pack_empty():
    assert pack_regions([], 30) == []


def test_pack_exactly_at_limit_merges():
    assert pack_regions([(0, 10), (12, 30)], 30) == [(0, 30)]
