# publish as an agent skill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the publish stage's LLM calls with deterministic tools (`publish_tools.sh view|frames|write|render`) driven by a `nagare-publish` agent skill, then delete the LLM publish and the `publish`/`render` pipeline stages.

**Architecture:** New package `src/nagare_clip/publish/tools/` loads the director's display view (`director.tools.project.load_project`), maps display lines to source spans and finished-timeline seconds (existing `publish.timeline`), validates an edit file strictly (reusing render's allowlists), and writes `publish.json`/`publish.md` in today's shape, then calls the unchanged `render.run.run_render`. Steps 1–7 add the tools beside the existing stage; step 8 is a user trial; steps 9–10 delete.

**Tech Stack:** Python 3 / uv, pytest, argparse, existing whisperx Docker batch (`build_snapshot_batch_cmd`), host ImageMagick via `run_magick`.

**Spec:** `docs/superpowers/specs/2026-11-21-publish-skill-design.md`

## Global Constraints

- No module under `src/nagare_clip/publish/` or `src/nagare_clip/render/` may import `nagare_clip.llm_client` once Task 9 lands; `publish/tools/` never does, from Task 1.
- Line numbers in every tool and in the edit file are the director's display numbering (`DisplayView`).
- The tool computes every second; the edit carries none.
- Title: ≤ 100 characters, no `<` or `>`. Assembled description: ≤ 5000 characters.
- A chapter shorter than `publish.min_chapter_duration` (default `10.0`) is a **problem**, not merged.
- Fewer than 3 chapters (including 0) is a **warning**; `write` still writes.
- `background` is required per thumbnail set; `role` is optional (absent → `hook`); no line-count cap.
- Every `magick` call stays an argv list (`run_magick`); every still from one `frames` call goes through ONE `docker compose run`.
- `write` reports all problems at once on stderr, exit 1, and writes nothing; tool-usage errors (no project, bad args) are exit 2, as the director tools.
- `make check` passes at the end of every task. Commit straight to `main`.
- TDD: each new test is seen failing first (or, where code exists, against a deliberate mutation) — report the mutation evidence.

## Review Focus

