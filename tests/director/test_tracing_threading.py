from nagare_clip.director.director_llm import generate_director_ops
from nagare_clip.llm_report import Recorder
from nagare_clip.plan.plan_llm import ProjectSummary, generate_plan
from nagare_clip.summary.summarize import PartSummary, generate_project_summary, segment_video


def _capturing_call_llm(store, response):
    def fake(messages, cfg):
        store.append(cfg)
        return response

    return fake


def test_director_threads_trace_meta():
    store = []
    rec = Recorder("director", None, enabled=False)
    generate_director_ops(
        ["one", "two"],
        {"prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, '{"ops": []}'),
        recorder=rec,
        unit="vidA",
    )
    assert store and store[0]["_trace"]["generation_name"] == "director/vidA"


def test_plan_threads_trace_meta():
    store = []
    rec = Recorder("plan", None, enabled=False)
    ps = ProjectSummary(summary="s", parts=[PartSummary(stem="v", lines=(1, 2), summary="x")])
    generate_plan(
        ps,
        {"prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, '{"directions": []}'),
        recorder=rec,
        unit="planU",
    )
    assert store and store[0]["_trace"]["tags"] == ["stage:plan", "stem:planU"]


def test_summary_segment_threads_trace_meta():
    store = []
    rec = Recorder("summary", None, enabled=False)
    segment_video(
        "stemX",
        ["a", "b"],
        {"prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, '{"parts": []}'),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["generation_name"] == "summary/stemX"


def test_summary_overall_threads_trace_meta():
    store = []
    rec = Recorder("summary", None, enabled=False)
    generate_project_summary(
        [PartSummary(stem="v", lines=(1, 2), summary="x")],
        {"overall_prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, "a summary"),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["generation_name"] == "summary/overall"
