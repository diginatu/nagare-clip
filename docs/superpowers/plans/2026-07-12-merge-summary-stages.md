# Merge text_filter's Summary LLM into the summary Stage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicate summary LLM inside text_filter (`text_filter/summary_llm.py`) by moving the project-wide `summary` stage *before* `text_filter`, extending it to also emit per-video misspelling-prone keywords, and having text_filter read `summary.json` for its filter-prompt context.

**Architecture:** The `summary` stage moves between `sentence_split` and `text_filter` in the pipeline order and now reads the sentence_split `{stem}.txt` files (instead of text_filter's `{stem}_edits.txt`). This is safe because text_filter is line-preserving (patches are within-line `{{old->new}}` markers; `check_edits` enforces line count), so part line-ranges computed pre-filter remain valid for `plan`/`director`. The per-video `segment_video` LLM call additionally returns `"keywords"` (correct spellings of rare/domain words), stored per-stem in `summary.json`. `run_text_filter` gains a `summary_json` parameter: when the file has parts/keywords for this video's stem, they are appended to the filter LLM's system prompt (replacing the old `generate_summary` call). The `text_filter.summary_llm` config section is deleted; its constant `keywords` list moves to `text_filter.keywords`.

**Tech Stack:** Python 3 (src layout, uv), pydantic config models, pytest, LiteLLM transport via `nagare_clip.llm_client.call_llm`.

## Global Constraints

- Run everything with `uv run` (e.g. `uv run pytest`); dependency management is uv + pyproject.toml.
- All LLM access goes through `nagare_clip.llm_client.call_llm` — no new HTTP clients.
- Stage `run()` functions receive the already-merged plain `cfg` **dict** (the "dict boundary"); do not pass pydantic models into stages.
- Stages are identified only by functional name — never introduce stage numbers.
- TDD: write each test first, run it, **see it fail**, then implement. If a test is added against already-written code, briefly mutate the implementation to confirm the test catches it, then revert (per the user's global CLAUDE.md).
- `rm` is shell-aliased off in this environment; use `\rm` or `git rm`.
- `config.example.yml` is generated — after any `config.py` model change run `make config-example` (a test fails if it drifts).
- Docs policy: when behavior changes, update `README.md`, `plan.md`, `AGENTS.md`, and the affected `docs/stages/*.md` (Task 7).
- Final gate: `make check` (ruff lint + format check + validate + pytest) must pass.

**Breaking config change (intentional, loud):** a user YAML containing `text_filter.summary_llm:` will now raise `ValidationError` (unknown key). The migration is: delete the block; move any constant `summary_llm.keywords` to `text_filter.keywords`; enable `summary.enabled` to regain LLM-generated summary/keyword context. Documented in Task 7.

**New pipeline order:** `transcription, audio_silence, sentence_split, summary, text_filter, plan, director, guided_edit, intervals, blender` (only `summary` moves; `plan` stays put — it reads only `summary.json`).

**New `summary.json` schema (superset of old):**

```json
{
  "summary": "all-videos summary",
  "parts": [{"stem": "a", "lines": [1, 12], "summary": "...", "start": 1.0, "end": 9.5}],
  "keywords": {"a": ["Kubernetes", "PostgreSQL"]}
}
```

`keywords` maps stem → list of correct spellings; stems with no keywords are omitted. `summary_from_dict` tolerates a missing/malformed `keywords` key, so old `summary.json` files and the `plan`/`director` consumers keep working unchanged.

---

### Task 1: summary schema — per-video keywords in `summarize.py`

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py`
- Modify: `src/nagare_clip/config.py` (only the `SUMMARY_PROMPT` constant, around line 83)
- Test: `tests/summary/test_summarize.py`

**Interfaces:**
- Consumes: existing `PartSummary`, `_parse_parts_response`, `segment_video`, `build_summary`, `summary_to_dict`, `summary_from_dict` in `summarize.py`.
- Produces (later tasks rely on these exact shapes):
  - `segment_video(stem, clean_lines, cfg, *, call_llm, recorder) -> tuple[list[PartSummary], list[str]]` (was `-> list[PartSummary]`)
  - `ProjectSummary` gains field `keywords: dict[str, list[str]] = field(default_factory=dict)`
  - `summary_to_dict` output always has a top-level `"keywords"` dict; `summary_from_dict` populates `.keywords` leniently (missing/garbage → `{}`).

- [ ] **Step 1: Write the failing tests**

In `tests/summary/test_summarize.py`, add (imports `segment_video`, `build_summary`, `summary_to_dict`, `summary_from_dict`, `ProjectSummary`, `PartSummary` already exist at the top of the file):

```python
class TestSegmentVideoKeywords:
    def test_parses_keywords_stripped(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}],'
            ' "keywords": [" Kubernetes ", "PostgreSQL"]}'
        )
        parts, keywords = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert [p.summary for p in parts] == ["s"]
        assert keywords == ["Kubernetes", "PostgreSQL"]

    def test_missing_keywords_is_empty_list(self):
        resp = '{"parts": [{"lines": [1, 1], "summary": "s"}]}'
        _, keywords = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert keywords == []

    def test_malformed_keyword_entries_dropped(self):
        resp = '{"parts": [], "keywords": ["ok", 42, "", null]}'
        _, keywords = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert keywords == ["ok"]

    def test_keywords_non_list_is_empty(self):
        resp = '{"parts": [], "keywords": "not a list"}'
        _, keywords = segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert keywords == []


