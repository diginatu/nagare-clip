# Sentence-Split Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `sentence_split` pipeline stage that re-segments the WhisperX transcript into one-sentence-per-line units via an LLM, rewriting both `{stem}.json` and `{stem}.txt` so the 1:1 line↔segment contract holds downstream.

**Architecture:** Per source, process segments in windows of W. For each window, GiNZA produces bunsetsu; an LLM (through `call_llm`) returns sentence groupings as **bunsetsu-index ranges** (`{"sentences":[[a,b],…]}`); we rebuild segments by slicing the original `words` array at bunsetsu boundaries, so word timings are preserved and text is verbatim by construction. Disabled by default → byte-identical copy-through.

**Tech Stack:** Python (uv), GiNZA/spaCy (`ja_ginza`, existing runtime dep), LiteLLM via `nagare_clip.llm_client.call_llm`, pytest, bash (`run_pipeline.sh`).

## Global Constraints

- Dependency management: uv + `pyproject.toml`; run all Python via `uv run`.
- All LLM access goes through `nagare_clip.llm_client.call_llm` (LiteLLM); no provider-specific clients.
- NLP via `ginza` + `ja_ginza` (spaCy); import GiNZA lazily (mirror `intervals/bunsetu.py`).
- Stages are identified by functional name only — no stage numbers. New stage name: `sentence_split`.
- Config priority: CLI flags > YAML config > `DEFAULTS`.
- TDD: write each test first, run it to confirm it fails, then implement. Where a test guards existing-looking code, mutate the implementation to confirm the test catches it, then revert; report the mutation evidence with the green result.
- Verbatim invariant: concatenation of all new segments' word `word` fields must equal the original concatenation; on violation, copy through unchanged.
- Default-off: when `sentence_split.enabled` is false, output `.json`/`.txt` are byte-identical to the transcription inputs.
- Work happens on branch `sentence-split-stage` (already created).

---

### Task 1: Config section `sentence_split`

**Files:**
- Modify: `src/nagare_clip/config.py` (insert a `"sentence_split"` block into `DEFAULTS`, after the `"summary"` block ~line 86)
- Modify: `config.example.yml` (add a documented `sentence_split:` block)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DEFAULTS["sentence_split"]` with keys `enabled`(False), `provider`, `api_base`, `model`, `api_key`, `temperature`, `thinking`, `timeout`, `response_format`, `max_retries`, `retry_temp_step`, `retry_temp_cap`, `window_segments`(20), `prompt`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_sentence_split_defaults_present():
    from nagare_clip.config import get_effective_config
    cfg = get_effective_config(None, {})
    sp = cfg["sentence_split"]
    assert sp["enabled"] is False
    assert sp["window_segments"] == 20
    assert sp["max_retries"] == 2
    assert sp["response_format"] == "json"
    assert isinstance(sp["prompt"], str) and sp["prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_sentence_split_defaults_present -v`
Expected: FAIL with `KeyError: 'sentence_split'`.

- [ ] **Step 3: Add the DEFAULTS block**

In `src/nagare_clip/config.py`, immediately after the `"summary": { … },` block in `DEFAULTS`, insert:

```python
    "sentence_split": {
        "enabled": False,
        "provider": "ollama_chat",
        "api_base": "",
        "model": "gpt-oss:120b",
        "api_key": "",
        "temperature": 0.2,
        "thinking": False,
        "timeout": 300,
        "response_format": "json",
        "max_retries": 2,
        "retry_temp_step": 0.2,
        "retry_temp_cap": 0.8,
        "window_segments": 20,
        "prompt": (
            "あなたは日本語の文字起こしを文単位に区切る編集者です。"
            "句読点はほとんどありません。\n"
            "入力は文節(bunsetsu)に 0 から連番を振ったものです。\n"
            "連続する文節をまとめて自然な1文を作り、各文を "
            "[最初の文節番号, 最後の文節番号] で表してください。\n"
            "規則:\n"
            "- 範囲は連続し、全文節(0..N-1)を漏れなく覆うこと。\n"
            "- 文節の順序や中身は変えない。\n"
            '- JSONのみ出力: {"sentences":[[0,3],[4,7],...]}'
        ),
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_sentence_split_defaults_present -v`
Expected: PASS.

- [ ] **Step 5: Document in `config.example.yml`**

After the `summary:` block (before the `plan:` comment block), add:

