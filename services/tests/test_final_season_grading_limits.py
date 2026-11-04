"""Bench of the grading traffic limits: what the grader does when the service says no, and which requests
the service's per-address limit counts.

The grader half drives `final_season_grader.call` and `grade_day` with a fake transport that raises the
real `urllib.error.HTTPError`, and a recorded sleep. The service half runs the real handler over real
HTTP, with the signature and the chain faked as in test_final_season_server.py; the grader is then
played against it through its own HTTP client, so a grading run of a full day is measured end to end.
"""
import email.message
import importlib
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)

import pytest  # noqa: E402

import final_season_grader as G  # noqa: E402

TOKEN = "grader-token-7f3c9a"
SAMPLE = [{"kind": "work", "job_id": "job1", "miner_id": "a", "prompt": "p", "answer": "x"}]


def http_error(code, body=b"", retry_after=None, url="https://p/final-season/v1/grading/result"):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, "status", headers, io.BytesIO(body))


def scripted(post_errors, get_error=None):
    """A fake service: GET serves SAMPLE (or raises get_error); each POST raises the next error of
    `post_errors`, then accepts. Records every call."""
    calls = []
    errors = list(post_errors)

    def http(method, url, body=None, headers=None, timeout=120):
        calls.append((method, url))
        assert headers["Authorization"] == "Bearer " + TOKEN
        if method == "GET":
            if get_error is not None:
                raise get_error
            return {"sample": SAMPLE}
        if errors:
            raise errors.pop(0)
        return {"ok": True}
    return http, calls


def yes(ollama, model, prompt):
    return "YES"


# -- the grader ----------------------------------------------------------------------------------
def test_a_429_is_waited_out_with_a_growing_pause():
    waits = []
    http, calls = scripted([http_error(429, b'{"error": "too many requests"}')] * 3)
    c = G.grade_day("https://p/final-season/v1", TOKEN, 3, "http://o", "m", ask=yes, http=http, sleep=waits.append)
    assert c["coherent"] == 1 and c["refused"] == 0
    assert waits == [G.RETRY_FIRST_PAUSE_S, G.RETRY_FIRST_PAUSE_S * 2, G.RETRY_FIRST_PAUSE_S * 4]
    assert [m for m, _ in calls] == ["GET", "POST", "POST", "POST", "POST"]


def test_retry_after_in_seconds_is_followed():
    waits = []
    http, _ = scripted([http_error(429, retry_after="7"), http_error(429)])
    c = G.grade_day("https://p/final-season/v1", TOKEN, 3, "http://o", "m", ask=yes, http=http, sleep=waits.append)
    # the service's own delay first; without one, the grader's pause, which kept growing meanwhile
    assert c["coherent"] == 1 and waits == [7.0, G.RETRY_FIRST_PAUSE_S * 2]


def test_retry_after_as_an_http_date_is_followed():
    err = http_error(429, retry_after="Sat, 10 Oct 2026 12:00:30 GMT")
    noon = 1791633600.0                      # 2026-10-10 12:00:00 UTC
    assert G._retry_after(err, now=lambda: noon) == 30.0
    assert G._retry_after(err, now=lambda: noon + 60) == 0.0          # already past: no wait, never negative
    assert G._retry_after(http_error(429, retry_after="soon")) is None
    assert G._retry_after(http_error(429)) is None


def test_a_retry_after_beyond_the_ceiling_is_not_waited_for(capsys):
    waits = []
    http, _ = scripted([http_error(429, retry_after=str(int(G.RETRY_MAX_PAUSE_S) + 1))])
    c = G.grade_day("https://p/final-season/v1", TOKEN, 3, "http://o", "m", ask=yes, http=http, sleep=waits.append)
    assert c["refused"] == 1 and c["coherent"] == 0 and waits == []
    assert "HTTP 429" in capsys.readouterr().err


