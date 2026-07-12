# Per-Video Summaries in the summary Stage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the summary stage a mandatory per-video whole-video summary that is fed to the overall-summary LLM call, persisted in `summary.json`, and exposed to the plan/director/text_filter context builders.

**Architecture:** The map call (`segment_video`) gains a required `video_summary` JSON field, returning `(parts, keywords, video_summary)`. `build_summary` collects a `{stem: video_summary}` dict onto `ProjectSummary.video_summaries`. The reduce call (`generate_project_summary`) restructures its input into per-video groups (a `##` header carrying that video's summary, parts nested beneath, global 1-based numbering kept). The dict round-trips through `summary.json` and the plan/director/text_filter context builders each render the relevant video summary, all with a byte-identical fallback when `video_summaries` is empty (stage disabled / old file).

**Tech Stack:** Python 3, pydantic-settings config, pytest, uv, LiteLLM (mocked in tests).

## Global Constraints

- Run all Python via `uv run` (e.g. `uv run pytest`).
- TDD: write the failing test, run it and confirm it fails, then implement. For behaviors added to already-existing code, mutate the implementation to confirm the test catches it, then revert (per repo AGENTS.md / global CLAUDE.md).
- `video_summary` is **mandatory at parse level**: `_parse_parts_response` returns `None` (hard parse failure → retry) when it is missing / non-string / empty-or-whitespace. This mirrors a missing `"parts"` array.
- `summary_from_dict` must remain **backward compatible**: an old `summary.json` without `video_summaries` loads with `video_summaries == {}`. This is compatibility, not parse leniency.
- Downstream context builders (plan/director/text_filter) must be **byte-identical** to today when `video_summaries` is empty; each gets a regression test proving it.
- LLM stages degrade gracefully: all-attempts-failed → `([], [], "")` for `segment_video` (existing degrade path, now a 3-tuple).
- Config defaults are the single source of truth; after editing prompt defaults run `make config-example` and keep `config.example.yml` in sync (`tests/test_config.py::test_example_file_matches_generator`).

## File Structure

- `src/nagare_clip/summary/summarize.py` — map/reduce core; `PartSummary`/`ProjectSummary`, parse, dict round-trip. **Primary changes.**
- `src/nagare_clip/config.py` — `SUMMARY_PROMPT` (request `video_summary`), `SUMMARY_OVERALL_PROMPT` (mention per-video headers).
- `src/nagare_clip/summary/run.py` — disabled no-op `ProjectSummary(...)` construction (add `video_summaries={}`; already defaults, but keep explicit-free).
- `src/nagare_clip/plan/plan_llm.py` — `_format_parts_for_plan` renders per-video summary header.
- `src/nagare_clip/director/context.py` — `build_director_context` renders this video's summary + sibling summaries.
- `src/nagare_clip/text_filter/context.py` + `run.py` — `build_enhanced_prompt` shows this video's whole-video summary.
- Tests: `tests/summary/test_summarize.py`, `tests/summary/test_run.py`, `tests/plan/test_plan_llm.py`, `tests/director/test_context.py`, `tests/text_filter/test_context.py`, `tests/text_filter/test_run.py`.
- Docs: `AGENTS.md`, `README.md`, `plan.md`, `docs/stages/text_filter.md`, `config.example.yml` (generated).

---

### Task 1: `video_summary` field — dataclass, parse, and `segment_video` return

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py`
- Test: `tests/summary/test_summarize.py`

**Interfaces:**
- Produces:
  - `ProjectSummary` gains field `video_summaries: dict[str, str] = field(default_factory=dict)`.
  - `_parse_parts_response(response, stem, num_lines, drops=None) -> tuple[list[PartSummary], list[str], str] | None` (now a 3-tuple; `None` on hard failure incl. missing `video_summary`).
  - `segment_video(stem, clean_lines, cfg, *, call_llm=_call_llm, recorder=NULL_RECORDER) -> tuple[list[PartSummary], list[str], str]` (adds `video_summary` as 3rd element; degrade path returns `([], [], "")`).
- Consumes: existing `_strip_fence`, `_coerce_lines`, `_coerce_keywords`, recorder API.

- [ ] **Step 1: Write the failing tests**

Add to `tests/summary/test_summarize.py` inside `class TestSegmentVideo` (and update the existing 2-tuple unpackings in this file to 3-tuple — see Step 3 note):

```python
    def test_returns_video_summary(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}],'
            ' "keywords": [], "video_summary": "whole video overview"}'
        )
        parts, keywords, vsum = segment_video(
            "v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp
        )
        assert parts == [PartSummary(stem="v", lines=(1, 1), summary="s")]
        assert vsum == "whole video overview"

    def test_missing_video_summary_is_hard_failure(self):
        # No video_summary at all -> retried, then degraded to empty.
        resp = '{"parts": [{"lines": [1, 1], "summary": "s"}], "keywords": []}'
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp) == (
            [],
            [],
            "",
        )

    def test_empty_video_summary_is_hard_failure(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}],'
            ' "keywords": [], "video_summary": "   "}'
        )
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp) == (
            [],
            [],
            "",
        )

    def test_non_string_video_summary_is_hard_failure(self):
        resp = (
            '{"parts": [{"lines": [1, 1], "summary": "s"}],'
            ' "keywords": [], "video_summary": 5}'
        )
        assert segment_video("v", ["a"], {"prompt": "P"}, call_llm=lambda m, c: resp) == (
            [],
            [],
            "",
        )
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/summary/test_summarize.py::TestSegmentVideo -v`
Expected: the new `test_returns_video_summary` / `*_hard_failure` FAIL (ValueError: not enough values to unpack, or wrong tuple). Pre-existing tests in the class will also error on unpacking until Step 3 updates them — that is expected; proceed.

- [ ] **Step 3: Implement the parse + return change**

In `summarize.py`, change `_parse_parts_response` to validate and return `video_summary`:

```python
def _parse_parts_response(
    response: str, stem: str, num_lines: int, drops: list[str] | None = None
) -> tuple[list[PartSummary], list[str], str] | None:
    """Parse ``{"parts": [...], "keywords": [...], "video_summary": "…"}``.

    Returns ``None`` on a hard parse failure — invalid JSON, no ``parts`` array,
    or a missing/non-string/empty ``video_summary`` — so the caller can retry.
    Otherwise ``(parts, keywords, video_summary)`` where ``parts`` is the
    validated (possibly empty) list with malformed entries dropped (logged).
    """

    def _drop(msg: str) -> None:
        logger.warning("summary: %s", msg)
        if drops is not None:
            drops.append(msg)

    try:
        data = json.loads(_strip_fence(response))
    except (ValueError, TypeError):
        logger.warning("summary: parts response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list):
        logger.warning("summary: parts response has no 'parts' array; ignoring")
        return None
    video_summary = data.get("video_summary")
    if not isinstance(video_summary, str) or not video_summary.strip():
        logger.warning("summary: parts response has no 'video_summary'; ignoring")
        return None

    parts: list[PartSummary] = []
    for raw in data["parts"]:
        if not isinstance(raw, dict):
            continue
        lines = _coerce_lines(raw.get("lines"), num_lines)
        if lines is None:
            _drop(f"part dropped, bad lines {raw.get('lines')!r}")
            continue
        summary = raw.get("summary")
        if not isinstance(summary, str) or summary == "":
            _drop("part dropped, empty/missing summary")
            continue
        parts.append(PartSummary(stem=stem, lines=lines, summary=summary))
    return parts, _coerce_keywords(data.get("keywords")), video_summary.strip()
```

Add the `video_summaries` field to `ProjectSummary`:

```python
@dataclass
class ProjectSummary:
    summary: str
    parts: list[PartSummary] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict)  # stem -> correct spellings
    video_summaries: dict[str, str] = field(default_factory=dict)  # stem -> whole-video summary