```yaml
# sentence_split stage: runs per source between audio_silence and text_filter.
# An LLM re-segments the WhisperX transcript into one-sentence-per-line units
# (rewriting {stem}.json and {stem}.txt). Disabled by default = byte-identical
# copy-through (no behaviour change).
sentence_split:
  enabled: false               # Enable LLM sentence re-segmentation
  provider: "ollama_chat"      # LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic
  api_base: ""                 # Base URL; empty -> Ollama localhost default; leave empty for cloud providers
  model: "gpt-oss:120b"        # Model (passed to LiteLLM as "<provider>/<model>")
  api_key: ""                  # API key for the provider (or set the provider's env var)
  temperature: 0.2
  thinking: false
  timeout: 300
  response_format: "json"
  max_retries: 2               # Extra attempts on LLM error / invalid ranges (0 = single attempt)
  retry_temp_step: 0.2
  retry_temp_cap: 0.8
  window_segments: 20          # Segments per LLM window
  # prompt: "..."              # Bunsetsu-grouping prompt (has a sensible default)
```

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py
git commit -m "feat(sentence_split): add config defaults and example"
```

---

### Task 2: Pure re-segmentation core (`segment.py`)

**Files:**
- Create: `src/nagare_clip/sentence_split/__init__.py` (empty)
- Create: `src/nagare_clip/sentence_split/segment.py`
- Test: `tests/sentence_split/__init__.py` (empty), `tests/sentence_split/test_segment.py`

**Interfaces:**
- Produces:
  - `iter_windows(segments: list, window_segments: int) -> Iterator[tuple[int, list]]`
  - `window_text_and_words(window: list) -> tuple[str, list[dict]]`
  - `char_to_word_index(words: list[dict]) -> list[int]`
  - `concat_word_text(segments: list[dict]) -> str`
  - `segment_from_words(words: list[dict]) -> dict`
  - `rebuild_window_segments(words: list[dict], bunsetsu: list[tuple[int,int,str]], ranges: list[tuple[int,int]], char2word: list[int]) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Create `tests/sentence_split/__init__.py` (empty) and `tests/sentence_split/test_segment.py`:

```python
from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    window_text_and_words,
)


def _w(ch, start, end):
    return {"word": ch, "start": start, "end": end, "score": 1.0}


def test_char_to_word_index_handles_multichar_and_space():
    words = [{"word": "ab"}, {"word": " "}, {"word": "c"}]
    assert char_to_word_index(words) == [0, 0, 1, 2]


def test_window_text_and_words_concatenates():
    win = [{"words": [_w("あ", 0, 1), _w("い", 1, 2)]},
           {"words": [_w("う", 2, 3)]}]
    text, words = window_text_and_words(win)
    assert text == "あいう"
    assert len(words) == 3


def test_iter_windows_chunks_whole_segments():
    segs = list(range(5))
    assert list(iter_windows(segs, 2)) == [(0, [0, 1]), (2, [2, 3]), (4, [4])]


def test_rebuild_splits_at_bunsetsu_boundaries():
    # text "あいうえお", 5 single-char words; 2 bunsetsu split after char 2.
    words = [_w("あ", 0.0, 0.5), _w("い", 0.5, 1.0), _w("う", 1.0, 1.5),
             _w("え", 1.5, 2.0), _w("お", 2.0, 2.5)]
    bunsetsu = [(0, 2, "あい"), (2, 5, "うえお")]
    char2word = char_to_word_index(words)
    ranges = [(0, 0), (1, 1)]
    segs = rebuild_window_segments(words, bunsetsu, ranges, char2word)
    assert [s["text"] for s in segs] == ["あい", "うえお"]
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 1.0
    assert segs[1]["start"] == 1.0 and segs[1]["end"] == 2.5
    # words preserved, nothing duplicated or lost
    assert concat_word_text(segs) == "あいうえお"


def test_rebuild_single_range_is_whole_window():
    words = [_w("あ", 0, 1), _w("い", 1, 2)]
    bunsetsu = [(0, 2, "あい")]
    segs = rebuild_window_segments(words, bunsetsu, [(0, 0)],
                                   char_to_word_index(words))
    assert [s["text"] for s in segs] == ["あい"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sentence_split/test_segment.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nagare_clip.sentence_split'`.

- [ ] **Step 3: Implement `segment.py`**

Create `src/nagare_clip/sentence_split/__init__.py` (empty), then `src/nagare_clip/sentence_split/segment.py`:

