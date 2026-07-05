"""Typed configuration models, loading, and merging (pydantic-settings).

The pydantic models below are the single source of truth for every config
default, its type, and its documentation.  ``get_effective_config`` validates a
merged (defaults <- file <- CLI) config and returns a plain ``dict`` so existing
stage code keeps reading ``cfg["section"]["key"]`` unchanged (the "dict
boundary").  ``config.example.yml`` is generated from these models
(``generate_example_yaml`` / ``--write-example``).
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    "\n"
    "Example:\n"
    "Input:\n"
    "1: えーとそれは急はいい天気ですね\n"
    "2: 正しい文です\n"
    "3: (雑音)\n"
    "\n"
    "Output:\n"
    "1: {{えーと->}}それは{{急は->今日は}}いい天気ですね\n"
    "2: 正しい文です\n"
    "3: {{(雑音)->}}"
)

SUMMARY_LLM_PROMPT = (
    "Analyze the following Japanese transcript from a video.\n"
    "Provide a JSON object with:\n"
    '- "summary": A very short summary (1-2 sentences) of the content.\n'
    '- "keywords": A list of rare or domain-specific words that speech '
    "recognition might misspell.\n"
    "\n"
    "Output only the JSON object, no other text."
)

SUMMARY_PROMPT = (
    "You are a video editor. You receive ONE Japanese transcript as "
    "numbered lines (one line per subtitle segment). Split it into a few "
    "contiguous PARTS by topic/section and summarise each part. Reference "
    "lines by their 1-based numbers (inclusive). Output ONLY a JSON object.\n"
    "\n"
    "JSON shape:\n"
    '{"parts": [\n'
    '  {"lines": [1, 12], "summary": "what this part covers"},\n'
    '  {"lines": [13, 40], "summary": "..."}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    "- Parts must be contiguous and within the transcript range.\n"
    "- Keep each summary to one short sentence.\n"
    "- Output only the JSON object, no other text."
)

SUMMARY_OVERALL_PROMPT = (
    "You are a video editor. You receive numbered per-part summaries "
    "spanning several source videos of one project. Write ONE concise "
    "overall summary of the whole project. Output ONLY a JSON object:\n"
    '{"summary": "..."}\n'
    "Output only the JSON object, no other text."
)

PLAN_PROMPT = (
    "You are a video editor planning a rough cut across several source "
    "videos. You receive numbered PARTS (each with a source video, a line "
    "range, and a summary) plus an overall summary. For each part, give a "
    "ROUGH editorial direction — what to do with it (e.g. remove, shorten, "
    "speed up, keep, emphasise) and why, considering the whole project "
    "(e.g. a part that repeats an earlier one can be removed). Reference "
    "parts by their 1-based index. Output ONLY a JSON object.\n"
    "\n"
    'By default, non-speech stretches are dropped. "keep" preserves ALL '
    "content in the range (silences and non-speech gaps included) — use "
    "it when those moments matter.\n"
    "\n"
    "JSON shape:\n"
    '{"directions": [\n'
    '  {"index": 1, "direction": "keep — the product\'s operating noise '
    'is the point"},\n'
    '  {"index": 2, "direction": "remove — repeats part 1"}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    '- "index" must be one of the given part numbers.\n'
    "- Keep each direction to one short, actionable phrase.\n"
    "- Output only the JSON object, no other text."
)

DIRECTOR_PROMPT = (
    "You are a video editor. You receive a Japanese transcript as "
    "numbered lines (one line per subtitle segment). Decide high-level "
    "edits to tighten the video. Do NOT rewrite or output the "
    "transcript text. Output ONLY a JSON object.\n"
    "\n"
    "Operations (reference lines by their 1-based numbers, inclusive):\n"
    "- cut: remove a boring/redundant span entirely (deletes audio+video).\n"
    '- speed: play a span faster; give "factor" (e.g. 2.0). Internal silences/pauses are still dropped — add a "keep" over the same lines to preserve them while sped up.\n'
    '- overlay: show an on-screen caption over a span; give "text".\n'
    "- keep: protect a span from cutting, INCLUDING its silences/"
    "non-speech gaps (which are dropped by default).\n"
    '- edit: request a fine within-line text deletion/fix; describe it in "note".\n'
    "\n"
    "JSON shape:\n"
    '{"ops": [\n'
    '  {"type": "cut", "lines": [12, 18], "note": "why / where precisely"},\n'
    '  {"type": "speed", "lines": [30, 34], "factor": 2.0, "note": "..."},\n'
    '  {"type": "overlay", "lines": [5, 5], "text": "ポイント", "note": ""},\n'
    '  {"type": "keep", "lines": [40, 42], "note": "..."},\n'
    '  {"type": "edit", "lines": [7, 7], "note": "delete the redundant restatement"}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    '- "lines" must be within the transcript range.\n'
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
    '- Overlay text:  wrap it in <overlay text="...">...</overlay>\n'
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
    prompt: str = _commented(
        SENTENCE_SPLIT_PROMPT,
        sample='"..."',
        description="Bunsetsu-grouping prompt (has a sensible default)",
    )


class SummaryLLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "Summary LLM: generates context (summary + keywords) for the filter LLM"
    )
    enabled: bool = Field(False, description="Enable summary generation before filtering")
    keywords: list[str] = _commented(
        [], sample="[]", description="Constant keywords always injected into the filter LLM prompt"
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
        "qwen3.5:4b",
        description='Model name (passed to LiteLLM as "<provider>/<model>"; can differ from filter LLM)',
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.3, description="Higher temperature for summarization")
    thinking: bool | str = Field(False, description="Thinking mode for summary LLM")
    timeout: int = Field(120, description="Longer timeout since full transcript is sent")
    response_format: str = Field("json", description="Request a JSON object from the summary LLM")
    prompt: str = _commented(
        SUMMARY_LLM_PROMPT,
        sample='"..."',
        description="System prompt for summary LLM (has a sensible default)",
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
    summary_llm: SummaryLLMConfig = Field(default_factory=SummaryLLMConfig)


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "summary stage: runs once project-wide (over all videos) before director. A\n"
        "larger LLM segments each transcript into line-range parts and summarises each,\n"
        "then a reduce step writes one all-videos summary. Output summary.json is a\n"
        "reviewable intermediate consumed by the plan + director stages. Disabled by\n"
        "default (writes an empty summary = no-op)."
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
        'Overlay TEXT strip style for <overlay text="..."> markers in _edits.txt.\n'
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
        "blender", description="Stop after this stage (inclusive). Must not precede from_stage"
    )


class NagareClipConfig(BaseSettings):
    """Root config. ``BaseSettings`` keeps env-var support available for the
    future, but ``get_effective_config`` uses ``model_validate`` (which does not
    read env), so current precedence is exactly CLI > YAML file > defaults."""

    model_config = SettingsConfigDict(extra="forbid")
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    audio_silence: AudioSilenceConfig = Field(default_factory=AudioSilenceConfig)
    sentence_split: SentenceSplitConfig = Field(default_factory=SentenceSplitConfig)
    text_filter: TextFilterConfig = Field(default_factory=TextFilterConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    director: DirectorConfig = Field(default_factory=DirectorConfig)
    guided_edit: GuidedEditConfig = Field(default_factory=GuidedEditConfig)
    intervals: IntervalsConfig = Field(default_factory=IntervalsConfig)
    blender: BlenderConfig = Field(default_factory=BlenderConfig)
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