1. **A reordered timeline** (`director/order.json` not shooting order): chapter times must come from the manifest's playback order, and chapters given out of display order must still sort by finished time — Task 3 test `test_chapters_follow_reordered_timeline`.
2. **A chapter on a silence line** (display line that is `is_silence`): it resolves over the silence span, and is a "entirely cut" problem when that silence was dropped (the default) — Task 1 test `test_silence_line_span` and Task 3 test `test_chapter_on_dropped_silence_is_a_problem`.
3. **Stale `edit.json` after a re-edit** (line beyond the view): a problem naming the line, never an IndexError — Task 3 test `test_line_out_of_range_is_a_problem`.
4. **Background given as an absolute path, or containing spaces**: accepted when the file exists, rendered unchanged — Task 3 test `test_absolute_background_accepted`.
5. **Source filenames with spaces/uppercase extensions** in `frames`: the container path is shell-quoted and the source resolved from `discover_sources` — Task 5 test `test_jobs_use_discovered_relative_path`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/nagare_clip/publish/tools/__init__.py`, `__main__.py` | package + `python -m` entry |
| `src/nagare_clip/publish/tools/lines.py` | display line → source span; finished-timeline time of a line |
| `src/nagare_clip/publish/tools/edit.py` | `PublishEdit` parse + full validation → problems/warnings |
| `src/nagare_clip/publish/tools/build.py` | edit + timeline + stills → `publish.json` dict |
| `src/nagare_clip/publish/document.py` | `empty_publish()`, `render_publish_markdown()` (moved out of `run.py`) |
| `src/nagare_clip/publish/tools/frames.py` | shortlist + `--line` jobs, one Docker batch, `stills.json` |
| `src/nagare_clip/publish/tools/cli.py` | `view`, `frames`, `write`, `render` subcommands |
| `scripts/publish_tools.sh` | shim, like `director_tools.sh` |
| `src/nagare_clip/render/thumbnail.py` | + `line_style_problems`/`set_style_problems`, height-overflow warning |
| `skills/.apm/skills/nagare-publish/` | `SKILL.md` + `references/` |

Spec addition (made in Task 5): `frames` persists the extracted list to `output/publish/stills.json` (`[{stem, source_time, kind, label, line, path}]`) so `write` can fill `publish.json`'s `thumbnails` without re-extracting. This is not the deleted `frames.json` (no descriptions).

---

### Task 1: Line spans and finished times

**Files:**
- Create: `src/nagare_clip/publish/tools/__init__.py` (empty), `src/nagare_clip/publish/tools/lines.py`
- Modify: `src/nagare_clip/pipeline/stages.py` — rename `_ordered_sources` → public `ordered_sources_for(ctx)` (both call sites)
- Test: `tests/publish/tools/__init__.py`, `tests/publish/tools/test_lines.py`

**Interfaces:**
- Produces:
  - `LineSpan(stem: str, start: float, end: float)` frozen dataclass
  - `line_span(project: Project, number: int) -> LineSpan | None` — speech line: its `seg_times` entry of the line's segment transcript (`project.transcripts[line.segment - 1]`, index `source_line - first_line`); silence line after `n`: `(end(n), start(n+1))`, leading (`source_line == 0`): `(0.0, start(1))`; `None` when out of range or untimed.
  - `line_time(placements: Sequence[Placement], project: Project, number: int) -> float | None` — `first_surviving_time` over `line_span`.
  - `finished_placements(project: Project) -> list[Placement] | None` — `build_placements(ordered_sources_for(project.ctx))`, `None` when no `{stem}_intervals.json` exists for any source.
  - `pipeline.stages.ordered_sources_for(ctx: PipelineContext) -> list[tuple[str, dict]]`.

- [ ] **Step 1: Write failing tests** in `test_lines.py`, building a `Project` from a hand-made `SegmentTranscript` (as `tests/test_apm_skill.py::test_skill_md_edit_example_passes_write_validation` does) with `seg_times=[(1.0,2.0),(5.0,6.0)]` and `silence_lines` such that display has `1` speech, `2` silence, `3` speech:
  - `test_speech_line_span`: `line_span(p, 1) == LineSpan("a", 1.0, 2.0)`
  - `test_silence_line_span`: `line_span(p, 2) == LineSpan("a", 2.0, 5.0)`
  - `test_out_of_range_is_none`: `line_span(p, 99) is None`
  - `test_line_time_uses_first_surviving_moment`: placements `[Placement("a", 1.5, 2.0, 0.0, 0.5, 1.0), Placement("a", 5.0, 6.0, 0.5, 1.5, 1.0)]` → `line_time(pl, p, 1) == 0.0`, `line_time(pl, p, 3) == 0.5`, `line_time(pl, p, 2) is None`
- [ ] **Step 2:** `uv run pytest tests/publish/tools/test_lines.py -v` → FAIL (module not found)
- [ ] **Step 3:** Implement `lines.py`; rename `_ordered_sources` in `stages.py`.
- [ ] **Step 4:** `uv run pytest tests/publish tests/pipeline tests/cut_report -q` → PASS
- [ ] **Step 5:** Commit `feat(publish-tools): display line -> source span -> finished time`

### Task 2: Strict style checks and the height warning in render

**Files:**
- Modify: `src/nagare_clip/render/thumbnail.py`, `src/nagare_clip/render/run.py`
- Test: `tests/render/test_thumbnail.py`, `tests/render/test_run.py`

**Interfaces:**
- Produces:
  - `line_style_problems(raw: Mapping[str, Any], fonts: Mapping[str, str]) -> list[str]` — one message per unknown key (outside `LINE_KEYS + ("role", "text")`), per value the `resolve_line_style` validators would reject (`_color`, `_int_in` with `MIN_POINTSIZE..MAX_POINTSIZE`, `0..MAX_STROKEWIDTH`), a `font` not in `fonts`, a `role` not in `VALID_ROLES`.
  - `set_style_problems(raw: Mapping[str, Any]) -> list[str]` — same for `SET_KEYS + ("lines", "background")`, `GRAVITIES`, `_OFFSET_RE`, shadow (`bool`, or mapping with `color`/`blur` checked by `_color`/`_BLUR_RE`).
  - `RenderResult.warnings: list[tuple[int, str]]` (set index, message); `render.json` gains `"warnings": [{"set", "message"}]`; `render.md` prints `**Warning:** <message>` under the set.
- Height rule: after `layout_lines`, if the stacked block height (sum of placed heights + gaps + `abs(y0)`) exceeds the canvas height, add the warning `text block is {h}px tall; canvas is {H}px`.

- [ ] **Step 1: Failing tests**
  - `test_line_style_problems_reports_each_bad_key`: `line_style_problems({"fill": "nope", "pointsize": 9999, "font": "x", "glow": 1}, {"sans": "Noto"})` has 4 entries mentioning `fill`, `pointsize`, `font`, `glow`
  - `test_line_style_problems_accepts_valid`: `{"role": "tag", "text": "t", "fill": "#FFCC00", "pointsize": 120, "font": "sans"}` → `[]`
  - `test_set_style_problems`: `{"gravity": "middle", "offset": "10,10"}` → 2 entries; `{"gravity": "south", "offset": "+0+40", "shadow": {"blur": "0x6"}}` → `[]`
  - `test_role_optional_uses_hook_preset`: `resolve_line_style({}, "", {}, preset_for(1)).pointsize == preset_for(1).lines["hook"].pointsize`
  - `test_five_lines_all_rendered`: `render_sets` with a fake `run` and a 5-line set → the render argv contains all five texts
  - `test_height_overflow_warning`: fake metrics 5 × 300px on a 720px canvas → `result.warnings == [(1, ...)]` and `render.json["warnings"]` non-empty
- [ ] **Step 2:** run them → FAIL
- [ ] **Step 3:** Implement. Refactor the per-key validators so `resolve_*` and `*_problems` share them (one predicate per key, no second copy of the rules).
- [ ] **Step 4:** `uv run pytest tests/render -q` → PASS
- [ ] **Step 5:** Commit `feat(render): strict style checks for the publish tool; warn on a block taller than the canvas`

### Task 3: Edit parsing and validation

**Files:**
- Create: `src/nagare_clip/publish/tools/edit.py`
- Test: `tests/publish/tools/test_edit.py`

**Interfaces:**
- Consumes: Task 1 `line_span`, `line_time`; Task 2 `line_style_problems`, `set_style_problems`.
- Produces:
  - `ChapterRef(line: int, title: str)`, `PublishEdit(titles: list[str], lead: str, chapters: list[ChapterRef], thumbnails: list[dict])` (thumbnails kept as validated raw dicts — render's contract shape)
  - `ResolvedChapter(line: int, time: float, title: str)`
  - `validate_edit(data: Any, project: Project, placements: list[Placement] | None, *, fonts: Mapping[str, str], publish_dir: Path, min_chapter: float) -> tuple[PublishEdit | None, list[ResolvedChapter], list[str], list[str]]` → `(edit, chapters sorted by time, problems, warnings)`. Pure: reads only the filesystem for `background` existence.
- Rules: exactly the spec's "Problems"/"Warnings" lists. Top-level keys allowed: `titles`, `lead`, `chapters`, `thumbnails`. Chapter keys: `line`, `title` (non-empty). Chapter time is `line_time`; the first resolved chapter's span is measured from `0.0` (`chapters._spans`); "two at one time" = equal `time`. Description length is measured on `lead + "\n\n" + render_chapter_lines(...)` exactly as `build.py` assembles it (call the same helper — define `assemble_description(lead, chapters) -> str` here and have Task 4 use it).
- Problem strings name their location: `titles[1]: …`, `chapters[0] (line 40): …`, `thumbnails[2].lines[0]: …`.

- [ ] **Step 1: Failing tests** (fixture: the Task 1 project + placements; `tmp_path` publish dir with `frames/a/1.000.jpg`):
  - `test_valid_edit_has_no_problems` (also asserts `ResolvedChapter` times)
  - `test_all_problems_reported_at_once`: an edit with an empty `titles`, a 101-char title, a bad gravity and a missing background → ≥ 4 problems in one call
  - `test_title_with_angle_bracket`
  - `test_description_over_5000`
  - `test_line_out_of_range_is_a_problem` (Review Focus 3)
  - `test_chapter_on_dropped_silence_is_a_problem` (Review Focus 2)
  - `test_two_chapters_same_time`
  - `test_short_chapter_is_a_problem`: `min_chapter=10.0`, chapters 9s apart → problem containing `10`
  - `test_chapters_need_timeline`: `placements=None` with a chapter → problem; with `chapters: []` → none
  - `test_fewer_than_three_chapters_warns`: 2 chapters → `warnings` non-empty, `problems == []`
  - `test_chapters_follow_reordered_timeline` (Review Focus 1): placements putting line 3's span before line 1's → resolved order `[3, 1]`
  - `test_background_required_and_must_exist`
  - `test_absolute_background_accepted` (Review Focus 4: path with a space)
  - `test_unknown_top_level_key`
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement `edit.py`.
- [ ] **Step 4:** `uv run pytest tests/publish/tools -q` → PASS; mutate (e.g. drop the `<`/`>` check, change `>` to `>=` on 100) and confirm the matching test fails; revert.
- [ ] **Step 5:** Commit `feat(publish-tools): validate a publish edit, every problem at once`

### Task 4: Building publish.json and publish.md

**Files:**
- Create: `src/nagare_clip/publish/document.py`, `src/nagare_clip/publish/tools/build.py`
- Modify: `src/nagare_clip/publish/run.py` (import `empty_publish`/markdown from `document.py`; no behaviour change)
- Test: `tests/publish/tools/test_build.py`; existing `tests/publish/test_run.py` must stay green

**Interfaces:**
- Consumes: Task 3 `PublishEdit`, `ResolvedChapter`, `assemble_description`; `chapters.chapter_timestamps`, `chapter_issues`, `total_duration`.
- Produces:
  - `document.empty_publish() -> dict`, `document.render_publish_markdown(data: dict, markup: str) -> str` (today's enabled branch of `_render_markdown`; the disabled branch stays in `run.py` until Task 9)
  - `build.build_publish(edit: PublishEdit, chapters: list[ResolvedChapter], placements: list[Placement] | None, stills: list[dict]) -> dict` — today's `publish.json` keys exactly: `titles, lead, description, chapters[{time, timestamp, title}], chapters_qualify, chapter_issues, thumbnail_copy (= edit.thumbnails), thumbnails (= stills with timeline_time)`.

- [ ] **Step 1: Failing tests**
  - `test_build_shape_matches_today`: keys equal `set(empty_publish())`
  - `test_first_chapter_forced_to_zero`: first resolved at 3.2s → `timestamp == "0:00"`, `time == 3.2`
  - `test_chapter_issues_when_fewer_than_three`: `chapters_qualify is False`, an issue mentions `3`
  - `test_thumbnail_copy_round_trips_through_render`: `render.thumbnail.sets_from_dict(build_publish(...))` yields the edit's lines and background
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Move the markdown/empty helpers to `document.py`; implement `build_publish`.
- [ ] **Step 4:** `uv run pytest tests/publish tests/render -q` → PASS
- [ ] **Step 5:** Commit `feat(publish-tools): build publish.json from an edit`

### Task 5: Frames

**Files:**
- Create: `src/nagare_clip/publish/tools/frames.py`
- Modify: spec — add the `stills.json` sentence from "File Structure" above to the Tools section
- Test: `tests/publish/tools/test_frames.py`

**Interfaces:**
- Consumes: Task 1 `line_span`, `line_time`; `thumbs.select_candidates`, `cap_candidates`, `frame_relpath`; `director.files.read_ops`; `pipeline.sources.discover_sources`; `pipeline.external.build_snapshot_batch_cmd`, `run_command`; `pipeline.cli.PROJECT_ROOT`.
- Produces:
  - `Still(stem: str, source_time: float, kind: str, label: str, line: int | None, path: str)` (`kind` is a shortlist kind or `"line"`)
  - `plan_stills(project: Project, cfg: dict, requests: list[tuple[int, float]]) -> tuple[list[Still], list[str]]` — no requests → shortlist (per source: `select_candidates(stem, ops, seg_times)`, then `cap_candidates(..., cfg["publish"]["max_frames"])`); with requests `(line, pos)` → `start + pos*(end-start)` of `line_span`; returns problems for an unknown line or `pos` outside `[0, 1]`. Each still's `line` is the display line containing its time (`from_source`), `None` if none.
  - `snapshot_jobs(stills, relative_by_stem: Mapping[str, str]) -> list[tuple[str, float, str]]` — `(relative, time, "/output/publish/" + path)`
  - `extract_stills(project: Project, stills: list[Still], *, run=run_command) -> list[Still]` — one batch; Docker failure → `PipelineError` with `_failure_reason`-style text (move `_failure_reason` to `pipeline/external.py` as public `failure_reason` and import it in `stages.py`); a still whose file is missing afterwards is dropped with a warning.
  - `write_stills(path: Path, stills: list[Still], placements) -> None` (adds `timeline_time`), `read_stills(path: Path) -> list[dict]` (`[]` when missing)
  - `format_stills(stills, placements) -> str` — one row `line  M:SS|—  kind  label  path`, in playback order (by timeline time, untimed last).

- [ ] **Step 1: Failing tests**
  - `test_shortlist_from_director_ops`: an overlay op on source line 1 → one still, kind `overlay`, at the line midpoint
  - `test_line_requests_add_stills`: `[(3, 0.0)]` → time = span start
  - `test_bad_pos_is_a_problem`
  - `test_jobs_use_discovered_relative_path` (Review Focus 5): `relative_by_stem={"My Clip": "My Clip.MP4"}` → job relative `"My Clip.MP4"`, and `build_snapshot_batch_cmd(...).script` contains `'My Clip.MP4'` quoted
  - `test_one_docker_call_for_all_stills`: fake `run` counts calls == 1 for 3 stills
  - `test_docker_failure_raises_pipeline_error`
  - `test_missing_output_dropped`
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/publish tests/pipeline -q` → PASS
- [ ] **Step 5:** Commit `feat(publish-tools): extract the shortlist and any line's still in one container`