```python
"""Pure re-segmentation core for the sentence_split stage.

No I/O, no LLM, no GiNZA: given a window's words, the window's bunsetsu spans,
and the LLM's bunsetsu-index sentence ranges, rebuild the segment list by
slicing the original words at bunsetsu boundaries.  Words are only reassigned,
never edited, so timing is preserved and text is verbatim by construction.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple


def iter_windows(
    segments: List[Any], window_segments: int
) -> Iterator[Tuple[int, List[Any]]]:
    """Yield (base_index, window) for contiguous whole-segment windows."""
    step = max(1, int(window_segments))
    for base in range(0, len(segments), step):
        yield base, segments[base : base + step]


def window_text_and_words(window: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Concatenate a window's segment words into (text, words)."""
    words: List[Dict[str, Any]] = []
    for seg in window:
        words.extend(seg.get("words", []))
    text = "".join(str(w.get("word", "")) for w in words)
    return text, words


def char_to_word_index(words: List[Dict[str, Any]]) -> List[int]:
    """Map each character position of the concatenated text to its word index."""
    mapping: List[int] = []
    for wi, w in enumerate(words):
        mapping.extend([wi] * len(str(w.get("word", ""))))
    return mapping


def segment_from_words(words: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a WhisperX-shaped segment dict from a slice of words."""
    text = "".join(str(w.get("word", "")) for w in words)
    starts = [w["start"] for w in words if "start" in w]
    ends = [w["end"] for w in words if "end" in w]
    seg: Dict[str, Any] = {}
    if starts:
        seg["start"] = min(starts)
    if ends:
        seg["end"] = max(ends)
    seg["text"] = text
    seg["words"] = words
    return seg


def rebuild_window_segments(
    words: List[Dict[str, Any]],
    bunsetsu: List[Tuple[int, int, str]],
    ranges: List[Tuple[int, int]],
    char2word: List[int],
) -> List[Dict[str, Any]]:
    """Rebuild segments from bunsetsu-index sentence ``ranges``.

    Each sentence boundary is the start char of its first bunsetsu, snapped to a
    whole-word boundary via ``char2word`` (never splitting a word).  The first
    sentence always starts at word 0 and the last ends at the final word, so the
    union of word slices is the whole window (verbatim by construction).
    """
    n_words = len(words)
    wbounds: List[int] = [0]
    for a, _ in ranges[1:]:
        c = bunsetsu[a][0]
        wbounds.append(char2word[c] if 0 <= c < len(char2word) else n_words)
    wbounds.append(n_words)
    # Enforce non-decreasing boundaries.
    for i in range(1, len(wbounds)):
        if wbounds[i] < wbounds[i - 1]:
            wbounds[i] = wbounds[i - 1]
    segments: List[Dict[str, Any]] = []
    for i in range(len(wbounds) - 1):
        w0, w1 = wbounds[i], wbounds[i + 1]
        if w1 <= w0:
            continue
        segments.append(segment_from_words(words[w0:w1]))
    return segments


def concat_word_text(segments: List[Dict[str, Any]]) -> str:
    """Concatenate every word field across segments (verbatim-invariant key)."""
    return "".join(
        str(w.get("word", "")) for seg in segments for w in seg.get("words", [])
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/sentence_split/test_segment.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Mutation check (evidence required)**

Temporarily change `rebuild_window_segments` boundary `wbounds.append(char2word[c] …)` to `char2word[c] + 1`. Run the suite; confirm `test_rebuild_splits_at_bunsetsu_boundaries` and `test_rebuild_single_range_is_whole_window` FAIL. Revert. Record the failing-then-green evidence.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/sentence_split/__init__.py src/nagare_clip/sentence_split/segment.py tests/sentence_split/
git commit -m "feat(sentence_split): pure re-segmentation core"
```

---

### Task 3: Bunsetsu extraction (`nlp.py`)

**Files:**
- Create: `src/nagare_clip/sentence_split/nlp.py`
- Test: `tests/sentence_split/test_nlp.py`

**Interfaces:**
- Produces:
  - `load_nlp() -> spacy.language.Language` (loads `ja_ginza`)
  - `bunsetsu_units(text: str, nlp) -> list[tuple[int, int, str]]` — `(start_char, end_char, surface)` per bunsetsu.

- [ ] **Step 1: Write the failing test**

Create `tests/sentence_split/test_nlp.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sentence_split/test_nlp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nagare_clip.sentence_split.nlp'`.

- [ ] **Step 3: Implement `nlp.py`**

```python
"""GiNZA bunsetsu extraction for the sentence_split stage (lazy import)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    import spacy


def load_nlp() -> "spacy.language.Language":
    import spacy

    return spacy.load("ja_ginza")


def bunsetsu_units(text: str, nlp: "spacy.language.Language") -> List[Tuple[int, int, str]]:
    """Return ``(start_char, end_char, surface)`` for every bunsetsu in ``text``."""
    import ginza

    doc = nlp(text)
    return [(sp.start_char, sp.end_char, sp.text) for sp in ginza.bunsetu_spans(doc)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/sentence_split/test_nlp.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation check**

Temporarily change `(sp.start_char, sp.end_char, sp.text)` to `(sp.start_char + 1, sp.end_char, sp.text)`. Run; confirm `test_bunsetsu_units_offsets_match_surface` FAILS (surface mismatch). Revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/sentence_split/nlp.py tests/sentence_split/test_nlp.py
git commit -m "feat(sentence_split): GiNZA bunsetsu extraction"
```

