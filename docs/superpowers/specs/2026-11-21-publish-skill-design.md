# publish becomes an agent skill — design

> **Status: implemented** (2026-11-21, through `4a2f9f9` plus the docs and the
> runtime zero-LLM test). Added after this spec, from a real-video trial:
> `view --compact` (the director's played transcript with finished times);
> `view` printing `output dir: …` first, the effective `render.fonts` slots
> (fallback marked) and the captions with their finished times; the `chapters`
> command (the same resolution as `write`); shortlist stills not on the finished
> timeline left out before the `max_frames` cap; `render` deleting stale
> `set<N>.jpg` before compositing; the height-overflow warning; every path
> under `general.output_dir`. Where the code differs from the text below, the
> code and `docs/stages/publish.md` are the truth.

The publish stage's LLM calls are replaced by a coding agent working with the user
through deterministic tools, as the director was. The stage's complexity exists to make
up for models that cannot see the result: the copy call is kept blind so it does not
caption the photographs, a vision call describes each still so a text call can pair copy
to a frame, and `render` falls back per key to a preset for whatever the pairing got
wrong. Nobody ever looks at the composited thumbnail. A coding agent can: it reads the
stills and the rendered `set{N}.jpg` with its own image input and iterates.

## Decisions

1. **The LLM publish is deleted, not kept beside the skill.** The director checkpoint
   already stops every run, so the pipeline has no unattended path left for an LLM
   publish to serve. Deleted: `publish/publish_llm.py`, `publish/pairing.py`,
   `publish/describe_frames.py`, the LLM half of `publish/run.py`, `frames.json`, the
   publish LLM-report recorder.
2. **`publish` and `render` stop being pipeline stages.** The pipeline ends at `blender`
   (`STAGE_NAMES` loses both; `_publish_run`, `_render_run` and the frame-extraction
   adapter leave `pipeline/stages.py`; `--from-stage publish|render` is an unknown-stage
   error). Only the tool writes `publish.json`, so a render stage after `blender` would
   only re-composite the file the tool already rendered, and it would be a second way
   in, which `render.md` rules out.
3. **A separate skill, `nagare-publish`**, in the same APM package
   (`skills/.apm/skills/nagare-publish/`), used in its own session after the director's.
4. **The agent writes `publish.json` through the tool. There is no stage that rebuilds
   it.** When an upstream edit changes the timeline, the file goes stale, as every other
   stage's output does; the agent runs `write` again with its saved edit.
5. **Seconds are always computed by the tool.** The agent names chapter positions by
   display line; `write` resolves them on the finished timeline with the existing
   `timeline.py`/`chapters.py` code. The authority rule is unchanged: after `intervals`,
   `timeline.json` is the authority.
6. **Chapters are chosen freely by the agent**, by line, not tied to `summary.json` parts.
7. **Frames: the director-ops shortlist stays, and any line can be added.**

## Tools — `src/nagare_clip/publish/tools/`, no LLM call

`scripts/publish_tools.sh` → `python -m nagare_clip.publish.tools`, run in the video
directory, `--config` repeatable and deep-merged like the director tools. Line numbers
are the director's **display numbering** (`director.display.DisplayView`, loaded through
`director.tools.project.load_project`).

| command | does | writes |
| --- | --- | --- |
| `view [--lines A-B]` | brief, summary (overall + per video), `director/plan.md`, the director's captions in playback order, the finished runtime, then one row per display line: `N  M:SS  text`, `✂` for a line that does not survive, `—` for the time when the timeline is unavailable | nothing |
| `frames [--line N[@pos]]...` | with no `--line`: the shortlist (`thumbs.select_candidates` + `cap_candidates`, `publish.max_frames`). With `--line`: stills at those lines (`pos` ∈ [0,1] within the line's span, default 0.5). Every still from one call goes through one `docker compose run` (`build_snapshot_batch_cmd`). Lists each still with why it was chosen (kind + the director's label), display line, finished time and path, in playback order; persists the list to `output/publish/stills.json` (not a frames.json; no descriptions) | `output/publish/frames/{stem}/{t:.3f}.jpg`, `output/publish/stills.json` |
| `write --edit F\|- [--no-render]` | validates; any problem → all of them on stderr, exit 1, nothing written. Else resolves chapters, writes `edit.json`, `publish.json`, `publish.md`, then runs the render unless `--no-render` | `output/publish/`, `output/render/` |
| `render` | `render.run.run_render()` as it is today, for a hand-edited `publish.json` | `output/render/` |

