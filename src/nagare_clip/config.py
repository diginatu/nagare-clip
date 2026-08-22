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
    "Timing: a part may carry a bracket after its line range — "
    "[4.2s, gap 0.8s] means the part has a duration of 4.2 seconds and is "
    "followed by a 0.8-second silent gap before the next part of the same "
    "video. A negligible gap, and the last part of a video, show no gap "
    "([4.2s]); a part with unknown timing has no "
    "bracket. "
    "A part containing long internal silences splits its duration — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken; the "
    "silent seconds are dropped by default. Judge "
    "pacing from the speech figure. "
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
    "You may also receive a conversation with the human editor, and your own "
    "previous directions shown under the part they belong to. Treat that as an "
    "EDIT of the existing plan, not a fresh start: change only the directions "
    "the conversation calls for, and repeat every other direction unchanged, "
    'word for word. Use "message" to say what you were unsure about, which '
    "directions you would like confirmed, and what you did with the human's "
    "last instruction. The human reads that message and may reply.\n"
    "\n"
    "JSON shape:\n"
    '{"directions": [\n'
    '  {"index": 1, "direction": "feature — the product\'s operating noise '
    'is the point"},\n'
    '  {"index": 2, "direction": "remove — repeats part 1"},\n'
    '  {"index": 3, "lines": [12, 20], "direction": "emphasise — the '
    'demonstration itself"}\n'
    "],\n"
    ' "message": "part 3 mixed an announcement with the demonstration, so I '
    'split it — is that right?"}\n'
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
    "- Output only the JSON object, no other text."
)

