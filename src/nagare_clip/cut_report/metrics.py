"""What the finished cut actually is, in numbers.

Everything here is read off the intervals JSONs the pipeline already wrote,
in the blender stage's concatenation order.  Even the strip count is: the
blender stage derives it the same way (``split_intervals_by_speed`` over each
source's keep intervals), so it needs no Blender to compute and the report is
available on a ``--to-stage intervals`` run too.

The finished-timeline arithmetic is ``publish.timeline``'s, not a second copy
of it: that module already reproduces the concatenation in seconds precisely
because it must not disagree with what blender builds.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.blender.frames import split_intervals_by_speed
from nagare_clip.publish.timeline import build_placements, total_duration

# Sensible when the caller has no config to hand (tests, ad-hoc use).
DEFAULT_GAP_THRESHOLD = 0.4
DEFAULT_FRAGMENT_THRESHOLD = 1.0

Sources = Sequence[tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class SpeedSpan:
    """One ``speed_range``, and how long it really plays for.

    ``screen`` is the on-screen time, so it counts only the footage that
    survived the cut: a range half of which was cut plays for half of
    ``span / factor``.  That is the number the director prompt's "about a
    minute on screen" is about.
    """

    stem: str
    start: float
    end: float
    factor: float
    kept: float  # source seconds of keep intervals inside the range
    screen: float  # kept / factor

    @property
    def span(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Span:
    """A keep interval or an inter-keep gap, with the source it came from."""

    stem: str
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class SpanStats:
    """Shape of a set of spans -- printed even when nothing is wrong.

    Listing all 133 healthy gaps would be the same as no check, so the report
    prints this summary and only names the ones under ``threshold``.
    """

    threshold: float
    spans: list[Span] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.spans)

    @property
    def minimum(self) -> float:
        return min((s.length for s in self.spans), default=0.0)

    @property
    def median(self) -> float:
        return statistics.median([s.length for s in self.spans]) if self.spans else 0.0

    @property
    def under(self) -> list[Span]:
        return [s for s in self.spans if s.length < self.threshold]

    @property
    def below(self) -> int:
        return len(self.under)


@dataclass(frozen=True)
class CutMetrics:
    sources: int
    segments: int
    source_duration: float
    finished_duration: float
    plain_duration: float  # on-screen seconds played at 1x
    sped_duration: float  # on-screen seconds played under a speed range
    keep_intervals: int
    strips: int
    captions: int
    captions_in_speed: int
    overlays: int
    speed_spans: list[SpeedSpan]
    gaps: SpanStats
    fragments: SpanStats

    def _share(self, part: float) -> float:
        return part / self.finished_duration if self.finished_duration else 0.0

    @property
    def plain_share(self) -> float:
        return self._share(self.plain_duration)

    @property
    def sped_share(self) -> float:
        return self._share(self.sped_duration)

    @property
    def finished_share(self) -> float:
        """How much of the source footage survived."""
        return self.finished_duration / self.source_duration if self.source_duration else 0.0

    @property
    def overlay_density(self) -> float:
        """Overlays per minute of FINISHED video (the density the prompt targets)."""
        return self.overlays / (self.finished_duration / 60.0) if self.finished_duration else 0.0


def _keeps(data: dict[str, Any]) -> list[dict[str, Any]]:
    return data.get("keep_intervals", []) or []


def _speed_ranges(data: dict[str, Any]) -> list[dict[str, Any]]:
    return data.get("speed_ranges", []) or []


def covering_range(speed_ranges: Sequence[dict[str, Any]], time: float) -> dict[str, Any] | None:
    """The speed range a moment starts inside, if any (half-open, as blender reads it)."""
    for sr in speed_ranges:
        if float(sr["start"]) <= time < float(sr["end"]):
            return sr
    return None


def _kept_inside(keeps: Sequence[dict[str, Any]], start: float, end: float) -> float:
    return sum(
        max(0.0, min(float(iv["end"]), end) - max(float(iv["start"]), start)) for iv in keeps
    )


def measure(
    sources: Sources,
    *,
    gap_threshold: float = DEFAULT_GAP_THRESHOLD,
    fragment_threshold: float = DEFAULT_FRAGMENT_THRESHOLD,
) -> CutMetrics:
    """Measure the finished cut from every source's intervals JSON, in order."""
    placements = build_placements(sources)
    plain = sum(p.tl_end - p.tl_start for p in placements if p.speed == 1.0)
    sped = sum(p.tl_end - p.tl_start for p in placements if p.speed != 1.0)

    keep_count = strips = captions = captions_in_speed = overlays = 0
    # Per DISTINCT stem: one source split into three segments is still one
    # source of one length, however many places it plays in.
    durations: dict[str, float] = {}
    speed_spans: list[SpeedSpan] = []
    gaps: list[Span] = []
    fragments: list[Span] = []

    for stem, data in sources:
        keeps = _keeps(data)
        ranges = _speed_ranges(data)
        durations.setdefault(stem, float(data.get("duration_sec", 0.0) or 0.0))
        keep_count += len(keeps)
        strips += len(split_intervals_by_speed(keeps, ranges))
        overlays += len(data.get("overlays", []) or [])

        for cap in data.get("captions", []) or []:
            captions += 1
            if covering_range(ranges, float(cap["start"])) is not None:
                captions_in_speed += 1

        for sr in ranges:
            start, end = float(sr["start"]), float(sr["end"])
            factor = float(sr["factor"]) or 1.0
            kept = _kept_inside(keeps, start, end)
            speed_spans.append(
                SpeedSpan(
                    stem=stem,
                    start=start,
                    end=end,
                    factor=factor,
                    kept=kept,
                    screen=kept / factor,
                )
            )

        # Gaps live strictly inside one SEGMENT: the seam between two segments
        # is a concatenation boundary, not a cut -- whether the segments belong
        # to different sources or are two stretches of the same one.
        ordered = sorted(keeps, key=lambda iv: float(iv["start"]))
        fragments += [Span(stem, float(iv["start"]), float(iv["end"])) for iv in ordered]
        gaps += [
            Span(stem, float(a["end"]), float(b["start"]))
            for a, b in zip(ordered, ordered[1:])
            if float(b["start"]) > float(a["end"])
        ]

    return CutMetrics(
        sources=len(durations),
        segments=len(sources),
        source_duration=sum(durations.values()),
        finished_duration=total_duration(placements),
        plain_duration=plain,
        sped_duration=sped,
        keep_intervals=keep_count,
        strips=strips,
        captions=captions,
        captions_in_speed=captions_in_speed,
        overlays=overlays,
        speed_spans=speed_spans,
        gaps=SpanStats(gap_threshold, gaps),
        fragments=SpanStats(fragment_threshold, fragments),
    )
