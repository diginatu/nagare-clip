"""Typed configuration models, loading, and merging (pydantic-settings).

The pydantic models below are the single source of truth for every config
default, its type, and its documentation.  ``get_effective_config`` validates a
merged (defaults <- file <- CLI) config and returns a plain ``dict`` so existing
stage code keeps reading ``cfg["section"]["key"]`` unchanged (the "dict
boundary").  ``config.example.yml`` is generated from these models
(``generate_example_yaml`` / ``--write-example``).
"""

# pyright: reportArgumentType=false
# Suppresses false positives on every ``Field(default_factory=<ModelClass>)``
# below: pyright (verified 1.1.391 and 1.1.411) mis-models a pydantic v2 model
# that sets ``model_config = ConfigDict(...)`` -- it treats each ``Field(<default>)``
# field as *required* and so rejects ``type[X]`` as a valid ``() -> X`` factory.
# Reproducible in ~5 lines of plain pydantic; no code spelling avoids it
# (instance/lambda defaults only trade it for reportCallIssue). Editor-only noise
# -- pyright is not in this repo's CI. Trade-off: a genuine reportArgumentType in
# this (declarative) module would also be silenced.

from __future__ import annotations

import copy
import logging
import re
import sys
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Long prompt defaults (verbatim; kept out of the class bodies for readability)
# ---------------------------------------------------------------------------

SENTENCE_SPLIT_PROMPT = (
    "You split a Japanese transcript into sentence units. The transcript "
    "has little or no punctuation.\n"
    "The input is a sequence of bunsetsu (Japanese phrase units) numbered "
    "from 0, given as `index:surface` tokens.\n"
    "Group consecutive bunsetsu into natural sentences, and represent each "
    "sentence as [first bunsetsu index, last bunsetsu index].\n"
    "Rules:\n"
    "- Ranges must be contiguous and cover every bunsetsu (0..N-1) with no "
    "gaps or overlaps.\n"
    "- Do not change the order or content of the bunsetsu.\n"
    '- Output ONLY JSON: {"sentences":[[0,3],[4,7],...]}'
)

TEXT_FILTER_PROMPT = (
    "Fix speech recognition errors in Japanese text.\n"
    "Remove filler words (あのー, えーと) and noise like (雑音).\n"
    "Only fix clear mistakes. Do NOT rephrase correct text.\n"
    "\n"
    "Rules:\n"
    "- Copy each line fully with its number.\n"
    "- Wrap ONLY the erroneous part: {{error->fix}} or {{delete->}}.\n"
    "- Keep all surrounding text unchanged.\n"
    "- If a phrase is repeated, keep the later occurrence and delete the "
    "earlier one with a {{...->}} marker — never rewrite the line without "
    "markers.\n"
    "\n"
    "Example:\n"
    "Input:\n"
    "1: えーとそれは急はいい天気ですね\n"
    "2: 正しい文です\n"
    "3: (雑音)\n"
    "4: 映ってる映ってるね\n"
    "\n"
    "Output:\n"
    "1: {{えーと->}}それは{{急は->今日は}}いい天気ですね\n"
    "2: 正しい文です\n"
    "3: {{(雑音)->}}\n"
    "4: {{映ってる->}}映ってるね"
)

SUMMARY_PROMPT = (
    "You are a video editor. You receive ONE Japanese transcript as "
    "numbered lines (one line per subtitle segment). The transcript is "
    "produced by automatic speech recognition and may contain recognition "
    "errors (mis-heard or misspelled words); infer the intended meaning. "
    "Split it into a few contiguous PARTS by topic/section and summarise "
    "each part. Reference lines by their 1-based numbers (inclusive). Also "
    "write ONE short summary of the whole video, and list rare or "
    "domain-specific words that speech recognition might misspell. Output "
    "ONLY a JSON object.\n"
    "\n"
    "JSON shape:\n"
    '{"parts": [\n'
    '  {"lines": [1, 12], "summary": "what this part covers"},\n'
    '  {"lines": [13, 40], "summary": "..."}\n'
    '], "keywords": ["word1", "word2"], "video_summary": "the whole video in one sentence"}\n'
    "\n"
    "Rules:\n"
    "- Parts must be contiguous and within the transcript range.\n"
    "- Keep each summary to one short sentence.\n"
    '- "video_summary": one short sentence covering the whole video '
    "(required, non-empty).\n"
    '- "keywords": correct spellings of rare/domain-specific words '
    "(may be empty).\n"
    "- Output only the JSON object, no other text."
)

SUMMARY_OVERALL_PROMPT = (
    "You are a video editor. You receive per-part summaries grouped by "
    "source video: each video starts with a `## <name> — <video summary>` "
    "header, followed by its numbered parts. Write ONE concise overall "
    "summary of the whole project. Output ONLY a JSON object:\n"
    '{"summary": "..."}\n'
    "Output only the JSON object, no other text."
)

PLAN_PROMPT = (
    "You are a video editor planning a rough cut across several source "
    "videos. You receive numbered PARTS (each with a source video, a line "
    "range, and a summary) plus an overall summary. For each part, give a "
    "ROUGH editorial direction — what to do with it (e.g. remove, shorten, "
    "speed up, feature, emphasise) and why, considering the whole project "
    "(e.g. a part that repeats an earlier one can be removed). Reference "
    "parts by their 1-based index. Output ONLY a JSON object.\n"
    "\n"
    # Same four shapes as DIRECTOR_PROMPT's legend, for the same reason: the
    # four-part form was 20% of a real run's part brackets and undocumented.
    "Timing: a part may carry a bracket after its line range. [4.2s] is the "
    "part's duration; [4.2s, gap 0.8s] adds the silent gap before the next "
    "part of the same video. A part holding long internal silence splits it "
    "instead — [13.0s speech, 62.9s silence] means only 13.0 seconds are "
    "spoken and the 62.9 silent seconds are dropped by default — and may "
    "carry a gap too: [13.0s speech, 62.9s silence, gap 0.8s]. Those are the "
    "only four forms; a negligible gap is omitted; no timing, no bracket. "
    "Judge pacing from the speech figure. "
    "Use these numbers to judge pacing: long parts are candidates "
    "for shortening or speeding up, and long gaps mean dead air.\n"
    "\n"
    "By default, non-speech stretches are dropped. If a part's silent "
    "moments are themselves worth watching (something visible happens, a "
    "result arrives), say so in the direction — a later stage decides how "
    "to preserve them.\n"
    "\n"
    "A part's line range may hold more than one thread — an announcement, a "
    "digression, and the event itself. When it does, SPLIT it: emit several "
    'directions for that part, each with its own "lines" range inside the '
    "part's range, so each thread gets the direction it deserves.\n"
    "\n"
    "The finished video is a sequence of SEGMENTS: a segment is one stretch of "
    "one source video, and they play in the order you give. Omit "
    '"order" to play the sources whole, in the order they are listed above. '
    "Include it to state a different one.\n"
    "\n"
    'Always write a "message" — every run, without exception. It is a short '
    "account of the plan you have just made: how you read the project as a "
    "whole, what you decided to compress and what you gave room to, and where "
    "you were unsure. A human reads it instead of reading every direction, so "
    "it must stand on its own. It explains this plan; it is NOT a reply and is "
    "not addressed to anyone.\n"
    "\n"
    "JSON shape:\n"
    '{"directions": [\n'
    '  {"index": 1, "direction": "feature — the product\'s operating noise '
    'is the point"},\n'
    '  {"index": 2, "direction": "remove — repeats part 1"},\n'
    '  {"index": 3, "lines": [12, 20], "direction": "emphasise — the '
    'demonstration itself"}\n'
    "],\n"
    ' "order": [{"stem": "<source A>"}, {"stem": "<source B>", "lines": [1, 30]},'
    ' {"stem": "<source B>", "lines": [31, 97]}],\n'
    ' "message": "how you read the project, what you compressed and what you '
    'gave room to, and where you were unsure"}\n'
    "\n"
    "Rules:\n"
    '- "index" must be one of the given part numbers.\n'
    '- "lines" is optional: omit it to direct the whole part, or give a range '
    "inside that part's own range to direct only some of it. Several "
    "directions may share one index.\n"
    "- One short, actionable phrase per direction.\n"
    '- Never use the word "keep" in a direction: a later stage reads it as '
    "a mechanical instruction to restore every silent second of the part. "
    'Say "feature", "retain" or "emphasise" instead.\n'
    '- "message" is required and never empty.\n'
    '- "order" is optional. Omit it and the parts play in the order listed '
    "above. "
    "When you give one, its segments must cover every line of every source "
    "EXACTLY ONCE — no gap, no overlap, no source left out. Dropping footage is "
    "a later stage's job, so an order that leaves lines out is rejected whole "
    "and the given order is used instead.\n"
    '- Each segment is {"stem": ..., "lines": [a, b]}; omit "lines" for a whole '
    "source. A source may appear more than once, as several segments.\n"
    '- If your "order" differs from the order the parts are listed in, say so '
    'and why in "message" — a change to the order of the finished video must '
    "never arrive unannounced.\n"
    "- Output only the JSON object, no other text."
)

