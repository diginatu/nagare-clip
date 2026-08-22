"""Direction ids: a hash of (stem, lines), abbreviated the way git abbreviates."""

from __future__ import annotations

from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.plan_revise.ids import (
    ID_FLOOR,
    abbrev_len,
    assign_ids,
    direction_id,
    resolve,
)


def _colliding_pair() -> tuple[int, int]:
    """Two line ranges whose full ids share their first ID_FLOOR characters."""
    seen: dict[str, int] = {}
    for i in range(50000):
        full = direction_id("s", (i, i))
        prefix = full[:ID_FLOOR]
        if prefix in seen and direction_id("s", (seen[prefix],) * 2) != full:
            return (seen[prefix], i)
        seen.setdefault(prefix, i)
    raise AssertionError("no 4-char collision found to test with")


class TestDirectionId:
    def test_stable_for_the_same_footage(self):
        # Nothing may make an id change between two runs over the same summary:
        # a random or sequential id would, and then nothing can be said to have
        # survived a revision.
        assert direction_id("a", (1, 4)) == direction_id("a", (1, 4))

    def test_changes_with_the_lines_and_with_the_stem(self):
        assert direction_id("a", (1, 4)) != direction_id("a", (1, 5))
        assert direction_id("a", (1, 4)) != direction_id("b", (1, 4))

    def test_is_lowercase_and_long(self):
        full = direction_id("a", (1, 4))
        assert full == full.lower() and len(full) > ID_FLOOR
        assert full.isalnum()


class TestAbbreviation:
    def test_floor_when_everything_differs_early(self):
        assert abbrev_len(["abcdef", "bcdefg", "cdefgh"]) == ID_FLOOR

    def test_grows_until_unique(self):
        assert abbrev_len(["abcdefgh", "abcdxfgh"]) == 5
        assert abbrev_len(["abcdefgh", "abcdefxh"]) == 7

    def test_duplicates_do_not_force_growth(self):
        # Two directions over the same footage share one id; that is not a
        # collision to abbreviate around.
        assert abbrev_len(["abcdefgh", "abcdefgh"]) == ID_FLOOR

    def test_empty(self):
        assert abbrev_len([]) == ID_FLOOR

    def test_real_collision_grows_the_prefix(self):
        ds = [PartDirection("s", (n, n), "x") for n in _colliding_pair()]
        ids = assign_ids(ds)
        assert len(set(ids)) == 2 and len(ids[0]) > ID_FLOOR


class TestAssignAndResolve:
    def _ds(self):
        return [
            PartDirection("a", (1, 4), "one"),
            PartDirection("a", (5, 9), "two"),
            PartDirection("b", (1, 3), "three"),
        ]

    def test_one_length_for_every_id(self):
        ids = assign_ids(self._ds())
        assert len(set(ids)) == 3
        assert len({len(i) for i in ids}) == 1
        assert all(len(i) == ID_FLOOR for i in ids)

    def test_ids_follow_the_directions_and_are_stable(self):
        assert assign_ids(self._ds()) == assign_ids(self._ds())

    def test_resolve_finds_the_direction(self):
        ds = self._ds()
        ids = assign_ids(ds)
        assert resolve(ds, ids[1]) == [1]

    def test_resolve_accepts_a_longer_prefix(self):
        ds = self._ds()
        assert resolve(ds, direction_id("a", (5, 9))) == [1]

    def test_resolve_is_case_insensitive_and_tolerates_whitespace(self):
        ds = self._ds()
        assert resolve(ds, f"  {assign_ids(ds)[0].upper()} ") == [0]

    def test_unknown_id_resolves_to_nothing(self):
        assert resolve(self._ds(), "zzzz") == []
        assert resolve(self._ds(), "") == []

    def test_ambiguous_prefix_resolves_to_nothing(self):
        # Two different directions whose ids share the rendered prefix length:
        # a token that short names neither, so it is dropped rather than guessed.
        ds = [PartDirection("s", (n, n), "x") for n in _colliding_pair()]
        shared = direction_id(ds[0].stem, ds[0].lines)[:ID_FLOOR]
        assert resolve(ds, shared) == []
        # the rendered (longer) ids still resolve
        ids = assign_ids(ds)
        assert resolve(ds, ids[0]) == [0] and resolve(ds, ids[1]) == [1]

    def test_duplicate_footage_resolves_to_both(self):
        ds = [PartDirection("a", (1, 4), "one"), PartDirection("a", (1, 4), "again")]
        assert resolve(ds, assign_ids(ds)[0]) == [0, 1]