class TestProjectKeywords:
    def test_build_summary_collects_keywords_by_stem_omitting_empty(self):
        responses = iter(
            [
                '{"parts": [{"lines": [1, 1], "summary": "sa"}], "keywords": ["KWA"]}',
                '{"parts": [{"lines": [1, 1], "summary": "sb"}], "keywords": []}',
                '{"summary": "all"}',
            ]
        )
        ps = build_summary(
            [("a", ["x"]), ("b", ["y"])], {"prompt": "P"}, call_llm=lambda m, c: next(responses)
        )
        assert ps.keywords == {"a": ["KWA"]}

    def test_to_dict_includes_keywords(self):
        ps = ProjectSummary(summary="s", parts=[], keywords={"a": ["K"]})
        assert summary_to_dict(ps)["keywords"] == {"a": ["K"]}

    def test_to_dict_empty_keywords_present(self):
        assert summary_to_dict(ProjectSummary(summary="", parts=[]))["keywords"] == {}

    def test_from_dict_round_trip_keywords(self):
        ps = ProjectSummary(
            summary="s", parts=[PartSummary("a", (1, 2), "x")], keywords={"a": ["K"]}
        )
        assert summary_from_dict(summary_to_dict(ps)).keywords == {"a": ["K"]}

    def test_from_dict_missing_keywords_is_empty(self):
        assert summary_from_dict({"summary": "s", "parts": []}).keywords == {}

    def test_from_dict_malformed_keywords_dropped(self):
        data = {
            "summary": "s",
            "parts": [],
            "keywords": {"a": ["ok", ""], "b": "not-a-list", "c": []},
        }
        assert summary_from_dict(data).keywords == {"a": ["ok"]}
```

Also update every **existing** call site of `segment_video` in this test file to the new tuple return (the change makes them fail first, which is our red):

- `parts = segment_video(...)` → `parts, _ = segment_video(...)` (tests at ~lines 41, 51, 56, 61)
- `test_llm_error_returns_empty` (~line 74–78): `assert segment_video(...) == ([], [])`
- `test_unparseable_returns_empty` (~line 80–81): `assert segment_video(...) == ([], [])`
- `test_retries_then_succeeds` (~line 83–85): `parts, _ = segment_video(...)`
- Recorder tests (~lines 147–167): `parts, _ = segment_video(...)`
- `test_uses_clean_numbered_input` (~line 64–71): return value unused — leave the call as is.

Because `summary_to_dict` now always emits `"keywords"`, also update the exact-dict assertions that would otherwise leave this commit red:

- `tests/summary/test_summarize.py::TestRoundTrip::test_to_from_dict` (~line 181): the expected dict gains `"keywords": {}` as a third key. (`test_from_dict_tolerates_garbage` still passes — both sides default to empty keywords.)
- `tests/summary/test_run.py::test_disabled_writes_empty` (~line 39): expect `{"summary": "", "parts": [], "keywords": {}}`.
- `tests/summary/test_run.py::test_enabled_writes_summary_with_stems_from_basename` (~line 62): the expected dict gains `"keywords": {}`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/summary/test_summarize.py -x -q`
Expected: FAIL — first the new keyword tests error with `ValueError: too many values to unpack` / `TypeError` (current `segment_video` returns a plain list), and `ProjectSummary(..., keywords=...)` raises `TypeError: unexpected keyword argument`.

- [ ] **Step 3: Implement in `summarize.py`**

1. Add a lenient keyword coercer after `_strip_fence`:

```python
def _coerce_keywords(value: Any) -> list[str]:
    """Lenient keyword list: keep stripped non-empty strings, drop everything else."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
```

2. Change `_parse_parts_response` to also return keywords. New signature and return type:

```python
def _parse_parts_response(
    response: str, stem: str, num_lines: int, drops: list[str] | None = None
) -> tuple[list[PartSummary], list[str]] | None:
```

Keep the body identical except the final line becomes:

```python
    return parts, _coerce_keywords(data.get("keywords"))
```

Update its docstring: it now returns `(parts, keywords)` where keywords are the video's misspelling-prone words (empty when absent), still `None` on hard parse failure.

3. `ProjectSummary` gains the field:

```python
@dataclass
class ProjectSummary:
    summary: str
    parts: list[PartSummary] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict)  # stem -> correct spellings
```

4. `segment_video` returns the tuple. Change its return annotation to `tuple[list[PartSummary], list[str]]`, its docstring to `"""Segment one video's transcript into summarised parts + misspelling-prone keywords."""`, and inside the retry loop replace:

```python
        drops: list[str] = []
        parts = _parse_parts_response(response, stem, num_lines=len(clean_lines), drops=drops)
        if parts is None:
```

with:

```python
        drops: list[str] = []
        parsed = _parse_parts_response(response, stem, num_lines=len(clean_lines), drops=drops)
        if parsed is None:
```

then after the unparseable `continue` branch:

```python
        parts, keywords = parsed
```

The `outcome` logic stays keyed on `parts` (a keywords-only response still records `OK_EMPTY`). The success return becomes `return parts, keywords`; the two failure `return []` (after-the-loop, line ~183) become `return [], []`.

5. `build_summary` collects keywords per stem (omit stems with no keywords):

```python
    parts: list[PartSummary] = []
    keywords: dict[str, list[str]] = {}
    for stem, clean_lines in parts_input:
        video_parts, video_keywords = segment_video(
            stem, clean_lines, cfg, call_llm=call_llm, recorder=recorder
        )
        parts.extend(video_parts)
        if video_keywords:
            keywords[stem] = video_keywords
    if seg_times_by_stem:
        for p in parts:
            _attach_part_times(p, seg_times_by_stem.get(p.stem))
    summary = generate_project_summary(parts, cfg, call_llm=call_llm, recorder=recorder)
    return ProjectSummary(summary=summary, parts=parts, keywords=keywords)
```