There is no `dump`. The director needs one because its files hold per-source coordinates.
`publish.json` holds seconds, and seconds cannot be mapped back to lines without losing
information. Instead `write` saves the validated edit verbatim as `output/publish/edit.json`,
and a later session revises that file.

`frames` failure modes: if Docker itself fails, the command exits 1 with the exception and
the stderr tail (the information `PipelineError` carries today). A still that a batch which
otherwise ran did not write is dropped from the list with a warning.

When `timeline.json`/intervals are missing (blender not run yet), `view` and `frames`
work with `—` times, and `write` reports every chapter as a problem. With zero chapters,
`write` still writes.

## The edit file

```json
{
  "titles": ["まさかの水漏れ…ポンプ交換3日目", "..."],
  "lead": "Description body; the tool appends the chapter list",
  "chapters": [
    {"line": 1,  "title": "オープニング"},
    {"line": 40, "title": "分解して原因を探す"}
  ],
  "thumbnails": [
    {
      "background": "frames/a/63.200.jpg",
      "gravity": "south", "offset": "+0+40", "shadow": {"blur": "0x6"},
      "lines": [
        {"role": "tag",  "text": "3日目", "fill": "#FFCC00", "font": "sans-bold"},
        {"text": "まさかの水漏れ", "pointsize": 140, "stroke": "black", "strokewidth": 8}
      ]
    }
  ]
}
```

