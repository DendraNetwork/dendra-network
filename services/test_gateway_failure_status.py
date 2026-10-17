#!/usr/bin/env python3
"""Bench for the gateway's failure shape: an inference that failed must not read as an answer.

A total failure returned as `HTTP 200` with `finish_reason: "stop"` and an apology in the assistant's
message is the worst shape an error can take: no SDK raises on it, none retries it (retries follow the
status), and a caller cannot tell it from a model that really replied that sentence.

The shipped `Handler` is served on a loopback port and questioned over real HTTP, because asserting on
`_failure()` alone would test the predicate and never its invocation. The streaming branch is asked
too: it sends `200` and its headers before any content exists, so a fix placed after that branch would
miss every streaming client.

Every case has its twin: a real answer must keep its `200`, its content, its `finish_reason: "stop"`
and a non-zero `usage`, so a fix that turned every response into an error fails as well.

Usage: python3 test_gateway_failure_status.py
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
    """Replace both client entry points: which one runs depends on DYN_PRICING, and a bench that
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

    print("-- what must fail, and must be seen to fail --")

    # The four shapes the network actually produced during the outage.
    for name, reply, expected in [
        ("client raised          -> error status", RuntimeError("boom"), 502),
        ("no result at all       -> error status", None, 502),
        ("result carries error   -> error status", {"error": "no miner"}, 502),
        ("result without answer  -> error status", {"jid": "j1", "answer": ""}, 504),
    ]:
        answers(reply)
        code, body = post(port, "hello")
        case(name, expected, code)
        # The body must be an error object, not a completion carrying the error as text.
        try:
            d = json.loads(body)
        except Exception:
            d = {}
        case("   body is an error object, not a completion", True, isinstance(d.get("error"), dict))
        case("   no choices/finish_reason on a failure", True, "choices" not in d)

    print("\n-- the streaming branch, which cannot retract a 200 once its headers are out --")
    for name, reply, expected in [
        ("stream + client raised -> error status", RuntimeError("boom"), 502),
        ("stream + no result     -> error status", None, 502),
        ("stream + empty answer  -> error status", {"jid": "j1", "answer": ""}, 504),
    ]:
        answers(reply)
        code, _ = post(port, "hello", stream=True)
        case(name, expected, code)

    print("\n-- and what must still pass: a real success stays a success --")
    answers({"jid": "j9", "answer": "forty-two", "in_tok": 3, "out_tok": 5,
             "committee": ["m1"], "model_id": "x", "weights_hash": "h"})
    code, body = post(port, "hello")
    case("success, non-streaming -> 200", 200, code)
    d = json.loads(body)
    case("   the answer is actually served", "forty-two",
         (d.get("choices") or [{}])[0].get("message", {}).get("content"))
    case("   finish_reason = stop on a real success", "stop",
         (d.get("choices") or [{}])[0].get("finish_reason"))
    case("   usage is non-zero on a real success", 8, (d.get("usage") or {}).get("total_tokens"))

    answers({"jid": "j9", "answer": "forty-two", "in_tok": 3, "out_tok": 5, "committee": ["m1"]})
    code, body = post(port, "hello", stream=True)
    case("success, streaming     -> 200", 200, code)
    case("   the stream carries the content", True, "forty-two" in body)

    srv.shutdown()
    print("\nSUMMARY gateway_failure_status ok=%d ko=%d" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