6. `summary_to_dict` return becomes:

```python
    return {"summary": ps.summary, "parts": parts, "keywords": ps.keywords}
```

7. `summary_from_dict`: before the final `return`, parse keywords and pass them through:

```python
    keywords: dict[str, list[str]] = {}
    raw_kw = data.get("keywords")
    if isinstance(raw_kw, dict):
        for k, v in raw_kw.items():
            if isinstance(k, str):
                kws = _coerce_keywords(v)
                if kws:
                    keywords[k] = kws
    return ProjectSummary(summary=summary, parts=parts, keywords=keywords)
```

8. In `src/nagare_clip/config.py`, replace the `SUMMARY_PROMPT` constant (currently lines 83–99) with:

```python
SUMMARY_PROMPT = (
    "You are a video editor. You receive ONE Japanese transcript as "
    "numbered lines (one line per subtitle segment). Split it into a few "
    "contiguous PARTS by topic/section and summarise each part. Reference "
    "lines by their 1-based numbers (inclusive). Also list rare or "
    "domain-specific words that speech recognition might misspell. "
    "Output ONLY a JSON object.\n"
    "\n"
    "JSON shape:\n"
    '{"parts": [\n'
    '  {"lines": [1, 12], "summary": "what this part covers"},\n'
    '  {"lines": [13, 40], "summary": "..."}\n'
    '], "keywords": ["word1", "word2"]}\n'
    "\n"
    "Rules:\n"
    "- Parts must be contiguous and within the transcript range.\n"
    "- Keep each summary to one short sentence.\n"
    '- "keywords": correct spellings of rare/domain-specific words '
    "(may be empty).\n"
    "- Output only the JSON object, no other text."
)
```

