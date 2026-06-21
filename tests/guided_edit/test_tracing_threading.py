"""Tests that trace metadata is threaded through single-call stage functions."""

from nagare_clip.text_filter.llm_filter import _process_batch
from nagare_clip.text_filter.summary_llm import generate_summary
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.llm_report import Recorder


def _capture(store, response):
    def fake(messages, cfg):
        store.append(cfg)
        return response
    return fake


def test_text_filter_batch_threads_trace_meta():
    store = []
    rec = Recorder("text_filter", None, enabled=False)
    result = ["x", "y"]
    _process_batch(
        [(0, "x"), (1, "y")],
        result,
        {"prompt": "p"},
        2,
        None,
        call_llm=_capture(store, "1: x\n2: y"),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["tags"][0] == "stage:text_filter"


def test_summary_llm_threads_trace_meta():
    store = []
    rec = Recorder("text_filter", None, enabled=False)
    generate_summary(
        "some transcript text",
        {"prompt": "p"},
        call_llm=_capture(store, '{"summary": "s", "keywords": []}'),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["generation_name"] == "text_filter/summary_llm"


def test_guided_edit_threads_trace_meta():
    store = []
    rec = Recorder("guided_edit", None, enabled=False)
    op = DirectorOp(type="edit", lines=(1, 1), note="fix", factor=None, text=None)
    apply_ops(
        ["hello world"],
        [op],
        {"prompt": "p", "max_retries": 0},
        call_llm=_capture(store, "1: hello world"),
        recorder=rec,
        unit="vidB",
    )
    assert store and store[0]["_trace"]["generation_name"] == "guided_edit/vidB"
