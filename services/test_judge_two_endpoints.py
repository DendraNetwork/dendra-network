#!/usr/bin/env python3
"""Bench for the judge's routing: two inferences, two models, two endpoints.

The judge process makes two calls of different kinds:
  · the reference answer, a peer opinion produced by the miner's backend and model;
  · the verdict, produced by the judge model this node resolves at startup.
Launching the worker with `OLLAMA_ENDPOINT="$JEP"` also sent the reference call to the judge
endpoint, where the miner's model is not pulled: Ollama answers 404 "model not found" and the jury
seat stays silent at every start.

This bench opens no socket: it exercises the precedence rule, the one place where a mistake costs a
jury seat.

Usage: python3 test_judge_two_endpoints.py
"""
import sys

DEFAULT = "http://127.0.0.1:11434"


def verdict_endpoint(env, explicit=None):
    """A copy of modea.judge.judge_endpoint: argument, then DENDRA_JUDGE_ENDPOINT, then OLLAMA_ENDPOINT."""
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

print("== Two backends (the project's own topology) ==")
case("each role goes to its own backend",
     {"OLLAMA_ENDPOINT": GPU, "DENDRA_JUDGE_ENDPOINT": CPU}, GPU, CPU)

print()
print("== The fixed defect: the override sent the reference to the CPU ==")
# What `OLLAMA_ENDPOINT="$JEP" python3 judge_worker.py` produces: both roles point at the CPU, where
# the miner's model does not exist, so the reference call fails with 404.
case("override -> the reference goes to the wrong place",
     {"OLLAMA_ENDPOINT": CPU, "DENDRA_JUDGE_ENDPOINT": CPU}, CPU, CPU)
print("      (this case must yield CPU/CPU: it documents the defect, it does not approve it)")

print()
print("== One backend: removing the override breaks nothing ==")
case("without DENDRA_JUDGE_ENDPOINT, both go to the same place",
     {"OLLAMA_ENDPOINT": GPU}, GPU, GPU)
case("nothing set -> the default, for both",
     {}, DEFAULT, DEFAULT)

print()
print("== The verdict never follows the miner when a judge endpoint is declared ==")
case("judge declared, miner elsewhere",
     {"OLLAMA_ENDPOINT": "http://other:11434", "DENDRA_JUDGE_ENDPOINT": CPU},
     "http://other:11434", CPU)

print()
print("== An explicit argument overrides everything (test calls) ==")
env = {"OLLAMA_ENDPOINT": GPU, "DENDRA_JUDGE_ENDPOINT": CPU}
N += 1
if verdict_endpoint(env, explicit="http://forced:11434") == "http://forced:11434":
    print("  OK  explicit argument takes precedence")
else:
    KO += 1
    print("  KO  explicit argument ignored")

print()
print("== The invariant ==")
N += 1
env = {"OLLAMA_ENDPOINT": GPU, "DENDRA_JUDGE_ENDPOINT": CPU}
if reference_endpoint(env) != verdict_endpoint(env):
    print("  OK  with two backends, the two roles are separate")
else:
    KO += 1
    print("  KO  both roles point at the same endpoint — one of the two models will be missing")

print()
print(f"JUDGE_ENDPOINTS_SUMMARY cases={N} ko={KO}")
sys.exit(1 if KO else 0)