def test_a_429_that_never_clears_ends_refused_after_the_last_attempt():
    waits = []
    http, calls = scripted([http_error(429)] * (G.RETRY_ATTEMPTS + 5))
    c = G.grade_day("https://p/final-season/v1", TOKEN, 3, "http://o", "m", ask=yes, http=http, sleep=waits.append)
    assert c["refused"] == 1
    assert len([m for m, _ in calls if m == "POST"]) == G.RETRY_ATTEMPTS
    assert len(waits) == G.RETRY_ATTEMPTS - 1 and max(waits) <= G.RETRY_MAX_PAUSE_S


def test_another_refusal_is_not_retried_and_prints_status_and_message_without_the_token(capsys):
    waits = []
    # A service that echoes the request back in its error: the token must still not be printed.
    body = json.dumps({"error": "no sampled answer for this job (Authorization: Bearer %s)" % TOKEN}).encode()
    http, calls = scripted([http_error(404, body)])
    c = G.grade_day("https://p/final-season/v1", TOKEN, 3, "http://o", "m", ask=yes, http=http, sleep=waits.append)
    err = capsys.readouterr().err
    assert c["refused"] == 1 and waits == [] and len(calls) == 2
    assert "HTTP 404" in err and "no sampled answer for this job" in err and "job1" in err
    assert TOKEN not in err and "<token>" in err


def test_a_body_that_is_not_json_is_printed_as_text():
    http, _ = scripted([http_error(502, b"<html>Bad gateway</html>")])
    with pytest.raises(G.Refused) as e:
        G.call(http, "POST", "https://p/final-season/v1/grading/result", b"{}", {"Authorization": "Bearer " + TOKEN},
               token=TOKEN, sleep=lambda s: None)
    assert e.value.status == 502 and "Bad gateway" in e.value.message


def with_transport(monkeypatch, http):
    """main() as shipped, with grade_day talking to `http` instead of the network and never sleeping."""
    real = G.grade_day
    monkeypatch.setattr(G, "grade_day", lambda *args: real(*args, http=http, sleep=lambda s: None))


@pytest.mark.parametrize("code,payload,rc", [
    (409, b'{"error": "day 2 is final: its ranking is computed from the evidence as it stands"}', 3),
    (403, b'{"error": "grader route"}', 1),
])
def test_main_names_a_refused_sample_and_exits_by_kind(monkeypatch, capsys, code, payload, rc):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_GRADER_TOKEN", TOKEN)

    def refusing(method, url, body=None, headers=None, timeout=120):
        raise http_error(code, payload, url=url)
    with_transport(monkeypatch, refusing)
    assert G.main(["--programme", "https://p/final-season/v1", "--day", "2"]) == rc
    out = capsys.readouterr()
    assert "HTTP %d" % code in out.err and json.loads(payload)["error"] in out.err
    assert out.out == "" and TOKEN not in out.err


