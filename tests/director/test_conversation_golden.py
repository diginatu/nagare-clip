"""What the director is actually sent, and what it finally produces — in full.

Every other director test asserts a fragment.  This one pins the WHOLE thing
against a file a person can read top to bottom: ``golden/conversation.txt``
holds every message of every call (the cached system message once, since it
never changes, then each turn's history and live ask), followed by the end
result — every file of the director's directory (``_director.json``,
``order.json``, ``plan.md``, ``conversation.md``) and the order resolved to
source seconds, which is where a mistake is hardest to notice.

Three runs of the stage over one directory: the first edits the video to a
done mark; a person then deletes the mark and adds an editor entry
(``director_say``), and the second run answers it; the third run finds the
done mark again and makes no call at all.

The project is small but has every moving part: two sources, a summary, the
director's own planning turn reordering them, a long silence with a gap description, a
cut, a timelapse over a silence line, a caption, a revised plan, and an order
refused because it would split the timelapse.  The model is a script; everything else
is the real stage.

After an intended change, regenerate and READ the diff:

    UPDATE_GOLDEN=1 uv run pytest tests/director/test_conversation_golden.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.config import get_effective_config
from nagare_clip.director import conversation
from nagare_clip.director import director_llm as dl
from nagare_clip.intervals.manifest import build_manifest
from nagare_clip.order import manifest_to_dict
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia

GOLDEN = Path(__file__).parent / "golden" / "conversation.txt"

TEXT = {
    "dev": [
        "こんにちは、今日はポンプを直します",
        "まず道具を並べます",
        "これがインパクトドライバー",
        "じゃあ始めましょう",
    ],
    "mix": [
        "ポンプを外していきます",
        "ネジが固いですね",
        "よし外れた",
        "ここにホースをつなぎます",
        "ここからしばらく作業します",
        "終わりました",
        "水を入れてみます",
        "動いた！",
        "今日はここまで、ありがとうございました",
    ],
}
TIMES = {
    "dev": [(0.0, 2.0), (3.0, 7.0), (8.0, 9.5), (10.0, 13.0)],
    "mix": [
        (0.0, 4.0),
        (10.0, 12.0),
        (12.5, 15.0),
        (16.0, 18.0),
        (19.0, 29.0),
        (60.0, 61.0),
        (62.0, 65.0),
        (66.0, 68.0),
        (69.0, 71.0),
    ],
}
DURATIONS = {"dev": 14.0, "mix": 72.0}

# The display numbering these replies address (shooting order):
#   dev 1-4; mix 5, 6 = the silence after mix 1, 7-10 = mix 2-5,
#   11 = the 31 s silence after mix 5 (described), 12-15 = mix 6-9.
REPLIES = [
    # Turn 1, the planning turn: the plan, and an order replacing shooting
    # order: the result first, then dev, then mix up to and INCLUDING the long
    # silence (11 ends a range).
    {
        "plan": "ポンプ修理の一本。結果（動いた！）を冒頭に見せてから経緯を追う。"
        "道具紹介(2-3)は削る。配管作業(10-11)は速回し。",
        "order": [[12, 15], [1, 4], [5, 11]],
    },
    # Turn 2: a cut.
    {
        "range": [1, 8],
        "reviewed_through": 8,
        "ops": [{"type": "cut", "lines": [2, 3], "note": "道具紹介は冗長"}],
    },
    # Turn 3: a timelapse over the work and its silence line, a caption, and
    # the plan restated now that the caption is decided.
    {
        "range": [9, 15],
        "reviewed_through": 15,
        "plan": "ポンプ修理の一本。結果を冒頭に見せてから経緯を追う。道具紹介は削る。"
        "配管作業は速回し、「動いた！」にキャプション。",
        "ops": [
            {
                "type": "timelapse",
                "lines": [10, 11],
                "factor": 8.0,
                "text": "配管の締め直し",
                "note": "",
            },
            {"type": "overlay", "lines": [14, 14], "text": "動いた！", "duration": 2.0, "note": ""},
        ],
    },
    # Turn 4: an order whose break after line 10 would split that timelapse.
    {"order": [[11, 15], [1, 10]]},
    # Turn 5: finished.
    {"done": True},
]

EDITOR_NOTE = "冒頭のあいさつ（2-3行目）は残して。"

# Run 2 answers the editor: the cut is withdrawn by re-sending its range empty.
RERUN_REPLIES = [
    {"range": [1, 8], "reviewed_through": 8, "ops": []},
    {"done": True},
]


def _project(root: Path) -> PipelineContext:
    out = root / "out"
    (root / "in").mkdir()
    for sub in ("text_filter", "sentence_split", "audio_silence", "gap_context", "summary"):
        (out / sub).mkdir(parents=True)
    for stem, times in TIMES.items():
        (root / "in" / f"{stem}.mp4").touch()
        lines = TEXT[stem]
        (out / "text_filter" / f"{stem}_edits.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        segments = [
            {
                "start": s,
                "end": e,
                "text": t,
                # One word per character, spread evenly: speech fills the line.
                "words": [
                    {
                        "word": ch,
                        "start": round(s + (e - s) * k / len(t), 3),
                        "end": round(s + (e - s) * (k + 1) / len(t), 3),
                    }
                    for k, ch in enumerate(t)
                ],
            }
            for (s, e), t in zip(times, lines)
        ]
        (out / "sentence_split" / f"{stem}.json").write_text(
            json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8"
        )
    write_cuts(out / "audio_silence" / "mix_cuts.txt", [(29.5, 59.5)])
    (out / "gap_context" / "mix_gaps.json").write_text(
        json.dumps(
            {
                "gaps": [
                    {
                        "start": 29.0,
                        "end": 60.0,
                        "frames": [],
                        "description": "ACTION: 配管を締め直している",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out / "summary" / "summary.json").write_text(
        json.dumps(
            {
                "summary": "ポンプの修理",
                "parts": [{"stem": "mix", "lines": [1, 9], "summary": "修理作業"}],
                "video_summaries": {"mix": "ポンプを外して直す"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = get_effective_config(None, {})
    cfg["director"].update({"enabled": True, "max_retries": 0, "chunk_lines": 8})
    return PipelineContext(
        cfg=cfg,
        project_root=root,
        config_path=None,
        input_videos_dir=root / "in",
        output_dir=out,
        sources=[
            SourceMedia(abs_path=root / f"{s}.mp4", stem=s, relative=f"{s}.mp4") for s in TIMES
        ],
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )


def _render(runs: list[list[list[dict]]], ctx: PipelineContext) -> str:
    out: list[str] = []
    calls = [call for run in runs for call in run]
    system = calls[0][0]["content"]
    assert all(call[0]["content"] == system for call in calls), "the system message varied"
    out += ["################ SYSTEM (identical on every call, cached) ################", system]
    n = 0
    for r, run in enumerate(runs, start=1):
        out.append(f"################ RUN {r}: {len(run)} call(s) ################")
        for call in run:
            n += 1
            out.append(f"################ CALL {n}: after the system message ################")
            for message in call[1:]:
                out += [f"===== [{message['role']}] =====", message["content"]]
    out.append("################ RESULT ################")
    director = ctx.stage_dir("director")
    for path in sorted(director.iterdir()):
        out += [f"===== director/{path.name} =====", path.read_text(encoding="utf-8").rstrip()]
    # The order in seconds, as intervals will write timeline.json.
    segments = st._timeline_segments(ctx)
    manifest = build_manifest(
        segments,
        {s: TIMES[s] for s in TIMES},
        DURATIONS,
        pre_margin=float(ctx.cfg["intervals"]["keep_pre_margin"]),
    )
    out += [
        "===== intervals/timeline.json (resolved from the above) =====",
        json.dumps(manifest_to_dict(manifest), ensure_ascii=False, indent=2),
    ]
    return "\n\n".join(out) + "\n"


def test_the_whole_conversation_and_its_result(tmp_path, monkeypatch):
    ctx = _project(tmp_path)
    director = next(s for s in st.STAGES if s.name == "director")
    runs: list[list[list[dict]]] = []

    def run(replies):
        calls: list[list[dict]] = []

        def fake(messages, cfg):
            calls.append([dict(m) for m in messages])
            return json.dumps(replies[len(calls) - 1], ensure_ascii=False)

        monkeypatch.setattr(dl, "_call_llm", fake)
        director.run(ctx)
        runs.append(calls)

    run(REPLIES)
    conversation.say(ctx.stage_dir("director") / conversation.FILE_NAME, EDITOR_NOTE)
    run(RERUN_REPLIES)
    run([])
    assert [len(r) for r in runs] == [len(REPLIES), len(RERUN_REPLIES), 0]

    text = _render(runs, ctx)
    assert str(tmp_path) not in text, "a temporary path leaked into the golden file"
    assert f"Editor: {EDITOR_NOTE}" in text

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.is_file(), f"no golden file: run with UPDATE_GOLDEN=1 to create {GOLDEN}"
    assert text == GOLDEN.read_text(encoding="utf-8"), (
        f"the director's conversation changed; if intended, regenerate with "
        f"UPDATE_GOLDEN=1 and read the diff of {GOLDEN.name}"
    )
