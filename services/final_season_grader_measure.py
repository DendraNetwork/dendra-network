#!/usr/bin/env python3
"""final_season_grader_measure.py — measure the Final Testnet Season work grade on REAL answers (ADR-047,
*What was measured*). Needs an Ollama that serves the models below; it writes nothing to the programme.

    python3 final_season_grader_measure.py <this directory> <ollama url> <out.json> [N]

Uses the shipped code, never a copy: final_season_generator.make_prompt for the requests,
final_season_grader.WORK_PROMPT / ask_model / verdict_of for the grades, and the miner's own serving
settings for the answers (modea/inference.py: deterministic, temperature 0, top_k 1, seed 0,
num_predict = the generator's OUT_ALLOW). Results are written after every step.

Classes graded (the question asked of each: is it a coherent attempt to answer the request?):
  honest_8b      the miner's model, as served                       -> should be YES
  small_3b       llama3.2:3b, same settings                         -> YES expected (the grade cannot tell models)
  small_1_5b     qwen2.5:1.5b, same settings                        -> YES expected
  truncated      the first quarter of the 8B answer                 -> YES expected (an answer cut by the cap)
  refusal        a polite refusal                                   -> informational
  salad          random words                                       -> should be NO
  mismatch       the 8B answer to ANOTHER request                    -> should be NO
  filler         one sentence repeated                              -> should be NO
  echo           the request copied back                            -> should be NO
"""
import json
import os
import random
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
MODEA, OLLAMA, OUT = sys.argv[1:4]
N = int(sys.argv[4]) if len(sys.argv) > 4 else 40
sys.path.insert(0, MODEA)
import final_season_generator as GEN  # noqa: E402
import final_season_grader as G  # noqa: E402

MINER = "llama3.1:8b-instruct-q4_K_M"
GRADER = "llama3.1:8b-instruct-q4_K_M"     # final_season_grader.py's default --model
SMALL = {"small_3b": "llama3.2:3b", "small_1_5b": "qwen2.5:1.5b"}


def serve(model, prompt):
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"temperature": 0.0, "top_k": 1, "seed": 0, "num_predict": GEN.OUT_ALLOW}}
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        j = json.loads(r.read())
    return j.get("response", ""), int(j.get("eval_count", 0) or 0)


def grade(prompt, answer):
    reply = G.ask_model(OLLAMA, GRADER, G.WORK_PROMPT.format(prompt=prompt, answer=answer[:G.MAX_ANSWER_CHARS]))
    return {True: "YES", False: "NO", None: "UNCLEAR"}[G.verdict_of(reply)], reply.strip()[:40]


def save(state):
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, OUT)


rnd = random.Random(20261005)
prompts = [GEN.make_prompt(rnd) for _ in range(N)]
words = " ".join(GEN.SUBJECTS + GEN.AUDIENCES).replace(",", " ").split()
state = {"n": N, "miner": MINER, "grader": GRADER, "out_allow": GEN.OUT_ALLOW, "items": []}
t0 = time.time()
answers = []
for i, p in enumerate(prompts):
    a, toks = serve(MINER, p)
    answers.append((a, toks))
    print(f"[serve 8b] {i + 1}/{N} {toks} tokens", flush=True)
small = {k: [] for k in SMALL}
for k, m in SMALL.items():
    for i, p in enumerate(prompts):
        small[k].append(serve(m, p)[0])
    print(f"[serve {k}] done", flush=True)
for i, p in enumerate(prompts):
    a, toks = answers[i]
    cases = {
        "honest_8b": a,
        "small_3b": small["small_3b"][i],
        "small_1_5b": small["small_1_5b"][i],
        "truncated": a[: max(40, len(a) // 4)],
        "refusal": "I'm sorry, but I can't help with that request.",
        "salad": " ".join(rnd.choice(words) for _ in range(120)) + ".",
        "mismatch": answers[(i + 1) % N][0],
        "filler": "This is a good and important point to consider. " * 25,
        "echo": p,
    }
    item = {"i": i, "prompt": p, "tokens_8b": toks, "capped": toks >= GEN.OUT_ALLOW, "grades": {}, "answers": {}}
    for k, text in cases.items():
        item["grades"][k] = grade(p, text)
        item["answers"][k] = text[:1500]
    state["items"].append(item)
    state["elapsed_s"] = round(time.time() - t0)
    save(state)
    print(f"[grade] {i + 1}/{N} " + " ".join(f"{k}={v[0]}" for k, v in item["grades"].items()), flush=True)
print("DONE", OUT)