---

### Task 4: LLM layer (`llm.py`) — prompt, parse/validate, retry/degrade

**Files:**
- Create: `src/nagare_clip/sentence_split/llm.py`
- Test: `tests/sentence_split/test_llm.py`

**Interfaces:**
- Consumes: `nagare_clip.text_filter.llm_filter._call_llm`, `nagare_clip.llm_client.with_trace_meta`, `nagare_clip.llm_retry.{retry_attempts,cfg_for_attempt}`, `nagare_clip.llm_report.{NULL_RECORDER,Recorder,OK,LLM_ERROR,UNPARSEABLE}`.
- Produces:
  - `build_messages(bunsetsu: list[tuple[int,int,str]], system_prompt: str) -> list[dict]`
  - `parse_ranges(response: str, num_bunsetsu: int) -> list[tuple[int,int]] | None`
  - `split_window(bunsetsu, cfg, *, call_llm=_call_llm, recorder=NULL_RECORDER, unit="window") -> list[tuple[int,int]] | None`
  - `CallLLM = Callable[[list[dict], dict], str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/sentence_split/test_llm.py`:

```python
from nagare_clip.sentence_split.llm import (
    build_messages,
    parse_ranges,
    split_window,
)

BUNSETSU = [(0, 2, "あい"), (2, 4, "うえ"), (4, 6, "おか")]


def test_build_messages_numbers_bunsetsu():
    msgs = build_messages(BUNSETSU, "SYS")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["content"] == "0:あい 1:うえ 2:おか"


def test_parse_ranges_valid_contiguous_full_coverage():
    assert parse_ranges('{"sentences":[[0,1],[2,2]]}', 3) == [(0, 1), (2, 2)]


def test_parse_ranges_rejects_gaps_and_bad_coverage():
    assert parse_ranges('{"sentences":[[0,0],[2,2]]}', 3) is None      # gap (skips 1)
    assert parse_ranges('{"sentences":[[1,2]]}', 3) is None            # doesn't start at 0
    assert parse_ranges('{"sentences":[[0,1]]}', 3) is None            # doesn't reach 2
    assert parse_ranges('{"sentences":[[0,2],[1,2]]}', 3) is None      # overlap/not contiguous
    assert parse_ranges('not json', 3) is None
    assert parse_ranges('{"nope":[]}', 3) is None


def test_split_window_returns_ranges_on_valid_response():
    def fake(messages, cfg):
        return '{"sentences":[[0,1],[2,2]]}'
    assert split_window(BUNSETSU, {"max_retries": 0}, call_llm=fake) == [(0, 1), (2, 2)]


def test_split_window_degrades_to_none_after_failures():
    calls = []

    def boom(messages, cfg):
        calls.append(1)
        raise RuntimeError("no server")

    assert split_window(BUNSETSU, {"max_retries": 1}, call_llm=boom) is None
    assert len(calls) == 2  # first try + one retry


def test_split_window_single_bunsetsu_no_call():
    def fail(messages, cfg):
        raise AssertionError("should not be called")
    assert split_window([(0, 2, "あい")], {}, call_llm=fail) == [(0, 0)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sentence_split/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nagare_clip.sentence_split.llm'`.

- [ ] **Step 3: Implement `llm.py`**

