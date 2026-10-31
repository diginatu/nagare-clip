# Editorial rules

Migrated from the LLM director's prompt (`director.prompt` in
`src/nagare_clip/config.py`), which past prompt-tuning shaped. Where a rule
says "the tools refuse", `write` enforces it; the rest is judgement.

## The goal: tighten AND stage

Cut what drags, but also mark the moments that make the video worth watching.
Every stretch is one of two things:

- **Listening** — the speech carries something the viewer needs. It plays at
  1x. If it drags, cut the weakest parts. There is no "slightly faster"
  option for speech.
- **Timelapse** — the speech is inessential and the span is manual work worth
  seeing. Go genuinely fast (factor 4.0 or more) and accept that the words
  become unintelligible: that sacrifice is the point of the mode, so be sure
  first.

The default, with no op at all: every line's speech plays once, at 1x, in the
order given, and the silences inside and after it are dropped. Each op changes
that for the lines in its range only.

## Ops

- **cut** — removes a span entirely, audio and video. Reserve it for spans
  that leave the throughline: digressions, dead ends, redundant retakes with
  no payoff. Where the repetition is VISIBLE WORK building toward a payoff
  (failed attempts, assembly, waiting for a result), prefer a timelapse: the
  buildup is part of the story. A cut range must not overlap any other op's
  range (the tools refuse it): to cut 12-18 but keep 18, cut 12-17.
- **timelapse** — plays a long stretch of manual work fast under one on-screen
  caption. Give `factor` (4.0 or more; the tools refuse less — below that it
  is a mild speed-up that would smuggle an uncapped keep over talking) and
  usually `text`. Pick the factor so the result runs about a minute on
  screen: a longer span needs a bigger number, a span where little happens
  can go faster still. Do not settle on one habitual number; `preview` shows
  the on-screen seconds, so compute it. One op does the whole arrangement:
  the work runs continuously and the caption stays up for the whole
  timelapse — do not add a `keep` or `overlay` over the same lines. To change
  the caption partway, use consecutive timelapses (a new caption is a new
  phase of work). A timelapse must lie inside one range of the order.
- **overlay** — an on-screen caption. Give `text` and `duration` in seconds
  (the tools refuse blank text or a non-positive duration). Pick the duration
  from reading length — a short label about 2 s, a full sentence 4-6 s; never
  a fixed value. Its `lines` say WHERE it appears: the first line of the
  range. Use it at turning points, conclusions, failures and mishaps. Aim for
  about one per 3-5 minutes of finished video unless the brief sets a rate.
  All captions share one track, so two on screen at once collide;
  `preview` lists them in playback order.
- **keep** — protects a span INCLUDING its silences. Width follows what is on
  screen: the narrowest range covering one silent gap (normally the silence
  line alone, or `[N, N+1]`), or the WHOLE event in one keep when a
  continuous action plays out across several gaps (an accident and the
  cleanup, a demo running, a result arriving) — rescuing each gap separately
  chops the payoff into jump cuts. Never widen a keep over talking: speech is
  never dropped by default, so such a keep only restores its pauses and
  inflates the runtime. A request to "feature", "retain" or "emphasise"
  something is editorial emphasis, NOT a keep. The tools refuse a keep wider
  than `director.max_keep_lines`; hold a longer event with consecutive keeps.
- **edit** — a fine within-line text deletion or fix; describe it precisely in
  `note` (where it starts and ends). Speech lines only.
- **speed** does not exist for you (the tools refuse it). Repeated prompt
  rounds could not keep a mild 1.3-2.0x from spreading over large parts of
  the video, because a middle option is always easier to choose than a real
  decision.

Use `note` on every op to say why, and precisely where in the line(s).

## Silence is not always dead air

A silence line (`54: [silent 29.9s: a build runs and logs scroll past]`) is a
wait in which nobody speaks, plus what is VISIBLE on screen during it. Dead
air is the fallback reading, not the only one: if the speech either side
announces something happening (an accident, a cleanup, a wait for a result),
that silence may be the most watchable moment in the shot — a keep restores
it, a timelapse plays it fast. An ordinary long timing gap next to such
speech is a keep candidate too.

## Sources and joins

Each `[k]` block was recorded as its own video, so it can open with a greeting
or end with a sign-off; whether to cut those is your call. One op stays inside
one block (the tools refuse a crossing; split it at the join).

## Order

The transcript is numbered in shooting order and stays so. Change what plays
when only with a reason a viewer would feel (a demonstration that belongs to
an earlier thread, a payoff that should open the video). The order moves
footage; it never removes it — that is `cut`. A silence line plays with the
range it is in. After a reorder, read every seam `preview` quotes: both sides
must make sense back to back.

## Pacing

Judge pacing from the **speech** figure in a line's bracket, never
speech+silence. A long speech duration is a candidate for cutting, not for
speeding up; a long stretch of manual work is a timelapse candidate. A keep
plays the silence too; a timelapse plays speech+silence divided by its factor.

## The playback is the viewer's

A range that reads fine as a spec can still play wrong. The usual failure is a
span that opens one line too early: the spec says "timelapse the work", the
playback has the viewer hearing that very work announced at 5x. Read the
preview, then move the boundary.

## The editorial brief

`view` prints the project's brief (the `project:` config section: audience,
purpose, target duration, tone, story so far, previous episode) at the top.
Follow it when deciding what to keep, cut, tighten and emphasise, and check
the runtime against its target duration every time you preview. The user's
live instructions override it.
