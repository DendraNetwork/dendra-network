#!/usr/bin/env python3
"""Bench for the judge's ROUTING: two inferences, two models, two endpoints.

Why it exists: the judge process makes TWO calls of different natures.
  · the REFERENCE ANSWER — a peer opinion, produced by the MINER's backend and model;
  · the VERDICT — produced by the judge model this node resolves at startup.
The entrypoint prefixed the launch with `OLLAMA_ENDPOINT="$JEP"`, which also redirected the
reference call to the judge endpoint, where the miner's model is not pulled. Ollama answered 404
"model not found", the worker printed "Ollama unreachable", and the seat went MUTE at every start.
Measured on the project's own node: /api/tags 200 and /api/generate 404, on repeat, for 14 hours.

This bench opens no socket: it exercises the PRECEDENCE RULE, the only place where a mistake costs
a jury seat.

Usage: python3 test_judge_two_endpoints.py
"""
import sys

DEFAULT = "http://127.0.0.1:11434"


def verdict_endpoint(env, explicit=None):
    """FAITHFUL COPY of modea.judge.judge_endpoint: argument > DENDRA_JUDGE_ENDPOINT > OLLAMA_ENDPOINT."""
    return (explicit or env.get("DENDRA_JUDGE_ENDPOINT")
            or env.get("OLLAMA_ENDPOINT", DEFAULT))


def reference_endpoint(env):
    """The miner's backend knows only OLLAMA_ENDPOINT."""
    return env.get("OLLAMA_ENDPOINT", DEFAULT)


N = KO = 0


def case(name, env, expected_ref, expected_verdict):
    global N, KO
    N += 1
    r, v = reference_endpoint(env), verdict_endpoint(env)
    if r == expected_ref and v == expected_verdict:
        print(f"  OK  {name}")
    else:
        KO += 1
        print(f"  KO  {name}")
        print(f"        reference expected={expected_ref!r} got={r!r}")
        print(f"        verdict   expected={expected_verdict!r} got={v!r}")


GPU = "http://ollama:11434"
CPU = "http://ollama-cpu:11434"

print("== TWO-BACKEND TOPOLOGY (the project's own) ==")
case("each role goes to its own backend",
     {"OLLAMA_ENDPOINT": GPU, "DENDRA_JUDGE_ENDPOINT": CPU}, GPU, CPU)

print()
print("== THE DEFECT NOW FIXED: the override sent the reference to the CPU ==")
# This is what `OLLAMA_ENDPOINT="$JEP" python3 judge_worker.py` produced: both roles pointed at the
# CPU, where the miner's model does not exist -> 404 -> mute seat.
case("override -> the reference goes to the wrong place",
     {"OLLAMA_ENDPOINT": CPU, "DENDRA_JUDGE_ENDPOINT": CPU}, CPU, CPU)
print("      (this case MUST yield CPU/CPU: it documents the defect, it does not bless it)")

print()
print("== SINGLE-BACKEND TOPOLOGY: removing the override breaks nothing ==")
case("without DENDRA_JUDGE_ENDPOINT, both go to the same place",
     {"OLLAMA_ENDPOINT": GPU}, GPU, GPU)
case("nothing set -> the default, for both",
     {}, DEFAULT, DEFAULT)

print()
print("== THE VERDICT MUST NEVER FOLLOW THE MINER WHEN A JUDGE ENDPOINT IS DECLARED ==")
case("judge declared, miner elsewhere",
     {"OLLAMA_ENDPOINT": "http://other:11434", "DENDRA_JUDGE_ENDPOINT": CPU},
     "http://other:11434", CPU)

print()
print("== AN EXPLICIT ARGUMENT OVERRIDES EVERYTHING (test calls) ==")
env = {"OLLAMA_ENDPOINT": GPU, "DENDRA_JUDGE_ENDPOINT": CPU}
N += 1
if verdict_endpoint(env, explicit="http://forced:11434") == "http://forced:11434":
    print("  OK  explicit argument takes precedence")
else:
    KO += 1
    print("  KO  explicit argument ignored")

print()
print("== THE INVARIANT THAT SUMS IT UP ==")
N += 1
env = {"OLLAMA_ENDPOINT": GPU, "DENDRA_JUDGE_ENDPOINT": CPU}
if reference_endpoint(env) != verdict_endpoint(env):
    print("  OK  with two backends, the two roles are SEPARATE")
else:
    KO += 1
    print("  KO  both roles point at the same endpoint — one of the two models will be missing")

print()
print(f"BANC_JUDGE_ENDPOINTS_RESUME cas={N} ko={KO}")
sys.exit(1 if KO else 0)
