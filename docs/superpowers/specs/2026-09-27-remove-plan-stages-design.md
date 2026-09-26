# Remove the plan and plan_revise stages

Status: spec, 2026-09-27. Step 4 of folding `plan`/`plan_revise` into the
director conversation (steps 1-3: the director decides the order, writes its
own plan, and is a step over its own directory with an editor entry as the
human checkpoint).

## What each stage still did

| stage output | consumer before this change | after |
|---|---|---|
| `plan/plan.json` `directions` | `publish` (context for chapter titles) | `publish` reads `director/plan.md` |
| `plan/plan.json` `order` | the director's seed order | shooting order is the seed |
| `plan_dialogue/history.md` | `plan_revise` | `director/conversation.md` (`## editor`) |
| `plan_revise/plan.json` | the two rows above | — |

Nothing else read them. So both stages, their packages, `plan_dialogue/`,
`scripts/plan_say.sh`, the unanswered-turn guard in `cli.py` and its
`--retire-turns` flag go.

## Changes

- `STAGE_NAMES` loses `plan` and `plan_revise`; `--from-stage plan` is an
  unknown stage like any other.
- `order_from_dict` moves to `order.py` (it only reads the `order` key).
- `_resolve_order`: `director/order.json`, else shooting order.
- `publish`: `format_publish_context(summary, plan=…, overlay_texts=…)` renders
  the director's plan as one block after the parts; `run_publish(plan_md=…)`;
  `PUBLISH_PROMPT` says so.
- Config: the `plan:` and `plan_revise:` sections are removed. A config that
  still has either fails with a message naming them and saying the director
  now writes its own plan (like the removed `thinking` key) — not the generic
  "extra inputs" line.
- `index_page` lists `director/plan.md` and `director/conversation.md` instead
  of `plan_dialogue/history.md`.
- The brief is applied to the stages that remain.

## Tests

Removed with the code: `tests/plan/`, `tests/plan_revise/`, the unanswered-turn
guard tests. Order tests that lived under `tests/plan/` and are about the
order itself (coverage, manifest) already live in `tests/test_order.py` and
`tests/intervals/`; any that are not are moved. New: the removed-section error;
publish renders the plan; the stage passes `director/plan.md`.