Then run `make config-example` (the prompt default appears in the generated file's comments).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/ tests/test_config.py tests/plan tests/director -q`
Expected: PASS (plan/director consume `summary_from_dict` — the additive field must not break them).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/summary/summarize.py src/nagare_clip/config.py config.example.yml tests/summary/
git commit -m "feat(summary): per-video misspelling keywords in segment response + summary.json"
```

---

### Task 2: `run_summary` reads sentence_split `.txt` files and writes keywords

**Files:**
- Modify: `src/nagare_clip/summary/run.py`
- Test: `tests/summary/test_run.py`

**Interfaces:**
- Consumes: Task 1's `ProjectSummary.keywords` and `summary_to_dict` `"keywords"` key.
- Produces: `run_summary(txts: list[Path], output: Path, cfg: dict, *, json_paths: list[Path] | None = None, recorder: Recorder = NULL_RECORDER) -> None` — first param renamed `edits_txts` → `txts`; each path is a sentence_split `{stem}.txt` and the stem is `path.stem`. Task 6's orchestrator calls this signature.

- [ ] **Step 1: Update the tests (this is the red)**

Rewrite `tests/summary/test_run.py`'s helper and tests:

1. In `_run`, rename `edits_by_stem` → `txt_by_stem`, write plain `{stem}.txt` files, and pass positionally as before:

```python
def _run(monkeypatch, tmp_path, cfg_dict, txt_by_stem, json_by_stem=None):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    txt_args = []
    for stem, text in txt_by_stem.items():
        p = tmp_path / f"{stem}.txt"
        p.write_text(text, encoding="utf-8")
        txt_args.append(p)
```

(keep the `json_by_stem` block and the `run_summary(txt_args, out, ...)` call otherwise unchanged; the exact-dict expectations for the disabled/enabled tests were already updated to include `"keywords": {}` in Task 1 — here only the fixture filenames change, and `captured["stems"]` must still be `["a", "b"]`, now derived from `path.stem` of `a.txt`/`b.txt`).

2. Add a raw-lines pass-through test (guards that no marker-cleaning is applied — the input is pre-filter and has none):

```python
def test_lines_passed_verbatim(monkeypatch, tmp_path):
    captured = {}

    def fake_build(parts_input, cfg, **kwargs):
        captured["lines"] = parts_input[0][1]
        return ProjectSummary(summary="", parts=[])

    monkeypatch.setattr(summary_run, "build_summary", fake_build)
    _run(monkeypatch, tmp_path, {"summary": {"enabled": True}}, {"v": "line {{a->b}} raw\n"})
    assert captured["lines"] == ["line {{a->b}} raw"]
```

3. Add a keywords-written test:

```python
def test_keywords_written_to_output(monkeypatch, tmp_path):
    def fake_build(parts_input, cfg, **kwargs):
        return ProjectSummary(summary="all", parts=[], keywords={"a": ["K"]})

    monkeypatch.setattr(summary_run, "build_summary", fake_build)
    data = _run(monkeypatch, tmp_path, {"summary": {"enabled": True}}, {"a": "ax\n"})
    assert data["keywords"] == {"a": ["K"]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/summary/test_run.py -q`
Expected: FAIL — `test_lines_passed_verbatim` is the load-bearing red: the current implementation runs `clean_for_display`, which strips the `{{a->b}}` marker. (The stems test happens to pass already — `_stem_from_edits` falls back to `path.stem` for `a.txt` — that's fine; one genuine red is enough.)

- [ ] **Step 3: Implement in `summary/run.py`**

- Rename the first parameter `edits_txts` → `txts`.
- Delete `_stem_from_edits` and the `from nagare_clip.director.director_llm import clean_for_display` import.
- Replace the `parts_input` loop with:

```python
        parts_input = []
        for path in txts:
            lines = path.read_text(encoding="utf-8").splitlines()
            parts_input.append((path.stem, lines))
```

- Update the module docstring's first paragraph to say the stage runs before text_filter on the sentence_split transcripts and also emits per-video keywords.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/summary/run.py tests/summary/test_run.py
git commit -m "feat(summary): read sentence_split transcripts; write per-video keywords"
```

---

### Task 3: config — delete `text_filter.summary_llm`, add `text_filter.keywords`

**Files:**
- Modify: `src/nagare_clip/config.py`
- Modify: `config.example.yml` (regenerated)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: effective config has `cfg["text_filter"]["keywords"]: list[str]` (default `[]`) and **no** `cfg["text_filter"]["summary_llm"]`. Task 5's `run_text_filter` reads `s2.get("keywords", [])`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, replace `test_summary_llm_defaults_present` and `test_summary_llm_config_override` (~lines 254–272) with (`yaml`, `pytest`, and `from pydantic import ValidationError` are already imported at the top of this file — no new imports needed):

```python
    def test_text_filter_keywords_default_empty(self):
        cfg = get_effective_config(None)
        assert cfg["text_filter"]["keywords"] == []
        assert "summary_llm" not in cfg["text_filter"]

    def test_text_filter_keywords_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"text_filter": {"keywords": ["Kubernetes"]}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["text_filter"]["keywords"] == ["Kubernetes"]
        # Other text_filter defaults intact
        assert cfg["text_filter"]["batch_size"] == 10

    def test_removed_summary_llm_section_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"text_filter": {"summary_llm": {"enabled": True}}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)
```

And in `test_llm_sections_default_provider_and_empty_api_base` (~line 373) delete the line `DEFAULTS["text_filter"]["summary_llm"],` from the `sections` list.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `test_text_filter_keywords_default_empty` (`KeyError: 'keywords'` / `summary_llm` still present), `test_removed_summary_llm_section_rejected` (no error raised).

- [ ] **Step 3: Implement in `config.py`**

1. Delete the `SUMMARY_LLM_PROMPT` constant (lines 73–81).
2. Delete the whole `SummaryLLMConfig` class (lines 306–336).
3. In `TextFilterConfig`, delete the field `summary_llm: SummaryLLMConfig = Field(default_factory=SummaryLLMConfig)` and add after `thinking`:

```python
    keywords: list[str] = _commented(
        [],
        sample="[]",
        description="Constant keywords always injected into the filter LLM prompt",
    )
```

4. Update `SummaryConfig.section_comment` to:

```python
    section_comment: ClassVar[str] = (
        "summary stage: runs once project-wide (over all videos) between sentence_split\n"
        "and text_filter. A larger LLM segments each transcript into line-range parts\n"
        "and summarises each (also listing misspelling-prone keywords per video), then\n"
        "a reduce step writes one all-videos summary. Output summary.json is a\n"
        "reviewable intermediate consumed by the text_filter, plan and director stages.\n"
        "Disabled by default (writes an empty summary = no-op)."
    )
```

5. Regenerate: `make config-example`.

Note: `text_filter/run.py` still calls `s2.get("summary_llm", {})` at this point — `.get` returns `{}`, so `enabled` is falsy and the old path is dead but not crashing. It is removed in Task 5.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py tests/text_filter -q`
Expected: PASS (text_filter tests construct their own config dicts and still pass).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py
git commit -m "feat(config)!: drop text_filter.summary_llm; add text_filter.keywords"
```

---

### Task 4: `text_filter/context.py` — prompt-context builder

**Files:**
- Create: `src/nagare_clip/text_filter/context.py`
- Test: `tests/text_filter/test_context.py` (new)

**Interfaces:**
- Produces: `build_enhanced_prompt(base_prompt: str, summaries: list[str], keywords: list[str]) -> str`. Task 5 imports it from `nagare_clip.text_filter.context`. (This replaces the old `build_enhanced_prompt(base_prompt, SummaryResult)` — the `SummaryResult` dataclass is retired.)

- [ ] **Step 1: Write the failing tests**

Create `tests/text_filter/test_context.py`:

```python
"""Tests for the filter-LLM prompt-context builder."""

from __future__ import annotations

from nagare_clip.text_filter.context import build_enhanced_prompt


class TestBuildEnhancedPrompt:
    def test_appends_summary_and_keywords(self):
        result = build_enhanced_prompt(
            "Fix errors.", ["プログラミング解説"], ["Kubernetes", "PostgreSQL"]
        )
        assert result.startswith("Fix errors.")
        assert "プログラミング解説" in result
        assert "Kubernetes" in result
        assert "PostgreSQL" in result

    def test_single_summary_on_one_line(self):
        result = build_enhanced_prompt("Base.", ["概要のみ"], [])
        assert "Summary: 概要のみ" in result

    def test_multiple_summaries_bulleted(self):
        result = build_enhanced_prompt("Base.", ["part one", "part two"], [])
        assert "Summary:" in result
        assert "- part one" in result
        assert "- part two" in result

    def test_preserves_base_prompt(self):
        base = "Line 1\nLine 2\nLine 3"
        assert build_enhanced_prompt(base, ["概要"], ["w1"]).startswith(base)

    def test_keywords_only_omits_summary_line(self):
        result = build_enhanced_prompt("Fix errors.", [], ["Foo", "Bar"])
        assert "Foo" in result
        assert "Bar" in result
        assert "Summary:" not in result

    def test_no_context_returns_base_unchanged(self):
        assert build_enhanced_prompt("Fix errors.", [], []) == "Fix errors."

    def test_blank_entries_ignored(self):
        assert build_enhanced_prompt("Base.", [""], [""]) == "Base."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/text_filter/test_context.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nagare_clip.text_filter.context'`.

- [ ] **Step 3: Implement `src/nagare_clip/text_filter/context.py`**

```python
"""Filter-LLM prompt context: inject summary-stage summaries/keywords into the prompt."""

from __future__ import annotations


def build_enhanced_prompt(base_prompt: str, summaries: list[str], keywords: list[str]) -> str:
    """Append this video's part summaries and keywords to the filter LLM's base prompt."""
    summaries = [s for s in summaries if s]
    keywords = [k for k in keywords if k]
    if not summaries and not keywords:
        return base_prompt
    parts = [base_prompt, "", "Context about this transcript:"]
    if len(summaries) == 1:
        parts.append(f"Summary: {summaries[0]}")
    elif summaries:
        parts.append("Summary:")
        parts.extend(f"- {s}" for s in summaries)
    if keywords:
        parts.append(f"Keywords (correct spellings): {', '.join(keywords)}")
        parts.append("When you see words that sound similar to these keywords, correct them.")
    return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/text_filter/test_context.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/text_filter/context.py tests/text_filter/test_context.py
git commit -m "feat(text_filter): prompt-context builder for summary-stage context"
```

---

### Task 5: `run_text_filter` consumes `summary.json`; delete `summary_llm.py`

**Files:**
- Modify: `src/nagare_clip/text_filter/run.py`
- Delete: `src/nagare_clip/text_filter/summary_llm.py`
- Delete: `tests/text_filter/test_summary_llm.py`
- Modify: `tests/guided_edit/test_tracing_threading.py`
- Test: `tests/text_filter/test_run.py` (rewritten)

**Interfaces:**
- Consumes: Task 1's `summary_from_dict` (from `nagare_clip.summary.summarize`) and `ProjectSummary.keywords`; Task 3's `text_filter.keywords`; Task 4's `build_enhanced_prompt(base_prompt, summaries, keywords)`.
- Produces: `run_text_filter(txt: Path, output_txt: Path, cfg: dict, *, summary_json: Path | None = None, recorder: Recorder = NULL_RECORDER) -> None`. The video's stem is derived as `txt.stem` (the sentence_split file is `{stem}.txt`). Task 6's orchestrator passes `summary_json=`.
- Import-cycle note: `text_filter.run → summary.summarize → text_filter.llm_filter` is acyclic (`llm_filter` imports neither `run` nor `summarize`).

- [ ] **Step 1: Rewrite `tests/text_filter/test_run.py` (the red)**

Replace the file's helper section and `TestConstantKeywords` with:

```python
"""Tests for run_text_filter: summary.json context + keyword injection."""

from __future__ import annotations

import json
from unittest.mock import patch

from nagare_clip.config import get_effective_config
from nagare_clip.text_filter.run import run_text_filter


def _s2_config(constant_keywords: list | None = None) -> dict:
    return {
        "use_llm": True,
        "api_base": "http://localhost:11434",
        "model": "test",
        "api_key": "",
        "batch_size": 10,
        "timeout": 60,
        "retry_on_invalid": False,
        "retry_min_batch_size": 1,
        "prompt": "Base prompt.",
        "temperature": 0.1,
        "thinking": False,
        "keywords": constant_keywords if constant_keywords is not None else [],
    }


def _run(tmp_path, s2_config, summary_data=None, lines=None):
    """Run run_text_filter and return the filter_cfg passed to filter_transcript."""
    if lines is None:
        lines = ["test line"]

    txt = tmp_path / "test.txt"
    txt.write_text("\n".join(lines))
    output = tmp_path / "test_edits.txt"

    summary_json = None
    if summary_data is not None:
        summary_json = tmp_path / "summary.json"
        summary_json.write_text(
            summary_data if isinstance(summary_data, str) else json.dumps(summary_data),
            encoding="utf-8",
        )

    captured: dict = {}

    def mock_filter(lines, cfg, **kwargs):
        captured.update(cfg)
        return lines

    config = {
        "general": {"log_level": "WARNING", "log_file": ""},
        "text_filter": s2_config,
    }

    with patch("nagare_clip.text_filter.run.filter_transcript", side_effect=mock_filter):
        run_text_filter(txt, output, config, summary_json=summary_json)

    return captured


class TestSummaryContext:
    def test_constant_keywords_injected_without_summary_json(self, tmp_path):
        cfg = _run(tmp_path, _s2_config(constant_keywords=["TestWord"]))
        assert "TestWord" in cfg.get("prompt", "")

    def test_no_context_prompt_unchanged(self, tmp_path):
        cfg = _run(tmp_path, _s2_config())
        assert cfg.get("prompt") == "Base prompt."

    def test_summary_json_summaries_and_keywords_injected(self, tmp_path):
        summary = {
            "summary": "overall",
            "parts": [{"stem": "test", "lines": [1, 1], "summary": "part summary"}],
            "keywords": {"test": ["Dynamic"]},
        }
        cfg = _run(tmp_path, _s2_config(constant_keywords=["Constant"]), summary_data=summary)
        prompt = cfg.get("prompt", "")
        assert "part summary" in prompt
        assert "Constant" in prompt
        assert "Dynamic" in prompt

    def test_other_stem_entries_ignored(self, tmp_path):
        summary = {
            "summary": "overall",
            "parts": [{"stem": "other", "lines": [1, 1], "summary": "not mine"}],
            "keywords": {"other": ["NotMine"]},
        }
        cfg = _run(tmp_path, _s2_config(), summary_data=summary)
        assert cfg.get("prompt") == "Base prompt."

    def test_empty_summary_json_prompt_unchanged(self, tmp_path):
        cfg = _run(
            tmp_path, _s2_config(), summary_data={"summary": "", "parts": [], "keywords": {}}
        )
        assert cfg.get("prompt") == "Base prompt."

    def test_malformed_summary_json_ignored(self, tmp_path):
        cfg = _run(tmp_path, _s2_config(), summary_data="not json {{{")
        assert cfg.get("prompt") == "Base prompt."


def test_disabled_copies_input(tmp_path):
    """When use_llm is false, input is copied to output unchanged."""
    src = tmp_path / "clip.txt"
    src.write_text("l1\nl2\n", encoding="utf-8")
    out = tmp_path / "clip_edits.txt"
    run_text_filter(src, out, get_effective_config(None, {}))
    assert out.read_text(encoding="utf-8") == "l1\nl2\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/text_filter/test_run.py -q`
Expected: FAIL with `TypeError: run_text_filter() got an unexpected keyword argument 'summary_json'`.

- [ ] **Step 3: Implement `text_filter/run.py`**

Replace the whole file with:

```python
"""text_filter stage: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the transcription
``.txt`` (when LLM is disabled) or LLM-filtered text with ``{{old->new}}``
markers preserved for human review. When the summary stage is enabled, its
``summary.json`` (per-video part summaries + misspelling-prone keywords)
enriches the filter LLM's system prompt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.summary.summarize import summary_from_dict
from nagare_clip.text_filter.context import build_enhanced_prompt
from nagare_clip.text_filter.llm_filter import filter_transcript
from nagare_clip.text_filter.rule_filter import remove_midstream_closing


def _summary_context(summary_json: Path | None, stem: str) -> tuple[list[str], list[str]]:
    """This stem's (part summaries, keywords) from summary.json; empty on any failure."""
    if summary_json is None or not summary_json.is_file():
        return [], []
    try:
        project = summary_from_dict(json.loads(summary_json.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        logging.warning("text_filter: could not read summary json %s", summary_json)
        return [], []
    summaries = [p.summary for p in project.parts if p.stem == stem]
    return summaries, project.keywords.get(stem, [])


def run_text_filter(
    txt: Path,
    output_txt: Path,
    cfg: dict,
    *,
    summary_json: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    s2 = cfg["text_filter"]
    lines = txt.read_text(encoding="utf-8").splitlines()

    # Rule filter — mark hallucinated closing phrases with {{->}} markers
    original_lines = lines
    lines = remove_midstream_closing(lines)
    rule_changes = sum(1 for o, r in zip(original_lines, lines) if o != r)
    if rule_changes:
        logging.info("text_filter: rule filter marked %d line(s)", rule_changes)

    if not s2["use_llm"]:
        logging.info("text_filter: AI filter disabled, writing edits file")
        result_lines = lines
    else:
        logging.info("text_filter: filtering %d lines with AI", len(lines))

        filter_cfg = dict(s2)
        summaries, summary_keywords = _summary_context(summary_json, txt.stem)
        keywords = list(s2.get("keywords", [])) + summary_keywords
        if summaries or keywords:
            filter_cfg["prompt"] = build_enhanced_prompt(s2.get("prompt", ""), summaries, keywords)
            logging.info(
                "text_filter: summary context: %d part summarie(s), %d keyword(s)",
                len(summaries),
                len(keywords),
            )

        result_lines = filter_transcript(lines, filter_cfg, recorder=recorder)

        changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
        logging.info("text_filter: %d/%d lines modified by AI", changes, len(lines))

    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("text_filter: wrote %s", output_txt)
```

- [ ] **Step 4: Delete the retired module and its tests**

```bash
git rm src/nagare_clip/text_filter/summary_llm.py tests/text_filter/test_summary_llm.py
```

In `tests/guided_edit/test_tracing_threading.py`, delete the import `from nagare_clip.text_filter.summary_llm import generate_summary` and the whole `test_summary_llm_threads_trace_meta` function (the summary stage's own trace threading is exercised via `segment_video`'s `with_trace_meta`, which its recorder tests in `tests/summary/test_summarize.py` cover).

Confirm nothing else references the module: `grep -rn "summary_llm" src/ tests/` — `src/` must have no hits; in `tests/` the only acceptable remaining hits are in `tests/test_llm_report.py`, where `"summary_llm"` is just an arbitrary unit-name string in a report fixture (no import — leave it).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/text_filter tests/guided_edit tests/summary -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A src/nagare_clip/text_filter tests/text_filter tests/guided_edit/test_tracing_threading.py
git commit -m "feat(text_filter): consume summary.json context; retire summary_llm"
```

---

### Task 6: pipeline — move `summary` before `text_filter`, rewire adapters

**Files:**
- Modify: `src/nagare_clip/pipeline/stages.py`
- Test: `tests/pipeline/test_stages.py`

**Interfaces:**
- Consumes: Task 2's `run_summary(txts, ...)`; Task 5's `run_text_filter(..., summary_json=...)`.
- Produces: `STAGE_NAMES == ["transcription", "audio_silence", "sentence_split", "summary", "text_filter", "plan", "director", "guided_edit", "intervals", "blender"]`. `--from-stage`/`--to-stage` and skip-validation pick this up automatically (nothing in `cli.py`/`runner.py` hardcodes the order).

- [ ] **Step 1: Write the failing tests**

In `tests/pipeline/test_stages.py`:

1. Update `test_stage_names_canonical_order` to the new order (move `"summary"` before `"text_filter"`).
2. Add two adapter tests (place after `test_sentence_split_adapter_passes_cuts_path`; `_NullRec` is defined in this file):

```python
def test_summary_adapter_reads_sentence_split_txts(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_summary",
        lambda txts, out, cfg, *, json_paths=None, recorder=None: seen.update(
            txts=txts, out=out, json_paths=json_paths
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["summary"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen["txts"] == [out / "sentence_split" / "a.txt"]
    assert seen["json_paths"] == [out / "sentence_split" / "a.json"]
    assert seen["out"] == out / "summary" / "summary.json"


def test_text_filter_adapter_passes_summary_json(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_text_filter",
        lambda txt, out, cfg, *, summary_json=None, recorder=None: seen.append(
            (txt, out, summary_json)
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["text_filter"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen == [
        (
            out / "sentence_split" / "a.txt",
            out / "text_filter" / "a_edits.txt",
            out / "summary" / "summary.json",
        )
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_stages.py -q`
Expected: FAIL — the order assertion (old order in `STAGE_NAMES`), `test_summary_adapter_reads_sentence_split_txts` (adapter still builds `text_filter/a_edits.txt` paths), and `test_text_filter_adapter_passes_summary_json` (adapter passes no `summary_json`, so the lambda records `None`).

- [ ] **Step 3: Implement in `stages.py`**

1. Reorder `STAGE_NAMES`: move `"summary"` directly before `"text_filter"`.
2. Reorder `STAGES`: move `Stage("summary", _summary_run, _summary_required)` directly before the text_filter entry. Also move the `# --- summary ---` adapter code block above the text_filter block so the file reads in pipeline order.
3. `_summary_run` becomes:

```python
def _summary_run(ctx: PipelineContext) -> None:
    print("[summary] Project-wide summaries")
    rec = _recorder(ctx, "summary")
    rec.clear()
    try:
        run_summary(
            [ctx.stage_dir("sentence_split") / f"{s}.txt" for s in ctx.stems],
            ctx.stage_dir("summary") / "summary.json",
            ctx.cfg,
            json_paths=[ctx.stage_dir("sentence_split") / f"{s}.json" for s in ctx.stems],
            recorder=rec,
        )
    finally:
        rec.rebuild_index()
```

4. `_text_filter_run`'s inner call becomes:

```python
            run_text_filter(
                sdir / f"{src.stem}.txt",
                odir / f"{src.stem}_edits.txt",
                ctx.cfg,
                summary_json=ctx.stage_dir("summary") / "summary.json",
                recorder=rec,
            )
```

(`_summary_required`, `_text_filter_required`, and `_transcription_required` are unchanged — the transcription check keys off `sentence_split`'s index, which still directly follows it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline -q`
Expected: PASS

- [ ] **Step 5: Smoke-run the resume path** (no Docker/LLM needed — everything is disabled by default; use two tiny fixtures)

```bash
cd "$(git rev-parse --show-toplevel)"
D=$(mktemp -d); mkdir -p "$D/out/sentence_split" "$D/out/audio_silence"
printf 'こんにちは\n' > "$D/out/sentence_split/x.txt"
printf '{"segments": [{"start": 0.0, "end": 1.0, "text": "こんにちは", "words": [{"word": "こんにちは", "start": 0.0, "end": 1.0}]}]}\n' > "$D/out/sentence_split/x.json"
printf '# silent spans\n' > "$D/out/audio_silence/x_cuts.txt"
touch "$D/x.mp4"
uv run python -m nagare_clip.pipeline --source "$D/x.mp4" --input-videos-dir "$D" \
  --output-dir "$D/out" --from-stage summary --to-stage text_filter
cat "$D/out/summary/summary.json"; cat "$D/out/text_filter/x_edits.txt"
```

Expected: exit 0; `summary.json` is `{"summary": "", "parts": [], "keywords": {}}`; `x_edits.txt` contains `こんにちは`. (If the pipeline CLI flag names differ, check `uv run python -m nagare_clip.pipeline --help` and adapt — the point is a summary→text_filter window run over pre-seeded sentence_split outputs.)

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/pipeline/stages.py tests/pipeline/test_stages.py
git commit -m "feat(pipeline): run summary before text_filter; feed summary.json to filter"
```

---

### Task 7: documentation + full validation

**Files:**
- Modify: `AGENTS.md`, `README.md`, `plan.md`, `docs/stages/text_filter.md`, `docs/stages/pipeline.md`

No code — but the docs policy makes this mandatory, and `make check` gates the branch.

- [ ] **Step 1: AGENTS.md**

1. **Objective list:** renumber so summary is 4 and text_filter is 5. New item texts:
   - `4. summary — a larger LLM segments every video into line-range parts + summaries (and lists per-video misspelling-prone keywords) and writes one all-videos summary (project-wide)`
   - `5. Text editing checkpoint — copies .txt or runs LLM filter with {{old->new}} markers, optionally primed with the summary stage's summaries/keywords`
2. **Naming convention blockquote:** reorder the canonical identifiers to `` `transcription:`, `audio_silence:`, `sentence_split:`, `summary:`, `text_filter:`, `plan:`, `director:`, `guided_edit:`, `intervals:`, `blender:` ``.
3. **Pipeline Overview:** move the whole `### summary — Project-Wide Summaries` section above `### text_filter`. Rewrite its body: runs once project-wide **between sentence_split and text_filter**; segment_video's response now also carries `"keywords"` (misspelling-prone words, per video, coerced leniently, empty on absence); `summary.json` schema is `{summary, parts:[{stem,lines,summary,start?,end?}], keywords:{stem:[...]}}`; **Inputs:** every sentence_split `{stem}.txt` (passed as `txts`, stem from basename) + optionally the sentence_split `{stem}.json` per source (`json_paths`); output unchanged.
4. **text_filter section:** replace the summary-LLM sentence in the stage description with: when `use_llm` is enabled, `run_text_filter` reads the summary stage's `summary.json` (passed as `summary_json`); this video's part summaries and keywords — merged with the constant `text_filter.keywords` list — are appended to the filter LLM's system prompt via `text_filter/context.build_enhanced_prompt`; missing/empty/malformed `summary.json` degrades to the base prompt. **Inputs:** `{stem}.txt`; `output/summary/summary.json` (as `summary_json`).
5. **Project Structure tree:** under `text_filter/`, delete the `summary_llm.py` line and add `context.py               # build_enhanced_prompt(): summary.json context -> filter prompt`; under `summary/`, note keywords in the `summarize.py` comment (e.g. `# PartSummary/ProjectSummary(+keywords), segment_video(), build_summary()`); update the `tests/` comments if they mention summary_llm.
6. **Configuration System paragraph (line ~232):** change `` (`sentence_split`, `text_filter` + its `summary_llm`, `summary`, …) `` to `` (`sentence_split`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`) ``.

- [ ] **Step 2: README.md**

1. **Pipeline Stages list (lines 9–18):** swap items 4/5 — summary becomes 4 (append “; also lists per-video misspelling-prone keywords used by text_filter”), text_filter becomes 5 (append “, optionally primed with summary-stage context”). Keep the rest numbered as-is.
2. **Line 33 paragraph:** change “run **once over all source videos** (project-wide) before `director`” so `summary` is described as running before `text_filter` (its summaries/keywords also prime the filter LLM), while `plan` still runs before `director`.
3. **`#### Summary LLM (optional)` section (lines 264–276):** replace entirely with:

```markdown
#### Filter context from the summary stage (optional)

When the project-wide `summary` stage is enabled (`summary.enabled: true`), it runs
before text_filter and its `summary.json` carries, per video, the part summaries and a
list of rare/domain-specific keywords that speech recognition might misspell. text_filter
appends this video's summaries and keywords to the filter LLM's system prompt so it can
better correct mis-dictated words. You can also pin constant keywords that are always
injected, without enabling the summary stage:

​```yaml
text_filter:
  use_llm: true
  keywords: ["Kubernetes", "PostgreSQL"]   # always appended to the filter prompt
summary:
  enabled: true                            # per-video summaries + keywords for the filter
​```

Falls back gracefully — a missing or empty `summary.json` just means filtering proceeds
without the extra context.

> **Migration:** the former `text_filter.summary_llm` section was removed and now fails
> validation. Move `summary_llm.keywords` to `text_filter.keywords` and use
> `summary.enabled` for LLM-generated context.
```

(Write the fenced block with plain backticks — the `​` markers above only protect this plan's own fencing.)
4. **`--from-stage` option (line 288):** reorder the name list to `transcription`, `audio_silence`, `sentence_split`, `summary`, `text_filter`, `plan`, `director`, `guided_edit`, `intervals`, `blender`.
5. Line 201: drop “and its `summary_llm`”.

- [ ] **Step 3: docs/stages/text_filter.md**

Replace the summary-LLM sentence in the bullet (line 5) with: “When `use_llm` is enabled and the orchestrator passes `summary_json` (the summary stage's `summary.json`), this video's part summaries and per-video keywords — merged after the constant `text_filter.keywords` list — are appended to the filter LLM's system prompt (`context.build_enhanced_prompt`); a missing, empty, or unreadable `summary.json` degrades to the base prompt. The stem is derived from the input filename (`{stem}.txt`).”

- [ ] **Step 4: docs/stages/pipeline.md**

Update ordering references: the `output/transcription|audio_silence|sentence_split|text_filter|summary|plan|…` directory string (line ~111) moves `summary` before `text_filter`; adjust any sentence saying summary runs after text_filter or reads `_edits.txt` (grep the file for `text_filter` and `summary` and fix each hit).

- [ ] **Step 5: plan.md**

Append a short status section:

```markdown
## summary/text_filter Merge

**Status: complete.**

The `summary` stage moved before `text_filter` (order: …, sentence_split, summary,
text_filter, plan, …) and now reads the sentence_split `{stem}.txt` transcripts — safe
because text_filter is line-preserving, so part line-ranges stay valid downstream. Its
per-video LLM call also returns `"keywords"` (misspelling-prone words), stored as
`summary.json`'s top-level `keywords: {stem: [...]}`. text_filter's built-in summary LLM
(`text_filter/summary_llm.py`, config `text_filter.summary_llm`) was removed; the filter
prompt is now primed from `summary.json` (+ the constant `text_filter.keywords` list) via
`text_filter/context.build_enhanced_prompt`. Config break: `text_filter.summary_llm` now
fails validation (migration: move `keywords` up; enable `summary.enabled`).
```

- [ ] **Step 6: Full validation**

Run: `make check`
Expected: lint, format check, validate, and the full pytest suite all pass. Fix anything that surfaces (e.g. ruff import ordering in touched files).

- [ ] **Step 7: Commit**

```bash
git add AGENTS.md README.md plan.md docs/stages/text_filter.md docs/stages/pipeline.md
git commit -m "docs: summary stage before text_filter; summary.json keywords context"
```
