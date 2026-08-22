# project brief — runtime notes

The `project:` config section is not a stage: it is a project-wide **editorial
brief** appended to the system prompts of the five LLM stages that make
editorial judgements — `summary`, `plan`, `director`, `text_filter` and
`publish`. It
carries what no transcript can state: who the video is for, how long it should
be, how it should feel, and what happened in a previous episode of a series.

Implemented in [`src/nagare_clip/brief.py`](../../src/nagare_clip/brief.py);
tests in [`tests/test_brief.py`](../../tests/test_brief.py) plus a
brief-injection test in each of those stages' `test_run.py`.

## Config

```yaml
project:
  audience: "DIY hobbyists on YouTube, already familiar with the build"
  purpose: "show whether the siphon overflow drain actually works"
  target_duration: "about 12 minutes"
  tone: "fast, punchy vlog; frequent on-screen captions"
  story_so_far: "the previous episode built the rig; this one tests it"
  previous_summary: "../water_pump_2/video-editor-ai/summary/summary.json"
```

All six fields are free text and default to `""` (`ProjectConfig` in
`config.py`). They are rendered in a fixed order — audience, purpose, target
duration, tone, story so far, previous video — so a brief is deterministic
regardless of YAML key order.

`previous_summary` is a **path to a previous project's `summary.json`**;
`load_previous_summary()` reads its top-level `summary` string and renders it as
`- Previous video (this project continues it): …`. A missing, unreadable, or
summary-less file logs a warning and drops just that line — a stale path never
fails a run, and never blanks the rest of the brief. It is independent of
`story_so_far`: set both and both lines render.

## Rendering and injection

`format_brief(cfg["project"])` returns the block:

```
Editorial brief (applies to the whole project; follow it when deciding what to keep, cut, tighten and emphasise):
- Audience: …
- Tone: …
```

`apply_brief(stage_cfg, cfg, keys=("prompt",))` returns a **copy** of the stage
config with the block appended to each named prompt key, separated by a blank
line. Only those stages' `run.py` call it, so the brief enters through the
already-merged `cfg` dict and no LLM module gained a new parameter:

| Stage | Prompt key(s) briefed | Ordering |
|-------|----------------------|----------|
| `summary` | `prompt`, `overall_prompt` | both the per-video map call and the all-videos reduce call |
| `plan` | `prompt` | — |
| `director` | `prompt` | brief first, then the summary/plan overview block appended by `generate_director_ops` |
| `text_filter` | `prompt` | brief first, then `build_enhanced_prompt`'s per-video summary/keyword context (which stays closest to the transcript) |
| `publish` | `prompt` | — (the audience/tone the title, lead and thumbnail copy are written for) |

## Whole-project instructions on a per-source prompt

The brief is whole-project text appended to a **per-source** prompt, so an
instruction phrased about the *finished* video ("冒頭で…", "最後に…", "一度だけ…")
reaches every source's LLM call independently and each one obeys it from its own
vantage point. A real 7-source run put the same "explain the rig early on" recap
caption on four of the videos, each op individually correct.

Nothing in the brief mechanism prevents that — the fix lives in the `director`
stage's context block, which states the video's position in the finished
timeline (`video 3 of 7`, FIRST/LAST marked, siblings split into earlier/later)
and lists the captions already committed on the earlier videos. So a START/END
instruction is readable as being about one particular video rather than about
each one. Deliberately no rule was added to `director.prompt` saying so: the
position is a fact the model can reason from, and this prompt anchors hard on
whatever examples it carries. `summary`, `plan`, `text_filter` and `publish`
have no equivalent — `summary`/`plan`/`publish` already run once project-wide,
and `text_filter` makes no editorial placement decisions.

`gap_context`, `sentence_split` and `guided_edit` are deliberately **not**
briefed: they are mechanical (describe frames / split sentences / apply an op
verbatim), and editorial framing there is noise at best and a licence to
paraphrase at worst.

## No-brief invariant

When every field is empty (the default), `format_brief` returns `""` and
`apply_brief` returns the **same dict object** it was given — so every prompt is
byte-identical to a run from before this feature existed. Each briefed
stage has a regression test pinning that (`test_no_brief_leaves_*`).
Whitespace-only values count as empty.