```python
"""LLM layer for sentence_split: prompt, range parsing/validation, retry."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]


def build_messages(
    bunsetsu: List[Tuple[int, int, str]], system_prompt: str
) -> List[Dict[str, str]]:
    listing = " ".join(f"{i}:{s}" for i, (_, _, s) in enumerate(bunsetsu))
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": listing},
    ]


def parse_ranges(response: str, num_bunsetsu: int) -> Optional[List[Tuple[int, int]]]:
    """Parse and validate ``{"sentences":[[a,b],…]}`` bunsetsu-index ranges.

    Returns the ranges only if they are contiguous, non-overlapping, and cover
    ``0..num_bunsetsu-1`` exactly; otherwise ``None``.
    """
    try:
        data = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return None
    raw = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    ranges: List[Tuple[int, int]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(x, int) for x in item)
        ):
            return None
        a, b = item
        if a > b:
            return None
        ranges.append((a, b))
    if ranges[0][0] != 0 or ranges[-1][1] != num_bunsetsu - 1:
        return None
    for (_, b0), (a1, _) in zip(ranges, ranges[1:]):
        if a1 != b0 + 1:
            return None
    return ranges


def split_window(
    bunsetsu: List[Tuple[int, int, str]],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "window",
) -> Optional[List[Tuple[int, int]]]:
    """Return validated sentence ranges, or ``None`` to keep original segments.

    Retries (``max_retries``) on LLM exception or invalid ranges, nudging
    temperature each attempt.  A 0/1-bunsetsu window needs no LLM call.
    """
    num = len(bunsetsu)
    if num == 0:
        return []
    if num == 1:
        return [(0, 0)]
    messages = build_messages(bunsetsu, cfg.get("prompt", ""))
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "sentence_split LLM call failed (attempt %d/%d)", attempt + 1, attempts,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        ranges = parse_ranges(response, num)
        if ranges is None:
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid/non-contiguous sentence ranges", cfg=attempt_cfg,
            )
            logger.warning(
                "sentence_split ranges invalid (attempt %d/%d)", attempt + 1, attempts
            )
            continue
        recorder.attempt(
            unit=unit, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=OK, reason="", cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=OK, reason="")
        return ranges
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning(
        "sentence_split: all %d attempt(s) failed; keeping original window", attempts
    )
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/sentence_split/test_llm.py -v`
Expected: PASS (all 6).

- [ ] **Step 5: Mutation check**

Temporarily delete the contiguity loop (`for (_, b0), (a1, _) … return None`). Run; confirm `test_parse_ranges_rejects_gaps_and_bad_coverage` FAILS on the gap case. Revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/sentence_split/llm.py tests/sentence_split/test_llm.py
git commit -m "feat(sentence_split): LLM range parsing + retry/degrade"
```

---

### Task 5: Stage CLI (`cli.py`) — orchestration + copy-through

**Files:**
- Create: `src/nagare_clip/sentence_split/cli.py`
- Test: `tests/sentence_split/test_cli.py`

**Interfaces:**
- Consumes: Tasks 2–4 (`segment`, `nlp`, `llm`), `nagare_clip.config.get_effective_config`, `nagare_clip.llm_report.recorder_from_config`, `nagare_clip.logging_setup.setup_logging`.
- Produces: `main()` plus a testable `resegment_json(json_data, sp_cfg, nlp, *, recorder, stem) -> dict` that returns the new WhisperX data dict (or the original on verbatim violation).

- [ ] **Step 1: Write the failing tests**

Create `tests/sentence_split/test_cli.py`:

```python
import json
from pathlib import Path

from nagare_clip.sentence_split import cli as ss_cli
from nagare_clip.sentence_split.segment import concat_word_text


def _seg(text, t0):
    return {
        "start": float(t0), "end": float(t0 + len(text)), "text": text,
        "words": [{"word": ch, "start": float(t0 + i), "end": float(t0 + i + 1),
                   "score": 1.0} for i, ch in enumerate(text)],
    }


def _data():
    # two WhisperX segments that together form two sentences
    return {"language": "ja", "segments": [_seg("あいうえお", 0), _seg("かきくけこ", 5)]}


def test_resegment_rebuilds_and_preserves_text(monkeypatch):
    data = _data()

    # Stub bunsetsu + LLM so the test is deterministic (no GiNZA/model needed).
    monkeypatch.setattr(ss_cli, "bunsetsu_units",
                        lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)])
    monkeypatch.setattr(ss_cli, "split_window",
                        lambda bunsetsu, cfg, **kw: [(0, 2), (3, len(bunsetsu) - 1)])

    sp_cfg = {"enabled": True, "window_segments": 20}
    out = ss_cli.resegment_json(data, sp_cfg, nlp=None, recorder=ss_cli.NULL_RECORDER, stem="x")
    # 10 chars, split after index 2 -> 2 new segments, text fully preserved
    assert [s["text"] for s in out["segments"]] == ["あいう", "えおかきくけこ"]
    assert concat_word_text(out["segments"]) == "あいうえおかきくけこ"
    assert out["word_segments"] == [w for s in out["segments"] for w in s["words"]]


def test_resegment_degraded_window_keeps_original(monkeypatch):
    data = _data()
    monkeypatch.setattr(ss_cli, "bunsetsu_units",
                        lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)])
    monkeypatch.setattr(ss_cli, "split_window", lambda bunsetsu, cfg, **kw: None)
    out = ss_cli.resegment_json(data, {"enabled": True, "window_segments": 20},
                                nlp=None, recorder=ss_cli.NULL_RECORDER, stem="x")
    assert [s["text"] for s in out["segments"]] == ["あいうえお", "かきくけこ"]