PLAN_REVISE_PROMPT = (
    "You are a video editor revising an existing rough-cut plan together with "
    "the human editor. You receive the numbered PARTS of the project (source "
    "video, line range, summary) with the CURRENT directions listed under the "
    "part each belongs to, every one tagged with a short id in square "
    "brackets, plus the CURRENT ORDER of the finished video and the "
    "conversation with the human editor (oldest first). "
    "Output ONLY a JSON object.\n"
    "\n"
    "State only what changes. Every direction you do not name stays exactly as "
    "it is — you never restate one to preserve it. Change nothing the "
    "conversation does not ask about.\n"
    "\n"
    "Timing: a part may carry a bracket after its line range — "
    "[4.2s, gap 0.8s] means the part has a duration of 4.2 seconds and is "
    "followed by a 0.8-second silent gap before the next part of the same "
    "video. A part containing long internal silences splits its duration — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken; the "
    "silent seconds are dropped by default. Judge pacing from the speech "
    "figure.\n"
    "\n"
    "Operations:\n"
    '- "delete": the ids of directions that should stop existing.\n'
    '- "add": new directions — the part "index" it belongs to, an optional '
    '"lines" range inside that part, and its "direction" text. There is no '
    "insertion position: a direction sits where its part and lines put it.\n"
    '- "update": an existing direction\'s id plus its new "direction" text; '
    "its line range is unchanged. To move a boundary, delete it and add.\n"
    '- "order": the WHOLE playback order of the finished video, restated. '
    "Unlike a direction, an order cannot be edited in pieces — the order IS "
    "the position — so give all of it, or omit the key and the current order "
    'stands unchanged. Omit "order" to leave it exactly as it is.\n'
    '- "message": your REPLY to the human — how you read the last instruction, '
    "what you changed because of it, and what you would like confirmed. It "
    "answers a person, so it is not a summary of the plan (the plan stage "
    "already wrote one); the human reads it and may reply again.\n"
    "\n"
    "To SPLIT a part the human says holds several threads: delete the "
    "direction covering it and add one per thread, each with its own "
    '"lines".\n'
    "\n"
    "JSON shape:\n"
    '{"delete": ["k7f2"],\n'
    ' "add": [{"index": 21, "lines": [60, 83], "direction": "feature — the '
    'demonstration itself"}],\n'
    ' "update": [{"id": "m3q8", "direction": "shorten heavily — the setup '
    'drags"}],\n'
    ' "order": [{"stem": "<source A>"}, {"stem": "<source B>", "lines": [1, 30]},'
    ' {"stem": "<source B>", "lines": [31, 97]}],\n'
    ' "message": "part 21 was one direction over two threads, so I split it '
    'at line 60 — is that the right boundary?"}\n'
    "\n"
    "Rules:\n"
    "- Use only ids shown in the parts document; never invent one.\n"
    '- "index" must be one of the given part numbers, and "lines" must sit '
    "inside that part's own range.\n"
    "- One short, actionable phrase per direction.\n"
    '- Never use the word "keep" in a direction: a later stage reads it as '
    "a mechanical instruction to restore every silent second of the part. "
    'Say "feature", "retain" or "emphasise" instead.\n'
    '- "order", when given, must cover every line of every source EXACTLY ONCE '
    "— no gap, no overlap, no source left out. Dropping footage is a later "
    "stage's job, so an order that leaves lines out is rejected whole and the "
    'current one stands. Each segment is {"stem": ..., "lines": [a, b]}; omit '
    '"lines" for a whole source, and a source may appear more than once.\n'
    '- If you change the order, say what moved and why in "message".\n'
    "- Every key is optional: answer a question with a message alone.\n"
    "- Output only the JSON object, no other text."
)