DIRECTOR_PROMPT = (
    "You are a video editor. You receive a Japanese transcript as "
    "numbered lines (one line per subtitle segment). Decide high-level "
    "edits to tighten AND STAGE the video: cut what drags, but also mark "
    "the moments that make it worth watching — turning points, payoffs, "
    "failures, mishaps. Do NOT rewrite or output the "
    "transcript text. Output ONLY a JSON object.\n"
    "\n"
    "Timing: a line may carry a bracket after its text — [4.2s, gap 0.8s] "
    "means the line lasts 4.2 seconds and is followed by a 0.8-second silent "
    "gap before the next line. A negligible gap, and the last line, show no "
    "gap ([4.2s]); a line with "
    "unknown timing has no bracket. "
    "A line containing long internal silences splits its duration — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken; the "
    '62.9 silent seconds are dropped by default (a "keep" over the line '
    "preserves them). Judge pacing from the speech figure, never from "
    "speech+silence. "
    "Use these numbers to judge pacing: a long speech duration is a "
    "candidate for cutting, not speeding up — a long stretch of manual "
    "work is a timelapse candidate instead. A long gap is "
    "dead air by default, already dropped — but that is the fallback "
    "reading, not the only one. Check the speech just before and after it: "
    "if it announces something happening (an accident, a cleanup, a wait "
    "for a result), the gap itself may be the most watchable moment in the "
    'shot, and a "keep" spanning that line and the next (lines [N, N+1]) '
    "preserves it.\n"
    "\n"
    "Visual context: an indented line like\n"
    "    [silent gap 12.4s: a build runs and logs scroll past]\n"
    "may follow a numbered line. It describes what is VISIBLE on screen "
    "during the silence after that line (nobody is speaking). Such gaps are "
    "dropped by default. If the gap shows something worth watching, emit a "
    '"keep" op spanning that line and the next one — a keep over lines '
    "[N, N+1] preserves the silence between them. If the described action "
    "continues across several gaps (it is still unfolding in the next "
    "annotation, or the speech around it narrates the same event), span the "
    "whole run in ONE keep instead of one narrow keep per gap. Annotation "
    "lines are not numbered; never reference them as op lines.\n"
    "\n"
    "Operations (reference lines by their 1-based numbers, inclusive). "
    "Prefer a timelapse over a cut where the repetition is VISIBLE WORK building "
    "toward a payoff (failed attempts, assembly, waiting for a result) — "
    "the buildup is part of the story, so timelapse it rather than "
    "deleting it. Where the repetition is SPEECH, there is no fast option: "
    "leave it at 1x, or cut the weakest passes. Reserve cut for spans "
    "that leave the throughline entirely (digressions, dead ends, "
    "redundant retakes with no payoff):\n"
    "- cut: remove a boring/redundant span entirely (deletes audio+video).\n"
    '- timelapse: play a long stretch of manual work fast under one on-screen caption; give "factor" (4.0 or more) and "text". Compressing a stretch is a choice between two modes and this is the second one. LISTENING: the speech carries something the viewer needs — play it at 1x, and if it drags cut the weakest parts instead. TIMELAPSE: the speech is inessential — go genuinely fast and accept that the words become unintelligible; that sacrifice is the point of the mode and is why you must be sure first. Pick "factor" so the result runs about a minute on screen: a longer span needs a bigger number, and a span where little is happening can go faster still. One op does the whole arrangement: the work runs continuously (its internal pauses are preserved, so it is not chopped into jump cuts) and the caption stays on screen for the entire timelapse — do not add a separate "keep" or "overlay" over the same lines. To change the caption partway through, emit consecutive timelapse ops; a new caption means a new phase of work.\n'
    '- overlay: show an on-screen caption; give "text" and "duration" '
    "(how many seconds it stays on screen). Pick the duration from reading "
    "length — a short label needs about 2 seconds, a full sentence 4 to 6; "
    'never a fixed value. Its "lines" say WHERE it appears (the caption '
    "starts at the first line of the range), not how long it shows. Reach "
    "for it at turning points, conclusions, failures, and mishaps — "
    "moments worth labeling on screen. Aim for roughly one overlay per "
    "3-5 minutes of finished video as a loose default target; if an "
    "editorial brief states otherwise, follow the brief instead.\n"
    "- keep: protect a span from cutting INCLUDING its silences/non-speech "
    "gaps (which are dropped by default). Let its width follow what is ON "
    "SCREEN. To rescue one silent gap, use the narrowest range covering it — "
    "the line before it and the next one ([N, N+1]). When a continuous event "
    "is playing out across several gaps — an accident and the cleanup after "
    "it, a demo running, a result arriving — span the WHOLE event in one "
    "keep, so the payoff is not chopped into jump cuts. Never widen a keep "
    "to mark talking as important: speech is never dropped by default, so a "
    "keep over a talking span only restores its pauses and inflates the "
    "runtime for nothing. A project-context direction saying a part should be "
    '"featured", "retained" or "emphasised" is editorial emphasis, NOT a '
    "request for a keep op.\n"
    '- edit: request a fine within-line text deletion/fix; describe it in "note".\n'
    "\n"
    "JSON shape:\n"
    '{"ops": [\n'
    '  {"type": "cut", "lines": [12, 18], "note": "why / where precisely"},\n'
    '  {"type": "timelapse", "lines": [60, 92], "factor": 8.0, "text": "配管の取り付け", "note": "..."},\n'
    '  {"type": "overlay", "lines": [5, 5], "text": "ポイント", "duration": 2.0, "note": ""},\n'
    '  {"type": "keep", "lines": [40, 42], "note": "..."},\n'
    '  {"type": "edit", "lines": [7, 7], "note": "delete the redundant restatement"}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    '- "lines" must be within the transcript range.\n'
    '- A "cut" range must not overlap any other op\'s range: cutting deletes '
    "the span, so never include a line you also keep/overlay/timelapse in "
    "a cut (e.g. to cut lines 12-18 but keep line 18, emit cut [12, 17]). "
    "Overlapping ops are clipped and the cut loses the shared lines.\n"
    '- Use "note" to describe in natural language precisely WHERE in the '
    "line(s) the edit starts and ends, so a downstream editor can place "
    "it exactly.\n"
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
    '    {"lines": [{"role": "tag", "text": "...", "font": "<slot>", "pointsize": 70,\n'
    '                "fill": "white", "stroke": "rgba(30,30,30,1)", "strokewidth": 8},\n'
    '               {"role": "hook", "text": "...", "font": "<slot>", "pointsize": 156,\n'
    '                "fill": "#B08D3E", "stroke": "rgba(250,250,250,1)", "strokewidth": 12}],\n'
    '     "gravity": "northwest", "offset": "+56+62",\n'
    '     "shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"}}\n'
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
    "- Each thumbnail set also carries its own LOOK, as ImageMagick options "
    'on a 1280x720 canvas: per line "fill" and "stroke" colours '
    '(#RRGGBB or rgba(r,g,b,a)), "strokewidth" (0-40, the outline that '
    'keeps text readable over a photo), "pointsize" (8-400; a hook is '
    'large, a tag small); per set "gravity" (northwest / north / … / '
    'southeast), "offset" (+x+y from that corner) and "shadow". The sets '
    "must DIFFER visibly from each other in colour and placement, not only "
    "in wording — they are alternatives a human chooses between. Line "
    "positions are computed, so give the block anchor, not a position per "
    "line.\n"
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
        False, description='Thinking mode: true/false, or "low"/"medium"/"high"'
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
        "# use_outline: false              # Enable text outline\n"
        "# outline_color: [0, 0, 0, 1]     # Outline color (RGBA)\n"
        "# use_box: false                  # Enable background box behind text\n"
        "# box_color: [0, 0, 0, 0.5]       # Box color (RGBA)\n"
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
        "# color: [1, 1, 1, 1]  # overrides caption_style"
    )
    anchor_y: str = Field("TOP", description="default: TOP (overlays sit at top of frame)")
    location_y: float = Field(0.95, description="default: 0.95")


class SpeedMarkConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    section_comment: ClassVar[str] = (
        "Speed-up mark: auto on-screen badge over every <speed> region."
    )
    example_extra: ClassVar[str] = (
        "# color: [1, 1, 1, 1]   # Font fill color (RGBA); overrides caption_style"
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


class BlenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = "blender stage: Blender VSE layout."
    default_fps: float = Field(30.0, description="Fallback FPS when source metadata unavailable")
    use_proxy: bool = Field(
        True, description="Enable proxy on movie strips for smooth VSE playback"
    )
    proxy_size: int = Field(100, description="Proxy render size percentage: 25 | 50 | 75 | 100")
    caption_style: CaptionStyleConfig = Field(default_factory=CaptionStyleConfig)
    overlay_style: OverlayStyleConfig = Field(default_factory=OverlayStyleConfig)
    speed_mark: SpeedMarkConfig = Field(default_factory=SpeedMarkConfig)


class ThumbnailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "Thumbnail rendering: one image per LLM copy set, composited with ImageMagick\n"
        "(`magick` must be on PATH). The LLM writes the colours, point sizes and\n"
        "placement for its own copy, in ImageMagick's vocabulary; only `fonts` is set\n"
        "here, because the model cannot know what is installed. Renders land in\n"
        "output/publish/thumbnails/ and are embedded in publish.md."
    )
    enabled: bool = Field(True, description="Render a thumbnail per copy set")
    background: str = Field(
        "",
        description=(
            "Still to composite onto: path relative to output/publish/ (or absolute); "
            "empty = the first candidate in the frame shortlist"
        ),
    )
    width: int = Field(1280, description="Canvas width in px")
    height: int = Field(720, description="Canvas height in px")
    line_gap: int = Field(12, description="Vertical gap between stacked lines in px")
    fonts: dict[str, str] = _commented(
        {},
        sample='{sans-bold: "Noto-Sans-CJK-JP-Bold", serif-black: "Noto-Serif-CJK-JP-Black"}',
        description="Font slots the LLM may choose from: slot name -> ImageMagick font name or path",
    )


class PublishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "publish stage: runs once project-wide AFTER blender. An LLM turns the summaries\n"
        "and the project brief into several title candidates, a description lead, chapter\n"
        "titles and alternative thumbnail-copy sets (copy AND look, in ImageMagick's own\n"
        "vocabulary); the chapter TIMESTAMPS are computed from the finished timeline (keep\n"
        "intervals + speed ranges), which exists nowhere else. Stills are extracted at the\n"
        "moments the director marked as payoffs, one thumbnail is rendered per copy set\n"
        "with ImageMagick (see publish.thumbnail below), and publish.md embeds the results\n"
        "alongside publish.json (the hand-editable contract for the look). Uploading, and\n"
        "picking which rendered set to ship, stay manual. Disabled by default (no-op)."
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
    image_markup: Literal["html", "markdown"] = Field(
        "html",
        description=(
            "How publish.md embeds images: html = <img> tags (sized), "
            "markdown = ![alt](path) for viewers that strip raw HTML"
        ),
    )
    thumbnail: ThumbnailConfig = Field(default_factory=ThumbnailConfig)
    prompt: str = _commented(
        PUBLISH_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
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
        "publish", description="Stop after this stage (inclusive). Must not precede from_stage"
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
    director: DirectorConfig = Field(default_factory=DirectorConfig)
    guided_edit: GuidedEditConfig = Field(default_factory=GuidedEditConfig)
    intervals: IntervalsConfig = Field(default_factory=IntervalsConfig)
    blender: BlenderConfig = Field(default_factory=BlenderConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
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


def _render_model(model_cls: type[BaseModel], indent: int) -> list[str]:
    """Render a model's fields (and nested models) as indented YAML lines."""
    pad = " " * indent
    lines: list[str] = []
    for name, field in model_cls.model_fields.items():
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            sub_comment = getattr(ann, "section_comment", "")
            if sub_comment:
                lines += [f"{pad}# {c}" for c in sub_comment.split("\n")]
            lines.append(f"{pad}{name}:")
            lines += _render_model(ann, indent + 2)
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
            out += [f"# {c}" for c in section_comment.split("\n")]
        out.append(f"{name}:")
        out += _render_model(model_cls, 2)
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
