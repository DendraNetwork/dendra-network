#!/usr/bin/env python3
"""final_season_grader.py — the model-graded check of the Final Testnet Season's work answers (ADR-047),
run where a GPU is.

    # The token is read from the environment: a token on the command line is readable by every local
    # user (/proc/<pid>/cmdline) and lands in shell history.
    export DENDRA_FINAL_SEASON_GRADER_TOKEN=...
    python3 final_season_grader.py --programme https://testnet-api.dendranetwork.com/final-season/v1 --day 3 \\
        [--ollama http://localhost:11434] [--model llama3.1:8b-instruct-q4_K_M]

Why it exists
The chain says a programme request was served, settled and survived its audit; it does not say the answer
made sense, and below the jury floor no audit concludes at all. The generator forwards each answer to the
programme service, which draws a sample of at most three per identity per day once the day is over. This
script asks a language model one question of each (is this a coherent attempt to answer the request?)
and posts one grade per answer. An identity whose graded work answers of a day are all incoherent, with
at least min(2, answers sampled) grades, earns no work reward that day, and so no presence reward either
(`final_season_facts.GRADES_TO_VOID`).

What it does not do
It does not tell which model wrote an answer, nor whether a small model on a processor wrote it. A grade
the model cannot give clearly (neither yes nor no) is not posted: an unclear grade is no grade, and the
identity keeps the benefit of the doubt.

When the service says no
A 429 (too many requests) is waited out and the request sent again: the pause doubles from
RETRY_FIRST_PAUSE_S, or follows the service's Retry-After when it gives one, for at most RETRY_ATTEMPTS
attempts. Any other refusal is printed with its HTTP status and the service's own message (never the
token). Exit status: 0 graded, 1 refused or unreachable, 3 the day is not open for grading (409: ranked,
final, or after the season's end).
"""
from __future__ import annotations

import argparse
import email.utils
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = 429
RETRY_ATTEMPTS = 8           # attempts per request, the first one included
RETRY_FIRST_PAUSE_S = 2.0    # doubled after each 429, up to RETRY_MAX_PAUSE_S
RETRY_MAX_PAUSE_S = 300.0    # a Retry-After longer than this is not waited for: the request is reported refused

WORK_PROMPT = (
    "You are checking whether a text answers a request.\n"
    "The request was:\n{prompt}\n\n"
    "Is the answer below a coherent attempt to answer this request? Random words, unrelated sentences, "
    "repeated filler or text about something else are NOT. Reply with exactly one word: YES or NO.\n\n"
    "ANSWER:\n{answer}\n"
)
MAX_ANSWER_CHARS = 8000


def verdict_of(reply: str):
    """True (yes), False (no) or None when the reply is neither clearly."""
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


class Refused(Exception):
    """The service answered with an HTTP error. Carries its status and the message it gave."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _service_message(err: urllib.error.HTTPError, token: str) -> str:
    """The service's explanation of a refusal: its JSON `error` field, else its body, else the reason
    phrase. The token is cut out wherever it appears, so a message that echoes a header cannot leak it."""
    try:
        raw = err.read() or b""
    except Exception:  # noqa: BLE001  (a body that cannot be read is no message, not a crash)
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    try:
        doc = json.loads(text)
        if isinstance(doc, dict) and doc.get("error"):
            text = str(doc["error"])
    except ValueError:
        pass
    text = text or str(getattr(err, "reason", "") or "") or "no message"
    if token:
        text = text.replace(token, "<token>")
    return " ".join(text.split())[:300]


def _retry_after(err: urllib.error.HTTPError, now=time.time):
    """Seconds the service asked to wait before the next attempt, or None when it did not say.
    Retry-After is either a number of seconds or an HTTP date."""
    value = (err.headers.get("Retry-After") if err.headers is not None else None) or ""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now())


def call(http, method: str, url: str, body=None, headers=None, token: str = "", sleep=time.sleep):
    """`http(...)`, sent again after a 429 as the module header describes. Any other HTTP error, or a 429
    still there after the last attempt, raises Refused with the service's message."""
    pause = RETRY_FIRST_PAUSE_S
    where = urllib.parse.urlsplit(url).path
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return http(method, url, body=body, headers=headers)
        except urllib.error.HTTPError as e:
            message = _service_message(e, token)
            if e.code != RETRY_STATUS or attempt == RETRY_ATTEMPTS:
                raise Refused(e.code, message) from None
            wait = _retry_after(e)
            if wait is None:
                wait = pause
            elif wait > RETRY_MAX_PAUSE_S:
                raise Refused(e.code, f"{message} (the service asks to wait {wait:.0f} s, more than "
                                      f"{RETRY_MAX_PAUSE_S:.0f} s)") from None
            pause = min(pause * 2, RETRY_MAX_PAUSE_S)
            _say(f"[grader] HTTP 429 on {method} {where}: {message}; attempt {attempt} of {RETRY_ATTEMPTS}, "
                 f"next one in {wait:.0f} s")
            sleep(wait)
    raise AssertionError("unreachable: the last attempt returns or raises")


def grade_day(programme: str, token: str, day: int, ollama: str, model: str, ask=ask_model, http=_http,
              sleep=time.sleep) -> dict:
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = programme.rstrip("/")
    sample = call(http, "GET", f"{base}/grading/sample?day={day}", headers=auth, token=token,
                  sleep=sleep).get("sample") or []
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
            call(http, "POST", f"{base}/grading/result",
                 json.dumps(dict(ref, miner_id=s["miner_id"], coherent=v, model=model)).encode(), auth,
                 token=token, sleep=sleep)
            counts["coherent" if v else "incoherent"] += 1
        except Refused as e:
            counts["refused"] += 1
            _say(f"[grader] day {day}: the grade of job {s['job_id']} was refused: {e}")
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
    try:
        counts = grade_day(a.programme, a.token, a.day, a.ollama, a.model)
    except Refused as e:
        _say(f"[grader] day {a.day}: the service refused the sample: {e}")
        return 3 if e.status == 409 else 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        # The service, or the model behind --ollama, did not answer usably. Said with its cause; the
        # error text holds URLs and reasons, never the token, which only travels in a header.
        _say(f"[grader] day {a.day}: stopped, {type(e).__name__}: {str(e).replace(a.token, '<token>')}")
        return 1
    print(json.dumps(counts), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