DIRECTOR_PROMPT = (
    "You are a video editor. You are given the WHOLE finished video as "
    "numbered lines — every segment of it in playback order, speech and "
    "silence together, under ONE numbering — and you edit it over several "
    "turns. Decide high-level "
    # No trigger list here: the overlay bullet already owns "turning points,
    # conclusions, failures, and mishaps", and a list stated twice is two
    # places to drift.
    "edits to tighten AND STAGE the video: cut what drags, but also mark "
    "the moments that make it worth watching.\n"
    "\n"
    # The protocol, stated once.  Each user message repeats the exact reply
    # shape (director.loop.REPLY_SHAPE), so what belongs here is what a turn
    # MEANS: that the range is approximate, that a reply owns its range, and
    # that the playback comes back — the three things that make rewriting an
    # earlier range a normal move rather than an admission.
    "Each turn asks you for an approximate range; you reply with the ops for "
    "the stretch you really reviewed, and are shown what they will play. A "
    "reply OWNS the range it names: re-send a range and its ops REPLACE the "
    "ones you gave for it, so a playback you did not mean is fixed by sending "
    'that range again. Reply {"done": true} once every line has been reviewed '
    "and the playback is what you meant.\n"
    "\n"
    # Every bracket shape the renderer can emit is shown, in one place.  The
    # four-part form was 17.0% of a real run's brackets while going
    # undocumented, and the three-part gap form the old legend led with was the
    # rarest at 4.4% — so the shape the LLM met most often was the one it had
    # to infer.  Enumerating them costs nothing: this paragraph is four
    # characters shorter than the one it replaces.
    "Timing: a line may carry a bracket after its text. [4.2s] is the line's "
    "duration; [4.2s, gap 0.8s] adds the silent gap before the next line. A "
    "line holding long internal silence splits it instead — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken and the "
    "62.9 silent seconds are dropped by default — "
    "and may carry a gap too: [13.0s speech, 62.9s silence, gap 0.8s]. Those "
    "are the only four forms; a negligible gap is omitted; no timing, no "
    "bracket. A bracket's gap is a SHORT one — a longer wait is a "
    # The gap-rescue reading used to live here, because a 29.9 s wait reached
    # the director only as `gap 29.9s` inside the preceding line's bracket and
    # nothing else could be said about it.  It is a line of its own now, which
    # is where the "this may be the best moment in the shot" reading belongs.
    "[silent …] line of its own, below.\n"
    "\n"
    "Use these numbers to judge pacing, always from the speech figure and "
    "never from speech+silence: a long speech duration is a "
    "candidate for cutting, not speeding up — a long stretch of manual "
    "work is a timelapse candidate.\n"
    "\n"
    "Silence: a line like\n"
    "54: [silent 29.9s: a build runs and logs scroll past]\n"
    # It used to carry no number (the per-segment transcript numbered source
    # lines only), so the prompt had to teach the "53~" form.  Under the
    # whole-video numbering it is a line like any other, and the parser
    # rejects "53~" — so the form is gone rather than kept as an alias.
    "is the wait between the lines either side of it — 29.9 s in which nobody "
    "speaks — followed by what is VISIBLE on screen during it. It is a line "
    "like any other: put its number in an op — a timelapse plays that wait "
    "fast, a keep restores it. Dead air is the "
    "fallback reading, not the only one: if the speech either side announces "
    "something happening (an accident, a cleanup, a wait for a result), that "
    "silence may be the most watchable moment in the shot. An indented "
    "[silent gap: …] with no seconds is silence INSIDE the line above.\n"
    "\n"
    "Operations (line ranges are inclusive). "
    "Prefer a timelapse over a cut where the repetition is VISIBLE WORK building "
    "toward a payoff (failed attempts, assembly, waiting for a result) — "
    "the buildup is part of the story, so timelapse it rather than "
    "deleting it. Where the repetition is SPEECH, there is no fast option: "
    "leave it at 1x, or cut the weakest passes. Reserve cut for spans "
    "that leave the throughline entirely (digressions, dead ends, "
    "redundant retakes with no payoff):\n"
    "- cut: remove a boring/redundant span entirely (deletes audio+video).\n"
    '- timelapse: play a long stretch of manual work fast under one on-screen caption; give "factor" (4.0 or more) and "text". Compressing a stretch is a choice between two modes and this is the second one. LISTENING: the speech carries something the viewer needs — play it at 1x, and if it drags cut the weakest parts instead. TIMELAPSE: the speech is inessential — go genuinely fast and accept that the words become unintelligible; that sacrifice is the point of the mode and is why you must be sure first. Pick "factor" so the result runs about a minute on screen: a longer span needs a bigger number, and a span where little is happening can go faster still. One op does the whole arrangement: the work runs continuously and the caption stays on screen for the entire timelapse — do not add a separate "keep" or "overlay" over the same lines. To change the caption partway through, emit consecutive timelapse ops; a new caption means a new phase of work.\n'
    '- overlay: show an on-screen caption; give "text" and "duration" '
    "(how many seconds it stays on screen). Pick the duration from reading "
    "length — a short label needs about 2 seconds, a full sentence 4 to 6; "
    'never a fixed value. Its "lines" say WHERE it appears (the caption '
    "starts at the first line of the range), not how long it shows. Reach "
    "for it at turning points, conclusions, failures, and mishaps — "
    # Not "a loose default target" any more.  A real project's brief states
    # the SAME 3-5 minutes as a floor, so the "if an editorial brief states
    # otherwise" escape hatch could never fire — two statements of one number
    # with opposite modality (a two-sided target vs a floor), and the softer
    # one arriving first.
    "moments worth labeling on screen. Aim for one overlay per 3-5 minutes "
    "of finished video; an editorial brief may set a different rate.\n"
    # The keep op owns keep mechanics outright: the width rule and the
    # whole-event span.  Rescuing ONE silence is no longer a keep-width
    # question at all — "53~" addresses it exactly — so the [N, N+1]
    # approximation that used to live here is gone.
    "- keep: protect a span from cutting INCLUDING its silences/non-speech "
    "gaps. When a continuous event "
    "is playing out across several gaps — an accident and the cleanup after "
    "it, a demo running, a result arriving — span the WHOLE event in one "
    "keep, so the payoff is not chopped into jump cuts. Never widen a keep "
    "to mark talking as important: speech is never dropped by default, so a "
    "keep over a talking span only restores its pauses and inflates the "
    'runtime for nothing. A direction to "feature", "retain" or '
    '"emphasise" something is editorial emphasis, NOT a request for a keep op.\n'
    '- edit: request a fine within-line text deletion/fix; describe it in "note".\n'
    "\n"
    "JSON shape, one object per turn:\n"
    '{"range": [40, 78], "reviewed_through": 78, "ops": [\n'
    '  {"type": "cut", "lines": [42, 48], "note": "why / where precisely"},\n'
    '  {"type": "timelapse", "lines": [60, 72], "factor": 8.0, "text": "配管の取り付け", "note": "..."},\n'
    '  {"type": "overlay", "lines": [45, 45], "text": "ポイント", "duration": 2.0, "note": ""},\n'
    '  {"type": "keep", "lines": [50, 52], "note": "..."},\n'
    '  {"type": "edit", "lines": [47, 47], "note": "delete the redundant restatement"}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    '- "lines" are this transcript\'s numbers. One op stays inside one [k] '
    "block — those are different footage, and an op across two is refused.\n"
    '- A "cut" range must not overlap any other op\'s range: cutting deletes '
    "the span, so never include a line you also keep/overlay/timelapse in "
    "a cut (e.g. to cut lines 12-18 but keep line 18, emit cut [12, 17]). "
    "Overlapping ops are clipped and the cut loses the shared lines.\n"
    '- Use "note" to describe in natural language precisely WHERE in the '
    "line(s) the edit starts and ends, so a downstream editor can place "
    "it exactly.\n"
    "- Playing it back starts from what happens with no op at all: every "
    "line's speech plays once, at 1x, in the order given, and the silences "
    "inside and after it are dropped. Each op changes that for the lines in "
    "its range only — a line you leave outside a range keeps the default.\n"
    "- The playback you are shown is the viewer's, not the spec's: a range "
    "that reads fine as a spec can still play wrong, and the usual way is a "
    'span that opens one line too early — the spec says "timelapse the work", '
    "the playback has the viewer hearing that very work announced at 5x, "
    "destroyed. Re-send the range with the boundary moved.\n"
    "- Output only the JSON object, no other text."
)

GUIDED_EDIT_PROMPT = (
    "You apply ONE editing instruction to Japanese subtitle lines.\n"
    "You are given numbered lines and an instruction. Insert the "
    "requested marker into the line text at the precise position "
    "described, and return the lines unchanged otherwise.\n"
    "\n"
    "Markers:\n"
    "- Cut a span:    wrap it in <cut>...</cut>\n"
    '- Speed up:      wrap it in <speed factor="N.N">...</speed>\n'
    '- Overlay text:  insert <overlay text="..." duration="N.N"/> where it '
    "starts (a self-closing point marker; there is no closing tag)\n"
    "- Keep/protect:  wrap it in <keep>...</keep>\n"
    "- Delete words within a line: {{old->}} (old copied verbatim)\n"
    "- Fix words within a line:    {{old->new}}\n"
    "\n"
    "Rules:\n"
    "- Copy each line fully with its number. Change ONLY by inserting "
    "markers or {{old->new}}; never rephrase or reorder the original text.\n"
    "- For a span across multiple lines, open the tag on the first line "
    "and close it on the last line.\n"
    "- Output the same numbered lines, nothing else."
)

GAP_CONTEXT_PROMPT = (
    "You are watching frames sampled from a SILENT gap in a video (no one is "
    "speaking). Describe what is happening ON SCREEN during the gap.\n"
    "\n"
    "The frames are in chronological order (start, middle, end of the gap).\n"
    "Focus on ACTION and CHANGE between the frames: is something happening "
    "(a demo running, code being typed, a game being played, a result "
    "appearing), or is the screen essentially static/dead air?\n"
    "\n"
    "Rules:\n"
    "- Start your answer with exactly 'ACTION: ' if something meaningful "
    "happens on screen, or 'STATIC: ' if the screen is essentially "
    "static/dead air.\n"
    "- After the marker, answer in ONE or TWO short sentences of plain text. "
    "No JSON, no markdown, no preamble.\n"
    "- If nothing meaningful happens, keep it minimal (e.g. 'STATIC: no "
    "visible activity.').\n"
    "- Describe only what you can see; do not speculate about the audio."
)

DESCRIBE_FRAMES_PROMPT = (
    "You are looking at ONE still frame taken from a video, so that a "
    "thumbnail headline can later be placed on it. Reply with plain prose, "
    "two to four sentences. No JSON, no bullet list, no preamble.\n"
    "\n"
    "Say, in this order:\n"
    "1. What is actually visible and LEGIBLE in the frame -- what a viewer "
    "would recognise at thumbnail size. If the subject is small, turned away, "
    "blurred, dark or out of frame, say so plainly; that is the most useful "
    "thing you can report.\n"
    "2. Where the subject sits: left / centre / right, upper / middle / lower.\n"
    "3. Which regions are EMPTY enough to carry large text, and for each of "
    "them, its colour and whether it is light or dark.\n"
    "\n"
    "Describe only what you can see in this frame. Do not guess what happened "
    "before or after it, and do not write a headline or a caption of any kind."
)

