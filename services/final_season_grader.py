#!/usr/bin/env python3
"""final_season_grader.py — the model-graded check of the Final Testnet Season's work answers (ADR-047),
run where a GPU is.

    # The token is read from the environment: a token on the command line is readable by every local
    # user (/proc/<pid>/cmdline) and lands in shell history.
    export DENDRA_FINAL_SEASON_GRADER_TOKEN=...
    python3 final_season_grader.py --programme https://testnet-api.dendranetwork.com/final-season/v1 --day 3 \\
        [--ollama http://localhost:11434] [--model llama3.1:8b-instruct-q4_K_M]

WHY IT EXISTS
The chain says a programme request was served, settled and survived its audit; it does not say the answer
made sense, and below the jury floor no audit concludes at all. The generator forwards each answer to the
programme service, which draws a sample of at most three per identity per day once the day is over. This
script asks a language model one question of each — is this a coherent attempt to answer the request? —
and posts one grade per answer. An identity whose graded work answers of a day are all incoherent, with
at least min(2, answers sampled) grades, earns no work reward that day, and so no presence reward either
(`final_season_facts.GRADES_TO_VOID`).

WHAT IT DOES NOT DO
It does not tell which model wrote an answer, nor whether a small model on a processor wrote it. A grade
the model cannot give clearly (neither YES nor NO) is not posted: an unclear grade is no grade, and the
identity keeps the benefit of the doubt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

WORK_PROMPT = (
    "You are checking whether a text answers a request.\n"
    "The request was:\n{prompt}\n\n"
    "Is the answer below a coherent attempt to answer this request? Random words, unrelated sentences, "
    "repeated filler or text about something else are NOT. Reply with exactly one word: YES or NO.\n\n"
    "ANSWER:\n{answer}\n"
)
MAX_ANSWER_CHARS = 8000


def verdict_of(reply: str):
    """True (YES), False (NO) or None when the reply is neither clearly."""
    m = re.match(r"\s*\**\s*(yes|no)\b", (reply or "").strip(), re.I)
    if not m:
        return None
    return m.group(1).lower() == "yes"


def _http(method, url, body=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def ask_model(ollama: str, model: str, prompt: str) -> str:
    d = _http("POST", f"{ollama.rstrip('/')}/api/generate",
              json.dumps({"model": model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.0, "top_k": 1, "seed": 0, "num_predict": 8}}).encode(),
              {"Content-Type": "application/json"}, timeout=300)
    return d.get("response", "")


def grade_day(programme: str, token: str, day: int, ollama: str, model: str, ask=ask_model, http=_http) -> dict:
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sample = http("GET", f"{programme.rstrip('/')}/grading/sample?day={day}", headers=auth).get("sample") or []
    counts = {"sampled": len(sample), "coherent": 0, "incoherent": 0, "unclear": 0, "refused": 0, "skipped": 0}
    for s in sample:
        kind = s.get("kind")
        if kind == "work":
            prompt = WORK_PROMPT.format(prompt=s["prompt"], answer=s["answer"][:MAX_ANSWER_CHARS])
            ref = {"kind": kind, "job_id": s["job_id"]}
        else:
            counts["skipped"] += 1         # a kind this grader does not know is not guessed at
            continue
        v = verdict_of(ask(ollama, model, prompt))
        if v is None:
            counts["unclear"] += 1
            continue
        try:
            http("POST", f"{programme.rstrip('/')}/grading/result",
                 json.dumps(dict(ref, miner_id=s["miner_id"], coherent=v, model=model)).encode(), auth)
            counts["coherent" if v else "incoherent"] += 1
        except urllib.error.HTTPError:
            counts["refused"] += 1
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Grade a sample of Final Testnet Season work answers with a local model")
    ap.add_argument("--programme", required=True)
    # --token stays for the benches; the documented way is the environment variable.
    ap.add_argument("--token", default=os.environ.get("DENDRA_FINAL_SEASON_GRADER_TOKEN", ""))
    ap.add_argument("--day", type=int, required=True)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="llama3.1:8b-instruct-q4_K_M")
    a = ap.parse_args(argv)
    if not a.token:
        ap.error("no grader token: set DENDRA_FINAL_SEASON_GRADER_TOKEN (or pass --token)")
    print(json.dumps(grade_day(a.programme, a.token, a.day, a.ollama, a.model)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