- `titles`: candidates, at least one; the human picks.
- `description` = `lead` + the chapter lines, as today.
- A chapter's time is `first_surviving_time` over its line's span; the chapters are
  sorted by finished time and the first is forced to `0:00` (today's `chapter_timestamps`).
- A thumbnail set has today's `thumbnail_copy` shape, with three changes. `background` is
  **required**. `role` is **optional** (absent → `hook`; it only picks the preset defaults
  for unstated keys). The line count is **unlimited**: `MAX_THUMB_LINES` was a cap on the
  copy call, and `layout_lines()` already stacks any number of lines.

### Problems (`write` writes nothing)

- structure: an unknown key, a wrong type, an empty `titles`
- title: over 100 characters, or containing `<` or `>` (YouTube rejects both)
- description: the assembled description is over 5000 characters
- chapter: a line that does not exist; a line that is entirely cut; two chapters at one
  time; a chapter shorter than `publish.min_chapter_duration` (today merged silently; the
  agent now re-chooses); any chapter while the timeline is unavailable
- thumbnail: no lines; empty text; an unknown role; an unknown style key; a style value
  outside render's allowlist (reported, not replaced with the preset value); a `font`
  that is not a `render.fonts` slot; a `background` that does not exist (relative to
  `output/publish/`, or absolute)

### Warnings (written anyway)

- fewer than three chapters (YouTube links them but draws no segmented bar), including zero

## render

`render/` stays, with its zero-LLM guarantee (static import check + fresh-interpreter
run), now reached only through the tool. Changes: no line cap, `role` optional, and a
**height-overflow warning** in `render.md`/`render.json` when the measured block exceeds
the canvas (width is already shrunk to fit; height was unchecked). `render.enabled` is
removed (`REMOVED_KEYS`): calling the tool is the decision to render.

## Config

- `publish:` keeps `min_chapter_duration`, `max_frames` and `frame_width`. Every LLM key
  (`enabled`, `provider`, `api_base`, `model`, `api_key`, `temperature`,
  `reasoning_effort`, `timeout`, `response_format`, `max_retries`, `retry_temp_step`,
  `retry_temp_cap`, `prompt`) and the `pairing:`/`describe_frames:` subsections go into
  `config.REMOVED_KEYS`, so loading fails and names every offending path.
- `render:` is unchanged except for `enabled`.
- `config.example.yml` is regenerated.

## The skill — `skills/.apm/skills/nagare-publish/`

`SKILL.md` holds the workflow: `view` → write the copy (titles, lead, chapters, thumbnail
text) **before** looking at frames → `frames` → Read the stills → choose a background and
look per set → `write` → Read the renders → revise. It also documents the tools, the edit
format and the enforced rules.

References:
- **the shortlist**: what each kind is and why. An overlay is taken mid-line, because a
  line's first frame is often still the previous shot. A keep is taken in the middle of
  the event it rescued. A timelapse gets both boundaries, for a before/after. The cap is
  `max_frames`. The shortlist is where to look, not an answer: add lines with `--line`.
  The label is the director's claim; Read the image and trust what is actually legible.
  This carries over, as a step, what `describe_frames` enforced by hiding the label.
- **thumbnail craft**: the style vocabulary, legibility against the background, placing
  text where the picture is empty.
- **YouTube chapter rules**: the four conditions and how `write` enforces them.
- **limits**: if the agent wants a look the tool cannot express (e.g. a colour change in
  the middle of a line), it proposes it to the user instead of settling for an
  approximation.

Writing the copy before looking keeps the copy-before-frames order that the blind copy
call guaranteed by code; it is now a step in the workflow, not a test.

## `output/index.md`

Rows for `publish.md`, `render.md` and the thumbnails stay. A file that is absent shows
`—`, as today. `index_page.py` must not depend on publish/render being stages.

## Testing (TDD; each new test seen failing against a mutated implementation)

- `tests/publish/tools/`:
  - validation: one case per problem kind, and all problems reported at once
  - chapter resolution: reorder, a cut line, `0:00`, the short-chapter problem
  - `write` end to end: `edit.json`, `publish.json`, `publish.md`, the render call
  - `frames`: the batch command for shortlist plus `--line`, built as one Docker call
  - `view` output, with and without a timeline
- render: an unlimited line count, a missing `role`, the height-overflow warning
- removals: every `REMOVED_KEYS` path fails loading; `STAGE_NAMES` has neither stage;
  `--from-stage publish` is an error
- the zero-LLM guard widened to all of `publish/` (tools included)
- `tests/test_apm_skill.py`: two skills, each with name, frontmatter and the tool
  commands it names
- `index_page`: rows still appear

## Docs

`docs/stages/publish.md` is rewritten (tools, edit format, timing, shortlist; the
three-phase material goes). `docs/stages/render.md` changes: no longer a stage, the tool
entry point, no line cap. `docs/stages/pipeline.md`: the pipeline ends at blender.
`AGENTS.md` and `README.md` change throughout: overview, stage list, project structure,
config notes, and the "all LLM stages" list.

## Order of work

1. Tools (`view`, `frames`, `write`, `render`) + validation, beside the existing stage
2. The skill
3. **The user tries one real video** (Docker, ImageMagick and fonts are on their machine)
4. Delete the LLM publish and both stages; config; docs

`make check` passes after every step.

## Out of scope / later

- **Runs**: a colour change within a line
  (`"text": [{"text": "まさかの"}, {"text": "水漏れ", "fill": "#FF3030"}]`), limited to
  `fill`/`stroke`/`strokewidth` per run, laid out by converting every run to absolute
  northwest coordinates. Its own spec, once real use shows what is wanted.
- **Raw `magick` commands** in `publish.json`: it would lose the validation, the escaping
  and the "the file shows the look" property. Not now.
- Uploading stays manual.