def test_main_says_why_when_nothing_answers(monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_GRADER_TOKEN", TOKEN)

    def down(method, url, body=None, headers=None, timeout=120):
        raise urllib.error.URLError("connection refused")
    with_transport(monkeypatch, down)
    assert G.main(["--programme", "https://p/final-season/v1", "--day", "2"]) == 1
    assert "connection refused" in capsys.readouterr().err


# -- the service ---------------------------------------------------------------------------------
GRADER = "g" * 40
INTERNAL = "t" * 40


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_DATA", str(tmp_path))
    monkeypatch.setenv("DENDRA_FINAL_SEASON_START_HEIGHT", "1")
    monkeypatch.setenv("DENDRA_TRUST_PROXY", "1")
    import final_season_server as S
    importlib.reload(S)

    class Reg:
        def operator(self, mid):
            return ("dendra1op" + mid, "FEES")

        def creator(self, mid):
            return ("dendra1op" + mid, "FEES")

    S.REGISTRY = Reg()
    S.ST = S.State(str(tmp_path), 1)
    S.ST.height = 1000
    S.ST.latest_time = S.END - 10 * 86400
    S._RATE.clear()
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    monkeypatch.setattr(S, "INTERNAL_TOKEN", INTERNAL)
    httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield S, f"http://127.0.0.1:{httpd.server_address[1]}/final-season/v1/"
    httpd.shutdown()


def post(base, route, body, ip=None, token=None):
    req = urllib.request.Request(base + route, data=json.dumps(body).encode(), method="POST")
    if ip:
        req.add_header("X-Forwarded-For", ip)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def sealed_day(S, base, miners=14):
    """Files three answers for each of `miners` identities, ends the day and draws its sample: three per
    identity is the most a day samples, so the day offers 3 x miners answers to grade."""
    root = base.split("/final-season/")[0]
    for m in range(miners):
        for j in range(3):
            body = {"job_id": f"job{m}x{j}", "miner_id": f"m{m:02d}", "prompt": "p", "answer": "a"}
            req = urllib.request.Request(root + "/internal/work_answer", data=json.dumps(body).encode(), method="POST")
            req.add_header("X-Final-Season-Internal", INTERNAL)
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
    day = S.ST.day_now()
    S.ST.height = S.day_bounds(day, 1)[1] + 1
    S.seal_work_day(day)
    return day


def test_42_grades_in_a_row_from_one_address_all_land(srv):
    S, base = srv
    day = sealed_day(S, base)
    assert 42 > S.POST_PER_MINUTE           # otherwise this case would not exercise the limit
    codes = [post(base, "grading/result", {"kind": "work", "job_id": f"job{m}x{j}", "miner_id": f"m{m:02d}",
                                          "coherent": True}, ip="198.51.100.20", token=GRADER)
             for m in range(14) for j in range(3)]
    assert codes == [200] * 42
    assert len([r for r in S.ST.ev.read(day) if r["type"] == "work_grade"]) == 42


def test_a_grading_run_of_42_answers_completes_through_the_grader_itself(srv):
    S, base = srv
    day = sealed_day(S, base)

    def no_sleep(seconds):
        raise AssertionError("the grader had to wait: the service refused a grade with 429")
    c = G.grade_day(base, GRADER, day, "http://o", "m", ask=yes, sleep=no_sleep)
    assert c == {"sampled": 42, "coherent": 42, "incoherent": 0, "unclear": 0, "refused": 0, "skipped": 0}


@pytest.mark.parametrize("token", [None, "wrong"])
def test_without_the_right_token_the_grader_route_is_still_limited(srv, token):
    S, base = srv
    codes = [post(base, "grading/result", {"kind": "work", "job_id": "j", "miner_id": "m1", "coherent": True},
                  ip="198.51.100.30", token=token)
             for _ in range(S.POST_PER_MINUTE + 2)]
    assert codes[:S.POST_PER_MINUTE] == [403] * S.POST_PER_MINUTE and codes[-1] == 429


def test_the_grader_token_exempts_no_other_route(srv):
    S, base = srv
    for route in ("payout", "no-such-route"):
        S._RATE.clear()
        codes = [post(base, route, {"miner_id": "m1", "address": "x"}, ip="198.51.100.40", token=GRADER)
                 for _ in range(S.POST_PER_MINUTE + 2)]
        assert 429 not in codes[:S.POST_PER_MINUTE] and codes[-1] == 429, route


def test_graded_posts_do_not_spend_the_address_allowance(srv):
    S, base = srv
    sealed_day(S, base)
    for m in range(14):
        for j in range(3):
            post(base, "grading/result", {"kind": "work", "job_id": f"job{m}x{j}", "miner_id": f"m{m:02d}",
                                          "coherent": False}, ip="198.51.100.50", token=GRADER)
    # the same address still has its whole allowance for the routes the limit protects
    codes = [post(base, "payout", {"miner_id": "m1", "address": "x"}, ip="198.51.100.50")
             for _ in range(S.POST_PER_MINUTE)]
    assert 429 not in codes
