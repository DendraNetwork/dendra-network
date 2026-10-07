"""Bench of the grader: it grades work answers only, and a clear YES or NO is posted, anything else is not
(no grade beats a guessed one)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import final_season_grader as G  # noqa: E402


def test_verdict_of():
    assert G.verdict_of("YES") is True
    assert G.verdict_of(" no.") is False
    assert G.verdict_of("**Yes**") is True
    assert G.verdict_of("I think it is coherent") is None
    assert G.verdict_of("") is None
    assert G.verdict_of(None) is None


def _service(sample, posted):
    """A fake programme service: serves `sample` on GET, keeps every POSTed grade in `posted`."""
    def http(method, url, body=None, headers=None, timeout=120):
        assert headers["Authorization"] == "Bearer tok"
        if method == "GET":
            assert "/grading/sample?day=3" in url
            return {"sample": sample}
        assert url.endswith("/grading/result")
        posted.append(json.loads(body))
        return {"ok": True}
    return http


def test_grade_day_posts_clear_verdicts_only():
    posted = []
    sample = [{"kind": "work", "job_id": "job1", "miner_id": "a", "prompt": "p", "answer": "x"},
              {"kind": "work", "job_id": "job2", "miner_id": "b", "prompt": "p", "answer": "y"},
              {"kind": "work", "job_id": "job3", "miner_id": "c", "prompt": "p", "answer": "z"}]
    replies = {"x": "YES", "y": "NO", "z": "maybe"}

    def ask(ollama, model, prompt):
        return next(v for k, v in replies.items() if f"ANSWER:\n{k}" in prompt)

    c = G.grade_day("https://p/final-season/v1", "tok", 3, "http://o", "m", ask=ask, http=_service(sample, posted))
    assert c == {"sampled": 3, "coherent": 1, "incoherent": 1, "unclear": 1, "refused": 0, "skipped": 0}
    assert [(p["miner_id"], p["job_id"], p["coherent"]) for p in posted] == [("a", "job1", True), ("b", "job2", False)]
    assert all(p["kind"] == "work" for p in posted)


def test_work_answers_get_the_work_question_and_are_posted_by_job():
    # Owner's decision of 2026-10-04: an answer to a programme request is asked whether it is a coherent
    # attempt to answer that request, and its grade names the job.
    posted, prompts = [], []
    sample = [{"kind": "work", "job_id": "job9", "miner_id": "a", "prompt": "Explain a compass.", "answer": "w"},
              {"kind": "mystery", "miner_id": "b", "prompt": "p", "answer": "v"}]

    def ask(ollama, model, prompt):
        prompts.append(prompt)
        return "NO"

    c = G.grade_day("https://p/final-season/v1", "tok", 3, "http://o", "m", ask=ask, http=_service(sample, posted))
    assert c["incoherent"] == 1 and c["skipped"] == 1 and len(prompts) == 1   # an unknown kind is not guessed
    assert "coherent attempt to answer this request" in prompts[0] and "Explain a compass." in prompts[0]
    assert posted == [{"kind": "work", "job_id": "job9", "miner_id": "a", "coherent": False, "model": "m"}]


def test_the_token_comes_from_the_environment_and_its_absence_is_refused(monkeypatch, capsys):
    # A token on the command line is readable by every local user; the documented way is the variable.
    seen = {}
    monkeypatch.setattr(G, "grade_day", lambda prog, tok, day, oll, model: seen.setdefault("tok", tok) and {})
    monkeypatch.setenv("DENDRA_FINAL_SEASON_GRADER_TOKEN", "from-env")
    assert G.main(["--programme", "https://p/final-season/v1", "--day", "0"]) in (0, None)
    assert seen["tok"] == "from-env"
    monkeypatch.delenv("DENDRA_FINAL_SEASON_GRADER_TOKEN")
    import pytest
    with pytest.raises(SystemExit):
        G.main(["--programme", "https://p/final-season/v1", "--day", "0"])
