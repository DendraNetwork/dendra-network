"""Separation of a judge's two Ollama endpoints.

WHAT THIS SUITE PROTECTS. A judge process queries Ollama for two unrelated things: its REFERENCE
answer (the MINER's model, `llama3.1:8b`, ~5 GB of VRAM) and its VERDICT (the judge model pinned
on-chain, Qwen3-30B-A3B, 19 GB). While both read the same `OLLAMA_ENDPOINT` variable, they
necessarily land on the same instance — and on an 8 GB card the two models never fit together:
Ollama evicts one, the reload exceeds the miner's deadline, and the jobs stay `open`. The failure is
SILENT: the chain advances, the judges run, nothing is served any more.

`DENDRA_JUDGE_ENDPOINT` makes the separation expressible; these cases check that it is effective on
ALL FOUR verdict stages — not only on the one that was inspected — and above all that in its absence
NOTHING changes: the addition must be strictly additive, so no already deployed machine behaves
differently because of this code.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modea import judge  # noqa: E402

DEFAULT = "http://localhost:11434"
CPU = "http://localhost:11435"

# The four verdict stages. Test them ALL, not one: a fix applied to llm_judge and forgotten on
# llm_coherent would leave a whole stage evicting the miner, with exactly the same symptom and no
# additional clue.
STAGES = [
    ("llm_judge", lambda j: j.llm_judge("ref", "cand")),
    ("llm_coherent", lambda j: j.llm_coherent("texte")),
    ("llm_relevant", lambda j: j.llm_relevant("prompt", "cand")),
    ("llm_multi_ok", lambda j: j.llm_multi_ok("prompt", "ref", "cand")),
]


@pytest.fixture
def endpoints_seen(monkeypatch):
    """Replaces _generate: Ollama is not wanted here, only WHERE the call would have gone."""
    seen = []

    def fake(model, prompt, endpoint, timeout):
        seen.append(endpoint)
        return "VALIDE"

    monkeypatch.setattr(judge, "_generate", fake)
    for k in ("OLLAMA_ENDPOINT", "DENDRA_JUDGE_ENDPOINT", "DENDRA_JUDGE_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    return seen


@pytest.mark.parametrize("name,call", STAGES)
def test_without_the_variable_behaviour_is_unchanged(endpoints_seen, name, call):
    """NON-REGRESSION: with no variable set, the historical default is kept."""
    call(judge)
    assert endpoints_seen[-1] == DEFAULT


@pytest.mark.parametrize("name,call", STAGES)
def test_ollama_endpoint_alone_is_unchanged(endpoints_seen, monkeypatch, name, call):
    """NON-REGRESSION: a machine that sets only OLLAMA_ENDPOINT is unaffected."""
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://elsewhere:9999")
    call(judge)
    assert endpoints_seen[-1] == "http://elsewhere:9999"


@pytest.mark.parametrize("name,call", STAGES)
def test_the_real_split_verdict_on_cpu_reference_on_gpu(endpoints_seen, monkeypatch, name, call):
    """THE CASE THAT MATTERS: the verdict goes to the CPU while the reference stays on the card.

    It is the only one of the three possible configurations where nothing is evicted — the card holds
    a single `llama3.1:8b` shared by the miner and the judge's reference, the CPU holds the judge
    model."""
    monkeypatch.setenv("OLLAMA_ENDPOINT", DEFAULT)
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", CPU)
    call(judge)
    assert endpoints_seen[-1] == CPU, f"{name} would still send the verdict to the card"


@pytest.mark.parametrize("name,call", STAGES)
def test_an_empty_variable_does_not_mask(endpoints_seen, monkeypatch, name, call):
    """A variable that is set but EMPTY (failed export, empty substitution) must not overwrite
    OLLAMA_ENDPOINT with the default: otherwise `export DENDRA_JUDGE_ENDPOINT=$NONEXISTENT` would
    silently move the verdicts of a correctly configured machine."""
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://elsewhere:9999")
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", "")
    call(judge)
    assert endpoints_seen[-1] == "http://elsewhere:9999"


def test_an_explicit_argument_wins(endpoints_seen, monkeypatch):
    """The harness passes the endpoint as an argument: it must beat both variables."""
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://a:1")
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", "http://b:2")
    judge.llm_judge("ref", "cand", endpoint="http://explicit:3")
    assert endpoints_seen[-1] == "http://explicit:3"


def test_the_full_priority_order(monkeypatch):
    """The function itself, without going through the stages — the truth table in one place."""
    cases = [
        ({}, None, DEFAULT),
        ({"OLLAMA_ENDPOINT": "http://o:1"}, None, "http://o:1"),
        ({"DENDRA_JUDGE_ENDPOINT": "http://j:2"}, None, "http://j:2"),
        ({"OLLAMA_ENDPOINT": "http://o:1", "DENDRA_JUDGE_ENDPOINT": "http://j:2"}, None, "http://j:2"),
        ({"OLLAMA_ENDPOINT": "http://o:1", "DENDRA_JUDGE_ENDPOINT": "http://j:2"}, "http://x:3", "http://x:3"),
    ]
    for env, explicit, expected in cases:
        for k in ("OLLAMA_ENDPOINT", "DENDRA_JUDGE_ENDPOINT"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert judge.judge_endpoint(explicit) == expected, f"env={env} explicit={explicit}"


def test_the_nli_backend_does_not_touch_ollama(monkeypatch):
    """Safeguard: on the deterministic backend, llm_relevant calls nothing — the endpoint separation
    must not have reintroduced a network call on that path."""
    calls = []
    monkeypatch.setattr(judge, "_generate", lambda *a, **k: calls.append(a) or "VALIDE")
    monkeypatch.setenv("DENDRA_JUDGE_BACKEND", "nli")
    assert judge.llm_relevant("prompt", "cand") is None
    assert calls == []
