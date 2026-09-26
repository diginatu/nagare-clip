"""Deterministic metrics and checks on the finished cut (no LLM call).

The pipeline states numeric intentions in several places -- the editorial brief
("30 minutes, no more"), the director prompt ("about a minute on screen", "one
overlay per 3-5 minutes") -- and until now nothing ever measured the finished
artifact against any of them.  A real run shipped seven captions compressed to
a fifth of a second inside an 8x timelapse and a timelapse that was over in
15.5 seconds; both took a human twenty minutes of reading JSON to find, and the
first had already happened once before.

Same shape as the order note and ``publish``'s ``chapter_issues``: a
machine check whose verdict is stated plainly rather than left implied.
"""
