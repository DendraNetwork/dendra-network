#!/usr/bin/env python3
"""Bench for the gateway's failure shape — an inference that FAILED must not read as an answer.

WHAT THIS BENCH FORBIDS. The gateway used to return a total inference failure as a SUCCESSFUL
OpenAI completion: `HTTP 200`, `finish_reason: "stop"`, `usage` all zeros, and the sentence
"[Dendra] the network could not process the request (please retry)." placed in the assistant's
message. That is the worst shape an error can take, for three reasons that compound:
  · no SDK raises on it, so an integrator's `try/except` never fires;
  · no SDK retries it, because retries are driven by the status, and the status said success;
  · a caller cannot tell it apart from a model that genuinely replied that sentence — the failure is
    INDISTINGUISHABLE FROM A REPLY, so it cannot even be detected downstream and counted.
It was measured three times out of three against the published demo endpoint, which is the command
the public announcement tells a newcomer to run first.

⚠️ WHY THIS BENCH DRIVES A REAL SERVER OVER A REAL SOCKET. Asserting on `_failure()` alone would
test the predicate, never its INVOCATION — and the defect was entirely in the invocation: the
predicate did not exist and the handler hard-coded `finish_reason: "stop"`. A bench that re-applied
the classification itself would have scored green through the whole outage. So the shipped
`Handler` is served on a loopback port and questioned with real HTTP.

⚠️ AND IT DRIVES THE STREAMING BRANCH TOO. Streaming sends `200` with its headers before any content
exists; once they are out, no status can be retracted. A fix placed after that branch would look
correct in a diff and stay mute in production for every streaming client — so both shapes are asked.

⚠️ AND IT ASKS WHAT MUST STILL PASS. Every case above has its twin: a real answer must keep its
`200`, its content, its `finish_reason: "stop"` and a non-zero `usage`. A fix that turned every
response into an error would satisfy the first half alone.

Usage: python3 test_openai_failure_status.py
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The gateway reads its configuration at import time; pin what the cases depend on.
os.environ.setdefault("DENDRA_FREE_TIER", "0")
os.environ.setdefault("DENDRA_SCREEN_OUTPUT", "0")
os.environ.setdefault("DENDRA_EXPOSE_META", "1")

import client as dc  # noqa: E402
import gateway as gw  # noqa: E402

PASSED = 0
FAILED = 0


def case(name, expected, got, detail=""):
    global PASSED, FAILED
    if expected == got:
        PASSED += 1
        print("  [ok]   %-58s %s" % (name, got))
    else:
        FAILED += 1
        print("  [FAIL] %-58s expected=%s got=%s  %s" % (name, expected, got, detail))


def answers(what):
    """Replace BOTH client entry points: which one runs depends on DYN_PRICING, and a bench that
    stubbed only one would measure whichever branch happened to be configured."""
    def _f(*a, **k):
        if isinstance(what, Exception):
            raise what
        return what
    dc.quick = _f
    dc.quick_metered = _f


def post(port, text, stream=False):
    body = json.dumps({"model": "dendra", "stream": stream,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port,
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("real server on 127.0.0.1:%d\n" % port)

    print("-- what MUST fail, and must be seen to fail --")

    # The four shapes the network actually produced during the outage.
    for name, reply, expected in [
        ("client raised          -> error status", RuntimeError("boom"), 502),
        ("no result at all       -> error status", None, 502),
        ("result carries error   -> error status", {"error": "no miner"}, 502),
        ("result WITHOUT answer  -> error status", {"jid": "j1", "answer": ""}, 504),
    ]:
        answers(reply)
        code, body = post(port, "hello")
        case(name, expected, code)
        # The body must be an OpenAI ERROR object, not a completion carrying the error as text.
        try:
            d = json.loads(body)
        except Exception:
            d = {}
        case("   body is an error object, not a completion", True, isinstance(d.get("error"), dict))
        case("   no choices/finish_reason on a failure", True, "choices" not in d)

    print("\n-- the STREAMING branch, which cannot retract a 200 once its headers are out --")
    for name, reply, expected in [
        ("stream + client raised -> error status", RuntimeError("boom"), 502),
        ("stream + no result     -> error status", None, 502),
        ("stream + empty answer  -> error status", {"jid": "j1", "answer": ""}, 504),
    ]:
        answers(reply)
        code, _ = post(port, "hello", stream=True)
        case(name, expected, code)

    print("\n-- and what must STILL pass: a real success stays a success --")
    answers({"jid": "j9", "answer": "forty-two", "in_tok": 3, "out_tok": 5,
             "committee": ["m1"], "model_id": "x", "weights_hash": "h"})
    code, body = post(port, "hello")
    case("success, non-streaming -> 200", 200, code)
    d = json.loads(body)
    case("   the answer is actually served", "forty-two",
         (d.get("choices") or [{}])[0].get("message", {}).get("content"))
    case("   finish_reason = stop on a REAL success", "stop",
         (d.get("choices") or [{}])[0].get("finish_reason"))
    case("   usage is non-zero on a real success", 8, (d.get("usage") or {}).get("total_tokens"))

    answers({"jid": "j9", "answer": "forty-two", "in_tok": 3, "out_tok": 5, "committee": ["m1"]})
    code, body = post(port, "hello", stream=True)
    case("success, streaming     -> 200", 200, code)
    case("   the stream carries the content", True, "forty-two" in body)

    srv.shutdown()
    print("\nRESUME openai_failure_status ok=%d ko=%d" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