```

Update `segment_video`: change its return type to `tuple[list[PartSummary], list[str], str]`, unpack the 3-tuple from `_parse_parts_response`, and return `video_summary` on success / `("", i.e. [], [], "")` on the all-failed path. Concretely, replace the success block's unpack and return:

```python
        parts, keywords, video_summary = parsed
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not parts:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=stem,
            attempt=attempt,
            total=attempts,
            messages=messages,
            response=response,
            outcome=outcome,
            reason=reason,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(stem, outcome=outcome, reason=reason)
        return parts, keywords, video_summary
```

and the final degrade line:

```python
    recorder.flush_unit(stem, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("summary: all %d attempt(s) failed for %s; no parts", attempts, stem)
    return [], [], ""
```

Also update the docstring of `segment_video` to mention the whole-video summary.

Then fix **every pre-existing** fixture and unpack in `tests/summary/test_summarize.py` — this is not limited to `TestSegmentVideo`; the `build_summary`/`generate_project_summary` tests (around lines 117-135, 192, 272-288) also feed parts-responses through `segment_video`, and a parts-response now *requires* `video_summary` or its parts are dropped on parse:

  1. For **every** JSON string that contains `"parts"` (i.e. a `segment_video` response fixture — inline lambdas and `seq`/`_seq_llm` item lists alike), add `"video_summary": "v"` (any non-empty string) as a sibling key before the closing brace. A pure reduce-step fixture (only `{"summary": ...}`) is left unchanged.
  2. Change each `parts, _ = segment_video(...)` to `parts, _, _ = segment_video(...)`, and `parts, keywords = ...` / `_, keywords = ...` to add the trailing `, _`.
  3. Change degrade-path assertions `== ([], [])` to `== ([], [], "")`.
  4. Any `build_summary(...)` test that asserts on collected parts must still see non-empty parts — confirm its per-video fixtures now carry `video_summary` (step 1 covers this).

  Search the file for `"parts"`, `segment_video(`, `== ([], [])`, and each `seq`/`_seq_llm(` list to find every site. Run the full file after editing to confirm none were missed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/test_summarize.py -v`
Expected: PASS (all, including updated pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/summary/summarize.py tests/summary/test_summarize.py
git commit -m "feat(summary): require per-video video_summary in segment_video"
```

---

### Task 2: `build_summary` collects `video_summaries`; dict round-trip

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py`
- Test: `tests/summary/test_summarize.py`

**Interfaces:**
- Consumes: `segment_video` 3-tuple (Task 1), `ProjectSummary.video_summaries` (Task 1).
- Produces:
  - `build_summary(...) -> ProjectSummary` now populates `.video_summaries` (a `{stem: video_summary}` map, only for videos with a non-empty summary).
  - `summary_to_dict(ps)` adds top-level `"video_summaries": {...}`.
  - `summary_from_dict(data)` reads `"video_summaries"` leniently (absent/non-dict/non-string values → dropped; old file → `{}`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/summary/test_summarize.py`:

```python
class TestVideoSummaries:
    def test_build_summary_collects_video_summaries(self):
        responses = iter(
            [
                '{"parts": [{"lines": [1, 1], "summary": "p"}],'
                ' "keywords": [], "video_summary": "vid-A overview"}',
                '{"summary": "project"}',  # reduce
            ]
        )
        project = build_summary(
            [("A", ["a"])], {"prompt": "P", "overall_prompt": "O"},
            call_llm=lambda m, c: next(responses),
        )
        assert project.video_summaries == {"A": "vid-A overview"}

    def test_to_dict_includes_video_summaries(self):
        ps = ProjectSummary(
            summary="s",
            parts=[PartSummary(stem="A", lines=(1, 1), summary="p")],
            video_summaries={"A": "overview"},
        )
        d = summary_to_dict(ps)
        assert d["video_summaries"] == {"A": "overview"}

    def test_from_dict_reads_video_summaries(self):
        d = {
            "summary": "s",
            "parts": [{"stem": "A", "lines": [1, 1], "summary": "p"}],
            "video_summaries": {"A": "overview", "B": 5, "": "x"},
        }
        ps = summary_from_dict(d)
        assert ps.video_summaries == {"A": "overview"}

    def test_from_dict_old_file_without_video_summaries(self):
        d = {"summary": "s", "parts": [{"stem": "A", "lines": [1, 1], "summary": "p"}]}
        ps = summary_from_dict(d)
        assert ps.video_summaries == {}

    def test_round_trip(self):
        ps = ProjectSummary(
            summary="s",
            parts=[PartSummary(stem="A", lines=(1, 2), summary="p")],
            keywords={"A": ["kw"]},
            video_summaries={"A": "overview"},
        )
        assert summary_from_dict(summary_to_dict(ps)).video_summaries == {"A": "overview"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/summary/test_summarize.py::TestVideoSummaries -v`
Expected: FAIL (`video_summaries` empty / key missing).

- [ ] **Step 3: Implement**

In `build_summary`, collect the third return value into a dict and set it on the result:

```python
def build_summary(
    parts_input: list[tuple[str, list[str]]],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    seg_times_by_stem: dict[str, list[tuple[float | None, float | None]]] | None = None,
) -> ProjectSummary:
    """Map (``segment_video`` per video) then reduce (``generate_project_summary``)."""
    parts: list[PartSummary] = []
    keywords: dict[str, list[str]] = {}
    video_summaries: dict[str, str] = {}
    for stem, clean_lines in parts_input:
        video_parts, video_keywords, video_summary = segment_video(
            stem, clean_lines, cfg, call_llm=call_llm, recorder=recorder
        )
        parts.extend(video_parts)
        if video_keywords:
            keywords[stem] = video_keywords
        if video_summary:
            video_summaries[stem] = video_summary
    if seg_times_by_stem:
        for p in parts:
            _attach_part_times(p, seg_times_by_stem.get(p.stem))
    summary = generate_project_summary(
        parts, cfg, call_llm=call_llm, recorder=recorder, video_summaries=video_summaries
    )
    return ProjectSummary(
        summary=summary, parts=parts, keywords=keywords, video_summaries=video_summaries
    )
```

(Note: `generate_project_summary` gains a `video_summaries` kwarg in Task 3. If implementing Task 2 before Task 3, temporarily call it without that kwarg and add the kwarg in Task 3; to avoid churn, prefer implementing Task 3's signature change together. The `test_build_summary_collects_video_summaries` test here does not assert on the reduce input, so either order works.)

In `summary_to_dict`, add the field to the returned dict:

```python
    return {
        "summary": ps.summary,
        "parts": parts,
        "keywords": ps.keywords,
        "video_summaries": ps.video_summaries,
    }
```

In `summary_from_dict`, read it leniently (place near the `keywords` block):

```python
    video_summaries: dict[str, str] = {}
    raw_vs = data.get("video_summaries")
    if isinstance(raw_vs, dict):
        for k, v in raw_vs.items():
            if isinstance(k, str) and k and isinstance(v, str) and v.strip():
                video_summaries[k] = v.strip()
    return ProjectSummary(
        summary=summary, parts=parts, keywords=keywords, video_summaries=video_summaries
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/test_summarize.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/summary/summarize.py tests/summary/test_summarize.py
git commit -m "feat(summary): persist video_summaries in ProjectSummary + summary.json"
```

---

### Task 3: Per-video grouped input for the overall (reduce) call

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py`, `src/nagare_clip/config.py`
- Test: `tests/summary/test_summarize.py`

**Interfaces:**
- Consumes: `ProjectSummary.video_summaries` / `build_summary`'s local `video_summaries` (Task 2).
- Produces:
  - New helper `_format_parts_doc(parts, video_summaries) -> str` grouping parts under per-video `##` headers with the video summary, keeping global 1-based numbering.
  - `generate_project_summary(parts, cfg, *, call_llm=_call_llm, recorder=NULL_RECORDER, video_summaries=None) -> str` (new keyword-only `video_summaries` arg; `None` → `{}`).

- [ ] **Step 1: Write the failing test**

Add to `tests/summary/test_summarize.py`:

```python
class TestOverallInputFormat:
    def test_groups_parts_by_video_with_summary_header(self):
        captured = {}

        def fake(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"summary": "ok"}'

        parts = [
            PartSummary(stem="A", lines=(1, 12), summary="a1"),
            PartSummary(stem="A", lines=(13, 20), summary="a2"),
            PartSummary(stem="B", lines=(1, 5), summary="b1"),
        ]
        generate_project_summary(
            parts,
            {"overall_prompt": "O"},
            call_llm=fake,
            video_summaries={"A": "video A overview", "B": "video B overview"},
        )
        assert captured["user"] == (
            "## A — video A overview\n"
            "1: [1-12] — a1\n"
            "2: [13-20] — a2\n"
            "## B — video B overview\n"
            "3: [1-5] — b1"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/summary/test_summarize.py::TestOverallInputFormat -v`
Expected: FAIL (`generate_project_summary` has no `video_summaries` kwarg / old flat format).

- [ ] **Step 3: Implement**

Replace `_format_parts_doc` with a grouped formatter (keeps global numbering, preserves first-seen video order):

```python
def _format_parts_doc(parts: list[PartSummary], video_summaries: dict[str, str]) -> str:
    lines: list[str] = []
    current: str | None = None
    for i, p in enumerate(parts):
        if p.stem != current:
            current = p.stem
            vs = video_summaries.get(p.stem, "")
            lines.append(f"## {p.stem} — {vs}" if vs else f"## {p.stem}")
        lines.append(f"{i + 1}: [{p.lines[0]}-{p.lines[1]}] — {p.summary}")
    return "\n".join(lines)
```

Update `generate_project_summary`'s signature and the message build:

```python
def generate_project_summary(
    parts: list[PartSummary],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    video_summaries: dict[str, str] | None = None,
) -> str:
    """Synthesise a single all-videos summary from the per-part summaries."""
    if not parts:
        return ""
    messages = [
        {"role": "system", "content": cfg.get("overall_prompt", "")},
        {"role": "user", "content": _format_parts_doc(parts, video_summaries or {})},
    ]
```

(Leave the rest of `generate_project_summary` unchanged.) Ensure `build_summary` passes `video_summaries=video_summaries` (done in Task 2).

Update `SUMMARY_PROMPT` in `config.py` to request the new field. Change the JSON-shape and rules blocks:

```python
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
```

Update `SUMMARY_OVERALL_PROMPT` to describe the grouped input:

```python
SUMMARY_OVERALL_PROMPT = (
    "You are a video editor. You receive per-part summaries grouped by "
    "source video: each video starts with a `## <name> — <video summary>` "
    "header, followed by its numbered parts. Write ONE concise overall "
    "summary of the whole project. Output ONLY a JSON object:\n"
    '{"summary": "..."}\n'
    "Output only the JSON object, no other text."
)
```

- [ ] **Step 4: Run tests + regenerate config**

Run: `uv run pytest tests/summary/test_summarize.py -v`
Expected: PASS.
Run: `make config-example`
Then: `uv run pytest tests/test_config.py -v`
Expected: PASS (example file back in sync).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/summary/summarize.py src/nagare_clip/config.py config.example.yml tests/summary/test_summarize.py
git commit -m "feat(summary): group overall-call input by video with per-video summary"
```

---

### Task 4: `run.py` no-op + run() test for `video_summaries`

**Files:**
- Modify: `src/nagare_clip/summary/run.py`
- Test: `tests/summary/test_run.py`

**Interfaces:**
- Consumes: `build_summary` populating `video_summaries` (Task 2), `summary_to_dict` writing it (Task 2).
- Produces: the disabled no-op and enabled path both emit a `"video_summaries"` key in `summary.json`.

- [ ] **Step 1: Write the failing tests**

First read `tests/summary/test_run.py` for the fixture style, then add:

```python
def test_disabled_writes_empty_video_summaries(tmp_path):
    out = tmp_path / "summary.json"
    run_summary([], out, {"summary": {"enabled": False}})
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["video_summaries"] == {}
```

(Match the existing imports/`run_summary` call convention already used in the file — mirror the closest existing disabled-path test.)

- [ ] **Step 2: Run test to verify it fails / passes for the right reason**

Run: `uv run pytest tests/summary/test_run.py -v`
Expected: If `summary_to_dict` already emits the key (Task 2), the disabled test may PASS because `ProjectSummary(summary="", parts=[])` defaults `video_summaries={}`. To confirm the test has teeth, temporarily remove the `"video_summaries"` key from `summary_to_dict`, run the test, confirm FAIL, then revert. Record this mutation-catch evidence.

- [ ] **Step 3: Implement (if needed)**

`run.py`'s disabled branch `ProjectSummary(summary="", parts=[])` already defaults `video_summaries={}`; no code change required beyond confirming Task 2's `summary_to_dict` includes the key. Leave `run.py` as-is unless the mutation test reveals a gap.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/summary/test_run.py
git commit -m "test(summary): assert video_summaries in run() output"
```

---

### Task 5: plan context renders per-video summary header

**Files:**
- Modify: `src/nagare_clip/plan/plan_llm.py`
- Test: `tests/plan/test_plan_llm.py`

**Interfaces:**
- Consumes: `ProjectSummary.video_summaries` (Task 2).
- Produces: `_format_parts_for_plan(project_summary)` inserts a `Video "<stem>": <summary>` line above each video's first part when a summary exists; byte-identical output when `video_summaries` is empty.

- [ ] **Step 1: Write the failing tests**

First read `tests/plan/test_plan_llm.py` to find existing `_format_parts_for_plan` tests, then add:

```python
def test_format_inserts_video_summary_header():
    from nagare_clip.plan.plan_llm import _format_parts_for_plan

    ps = ProjectSummary(
        summary="overall",
        parts=[
            PartSummary(stem="A", lines=(1, 2), summary="a1"),
            PartSummary(stem="B", lines=(1, 3), summary="b1"),
        ],
        video_summaries={"A": "video A overview", "B": "video B overview"},
    )
    text = _format_parts_for_plan(ps)
    assert 'Video "A": video A overview' in text
    assert 'Video "B": video B overview' in text
    # header appears before that video's part line
    assert text.index('Video "A"') < text.index("1: A [1-2]")


def test_format_byte_identical_when_no_video_summaries():
    from nagare_clip.plan.plan_llm import _format_parts_for_plan

    parts = [
        PartSummary(stem="A", lines=(1, 2), summary="a1"),
        PartSummary(stem="B", lines=(1, 3), summary="b1"),
    ]
    without = _format_parts_for_plan(ProjectSummary(summary="overall", parts=parts))
    # Expected == the exact current format (no Video: lines).
    assert "Video \"" not in without
```

(Import `ProjectSummary`, `PartSummary` at the top of the test file if not already imported.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plan/test_plan_llm.py -k "video_summary or byte_identical" -v`
Expected: `test_format_inserts_video_summary_header` FAILs; `test_format_byte_identical...` PASSes (guards the empty case — keep it green throughout).

- [ ] **Step 3: Implement**

Update `_format_parts_for_plan` to emit a per-video summary header on stem change (only when a summary exists), preserving all existing part-line formatting:

```python
def _format_parts_for_plan(project_summary: ProjectSummary) -> str:
    lines: list[str] = []
    if project_summary.summary:
        lines.append(f"Overall: {project_summary.summary}")
        lines.append("")
    parts = project_summary.parts
    video_summaries = project_summary.video_summaries
    current: str | None = None
    for i, p in enumerate(parts):
        if p.stem != current:
            current = p.stem
            vs = video_summaries.get(p.stem, "")
            if vs:
                lines.append(f'Video "{p.stem}": {vs}')
        dur = p.end - p.start if p.start is not None and p.end is not None else None
        gap: float | None = None
        if i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt.stem == p.stem and p.end is not None and nxt.start is not None:
                gap = nxt.start - p.end
        bracket = format_dur_gap(dur, gap)
        prefix = f"{i + 1}: {p.stem} [{p.lines[0]}-{p.lines[1]}]"
        head = f"{prefix} {bracket}" if bracket else prefix
        lines.append(f"{head} — {p.summary}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plan/ -v`
Expected: PASS (incl. existing format tests unchanged when `video_summaries` empty).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/plan/plan_llm.py tests/plan/test_plan_llm.py
git commit -m "feat(plan): show per-video summary header in plan context"
```

---

### Task 6: director context renders this-video + sibling summaries

**Files:**
- Modify: `src/nagare_clip/director/context.py`
- Test: `tests/director/test_context.py`

**Interfaces:**
- Consumes: `ProjectSummary.video_summaries` (Task 2).
- Produces: `build_director_context(project_summary, directions, stem)` — adds a `Summary: <video_summary>` line under the `This video (...)` header when present; sibling one-liners use the sibling's video summary when present (falling back to the first-part summary otherwise). Byte-identical output when `video_summaries` is empty.

- [ ] **Step 1: Write the failing tests**

First read `tests/director/test_context.py` to see the existing assertions and the byte-identical regression test (if any), then add:

```python
def test_this_video_summary_line():
    ps = ProjectSummary(
        summary="overall",
        parts=[PartSummary(stem="A", lines=(1, 2), summary="a1")],
        video_summaries={"A": "video A overview"},
    )
    ctx = build_director_context(ps, [], "A")
    assert "Summary: video A overview" in ctx


def test_sibling_uses_video_summary_when_present():
    ps = ProjectSummary(
        summary="overall",
        parts=[
            PartSummary(stem="A", lines=(1, 2), summary="a1"),
            PartSummary(stem="B", lines=(1, 3), summary="b-first-part"),
        ],
        video_summaries={"B": "video B overview"},
    )
    ctx = build_director_context(ps, [], "A")
    assert "- B: video B overview" in ctx
    assert "b-first-part" not in ctx


def test_byte_identical_without_video_summaries():
    parts = [
        PartSummary(stem="A", lines=(1, 2), summary="a1"),
        PartSummary(stem="B", lines=(1, 3), summary="b1"),
    ]
    ps = ProjectSummary(summary="overall", parts=parts)  # video_summaries == {}
    ctx = build_director_context(ps, [], "A")
    assert "Summary:" not in ctx
    assert "- B: b1" in ctx  # sibling falls back to first-part summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/director/test_context.py -k "video_summary or sibling or byte_identical" -v`
Expected: the `Summary:` / sibling-override tests FAIL; the byte-identical test PASSes.

- [ ] **Step 3: Implement**

Update `build_director_context` — add the summary line under the `This video` header and prefer the sibling video summary:

```python
def build_director_context(
    project_summary: ProjectSummary,
    directions: list[PartDirection],
    stem: str,
) -> str:
    parts = project_summary.parts
    video_summaries = project_summary.video_summaries
    own = [p for p in parts if p.stem == stem]
    if not project_summary.summary and not own:
        return ""

    dir_by_key = {(d.stem, d.lines): d.direction for d in directions}

    out: list[str] = ["Project context (all videos):"]
    if project_summary.summary:
        out.append(f"Overall: {project_summary.summary}")

    if own:
        out.append(f'This video ("{stem}"):')
        own_summary = video_summaries.get(stem, "")
        if own_summary:
            out.append(f"Summary: {own_summary}")
        for p in own:
            line = f"- lines {p.lines[0]}-{p.lines[1]}: {p.summary}"
            direction = dir_by_key.get((p.stem, p.lines), "")
            if direction:
                line += f" → direction: {direction}"
            out.append(line)

    # One line per other source video (its video summary, else first part's summary).
    seen: dict[str, str] = {}
    for p in parts:
        if p.stem != stem and p.stem not in seen:
            seen[p.stem] = video_summaries.get(p.stem) or p.summary
    if seen:
        out.append("Other videos:")
        for s, summary in seen.items():
            out.append(f"- {s}: {summary}")

    return "\n".join(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/director/ -v`
Expected: PASS (existing tests still green — empty `video_summaries` path unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/director/context.py tests/director/test_context.py
git commit -m "feat(director): inject per-video + sibling summaries into context"
```

---

### Task 7: text_filter shows this video's whole-video summary

**Files:**
- Modify: `src/nagare_clip/text_filter/context.py`, `src/nagare_clip/text_filter/run.py`
- Test: `tests/text_filter/test_context.py`, `tests/text_filter/test_run.py`

**Interfaces:**
- Consumes: `ProjectSummary.video_summaries` (Task 2), read via `summary_from_dict` in `run.py`.
- Produces:
  - `build_enhanced_prompt(base_prompt, summaries, keywords, video_summary="")` — new optional trailing arg; when non-empty, renders a `Video summary: <video_summary>` line above the part summaries. Absent/empty → byte-identical to today.
  - `_summary_context(summary_json, stem) -> tuple[list[str], list[str], str]` in `run.py` (adds this stem's video summary as 3rd element).

- [ ] **Step 1: Write the failing tests**

Read `tests/text_filter/test_context.py`, then add:

```python
def test_video_summary_line_when_present():
    from nagare_clip.text_filter.context import build_enhanced_prompt

    out = build_enhanced_prompt("BASE", ["part one"], ["kw"], video_summary="the whole video")
    assert "Video summary: the whole video" in out


def test_byte_identical_without_video_summary():
    from nagare_clip.text_filter.context import build_enhanced_prompt

    # Default (no video_summary) must equal the pre-change output.
    with_default = build_enhanced_prompt("BASE", ["part one"], ["kw"])
    assert "Video summary:" not in with_default
```

Add to `tests/text_filter/test_run.py` a test that `_summary_context` returns the stem's video summary (mirror the existing `_summary_context` test if present):

```python
def test_summary_context_returns_video_summary(tmp_path):
    from nagare_clip.text_filter.run import _summary_context

    sj = tmp_path / "summary.json"
    sj.write_text(
        json.dumps(
            {
                "summary": "o",
                "parts": [{"stem": "A", "lines": [1, 2], "summary": "p"}],
                "keywords": {"A": ["kw"]},
                "video_summaries": {"A": "video A overview"},
            }
        ),
        encoding="utf-8",
    )
    summaries, keywords, vsum = _summary_context(sj, "A")
    assert summaries == ["p"]
    assert keywords == ["kw"]
    assert vsum == "video A overview"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/text_filter/test_context.py tests/text_filter/test_run.py -k "video_summary or byte_identical" -v`
Expected: `*_when_present` / `_summary_context...` FAIL (unexpected kwarg / 2-tuple unpack); `byte_identical` PASSes.

- [ ] **Step 3: Implement**

`context.py` — add the optional arg and the line:

```python
def build_enhanced_prompt(
    base_prompt: str,
    summaries: list[str],
    keywords: list[str],
    video_summary: str = "",
) -> str:
    """Append this video's whole-video summary, part summaries and keywords to the base prompt."""
    summaries = [s for s in summaries if s]
    keywords = [k for k in keywords if k]
    video_summary = video_summary.strip()
    if not summaries and not keywords and not video_summary:
        return base_prompt
    parts = [base_prompt, "", "Context about this transcript:"]
    if video_summary:
        parts.append(f"Video summary: {video_summary}")
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

`run.py` — extend `_summary_context` to a 3-tuple and pass it through:

```python
def _summary_context(summary_json: Path | None, stem: str) -> tuple[list[str], list[str], str]:
    """This stem's (part summaries, keywords, video summary) from summary.json; empty on failure."""
    if summary_json is None or not summary_json.is_file():
        return [], [], ""
    try:
        project = summary_from_dict(json.loads(summary_json.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        logging.warning("text_filter: could not read summary json %s", summary_json)
        return [], [], ""
    summaries = [p.summary for p in project.parts if p.stem == stem]
    return summaries, project.keywords.get(stem, []), project.video_summaries.get(stem, "")
```

And in `run_text_filter`, update the call site (around line 61-64):

```python
        summaries, summary_keywords, video_summary = _summary_context(summary_json, txt.stem)
        keywords = list(dict.fromkeys(list(s2.get("keywords", [])) + summary_keywords))
        if summaries or keywords or video_summary:
            filter_cfg["prompt"] = build_enhanced_prompt(
                s2.get("prompt", ""), summaries, keywords, video_summary
            )
```

(Also update the immediately-following `logging.info(...)` if it references only summaries/keywords — leave counts as-is; adding the video summary to the message is optional and not required by tests.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/text_filter/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/text_filter/context.py src/nagare_clip/text_filter/run.py tests/text_filter/test_context.py tests/text_filter/test_run.py
git commit -m "feat(text_filter): surface this video's whole-video summary in prompt"
```

---

### Task 8: Full suite, lint, and documentation

**Files:**
- Modify: `AGENTS.md`, `README.md`, `plan.md`, `docs/stages/text_filter.md`

**Interfaces:** none (docs + verification only).

- [ ] **Step 1: Run the whole suite + checks**

Run: `make check`
Expected: PASS (`lint` + `format-check` + `validate` + `test`). Fix any lint/format issues (`make format`) and re-run.

- [ ] **Step 2: Update AGENTS.md**

In the `summary` stage overview: note that `segment_video()` also returns a mandatory whole-video summary (`"video_summary"`), that `summary.json` now carries a top-level `video_summaries:{stem:...}` map, and that the overall reduce call receives per-video grouped input (`## <stem> — <summary>`). In the `plan`, `director`, and `text_filter` overviews: note that each now surfaces the relevant video's whole-video summary (byte-identical when absent). Update the `summary.json` shape description (`{summary, parts, keywords, video_summaries}`) everywhere it appears.

- [ ] **Step 3: Update docs/stages/text_filter.md**

Document that `build_enhanced_prompt` now also injects this video's whole-video summary (`Video summary:` line) from `summary.json`'s `video_summaries`, degrading to the base prompt when absent.

- [ ] **Step 4: Update README.md and plan.md**

`README.md`: reflect the new `summary.json` field and prompt behavior in any user-facing summary-stage section. `plan.md`: mark the per-video-summaries work as done / update status.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md plan.md docs/stages/text_filter.md
git commit -m "docs: per-video summaries in summary/plan/director/text_filter"
```

---

## Self-Review Notes

- **Spec coverage:** map field (Task 1), persistence + round-trip (Task 2), grouped reduce input (Task 3), no-op schema (Task 4), plan/director/text_filter exposure (Tasks 5-7), docs (Task 8). All spec sections covered.
- **Mandatory-vs-compat split:** `_parse_parts_response` rejects missing `video_summary` (Task 1); `summary_from_dict` tolerates its absence (Task 2). Both tested.
- **Byte-identical guards:** plan (Task 5), director (Task 6), text_filter (Task 7) each have an explicit empty-`video_summaries` regression test.
- **Type consistency:** `segment_video`/`_parse_parts_response` 3-tuple used consistently in `build_summary`; `generate_project_summary(..., video_summaries=...)` kwarg defined in Task 3 and called in Task 2 (implement Task 3's signature alongside to avoid an intermediate broken state, or land Tasks 2+3 together).