### Task 6: The CLI and the shim

**Files:**
- Create: `src/nagare_clip/publish/tools/cli.py`, `src/nagare_clip/publish/tools/__main__.py`, `scripts/publish_tools.sh` (copy of `director_tools.sh`'s shape, module `nagare_clip.publish.tools`)
- Test: `tests/publish/tools/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1–5; `director.tools.project.load_project`; `config.get_effective_config`; `brief.format_brief`; `director.context.project_context_block`; `director.files.read_plan`; `director.ops.collect_overlay_texts`; `render.run.run_render`; `pipeline.external.run_magick`.
- Produces: `build_parser() -> argparse.ArgumentParser` with subcommands `view`, `frames`, `write`, `render`; `main(argv: list[str] | None = None) -> int`.
- Behaviour:
  - every subcommand: `--config` repeatable; `PipelineError` → stderr, exit 2
  - `view [--lines A-B]`: brief, project context, `plan.md`, captions in playback order, finished runtime (`total_duration`, or `unknown — no intervals yet`), then `N  M:SS  text` rows (`✂` when `line_time` is `None` with a timeline; `—` without one). Silence lines show the director's silence body.
  - `frames [--line N[@pos]]...`: `plan_stills` problems → exit 1; else extract, `write_stills(publish_dir/"stills.json")`, print `format_stills`.
  - `write --edit F|- [--no-render]`: `validate_edit`; problems → all on stderr, exit 1, nothing written; warnings → stderr; else write `edit.json` (the input verbatim, pretty), `publish.json` (`build_publish` with `read_stills`), `publish.md`, then `run_render(publish_json, render_dir/"render.json", cfg, run=run_magick, markdown=render_dir/"render.md")` unless `--no-render`; print the written paths.
  - `render`: `run_render` as above.

- [ ] **Step 1: Failing tests** (project fixture written to `tmp_path` as text_filter/sentence_split/intervals output; `monkeypatch.chdir`; fake `run_magick`/`run_command` via monkeypatch):
  - `test_view_prints_times_and_cut_marker`
  - `test_view_without_timeline_prints_dash`
  - `test_write_problems_exit_1_writes_nothing`
  - `test_write_writes_edit_publish_md_and_renders` (asserts `edit.json` equals input, `publish.json` keys, render called)
  - `test_write_no_render`
  - `test_frames_writes_stills_json`
  - `test_no_project_exit_2`
  - `test_shim_exists_and_is_executable`
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `make check` → PASS
- [ ] **Step 5:** Commit `feat(publish-tools): view, frames, write and render commands`

### Task 7: The nagare-publish skill

**Files:**
- Create: `skills/.apm/skills/nagare-publish/SKILL.md`, `references/shortlist.md`, `references/thumbnail-craft.md`, `references/youtube-chapters.md`
- Modify: `tests/test_apm_skill.py`, `skills/apm.yml` only if its description lists skills

**Content (SKILL.md):** frontmatter `name: nagare-publish`, ASCII description ≤ 1024 chars saying when to use it (after the director's edit has been rendered to a `.blend`; titles, description, chapters, thumbnails). Sections: where to run (video directory, the same `--config` files, the checkout path as in nagare-director); workflow — `view` → write titles, lead, chapters and thumbnail **text** first → `frames` → Read every still → `frames --line` for more → background + look per set → `write` → Read each `output/render/thumbnails/set{N}.jpg` → revise; `## Edit format` with a ```json example; the enforced rules (spec Problems/Warnings); revising later from `output/publish/edit.json`; limits — propose a look the tool cannot express (e.g. a colour change mid-line) to the user rather than approximating. References carry the spec's shortlist, craft and chapter material.

**Tests (in `tests/test_apm_skill.py`):**
- `test_package_manifest`: `== ["nagare-director", "nagare-publish"]`
- parametrise `test_skill_names_only_real_tool_commands` over `(skill dir, shim name, parser)` pairs: `publish_tools\.sh"? (\w+)` ⊆ `publish.tools.cli.build_parser()` commands
- `test_publish_skill_edit_example_passes_validation`: parse the `## Edit format` block; `validate_edit` against a fixture project with ≥ the example's line numbers, placements covering them, `tmp_path` holding the example's background path, `fonts` containing its font slots → `problems == []`

- [ ] **Step 1:** write the tests → FAIL
- [ ] **Step 2:** write the skill files
- [ ] **Step 3:** `uv run pytest tests/test_apm_skill.py -q` → PASS; `make apm` if the apm CLI is available (else say so)
- [ ] **Step 4:** Commit `feat(skills): nagare-publish — titles, chapters and thumbnails with an agent`

### Task 8: User trial (gate)

- [ ] Hand the user: install command (`apm install <checkout>/skills --target claude,codex` in a video dir) and a one-paragraph "try it" note. **Stop** until they report back. Fix findings as their own small TDD commits before Task 9.

### Task 9: Delete the LLM publish and the publish/render stages

**Files:**
- Delete: `src/nagare_clip/publish/publish_llm.py`, `pairing.py`, `describe_frames.py`, `run.py`; `tests/publish/test_publish_llm.py`, `test_pairing.py`, `test_describe_frames.py`, `test_run.py`, `test_stage_wiring.py`, `tests/render/test_stage_wiring.py` (move any still-relevant assertion, e.g. reorder, into tools tests first — `test_reorder.py` is rewritten against `build_publish`/`line_time`)
- Modify: `pipeline/stages.py` (remove `_director_ops`, `_extract_thumb_frames`, `_publish_run`, `_render_run`, their `Stage` entries and `STAGE_NAMES` entries); `config.py` (`PublishConfig` keeps `min_chapter_duration`, `max_frames`, `frame_width`; delete `DescribeFramesConfig`, `PairingConfig`; `RenderConfig.enabled` removed; `REMOVED_KEYS["publish"]` = the spec's LLM key list + `describe_frames`, `pairing`; `REMOVED_KEYS["render"]` = `{"enabled"}`; section comments rewritten for the tools); `render/run.py`/`thumbnail.py` (drop `enabled` handling); `index_page.py` (rows independent of stages); `MAX_THUMB_LINES` deleted; `config.example.yml` regenerated (`make config-example`)
- Test: `tests/test_config.py`, `tests/pipeline/`, `tests/render/test_run.py`, `tests/test_index_page.py`

- [ ] **Step 1: Failing tests**
  - `test_removed_publish_keys_fail`: each of `publish.enabled`, `publish.model`, `publish.pairing`, `publish.describe_frames`, `render.enabled` fails `get_effective_config` naming the path
  - `test_stage_names_end_at_blender`: `STAGE_NAMES[-1] == "blender"`, neither `publish` nor `render` present
  - `test_from_stage_publish_is_an_error`
  - widen the zero-LLM guard in `tests/render/test_run.py` (AST + fresh interpreter) to every file under `src/nagare_clip/publish/` and `src/nagare_clip/render/`
  - `test_index_page_rows_without_stages`
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** delete and modify as listed
- [ ] **Step 4:** `make check` → PASS; `rg -n "publish_llm|pairing|describe_frames|frames.json" src tests` → only intentional mentions (REMOVED_KEYS messages)
- [ ] **Step 5:** Commit `refactor(publish): delete the LLM publish; publish and render leave the pipeline`

### Task 10: Docs

**Files:** `docs/stages/publish.md` (rewrite: tools, edit format, timing, shortlist, stills.json; drop the three phases), `docs/stages/render.md` (tool entry, no stage, no line cap, warnings), `docs/stages/pipeline.md` (ends at blender), `AGENTS.md` (Objective list, Pipeline Overview publish/render sections, naming list, Project Structure, Configuration System's "All LLM stages" list, Runtime Quirks links), `README.md` (usage), the spec (status line).

- [ ] **Step 1:** edit; `rg -n "publish stage|render stage|--from-stage render|publish\.enabled" docs README.md AGENTS.md` → no stale claims
- [ ] **Step 2:** `make check` → PASS
- [ ] **Step 3:** Commit `docs: publish is an agent skill; the pipeline ends at blender`