PUBLISH_PROMPT = (
    "You write the publishing material for a finished video: the title, the "
    "description lead, the chapter titles and the thumbnail copy. You receive "
    "an overall summary of the project, a per-video summary, and the video's "
    "numbered PARTS (each with a line range and a one-sentence summary); some "
    "parts may carry the editing plan's direction for them, and a list of the "
    "on-screen captions the edit places at its payoff moments. Write in the "
    "same language as those summaries. Output ONLY a JSON object.\n"
    "\n"
    "JSON shape:\n"
    "{\n"
    '  "titles": ["candidate 1", "candidate 2", "candidate 3"],\n'
    '  "lead": "two or three sentences opening the description",\n'
    '  "chapters": [{"index": 1, "title": "short chapter title"}],\n'
    '  "thumbnail_copy": [\n'
    '    {"lines": [{"role": "tag", "text": "..."},\n'
    '               {"role": "hook", "text": "..."}]}\n'
    "  ]\n"
    "}\n"
    "\n"
    "Rules:\n"
    '- "titles": several genuinely different candidates, not variations of '
    "one phrasing — a human picks one. Each stands on its own without the "
    "thumbnail.\n"
    '- "lead": what the video does and why someone would watch it. Do NOT '
    "write timestamps or a chapter list; those are added mechanically.\n"
    '- "chapters": one entry per part you can title, keyed by the part\'s '
    "number as given. A few words each, naming what happens — not a "
    "sentence, and no timestamps (they are computed from the finished "
    "timeline, which you cannot see). Skipping a part is fine; its summary "
    "is used instead.\n"
    '- "thumbnail_copy": two to four ALTERNATIVE sets. Each set is one to '
    "three lines and must be readable at a glance: a `tag` names the "
    "genre/series in a word or two, a `hook` is the punchy line the "
    "thumbnail is built around, a `subtitle` adds the one detail that makes "
    "the hook land. Only the roles you need — a punchy video may want a hook "
    "alone. Never pad a set to three lines.\n"
    "- The sets must be genuinely different ANGLES on the video, not "
    "rewordings of one — they are alternatives a human chooses between.\n"
    "- Write the WORDS ONLY. Which photograph a set goes on, and what "
    "colour its text is and where it sits, are decided afterwards by "
    "someone who has looked at the pictures. You have not seen them, so "
    "do not describe, assume or refer to a background.\n"
    "- Prefer the concrete moments the captions and part summaries name "
    "(a failure, a fix, a result) over generic phrasing.\n"
    "- Output only the JSON object, no other text."
)


# ---------------------------------------------------------------------------
# Field helper: mark a field to be emitted commented-out in the example file
# ---------------------------------------------------------------------------


def _commented(default: Any, *, sample: str, description: str = ""):
    """A real config field that the example generator emits as ``# key: sample``.

    ``sample`` is the text shown after ``# key:`` (an illustrative value or
    ``"..."`` placeholder); it is documentation only — the actual default is
    ``default``.
    """
    return Field(
        default,
        description=description,
        json_schema_extra={"emit": "commented", "sample": sample},
    )


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------


class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    log_level: str = Field("INFO", description="DEBUG | INFO | WARNING | ERROR | CRITICAL")
    log_file: str = Field(
        "",
        description="Path to log file; empty = console only (run_pipeline.sh sets this automatically)",
    )
    llm_report: bool = Field(
        True, description="write a per-call LLM report under output/llm_report/"
    )
    llm_report_dir: str = Field("output/llm_report")
    langfuse: bool = Field(
        True,
        description="send LLM traces to Langfuse when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set (false to force-disable)",
    )
    image_markup: Literal["html", "markdown"] = Field(
        "html",
        description=(
            "How the reviewable markdown files (publish.md, render.md) embed images: "
            "html = <img> tags (sized), markdown = ![alt](path) for viewers that strip "
            "raw HTML. A property of your viewer, so it is set once here"
        ),
    )


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "project: the editorial brief for this project (all fields optional, free text).\n"
        "Whatever is set here is appended to the system prompts of the summary, plan,\n"
        "director and text_filter stages, so those LLMs know what the transcript cannot\n"
        "tell them: who the video is for, how long it should be, how it should feel, and\n"
        "what happened previously in a series. Leave everything empty (the default) and\n"
        "every prompt is byte-identical to a run without this section."
    )
    audience: str = _commented(
        "",
        sample='"DIY hobbyists on YouTube, already familiar with the build"',
        description="Who the video is for",
    )
    purpose: str = _commented(
        "",
        sample='"show whether the siphon overflow drain actually works"',
        description="What the video is trying to achieve",
    )
    target_duration: str = _commented(
        "",
        sample='"about 12 minutes"',
        description="Desired finished length (free text); guides how aggressively to cut",
    )
    tone: str = _commented(
        "",
        sample='"fast, punchy vlog; frequent on-screen captions"',
        description="Desired feel/pacing of the finished video",
    )
    story_so_far: str = _commented(
        "",
        sample='"the previous episode built the rig; this one tests it"',
        description="Series context the transcript never states",
    )
    previous_summary: str = _commented(
        "",
        sample='"../previous-project/output/summary/summary.json"',
        description="Path to a previous project's summary.json; its overall summary is added to the brief",
    )


class TranscriptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    compute_type: str = Field("float16")
    batch_size: int = Field(16)
    language: str = Field(
        "ja", description="ISO 639-1 language code passed to WhisperX (e.g. ja, en)"
    )
    align_model: str = _commented("", sample="vumichien/wav2vec2-large-xlsr-japanese")


class AudioSilenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "audio_silence stage: audio-silence (jump-cut) detection.\n"
        "Runs ffmpeg silencedetect on the waveform and writes an editable\n"
        "{stem}_cuts.txt checkpoint. NOTE: this is acoustic silence, distinct\n"
        "from intervals.silence_threshold (which is a WhisperX word-gap heuristic)."
    )
    enabled: bool = Field(
        True, description="false = write an empty cut list (no audio cuts applied)"
    )
    noise: float = Field(-30.0, description="ffmpeg silencedetect noise threshold, in dB")
    min_silence: float = Field(
        0.8, description="ffmpeg silencedetect minimum silence duration, seconds"
    )


class SentenceSplitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "sentence_split stage: runs per source between audio_silence and text_filter.\n"
        "An LLM re-segments the WhisperX transcript into one-sentence-per-line units\n"
        "(rewriting {stem}.json and {stem}.txt). Disabled by default = byte-identical\n"
        "copy-through (no behaviour change)."
    )
    enabled: bool = Field(False, description="Enable LLM sentence re-segmentation")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "gpt-oss:120b", description='Model (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.2)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    response_format: str = Field("json")
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / invalid ranges (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    window_segments: int = Field(
        20,
        description="Segments per LLM window (the batch size); a window carries its trailing sentence to the next",
    )
    force_split: bool = Field(
        True,
        description="Force a sentence boundary at long audio_silence spans (post-enforced on LLM output; no extra LLM calls)",
    )
    force_split_min_silence: float = Field(
        3.0,
        description="Seconds; only audio_silence cut spans at least this long force a split",
    )
    prompt: str = _commented(
        SENTENCE_SPLIT_PROMPT,
        sample='"..."',
        description="Bunsetsu-grouping prompt (has a sensible default)",
    )


class TextFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = "text_filter stage: text editing checkpoint."
    use_llm: bool = Field(
        False, description="Enable LLM text filter for the text-editing checkpoint"
    )
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "qwen3.5:4b", description='Model name (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    batch_size: int = Field(10, description="Number of transcript segments per LLM call")
    timeout: int = Field(60, description="API request timeout in seconds")
    retry_on_invalid: bool = Field(
        True, description="On mangled lines, retry just those with a halved batch size"
    )
    retry_min_batch_size: int = Field(
        1, description="Floor for the halving retry; set equal to batch_size to disable"
    )
    prompt: str = _commented(
        TEXT_FILTER_PROMPT,
        sample='"..."',
        description="System prompt for LLM (has sensible default for Japanese)",
    )
    temperature: float = Field(0.1, description="LLM sampling temperature")
    thinking: bool | str = Field(
        False, description='Thinking mode: true (= "high") / false, or "low"/"medium"/"high"'
    )
    keywords: list[str] = _commented(
        [],
        sample="[]",
        description="Constant keywords always injected into the filter LLM prompt",
    )


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "summary stage: runs once project-wide (over all videos) between sentence_split\n"
        "and text_filter. A larger LLM segments each transcript into line-range parts\n"
        "and summarises each (also listing misspelling-prone keywords per video), then\n"
        "a reduce step writes one all-videos summary. Output summary.json is a\n"
        "reviewable intermediate consumed by the text_filter, plan and director stages.\n"
        "Disabled by default (writes an empty summary = no-op)."
    )
    enabled: bool = Field(False, description="Enable the summary LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "gpt-oss:120b", description='A larger model (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.3)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    response_format: str = Field("json")
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / unparseable JSON (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    prompt: str = _commented(
        SUMMARY_PROMPT,
        sample='"..."',
        description="Per-video segment+summarise prompt (has a sensible default)",
    )
    overall_prompt: str = _commented(
        SUMMARY_OVERALL_PROMPT,
        sample='"..."',
        description="All-videos reduce prompt (has a sensible default)",
    )


class PlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "plan stage: runs once project-wide after summary, before director. A larger\n"
        "LLM reads the per-part summaries (with line ranges) of all videos and gives a\n"
        "coarse, cross-video editorial direction per part. Output plan.json is a\n"
        "reviewable intermediate consumed by director. Disabled by default (no-op)."
    )
    enabled: bool = Field(False, description="Enable the plan LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "gpt-oss:120b", description='A larger model (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.3)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    response_format: str = Field("json")
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / unparseable JSON (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    prompt: str = _commented(
        PLAN_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )


class PlanReviseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "plan_revise stage: runs once project-wide between plan and director. It reads\n"
        "plan/plan.json and the conversation in plan_dialogue/history.md and revises the\n"
        "directions as operations (delete/add/update), writing plan_revise/plan.json --\n"
        "which director prefers over plan/plan.json. It makes NO LLM call unless a human\n"
        "turn is unanswered, and a plan re-run invalidates its output. Disabled by\n"
        "default (no-op)."
    )
    enabled: bool = Field(False, description="Enable the plan_revise LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "gpt-oss:120b", description='A larger model (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.3)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    response_format: str = Field("json")
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / unparseable JSON (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    prompt: str = _commented(
        PLAN_REVISE_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )


class DirectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "director stage (Pass A): a larger LLM reads the whole numbered transcript and\n"
        "emits high-level edit operations as {stem}_director.json. It never re-outputs\n"
        "the transcript text. Disabled by default (writes an empty op list = no-op)."
    )
    enabled: bool = Field(False, description="Enable the director LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "gpt-oss:120b", description='A larger model (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.2)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    response_format: str = Field("json", description="JSON mode for reliable parsing")
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / unparseable JSON (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2, description="Temperature increment added on each retry")
    retry_temp_cap: float = Field(0.8, description="Maximum temperature any retry uses")
    chunk_lines: int = Field(
        40,
        description=(
            "How many lines of the whole-video transcript one turn of the director's "
            "conversation is asked to review (approximately — it stops where the footage "
            "breaks). The turn cap is ceil(display lines / this) * 2, and reaching it "
            "fails the stage after writing the ops accepted so far"
        ),
    )
    max_keep_lines: int = Field(
        8,
        description=(
            'Reject a "keep" op wider than this many lines (0 = no limit); a keep may span a '
            "continuous on-screen event, but one this wide is marking talking, not an event"
        ),
    )
    max_prior_captions: int = Field(
        100,
        description=(
            "How many captions already shown earlier in the finished video the director "
            "is told about (0 = no limit); the most recent ones are kept"
        ),
    )
    seam_lines: int = Field(
        3,
        description=(
            "How many lines of the videos playing immediately before/after this one the "
            "director is shown at each join (0 = off), so a sign-off or a greeting is "
            "visible as addressing an audience that is already mid-video"
        ),
    )
    silence_line_min: float = Field(
        5.0,
        description=(
            "Shortest wait between two lines (seconds) shown to the director as a "
            'silence line of its own, addressable as "n~"; a shorter one stays a '
            "`gap Xs` figure in the preceding line's bracket. Matches gap_context."
            "min_gap by default, so every described gap has a line to land in"
        ),
    )
    whole_project_context: bool = Field(
        False,
        description=(
            "Show every segment's call the whole finished video's transcript (cached, "
            "with each segment's default runtime) and the edits already made to the "
            "segments playing earlier, instead of only their captions"
        ),
    )
    prompt: str = _commented(
        DIRECTOR_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )


class GuidedEditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "guided_edit stage (Pass B2): a small local LLM applies each director op,\n"
        "inserting <cut>/<speed>/<overlay>/<keep> tags (and {{old->new}} patches) into\n"
        "the verbatim _edits.txt. Disabled by default (copies edits through)."
    )
    enabled: bool = Field(False, description="Enable applying director ops")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "qwen3.5:4b",
        description='The same small model as text_filter is fine (passed to LiteLLM as "<provider>/<model>")',
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.1)
    thinking: bool | str = Field(False)
    timeout: int = Field(60)
    context_lines: int = Field(
        1, description='Lines of context shown to the LLM around an "edit" op boundary'
    )
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / failed verification (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2, description="Temperature increment added on each retry")
    retry_temp_cap: float = Field(0.8, description="Maximum temperature any retry uses")
    prompt: str = _commented(
        GUIDED_EDIT_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )


class GapContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "gap_context stage: runs per video between sentence_split and summary. Long\n"
        "silent spans from the audio_silence cut list are snapshotted (up to 3 frames\n"
        "each, via ffmpeg in the whisperx image) and described by a VISION LLM, so the\n"
        "summary and director stages can see what happens on screen while nobody is\n"
        "speaking (and keep a gap worth watching). Output {stem}_gaps.json is a\n"
        "reviewable intermediate. Disabled by default (writes an empty gap list = no-op)."
    )
    enabled: bool = Field(False, description="Enable the gap_context vision LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "qwen2.5vl:7b",
        description='A VISION-capable model (passed to LiteLLM as "<provider>/<model>")',
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.2)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / empty response (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    min_gap: float = Field(
        3.0, description="Only silent spans at least this long (seconds) get snapshots"
    )
    frame_width: int = Field(
        960, description="Downscale width (px) of the extracted JPEG frames; height is auto"
    )
    static_ssim: float = Field(
        0.96,
        ge=0.0,
        le=1.0,
        description=(
            "Skip the vision call when the minimum SSIM across the gap's consecutive "
            "extracted-frame pairs is at least this (pixel-static prefilter; the gap is "
            "recorded as static). 0 disables"
        ),
    )
    context_lines: int = Field(
        1,
        ge=0,
        description="Transcript lines given to the vision LLM on each side of the gap (0 = none)",
    )
    prompt: str = _commented(
        GAP_CONTEXT_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )


class CaptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_bunsetu: int = Field(12, description="Maximum bunsetsu units per caption chunk")
    max_duration: float = Field(4.0, description="Maximum seconds per caption chunk")
    min_bunsetu: int = Field(3, description="Minimum bunsetsu before flushing chunk")
    min_duration: float = Field(1.5, description="Minimum seconds before flushing chunk")
    silence_flush: float = Field(1.5, description="Silence duration that forces chunk flush")
    bunsetu_separator: str = Field(" ", description="Separator between bunsetsu units")
    pre_margin: float = Field(
        0.0,
        description="Seconds to extend each caption before its start (clamped to previous caption end)",
    )
    post_margin: float = Field(
        0.0,
        description="Seconds to extend each caption after its end (clamped to next caption start)",
    )


class BunsetuConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    char_eps: float = Field(0.02)
    silence_max_word_span: float = Field(0.6)


class IntervalsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "intervals stage: patch application + keep-interval merge.\n"
        "Audio cuts from audio_silence are unioned here."
    )
    silence_threshold: float = Field(
        1.5,
        description="WhisperX word-gap silence threshold, seconds (NOT the audio_silence detector)",
    )
    min_keep: float = Field(1.0, description="Minimum keep interval length in seconds")
    keep_pre_margin: float = Field(1.0, description="Seconds to extend keep intervals before start")
    keep_post_margin: float = Field(1.0, description="Seconds to extend keep intervals after end")
    min_cut: float = Field(
        0.4,
        description=(
            "Merge adjacent keep intervals separated by less than this (seconds); "
            "absorbs cuts too short to be worth the jump, which margin arithmetic "
            "otherwise leaves behind as slivers. 0 disables"
        ),
    )
    caption: CaptionConfig = Field(default_factory=CaptionConfig)
    bunsetu: BunsetuConfig = Field(default_factory=BunsetuConfig)


class CaptionStyleConfig(BaseModel):
    # Open-ended: any extra key is forwarded 1:1 to the Blender TextStrip RNA
    # attribute of the same name (incl. `font`), so extras must be allowed.
    model_config = ConfigDict(extra="allow")
    example_extra: ClassVar[str] = (
        "# font: /abs/path/to/MyFont.ttf   # Absolute path to a font file; loaded as a Blender VectorFont.\n"
        "# color: [1, 1, 1, 1]             # Font fill color (RGBA, 0.0 - 1.0); default white\n"
        '# color: "#FFCC00"                # ...or a hex string, alpha optional (#RRGGBBAA)\n'
        "# use_outline: false              # Enable text outline\n"
        "# outline_color: [0, 0, 0, 1]     # Outline color (RGBA)\n"
        "# use_box: false                  # Enable background box behind text\n"
        "# box_color: [0, 0, 0, 0.5]       # Box color (RGBA)\n"
        "# Any color key (color / *_color) takes either form. To copy a color OUT of\n"
        "# Blender, use the color picker's Hex field -- NOT Ctrl-C on the swatch, which\n"
        "# linearises the value and pastes back visibly darker (0.5 copies as 0.214).\n"
        "# Any other key is forwarded verbatim to the Blender TextStrip attribute of the\n"
        "# same name (see bpy.types.TextStrip), e.g. shadow_color / shadow_offset /\n"
        "# shadow_blur / box_margin. An unknown key is logged and skipped."
    )
    font_size: int = Field(50)
    alignment_x: str = Field("CENTER", description="LEFT | CENTER | RIGHT")
    anchor_y: str = Field("BOTTOM", description="TOP | CENTER | BOTTOM")
    location_x: float = Field(0.5, description="Horizontal position (0.0 - 1.0)")
    location_y: float = Field(0.05, description="Vertical position (0.0 - 1.0)")
    use_shadow: bool = Field(True, description="Enable text shadow")
    wrap_width: float = Field(0.90, description="Text wrap width (0.0 = no wrap, 0.0 - 1.0)")


class OverlayStyleConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    section_comment: ClassVar[str] = (
        'Overlay TEXT strip style for <overlay text="..." duration="N.N"/> markers.\n'
        "Any field not set here is inherited from caption_style."
    )
    example_extra: ClassVar[str] = (
        "# font_size: 50       # inherits from caption_style if omitted\n"
        "# alignment_x: CENTER\n"
        "# location_x: 0.5\n"
        '# color: [1, 1, 1, 1]  # overrides caption_style (or a hex string, e.g. "#FFCC00")'
    )
    anchor_y: str = Field("TOP", description="default: TOP (overlays sit at top of frame)")
    location_y: float = Field(0.95, description="default: 0.95")


class SpeedMarkConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    section_comment: ClassVar[str] = (
        "Speed-up mark: auto on-screen badge over every <speed> region."
    )
    example_extra: ClassVar[str] = (
        "# color: [1, 1, 1, 1]   # Font fill color (RGBA or hex); overrides caption_style"
    )
    enabled: bool = Field(True, description="set false to disable all speed badges")
    template: str = Field(
        "x{factor}", description="{factor} is the speed factor (one decimal, e.g. 2.0)"
    )
    font_size: int = Field(35)
    alignment_x: str = Field("RIGHT")
    anchor_y: str = Field("TOP")
    location_x: float = Field(0.95)
    location_y: float = Field(0.95)


class BlenderRenderConfig(BaseModel):
    # Open-ended: every key is forwarded 1:1 to the `scene.render` RNA attribute
    # of the same name, and a nested mapping recurses into the sub-struct of
    # that name (`image_settings`, `ffmpeg`), so extras must be allowed.
    model_config = ConfigDict(extra="allow")
    section_comment: ClassVar[str] = (
        "Render/output settings, written onto the scene so the .blend opens ready to\n"
        "render instead of needing the encoder, resolution and audio set by hand every\n"
        "time. Empty by default: what is not set here keeps coming from the first\n"
        "source that plays (resolution and fps) or from Blender's own defaults.\n"
        "\n"
        "Every key is the Blender attribute name, forwarded verbatim to scene.render\n"
        "(see bpy.types.RenderSettings); a nested mapping recurses into the sub-struct\n"
        "of that name. Keys are applied in the order written, which matters where one\n"
        "setting widens another's choices (image_settings.media_type: VIDEO is what\n"
        "puts FFMPEG in file_format's enum at all). An unknown key is logged and\n"
        "skipped; an invalid VALUE (a misspelt enum member) is a hard error, because\n"
        "rendering with a codec other than the one asked for is a defect found only by\n"
        "playing the file.\n"
        "\n"
        "fps given without fps_base resets fps_base to 1.0 -- the scene otherwise\n"
        "carries the source's pulldown (1.001 for 29.97) and `fps: 30` would quietly\n"
        "stay 29.97."
    )
    example_extra: ClassVar[str] = (
        "# To use any of these, delete the `{}` above and uncomment the keys you want.\n"
        "# resolution_x: 1920\n"
        "# resolution_y: 1080\n"
        "# resolution_percentage: 100\n"
        "# fps: 30                        # Overrides the first source's measured fps\n"
        '# filepath: "//../renders/final"  # `//` is relative to the .blend, so `//../`\n'
        "#                                 # is output/ (the .blend sits in\n"
        "#                                 # output/blender/). Blender creates missing\n"
        "#                                 # directories; an absolute path also works.\n"
        "# image_settings:\n"
        "#   media_type: VIDEO             # Blender 5.x: VIDEO before file_format, which\n"
        "#   file_format: FFMPEG           # otherwise offers only the IMAGE formats.\n"
        "#                                 # Keys are applied in the order written here.\n"
        "# ffmpeg:\n"
        "#   format: MPEG4\n"
        "#   codec: H264\n"
        "#   constant_rate_factor: HIGH    # LOSSLESS|PERC_LOSSLESS|HIGH|MEDIUM|LOW|...\n"
        "#   ffmpeg_preset: GOOD           # BEST | GOOD | REALTIME\n"
        "#   gopsize: 18\n"
        "#   audio_codec: AAC              # NONE | AAC | AC3 | FLAC | MP2 | MP3 | OPUS | PCM | VORBIS\n"
        "#   audio_bitrate: 192\n"
        "#   audio_mixrate: 48000\n"
        "#   audio_channels: STEREO"
    )


class BlenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = "blender stage: Blender VSE layout."
    use_proxy: bool = Field(
        True, description="Enable proxy on movie strips for smooth VSE playback"
    )
    proxy_size: int = Field(100, description="Proxy render size percentage: 25 | 50 | 75 | 100")
    render: BlenderRenderConfig = Field(default_factory=BlenderRenderConfig)
    caption_style: CaptionStyleConfig = Field(default_factory=CaptionStyleConfig)
    overlay_style: OverlayStyleConfig = Field(default_factory=OverlayStyleConfig)
    speed_mark: SpeedMarkConfig = Field(default_factory=SpeedMarkConfig)


PAIRING_PROMPT = (
    "You put each thumbnail headline on the photograph that shows what it "
    "promises, and decide how the text sits on that photograph.\n"
    "\n"
    "You receive the thumbnail COPY SETS (alternatives a human will choose "
    "between, each one to three numbered lines) and a numbered list of "
    "CANDIDATE FRAMES. Each frame shows the moment's kind, its time, the "
    "editor's label for it, and -- where one exists -- a description of what "
    "is actually visible in it, written by someone who looked. The label is a "
    "claim about the moment; the description is what a viewer would really "
    "see. Where they disagree, believe the description.\n"
    "\n"
    "You never see the images themselves and you never name a file. Name a "
    "frame by its INDEX in the list. Output ONLY a JSON object.\n"
    "\n"
    "JSON shape:\n"
    "{\n"
    '  "sets": [\n'
    '    {"set": 1, "frame": 7,\n'
    '     "gravity": "northwest", "offset": "+56+62",\n'
    '     "shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"},\n'
    '     "lines": [{"line": 1, "font": "<slot>", "pointsize": 70,\n'
    '                "fill": "white", "stroke": "rgba(30,30,30,1)", "strokewidth": 8},\n'
    '               {"line": 2, "font": "<slot>", "pointsize": 156,\n'
    '                "fill": "#B08D3E", "stroke": "rgba(30,30,30,1)", "strokewidth": 12}]}\n'
    "  ]\n"
    "}\n"
    "\n"
    "Rules:\n"
    "- One entry per copy set, keyed by the set number as given.\n"
    '- "frame": the index of a frame that actually SHOWS what that set\'s '
    "hook promises. A hook about a leak belongs on a frame whose description "
    "mentions the leak, not on one whose label merely says so. If no frame "
    "shows it, pick the one that comes closest and do not pretend "
    "otherwise.\n"
    "- Two sets may use the same frame, but prefer different ones: they are "
    "alternatives, and four treatments of one photograph is one thumbnail "
    "with four captions.\n"
    '- "gravity" (northwest / north / … / southeast) and "offset" (+x+y from '
    "that corner) place the whole block. Put it where the frame's "
    "description says the picture is EMPTY -- never across the subject. Line "
    "positions within the block are computed for you, so give the block "
    "anchor, not a position per line.\n"
    '- "fill" and "stroke" are colours (#RRGGBB or rgba(r,g,b,a)); '
    '"strokewidth" (0-40) is the outline that keeps text readable over a '
    "photo. Choose them against what the description says is behind the "
    "text: light text with a dark outline on a dark region, and the reverse "
    "on a light one. Do not put a mid-tone colour on a mid-tone region.\n"
    '- "pointsize" (8-400) on a 1280x720 canvas: a hook is large, a tag '
    "small, a subtitle in between.\n"
    '- "line" is the line number within that set, as given.\n'
    "- Leave out anything you have no reason to choose; a sensible default is "
    "used for it.\n"
    "- Output only the JSON object, no other text."
)


class PairingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "Pairing: a second, text-only call that puts each thumbnail headline on the\n"
        "frame that shows what it promises, and decides where the text sits and in what\n"
        "colour -- from the frame DESCRIPTIONS above, never from images, and naming its\n"
        "frame by INDEX (publish resolves that to a path before writing publish.json).\n"
        "It is kept apart from the copy call because two dozen frame descriptions in\n"
        "front of that one makes it caption the photographs it can see instead of\n"
        "writing hooks from the story.\n"
        "\n"
        "Cost: exactly one extra text call per publish run, whatever the shortlist size.\n"
        "It needs NO new model -- it uses publish's own provider/model/api_base/api_key,\n"
        "AND publish's own sampling settings, since it is the same kind of call on the\n"
        'same model: a key left unset below means "inherit", not "use my own idea of a\n'
        'good temperature". That matters because some models accept exactly one\n'
        "temperature (claude-sonnet-5 wants 1.0) and reject anything else before the\n"
        "request leaves the machine.\n"
        "\n"
        "ON by default, unlike describe_frames, because the copy call no longer chooses\n"
        "colours or placement at all: turning this off is not a no-op but a fallback to\n"
        "the four built-in presets on one shared background -- which is what a project\n"
        "renders with no pairing at all, and is a downgrade rather than nothing."
    )
    enabled: bool = Field(True, description="Run the pairing call")
    # Unset = inherit publish's. The pairing call runs on publish's own model,
    # so it must be sampled the way that model requires: a hardcoded 0.2 beside
    # an inherited model that accepts only temperature=1 is a default
    # incompatible with the default it is paired with.
    temperature: float | None = _commented(
        None, sample="0.2", description="Sampling temperature; unset = publish.temperature"
    )
    max_retries: int | None = _commented(
        None, sample="2", description="Extra attempts; unset = publish.max_retries"
    )
    retry_temp_step: float | None = _commented(
        None, sample="0.2", description="Unset = publish.retry_temp_step"
    )
    retry_temp_cap: float | None = _commented(
        None, sample="0.8", description="Unset = publish.retry_temp_cap"
    )
    prompt: str = _commented(
        PAIRING_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )


class DescribeFramesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "Frame description: one VISION call per candidate still, describing what is\n"
        "actually legible in it, where the subject sits and which regions are empty --\n"
        "the director's label says what it THOUGHT was happening at that moment, only\n"
        "looking says what a viewer can make out. The pairing step below uses these to\n"
        "put each headline on a frame that shows what it promises.\n"
        "\n"
        "Cost: up to publish.max_frames calls the first time (24 by default), then only\n"
        "for frames whose picture CHANGED. Results are cached in publish/frames.json by\n"
        "a content hash of the JPEG itself, so re-running publish for better copy over\n"
        "an unchanged shortlist costs nothing, and a description you rewrite by hand is\n"
        "the description from then on.\n"
        "\n"
        "OFF by default because it needs a VISION-capable model and the rest of publish\n"
        "needs a text one: a project that has only configured publish.model would\n"
        "otherwise fire two dozen vision calls at a model that cannot see. Turning it\n"
        "off is not a no-op the way disabling a stage is -- frames.json is still written\n"
        "with the shortlist fields and hashes, so descriptions can be written by hand and\n"
        "are reused when this is switched on. Without descriptions the pairing call still\n"
        "runs, on the director's labels alone."
    )
    enabled: bool = Field(False, description="Enable the frame-description vision LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "qwen2.5vl:7b",
        description='A VISION-capable model (passed to LiteLLM as "<provider>/<model>")',
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.2)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / empty response (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    prompt: str = _commented(
        DESCRIBE_FRAMES_PROMPT,
        sample='"..."',
        description="System prompt (has a sensible default)",
    )


class PublishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "publish stage: runs once project-wide AFTER blender. An LLM turns the summaries\n"
        "and the project brief into several title candidates, a description lead, chapter\n"
        "titles and alternative thumbnail-copy sets (WORDS only -- see pairing below);\n"
        "the chapter TIMESTAMPS are computed from the finished timeline (keep intervals +\n"
        "speed ranges), which exists nowhere else. Stills are extracted at the moments the\n"
        "director marked as payoffs and shown in publish.md beside the copy.\n"
        "Compositing is NOT done here: the render stage (below) reads publish.json and\n"
        "runs ImageMagick, so a hook or a background can be hand-edited and re-rendered\n"
        "without paying for the copy again. Uploading, and picking which rendered set to\n"
        "ship, stay manual. Disabled by default (no-op).\n"
        "\n"
        "Cost of one enabled run: the copy call, plus the pairing call, plus one VISION\n"
        "call per candidate still that does not already have a description (see\n"
        "describe_frames)."
    )
    enabled: bool = Field(False, description="Enable the publish LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "gpt-oss:120b", description='A larger model (passed to LiteLLM as "<provider>/<model>")'
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(
        0.7,
        description="Higher than the editing stages: title and hook candidates should differ from each other",
    )
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    response_format: str = Field("json")
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / unparseable JSON (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    min_chapter_duration: float = Field(
        10.0,
        description=(
            "YouTube ignores a chapter list unless every chapter is at least this long; "
            "a shorter one is merged into a neighbour"
        ),
    )
    max_frames: int = Field(
        24,
        ge=0,
        description="Cap on thumbnail candidate stills (0 = no limit); overlays outrank keeps",
    )
    frame_width: int = Field(
        1280, description="Downscale width (px) of the extracted JPEG stills; height is auto"
    )
    prompt: str = _commented(
        PUBLISH_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )
    describe_frames: DescribeFramesConfig = Field(default_factory=DescribeFramesConfig)
    pairing: PairingConfig = Field(default_factory=PairingConfig)


class RenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "render stage: the LAST stage, and the only one that never makes an LLM call.\n"
        "It reads output/publish/publish.json -- the hand-editable contract -- and\n"
        "composites one thumbnail per copy set with ImageMagick (`magick` must be on\n"
        "PATH), each onto the background that set names (a path relative to\n"
        "output/publish/, or absolute; any aspect ratio, cropped to fill). Renders land\n"
        "in output/render/thumbnails/ beside render.json and render.md. Edit a hook or\n"
        "a background in publish.json and re-run --from-stage render --to-stage render:\n"
        "zero calls, every time. Only `fonts` is set here, because the model cannot know\n"
        "what is installed -- and SET IT if your copy is not plain ASCII: ImageMagick's\n"
        "default face draws nothing at all (not even a box) for a character it has no\n"
        "glyph for, so a CJK headline comes back invisible."
    )
    enabled: bool = Field(True, description="Render a thumbnail per copy set")
    width: int = Field(1280, description="Canvas width in px")
    height: int = Field(720, description="Canvas height in px")
    line_gap: int = Field(12, description="Vertical gap between stacked lines in px")
    fonts: dict[str, str] = _commented(
        {},
        sample='{sans-bold: "Noto-Sans-CJK-JP-Bold", serif-black: "Noto-Serif-CJK-JP-Black"}',
        description=(
            "Font slots the pairing call may choose from: slot name -> ImageMagick font "
            "name or path. THE FIRST ONE LISTED is also the fallback face for any line "
            "that named no slot (a preset cannot name one), so put a font that covers "
            "your language first. Leave this empty and ImageMagick's default face is "
            "used, which draws nothing at all for a CJK character"
        ),
    )


class CutReportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "cut_report: deterministic metrics + checks on the FINISHED cut (no LLM call).\n"
        "Runs after intervals (again after blender, with Blender's own warnings) and\n"
        "writes a section into llm_report/index.md next to the plan/director divergence.\n"
        "The measurements always print; only a breached threshold is flagged."
    )
    enabled: bool = Field(True, description="Write the finished-cut section into the LLM report")
    caption_chars_per_sec: float = Field(
        18.0,
        description=(
            "Flag a caption authored at or above this many characters per second. "
            "One inside a speed_range is flagged separately: the factor divides its "
            "on-screen time again, which is how a real run shipped 0.2s captions"
        ),
    )
    timelapse_max_screen: float = Field(
        180.0,
        description=(
            "Flag a timelapse that plays for more than this many seconds on screen -- "
            "a factor picked too low leaves a sustained fast-forward, a failure mode "
            "this pipeline hits repeatedly. There is deliberately NO floor: a short "
            "timelapse is ordinary, and its on-screen length is reported as a "
            "measurement instead"
        ),
    )
    min_keep_fragment: float = Field(
        1.0,
        description=(
            "Flag a keep interval shorter than this (seconds). Inter-keep GAPS are "
            "checked against intervals.min_cut instead -- that is the pass that owns them"
        ),
    )


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_videos_dir: str = Field("src_video")
    output_dir: str = Field(
        "output", description="Root dir; stage outputs go to per-stage named subdirs"
    )
    from_stage: str = Field(
        "transcription", description="Start from this stage; reuses earlier stage outputs"
    )
    to_stage: str = Field(
        "render", description="Stop after this stage (inclusive). Must not precede from_stage"
    )