def test_disabled_copy_through_byte_identical(tmp_path, monkeypatch):
    in_json = tmp_path / "in.json"
    in_txt = tmp_path / "in.txt"
    in_json.write_text(json.dumps(_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_txt.write_text("あいうえお\nかきくけこ\n", encoding="utf-8")
    out_json = tmp_path / "out.json"
    out_txt = tmp_path / "out.txt"
    monkeypatch.setattr(
        ss_cli.sys, "argv",
        ["prog", "--json", str(in_json), "--txt", str(in_txt),
         "--output-json", str(out_json), "--output-txt", str(out_txt), "--stem", "x",
         "--llm-report-dir", str(tmp_path / "report")],
    )
    ss_cli.main()
    assert out_json.read_bytes() == in_json.read_bytes()
    assert out_txt.read_bytes() == in_txt.read_bytes()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sentence_split/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nagare_clip.sentence_split.cli'`.

- [ ] **Step 3: Implement `cli.py`**

```python
"""sentence_split stage CLI (per source).

Re-segments a WhisperX ``{stem}.json`` into one-sentence-per-line segments and
writes the re-segmented ``.json`` plus a matching ``.txt`` (one segment per
line).  When ``sentence_split.enabled`` is false it copies the transcription
``.json``/``.txt`` through byte-identically, so downstream behaviour is
unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import NULL_RECORDER, Recorder, recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.sentence_split.llm import split_window
from nagare_clip.sentence_split.nlp import bunsetsu_units, load_nlp
from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    window_text_and_words,
)


def resegment_json(
    json_data: Dict[str, Any],
    sp_cfg: Dict[str, Any],
    nlp: Any,
    *,
    recorder: Recorder = NULL_RECORDER,
    stem: str = "",
) -> Dict[str, Any]:
    """Return a new WhisperX data dict with re-segmented segments.

    On a verbatim-invariant violation, returns ``json_data`` unchanged.
    """
    segments = json_data.get("segments", [])
    window = int(sp_cfg.get("window_segments", 20))
    new_segments = []
    for base, win in iter_windows(segments, window):
        text, words = window_text_and_words(win)
        if not text:
            new_segments.extend(win)
            continue
        bunsetsu = bunsetsu_units(text, nlp)
        if not bunsetsu:
            new_segments.extend(win)
            continue
        ranges = split_window(
            bunsetsu, sp_cfg, recorder=recorder, unit=f"{stem}.w{base + 1}"
        )
        if ranges is None:
            new_segments.extend(win)
            continue
        char2word = char_to_word_index(words)
        new_segments.extend(
            rebuild_window_segments(words, bunsetsu, ranges, char2word)
        )

    if concat_word_text(new_segments) != concat_word_text(segments):
        logging.error(
            "sentence_split: verbatim invariant violated for %s; keeping original",
            stem,
        )
        return json_data

    out = dict(json_data)
    out["segments"] = new_segments
    out["word_segments"] = [w for seg in new_segments for w in seg.get("words", [])]
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="sentence_split stage: LLM re-segmentation of a WhisperX transcript."
    )
    parser.add_argument("--json", required=True, dest="json", help="Input WhisperX JSON")
    parser.add_argument("--txt", required=True, dest="txt", help="Input transcript .txt")
    parser.add_argument("--output-json", required=True, dest="output_json")
    parser.add_argument("--output-txt", required=True, dest="output_txt")
    parser.add_argument("--stem", default="", dest="stem")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--llm-report-dir", default=None, dest="llm_report_dir")
    parser.add_argument(
        "--llm-report-no-clear", action="store_true", dest="llm_report_no_clear"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cli_overrides: Dict[str, Any] = {}
    if args.log_level is not None:
        cli_overrides.setdefault("general", {})["log_level"] = args.log_level
    cfg = get_effective_config(
        Path(args.config_path) if args.config_path else None, cli_overrides
    )
    setup_logging(
        cfg["general"]["log_level"], args.log_file or cfg["general"]["log_file"] or None
    )
    recorder = recorder_from_config(
        "sentence_split", cfg, override_dir=args.llm_report_dir
    )
    if not args.llm_report_no_clear:
        recorder.clear()

    sp_cfg = cfg["sentence_split"]
    out_json = Path(args.output_json)
    out_txt = Path(args.output_txt)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    if not sp_cfg.get("enabled", False):
        logging.info("sentence_split: disabled, copying %s through", args.stem)
        shutil.copyfile(args.json, out_json)
        shutil.copyfile(args.txt, out_txt)
        recorder.rebuild_index()
        return

    json_data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    nlp = load_nlp()
    new_data = resegment_json(
        json_data, sp_cfg, nlp, recorder=recorder, stem=args.stem
    )

    if new_data is json_data:
        # verbatim violation already logged; copy through for safety
        shutil.copyfile(args.json, out_json)
        shutil.copyfile(args.txt, out_txt)
        recorder.rebuild_index()
        return

    out_json.write_text(
        json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    out_txt.write_text(
        "\n".join(seg.get("text", "") for seg in new_data["segments"]) + "\n",
        encoding="utf-8",
    )
    logging.info(
        "sentence_split: %s %d -> %d segments",
        args.stem, len(json_data.get("segments", [])), len(new_data["segments"]),
    )
    recorder.rebuild_index()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/sentence_split/test_cli.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Mutation check**

Temporarily change the disabled branch `shutil.copyfile(args.txt, out_txt)` to write `out_txt.write_text("")`. Run; confirm `test_disabled_copy_through_byte_identical` FAILS. Revert. Also temporarily make `resegment_json` skip the verbatim check (always build `out`) and change `rebuild_window_segments` call to drop the last word — confirm `test_resegment_rebuilds_and_preserves_text` FAILS on the `concat_word_text` assertion. Revert.

- [ ] **Step 6: Run the whole stage suite + commit**

Run: `uv run pytest tests/sentence_split/ -v`
Expected: PASS.

```bash
git add src/nagare_clip/sentence_split/cli.py tests/sentence_split/test_cli.py
git commit -m "feat(sentence_split): stage CLI with copy-through"
```

---

### Task 6: Pipeline orchestration (`run_pipeline.sh`)

**Files:**
- Modify: `scripts/run_pipeline.sh`

**Interfaces:**
- Consumes: `nagare_clip.sentence_split.cli` (Task 5).
- Produces: `output/sentence_split/{stem}.json` + `{stem}.txt`, consumed by all downstream `.json`/`.txt` references.

- [ ] **Step 1: Add the stage to `STAGE_ORDER` and ord/dir vars**

Edit the `STAGE_ORDER` line (currently
`STAGE_ORDER=(transcription audio_silence text_filter summary plan director guided_edit intervals blender)`)
to insert `sentence_split` after `audio_silence`:

```bash
STAGE_ORDER=(transcription audio_silence sentence_split text_filter summary plan director guided_edit intervals blender)
```

After the `ORD_AUDIO_SILENCE="$(stage_index audio_silence)"` line, add:

```bash
ORD_SENTENCE_SPLIT="$(stage_index sentence_split)"
```

After the `AUDIO_SILENCE_DIR=…` line, add:

```bash
SENTENCE_SPLIT_DIR="${OUTPUT_DIR}/sentence_split"  # LLM sentence re-segmentation (.json + .txt)
```

Add `"$SENTENCE_SPLIT_DIR"` to the `mkdir -p` list that already creates the stage dirs.

- [ ] **Step 2: Insert the stage block**

Immediately **before** the `# --- text_filter: …` comment line, insert:

```bash
# --- sentence_split: LLM sentence re-segmentation (per source) ---
# Always runs (copy-through no-op when sentence_split.enabled is false).
if in_window "$ORD_SENTENCE_SPLIT"; then
  REPORT_CLEARED_SS=0
  for STEM in "${ALL_STEMS[@]}"; do
    REPORT_KEEP_SS=()
    if (( REPORT_CLEARED_SS )); then REPORT_KEEP_SS+=(--llm-report-no-clear); fi
    REPORT_CLEARED_SS=1
    echo "[sentence_split] Sentence re-segmentation: ${STEM}"
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.sentence_split.cli \
      --json "${TRANSCRIPTION_DIR}/${STEM}.json" \
      --txt "${TRANSCRIPTION_DIR}/${STEM}.txt" \
      --output-json "${SENTENCE_SPLIT_DIR}/${STEM}.json" \
      --output-txt "${SENTENCE_SPLIT_DIR}/${STEM}.txt" \
      --stem "${STEM}" \
      "${CONFIG_ARGS[@]}" \
      --log-file "$LOG_FILE" \
      --llm-report-dir "$LLM_REPORT_DIR" \
      "${REPORT_KEEP_SS[@]}"
  done
elif past_window "$ORD_SENTENCE_SPLIT"; then
  echo "[sentence_split] Skipped (--to-stage $TO_STAGE)"
else
  echo "[sentence_split] Skipped (--from-stage $FROM_STAGE)"
  for STEM in "${ALL_STEMS[@]}"; do
    if [[ ! -f "${SENTENCE_SPLIT_DIR}/${STEM}.json" || ! -f "${SENTENCE_SPLIT_DIR}/${STEM}.txt" ]]; then
      echo "Missing sentence_split output: ${SENTENCE_SPLIT_DIR}/${STEM}.{json,txt} (required when skipping sentence_split)" >&2
      exit 1
    fi
  done
fi

```

- [ ] **Step 3: Repoint downstream `.txt`/`.json` inputs**

Change these five argument lines from `TRANSCRIPTION_DIR` to `SENTENCE_SPLIT_DIR` (the re-segmented artifacts are now the source of truth for text + word timing):

- text_filter `--txt "${TRANSCRIPTION_DIR}/${STEM}.txt"` → `--txt "${SENTENCE_SPLIT_DIR}/${STEM}.txt"`
- summary `--json "${TRANSCRIPTION_DIR}/${STEM}.json"` → `--json "${SENTENCE_SPLIT_DIR}/${STEM}.json"`
- director `--json "${TRANSCRIPTION_DIR}/${STEM}.json"` → `--json "${SENTENCE_SPLIT_DIR}/${STEM}.json"`
- guided_edit `--json "${TRANSCRIPTION_DIR}/${STEM}.json"` → `--json "${SENTENCE_SPLIT_DIR}/${STEM}.json"`
- intervals `--json "${TRANSCRIPTION_DIR}/${STEM}.json"` → `--json "${SENTENCE_SPLIT_DIR}/${STEM}.json"`

(Leave the transcription-skip validation that checks `${TRANSCRIPTION_DIR}/${STEM}.json` and `.txt` exist — sentence_split still consumes those when transcription is skipped.)

- [ ] **Step 4: Syntax check**

Run: `bash -n scripts/run_pipeline.sh`
Expected: no output (exit 0).

- [ ] **Step 5: Behavioural smoke (stage ordering)**

Run: `bash -c 'source <(grep -nE "STAGE_ORDER=" scripts/run_pipeline.sh | sed "s/^[0-9]*://"); printf "%s\n" "${STAGE_ORDER[@]}"' | grep -n sentence_split`
Expected: prints `3:sentence_split` (confirms it sits between audio_silence and text_filter).

- [ ] **Step 6: Commit**

```bash
git add scripts/run_pipeline.sh
git commit -m "feat(sentence_split): wire stage into run_pipeline.sh"
```

---

### Task 7: Documentation

**Files:**
- Modify: `AGENTS.md`, `README.md`, `plan.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: `AGENTS.md`**

Make these edits:
1. In the numbered objective list, insert a step "audio-silence → **sentence re-segmentation** → text editing" and renumber prose (no stage numbers in identifiers).
2. In the naming-convention blockquote, add `sentence_split:` to the canonical identifier list.
3. Add a new `### sentence_split — LLM Sentence Re-Segmentation` section after the `audio_silence` section, documenting: inputs (`{stem}.json`, `{stem}.txt` from transcription), outputs (re-segmented `{stem}.json` + `{stem}.txt` in `output/sentence_split/`), the bunsetsu-index-range approach, verbatim-by-construction guarantee, windowing (`window_segments`, default 20), graceful degrade to original segmentation, and disabled→byte-identical copy-through.
4. In Project Structure, add the package:
   ```
     sentence_split/             # sentence_split stage (LLM re-segmentation)
       segment.py                # pure: windowing, char/word map, rebuild_window_segments, verbatim check
       nlp.py                    # GiNZA bunsetsu extraction (lazy import)
       llm.py                    # prompt + bunsetsu-range parse/validate + retry/degrade
       cli.py                    # stage CLI (copy-through when disabled)
   ```
   and under `tests/`: `  sentence_split/             # sentence_split unit + CLI tests`.
5. In "Pipeline orchestration", note that `sentence_split` always runs (copy-through when disabled) and that downstream `.json`/`.txt` consumers read from `output/sentence_split/` rather than `output/transcription/`.

- [ ] **Step 2: `README.md`**

Add a `sentence_split` entry to the user-facing stage list/usage describing the new `output/sentence_split/` outputs and the `sentence_split.enabled` toggle (off by default), and that enabling it improves caption readability and downstream LLM reasoning.

- [ ] **Step 3: `plan.md`**

Add a status entry recording the `sentence_split` stage as implemented (bunsetsu-index-range approach, default-off), referencing the design/plan docs under `docs/superpowers/`.

- [ ] **Step 4: Validate docs build/refs**

Run: `uv run pytest -q`
Expected: PASS (full suite, confirming no code regressions alongside the doc change).

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md plan.md
git commit -m "docs(sentence_split): document the new stage"
```

---

## Final verification

- [ ] Run the full suite: `uv run pytest -q` → all pass.
- [ ] `bash -n scripts/run_pipeline.sh` → clean.
- [ ] Confirm default-off regression safety: with `sentence_split.enabled` false, `output/sentence_split/{stem}.{json,txt}` are byte-identical to `output/transcription/{stem}.{json,txt}` (covered by `test_disabled_copy_through_byte_identical`).
