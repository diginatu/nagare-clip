"""Short, stable ids for the directions a revision operates on.

A revision names an existing direction by id, so the id has to survive between
the rendered input and the returned operations of one call — and, more
importantly, between two runs over the same summary.  It is therefore derived
from the direction's *identity*, ``(stem, lines)``:

- a **sequence number** invites the model to read it as a part index, and
  misleads the moment a boundary moves;
- a **random value** changes on every ``plan`` run, so nothing can be said to
  have survived a revision;
- a **hash of (stem, lines)** is stable while the footage a direction refers to
  is stable, and changes exactly when that footage changes.

Ids are rendered abbreviated to the shortest prefix that is unique within the
plan, the way git abbreviates object names: a full hash is pure noise to read
and gives the model more characters to copy wrong.  The abbreviation may differ
between runs as directions are added or removed, which is safe because **a human
never types an id** — they write prose in ``history.md``.
"""

from __future__ import annotations

import base64
import hashlib

from nagare_clip.plan.plan_llm import PartDirection

ID_FLOOR = 4
_FULL_LEN = 16


def direction_id(stem: str, lines: tuple[int, int]) -> str:
    """The full id of the direction covering *lines* of *stem*."""
    digest = hashlib.sha1(f"{stem}\x00{lines[0]}-{lines[1]}".encode()).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:_FULL_LEN]


def full_ids(directions: list[PartDirection]) -> list[str]:
    return [direction_id(d.stem, d.lines) for d in directions]


def abbrev_len(ids: list[str], floor: int = ID_FLOOR) -> int:
    """The shortest prefix length at which every *distinct* id still differs.

    Two directions over the same footage share an id; that is not a collision to
    abbreviate around, so distinct ids — not entries — are what must separate.
    """
    distinct = set(ids)
    if len(distinct) <= 1:
        return floor
    longest = max(len(i) for i in distinct)
    for length in range(floor, longest + 1):
        if len({i[:length] for i in distinct}) == len(distinct):
            return length
    return longest


def assign_ids(directions: list[PartDirection], floor: int = ID_FLOOR) -> list[str]:
    """The abbreviated id of each direction, one length for the whole plan."""
    ids = full_ids(directions)
    length = abbrev_len(ids, floor)
    return [i[:length] for i in ids]


def resolve(directions: list[PartDirection], token: str) -> list[int]:
    """Indices of the directions *token* names — empty when it names none.

    Any prefix of the full id is accepted (the rendered abbreviation is one), so
    a model echoing a longer id still lands.  A token matching more than one
    *distinct* id is ambiguous and resolves to nothing, which the caller drops
    and logs; identical footage resolves to every entry over it.
    """
    token = token.strip().lower() if isinstance(token, str) else ""
    if not token:
        return []
    ids = full_ids(directions)
    matches = [i for i, full in enumerate(ids) if full.startswith(token)]
    if len({ids[i] for i in matches}) > 1:
        return []
    return matches