class NagareClipConfig(BaseModel):
    """Root config model. Plain BaseModel -- no environment-variable source.
    Precedence (CLI > YAML file > model defaults) is realised entirely by the
    explicit merge in get_effective_config; validation happens via
    model_validate."""

    model_config = ConfigDict(extra="forbid")
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    audio_silence: AudioSilenceConfig = Field(default_factory=AudioSilenceConfig)
    sentence_split: SentenceSplitConfig = Field(default_factory=SentenceSplitConfig)
    gap_context: GapContextConfig = Field(default_factory=GapContextConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    text_filter: TextFilterConfig = Field(default_factory=TextFilterConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    plan_revise: PlanReviseConfig = Field(default_factory=PlanReviseConfig)
    director: DirectorConfig = Field(default_factory=DirectorConfig)
    guided_edit: GuidedEditConfig = Field(default_factory=GuidedEditConfig)
    intervals: IntervalsConfig = Field(default_factory=IntervalsConfig)
    blender: BlenderConfig = Field(default_factory=BlenderConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    cut_report: CutReportConfig = Field(default_factory=CutReportConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)


# Derived, NOT hand-authored: the canonical default dict.
DEFAULTS: dict[str, Any] = NagareClipConfig.model_validate({}).model_dump()


# ---------------------------------------------------------------------------
# Loading + merging (dict boundary preserved)
# ---------------------------------------------------------------------------


def load_config(path: Path | None) -> dict:
    """Load a YAML config file. Returns ``{}`` when *path* is ``None``."""
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*. *override* wins."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def get_effective_config(
    config_path: Path | None,
    cli_overrides: dict | None = None,
) -> dict:
    """Return the fully resolved, validated config as a plain dict.

    Precedence (highest wins): CLI overrides > YAML file > model defaults.
    Unknown or wrongly-typed keys raise ``pydantic.ValidationError`` (except in
    the open-ended Blender style blocks, which accept arbitrary extra keys).
    """
    merged = deep_merge(load_config(config_path), cli_overrides or {})
    if config_path is not None:
        logging.info("Config loaded from %s", config_path)
    return NagareClipConfig.model_validate(merged).model_dump()


# ---------------------------------------------------------------------------
# config.example.yml generation
# ---------------------------------------------------------------------------

PREAMBLE = """\
# Example configuration for nagare-clip pipeline.
# Copy to your project and pass via --config flag.
# All values shown are the defaults; remove or comment out any you don't want.
#
# LLM provider selection (applies to every LLM stage below):
#   Set `provider` to one of: ollama_chat (default, local), openai, gemini, anthropic.
#   `model` is the provider's model name; LiteLLM receives "<provider>/<model>".
#   For cloud providers set `api_key` (or the provider's env var, e.g.
#   OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY) and leave `api_base` empty.
#
# Langfuse tracing (optional, off unless keys are present):
#   Export LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to enable tracing. Disable
#   via general.langfuse: false or NAGARE_LANGFUSE=0.
"""


def _fmt_scalar(value: object) -> str:
    """Render a scalar/list default as inline YAML (deterministic)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_scalar(v) for v in value) + "]"
    if value is None:
        return "null"
    s = str(value)
    if s == "":
        return '""'
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        return s
    return '"' + s.replace('"', '\\"') + '"'


def _empty_suffix(rendered: list[str], indent: int) -> str:
    """`" {}"` for a section whose keys are ALL commented examples.

    ``project:`` and ``blender.render:`` render as a heading followed by nothing
    but comments; YAML reads that as ``null``, and the example file then cannot
    be loaded as a config at all.  An explicit empty mapping keeps it loadable
    while the commented keys stay where a human can uncomment them.
    """
    pad = " " * indent
    has_key = any(re.match(rf"^{pad}[A-Za-z_]", ln) for ln in rendered)
    return "" if has_key else " {}"


def _render_model(model_cls: type[BaseModel], indent: int) -> list[str]:
    """Render a model's fields (and nested models) as indented YAML lines."""
    pad = " " * indent
    lines: list[str] = []
    for name, field in model_cls.model_fields.items():
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            sub_comment = getattr(ann, "section_comment", "")
            if sub_comment:
                lines += [f"{pad}# {c}".rstrip() for c in sub_comment.split("\n")]
            sub_lines = _render_model(ann, indent + 2)
            lines.append(f"{pad}{name}:{_empty_suffix(sub_lines, indent + 2)}")
            lines += sub_lines
            continue
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
        desc = field.description or ""
        desc_suffix = f"   # {desc}" if desc else ""
        if extra.get("emit") == "commented":
            lines.append(f"{pad}# {name}: {extra['sample']}{desc_suffix}")
        else:
            lines.append(f"{pad}{name}: {_fmt_scalar(field.default)}{desc_suffix}")
    example_extra = getattr(model_cls, "example_extra", "")
    if example_extra:
        lines += [f"{pad}{c}" for c in example_extra.split("\n")]
    return lines


def generate_example_yaml() -> str:
    """Generate config.example.yml text from the models (single source of truth)."""
    out: list[str] = [PREAMBLE.rstrip("\n"), ""]
    for name, field in NagareClipConfig.model_fields.items():
        model_cls = field.annotation
        if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
            continue
        section_comment = getattr(model_cls, "section_comment", "")
        if section_comment:
            out += [f"# {c}".rstrip() for c in section_comment.split("\n")]
        section = _render_model(model_cls, 2)
        out.append(f"{name}:{_empty_suffix(section, 2)}")
        out += section
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _main(argv: list[str]) -> int:
    if argv and argv[0] == "--write-example":
        Path("config.example.yml").write_text(generate_example_yaml(), encoding="utf-8")
        print("Wrote config.example.yml")
        return 0
    print("usage: python -m nagare_clip.config --write-example", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
