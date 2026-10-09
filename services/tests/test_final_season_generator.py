"""Bench of the generator's fixed daily quota, its pacing and the forwarding of the miners' answers (no
chain: the sending is `client`'s, benched elsewhere)."""
import json
import os
import sys
import tempfile
import types
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# `DENDRA_MODEA_DIR` puts another copy of the modules first on the path: the mutation bench
# (dendra/onchain-staging/dendra_saison_vague1_test.sh) points it at a MUTATED copy and expects red.
if os.environ.get("DENDRA_MODEA_DIR"):
    sys.path.insert(0, os.environ["DENDRA_MODEA_DIR"])

import final_season_generator as G  # noqa: E402
from final_season_rules import RULES  # noqa: E402

DAY = RULES["day_blocks"]


def test_no_quota_no_request():
    assert G.spacing_s(0, 0, 100, 1) is None
    assert G.spacing_s(6, 6, 100, 1) is None


def test_requests_left_are_spread_over_the_blocks_left():
    # half the day gone, 10 requests left: one every (DAY/2 * 5 s) / 10
    s = G.spacing_s(12, 2, 1 + DAY // 2, 1)
    assert abs(s - (DAY // 2) * 5.0 / 10) < 1e-6


def test_a_day_started_late_is_still_spread_not_burst():
    # A generator that starts (or comes back) five minutes (60 blocks) before the day ends, with 600
    # requests still to send: an even spread would be one every 0.5 s.
    s = G.spacing_s(600, 0, 1 + DAY - 60, 1)
    assert s == 1.0                                  # floored at one per second, never a burst


class _Stop(BaseException):
    """Ends the generator's endless loop. A BaseException, so none of its `except Exception` can swallow it."""


_END = object()
_DROP = object()                                         # a status field the poll leaves out
RUNNING = 10 * 86400                                     # a running season's chain time: ten days before its end


def _drive(monkeypatch, days, outcome=lambda k: "served by dm1x, answer forwarded", state=None, start=1,
           status=None):
    """Runs the SHIPPED `main` loop against a scripted programme status: the i-th poll of the status reports
    programme day `days[i]`, and the poll after the last one stops the loop. Every poll is a RUNNING season's
    (`ended` false, chain time `RUNNING` seconds before the end) unless `status(i)` changes its fields; a field
    it sets to `_DROP` is left out. The clock jumps far ahead at every reading, so a poll with requests left
    always sends one: the bench measures the quota, never the pacing. Each run counts in a state file of its
    own unless one is given. Returns every URL the generator read and the day of every request it sent;
    main's return code, when it returns, is in `_drive.rc`, and every sleep it asked for in `_drive.sleeps`."""
    urls, served, sleeps = [], [], []
    polls = iter(enumerate(days))

    def get(url):
        urls.append(url)
        if url.endswith("/status"):
            i, day = next(polls, (None, _END))
            if day is _END:
                raise _Stop()
            st = {"season": "final-testnet", "start_height": start, "height": start + (day or 0) * DAY,
                  "day": day, "ended": False, "latest_block_time": G.END - RUNNING}
            for k, v in (status(i) if status else {}).items():
                if v is _DROP:
                    st.pop(k, None)
                else:
                    st[k] = v
            return json.dumps(st).encode()
        # Any other read gets an empty evidence file: a quota counted from it would be zero.
        return b""

    class Clock:
        t = 0.0

        def time(self):
            self.t += 10 ** 6                            # past any spacing a day can produce
            return self.t

        def sleep(self, s):
            sleeps.append(s)

    monkeypatch.setattr(G, "_get", get)
    monkeypatch.setattr(G, "time", Clock())
    monkeypatch.setattr(G, "INTERNAL_TOKEN", "t" * 40)
    def run_one(dc, day, n, rnd):
        served.append(day)
        text = outcome(len(served))
        if isinstance(text, tuple):
            return text                                  # (text, answered) as the test scripts it
        # what the shipped run_one answers: the text, and whether the answer reached the programme
        return text, text.endswith("answer forwarded")
    monkeypatch.setattr(G, "run_one", run_one)
    monkeypatch.setitem(sys.modules, "client", types.ModuleType("client"))
    monkeypatch.setattr(G, "STATE", state or os.path.join(tempfile.mkdtemp(), "sent.json"))
    _drive.rc, _drive.sleeps = None, sleeps
    try:
        _drive.rc = G.main()
    except _Stop:
        pass
    else:
        assert _drive.rc is not None
    return urls, served


@pytest.mark.parametrize("quota", [RULES["requests_per_day"], 7])
def test_each_day_sends_the_rules_fixed_number_and_reads_no_evidence(monkeypatch, capsys, quota):
    # Owner's decision of 2026-10-05: the programme sends a FIXED number of requests a day, which the chain
    # hands out among its present miners. The number comes from the rules alone (7 shows it is READ there,
    # not written into the generator), and nothing the generator could count changes it: it reads the
    # status and nothing else, so a farm adding identities cannot add paid work.
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    urls, served = _drive(monkeypatch, [0] * (quota + 5) + [1] * (quota + 5))
    assert served == [0] * quota + [1] * quota       # the quota, then nothing more until the next day
    assert urls and all(u == f"{G.PROGRAMME}/status" for u in urls), sorted(set(urls))
    out = capsys.readouterr().out
    assert f"day 0: {quota} answered requests" in out and f"day 1: {quota} answered requests" in out
    assert "NOT reached" not in out


def test_a_day_left_short_says_how_many_were_not_sent_and_the_next_day_does_not_make_up(monkeypatch, capsys):
    # The day ends after ten requests: the log names the shortfall, and the next day starts again from
    # zero against the same quota. Carrying the shortfall over would put more than the fixed number on
    # one day.
    quota = RULES["requests_per_day"]
    urls, served = _drive(monkeypatch, [0] * 10 + [1] * 3)
    assert served == [0] * 10 + [1] * 3
    out = capsys.readouterr().out
    assert f"day 0: 10 of {quota} answered, 10 sent -- quota NOT reached" in out
    assert f"day 1 request 1 (1/{quota} answered):" in out and f"day 1 request 3 (3/{quota} answered):" in out
    assert all(u.endswith("/status") for u in urls)


class _FakeClient:
    """`client.quick_metered`'s contract: `on_answer` is called with the answer BEFORE the settlement,
    and what it raises is caught and reported, never allowed to stop the settlement."""
    def __init__(self, answer):
        self.answer, self.prompts, self.events = answer, [], []

    def quick_metered(self, prompt, base, per_token, out_allow, relay, client="", on_answer=None):
        self.prompts.append(prompt)
        reported = None
        if on_answer is not None:
            try:
                on_answer("job17", ["dm1miner"], self.answer)
                reported = True
            except Exception as e:  # noqa: BLE001
                reported = f"{type(e).__name__}: {e}"
        self.events.append("settle")
        return {"jid": "job17", "committee": ["dm1miner"], "answer": self.answer, "reported": reported}


def test_every_answer_is_forwarded_with_its_request_and_no_trap_is_sent(monkeypatch):
    # Owner's decision of 2026-10-04: no trap, no decoy; the miner's answer to each programme request goes
    # to the service's internal route for the grader.
    import random
    sent = []
    monkeypatch.setattr(G, "report_work",
                        lambda jid, mid, prompt, answer: sent.append((jid, mid, prompt, answer)) or G.RECORDED)
    dc = _FakeClient("A lighthouse sends a beam of light.")
    rnd = random.Random(1)
    outs = [G.run_one(dc, 0, n, rnd) for n in range(50)]
    assert all(answered is True for _, answered in outs)
    outs = [text for text, _ in outs]
    assert all(any(_is_form(f, p) for f in G.FORMS) for p in dc.prompts)   # ordinary requests only
    assert len(sent) == 50 and sent[0][:2] == ("job17", "dm1miner") and sent[0][2] == dc.prompts[0]
    assert sent[0][3] == dc.answer and "forwarded" in outs[0]
    assert not hasattr(G, "trap_task") and not hasattr(G, "pick_request")


def test_no_answer_nothing_forwarded_and_said_not_counted(monkeypatch):
    import random
    sent = []
    monkeypatch.setattr(G, "report_work", lambda *a: sent.append(a))
    for empty in (None, ""):
        out, answered = G.run_one(_FakeClient(empty), 0, 1, random.Random(1))
        assert "no answer" in out and "not counted as work" in out and answered is False
    assert sent == []


def test_the_answer_is_recorded_before_the_job_is_settled(monkeypatch):
    # Recorded after the settlement, an answer to a job settled at the very end of a day would land on the
    # next day, and the job would not count as work on its own day.
    import random
    dc = _FakeClient("A compass needle points north.")
    monkeypatch.setattr(G, "report_work", lambda *a: dc.events.append("report") or G.RECORDED)
    assert G.run_one(dc, 0, 1, random.Random(1)) == ("served by dm1miner, answer forwarded", True)
    assert dc.events == ["report", "settle"]


def test_a_filing_that_fails_is_said_and_does_not_stop_the_generator(monkeypatch):
    import random
    def boom(*a):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(G, "report_work", boom)
    out, answered = G.run_one(_FakeClient("text"), 0, 1, random.Random(1))
    assert "NOT forwarded" in out and answered is False


def test_report_work_carries_the_token_and_cuts_at_the_graders_limit(monkeypatch):
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def urlopen(req, timeout=0):
        seen["url"], seen["token"], seen["body"] = req.full_url, req.get_header("X-final-season-internal"), req.data
        return R()

    monkeypatch.setattr(G, "INTERNAL_TOKEN", "t" * 40)
    monkeypatch.setattr(G.urllib.request, "urlopen", urlopen)
    assert G.report_work("job1", "dm1m", "a prompt", "x" * (G.MAX_ANSWER_CHARS + 500)) == G.RECORDED
    body = json.loads(seen["body"])
    assert seen["url"].endswith("/internal/work_answer") and seen["token"] == "t" * 40
    assert len(body["answer"]) == G.MAX_ANSWER_CHARS and body["prompt"] == "a prompt" and body["job_id"] == "job1"


# --- a forward whose reply is lost: retried, and the programme's own copy is read as recorded -----------------
# The relecture: the service wrote the answer, its reply was lost (a timeout), the generator read "not recorded"
# and replaced the request while the job was settled anyway -- one request more than the day's number.
def _real_programme(tmp_path, monkeypatch):
    """The SHIPPED service's internal route, on a real socket, with a day running."""
    import importlib
    import threading
    # The generator put ITS directory first on the path when it was imported: a copy under test (the mutation
    # bench) goes back in front, or a mutated service would be measured through the delivered one.
    modea = os.environ.get("DENDRA_MODEA_DIR", "")
    if modea:
        while modea in sys.path:
            sys.path.remove(modea)
        sys.path.insert(0, modea)
    import final_season_server as S
    S = importlib.reload(S)
    if modea and os.path.exists(os.path.join(modea, "final_season_server.py")):
        assert os.path.dirname(os.path.abspath(S.__file__)) == os.path.abspath(modea), S.__file__
    monkeypatch.setattr(S, "INTERNAL_TOKEN", "t" * 40)
    S.ST = S.State(str(tmp_path / "srv"), 1)
    S.ST.height, S.ST.latest_time = 100, S.END - 10 * 86400
    httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(G, "INTERNAL_TOKEN", "t" * 40)
    monkeypatch.setattr(G, "INTERNAL", f"http://127.0.0.1:{httpd.server_address[1]}/internal")
    return S, httpd


def test_a_forward_whose_reply_is_lost_is_retried_and_the_programmes_copy_counts(tmp_path, monkeypatch):
    import random
    S, httpd = _real_programme(tmp_path, monkeypatch)
    real, calls = G.urllib.request.urlopen, []

    def lossy(req, timeout=0):
        calls.append(req.full_url)
        r = real(req, timeout=timeout)
        if len(calls) == 1:
            r.read()
            r.close()
            raise TimeoutError("the reply was lost after the service wrote the answer")
        return r
    monkeypatch.setattr(G.urllib.request, "urlopen", lossy)
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    try:
        out, answered = G.run_one(_FakeClient("A kettle boils water."), 0, 1, random.Random(1))
    finally:
        httpd.shutdown()
    assert answered is True and "answer forwarded" in out, out
    assert len(calls) == 2                                    # the second try found the first one's record
    assert [r["job_id"] for r in S.ST.read_pending(0)] == ["job17"]       # filed once


def test_a_forward_lost_every_time_is_unknown_and_counted_in_the_days_number(monkeypatch, capsys):
    import random
    def gone(req, timeout=0):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(G.urllib.request, "urlopen", gone)
    slept = []
    assert G.report_work("job1", "dm1m", "p", "a", sleep=slept.append) == G.UNKNOWN
    assert len(slept) == G.FORWARD_TRIES - 1
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    out, answered = G.run_one(_FakeClient("text"), 0, 1, random.Random(1))
    assert answered == G.UNKNOWN and "NOT confirmed" in out, out
    # In the loop: an unknown forward takes its place in the day's number, so the day never pays more than it.
    quota = 3
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    _, served = _drive(monkeypatch, [0] * 10 + [1], outcome=lambda k: ("served, NOT confirmed", G.UNKNOWN))
    assert served.count(0) == quota
    assert f"day 0: {quota} of {quota} answered, {quota} sent" in capsys.readouterr().out


@pytest.mark.parametrize("code,doc,tries,want", [
    (410, {"error": "the season is over"}, 1, "refused"),          # answered, not recorded: final
    (409, {"error": "this job's answer is already filed"}, 1, "refused"),   # no field: not read as recorded
    (409, {"error": "x", "already_filed": "true"}, 1, "refused"),   # only the boolean true is recorded
    (503, {}, G.FORWARD_TRIES, G.UNKNOWN),                          # busy, nothing read: tried again
])
def test_what_a_programme_answer_means_for_a_forward(monkeypatch, code, doc, tries, want):
    import io
    calls = []

    def answer(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(json.dumps(doc).encode()))
    monkeypatch.setattr(G.urllib.request, "urlopen", answer)
    if want == "refused":
        with pytest.raises(G.ForwardRefused):
            G.report_work("job1", "dm1m", "p", "a", sleep=lambda s: None)
    else:
        assert G.report_work("job1", "dm1m", "p", "a", sleep=lambda s: None) == want
    assert len(calls) == tries


def _is_form(form, prompt):
    import re
    pat = re.escape(form)
    for key in ("subject", "other", "audience", "n"):
        pat = pat.replace(re.escape("{" + key + "}"), "(.+)")
    return re.fullmatch(pat, prompt) is not None


def test_a_day_the_chain_refuses_everything_sends_its_bound_and_says_none_was_answered(monkeypatch, capsys):
    # A refused request is not an answered one: the day keeps trying, up to its bound, then stops and says so;
    # the summary reads as an empty day, never as a full one.
    quota = 4
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    bound = G.ATTEMPTS_PER_REQUEST * quota
    _, served = _drive(monkeypatch, [0] * (bound + 5) + [1], outcome=lambda k: "refused: insufficient funds")
    assert served.count(0) == bound
    out = capsys.readouterr().out
    assert out.count(f"day 0: {bound} requests sent, the day's bound") == 1
    assert f"day 0: 0 of {quota} answered, {bound} sent -- quota NOT reached" in out


def test_an_unanswered_request_is_replaced_and_the_day_counts_answers_only(monkeypatch, capsys):
    # F8: an identity present without a model is drawn, never answers, and its job stays open until it expires.
    # It does not take a share of the day's number: the next request takes its place, and the day stops at
    # exactly `requests_per_day` ANSWERED requests -- never more, so the paid work never exceeds the rule.
    quota = 5
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    silent = {2, 3, 7}                                   # the 2nd, 3rd and 7th requests go unanswered

    def outcome(k):
        if k in silent:
            return "drawn dm1nomodel, answer NOT forwarded (ValueError: no answer to forward): not counted"
        return "served by dm1x, answer forwarded"
    _, served = _drive(monkeypatch, [0] * 20 + [1], outcome=outcome)
    assert served.count(0) == quota + len(silent)        # 8 sent on day 0: 5 answered, 3 replaced
    out = capsys.readouterr().out
    assert f"day 0: {quota} of {quota} answered, {quota + len(silent)} sent" in out and "NOT reached" not in out


def test_the_answered_count_is_resumed_apart_from_the_sent_count(monkeypatch, capsys, tmp_path):
    quota = 3
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    state = str(tmp_path / "sent.json")
    G.save_sent(state, 1, 0, 4, 1)                       # before a restart: 4 sent, 1 answered
    _, served = _drive(monkeypatch, [0] * 6 + [1], state=state)
    assert served.count(0) == 2                          # the 2 answers left, not 0 (4 sent >= 3)
    out = capsys.readouterr().out
    assert "day 0: 3 answered requests, 1 answered and 4 sent before a restart" in out
    assert "day 0: 3 of 3 answered, 6 sent" in out


def test_the_requests_are_composed_not_picked_from_a_short_list():
    # A short published list is answerable from a table written once, with no model: the requests are
    # composed from a form, a subject, an audience and a length, and few of them repeat.
    import random
    rnd = random.Random(7)
    prompts = [G.make_prompt(rnd) for _ in range(2000)]
    assert all(any(_is_form(f, p) for f in G.FORMS) for p in prompts)
    assert len(set(prompts)) > 1900
    assert len(G.FORMS) * len(G.SUBJECTS) * len(G.AUDIENCES) * 5 > 10_000
    assert not hasattr(G, "PROMPTS")


def test_a_status_without_a_day_waits_and_never_crashes(monkeypatch):
    # The service reports "day": null until it has read the chain: read as a number, it would end the loop.
    urls, served = _drive(monkeypatch, [None, None, 0, 0])
    assert served == [0, 0]


def test_a_restart_resumes_the_days_count_and_never_sends_the_quota_twice(monkeypatch, capsys, tmp_path):
    # Owner's decision of 2026-10-05: a FIXED number a day. A generator restarted mid-day that counted from
    # zero would send up to twice that number on the day.
    quota = 6
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    state = str(tmp_path / "sent.json")
    _, served = _drive(monkeypatch, [0] * 4, state=state)
    assert served == [0] * 4
    with open(state, encoding="utf-8") as f:
        assert json.load(f) == {"start_height": 1, "day": 0, "sent": 4, "answered": 4}
    capsys.readouterr()
    _, served = _drive(monkeypatch, [0] * 10 + [1], state=state)             # the restart
    assert served == [0] * 2 + [1]                                          # the 2 left, then day 1
    out = capsys.readouterr().out
    assert "day 0: 6 answered requests, 4 answered and 4 sent before a restart" in out
    assert f"day 0: {quota} of {quota} answered, 6 sent" in out and "NOT reached" not in out


def test_a_count_of_another_season_or_another_day_is_not_this_days(monkeypatch, tmp_path):
    quota = 3
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    state = str(tmp_path / "sent.json")
    G.save_sent(state, 999, 0, quota, quota)                                 # another season's day 0
    assert _drive(monkeypatch, [0] * 5, state=state)[1] == [0] * quota
    G.save_sent(state, 1, 4, quota, quota)                                   # this season's day 4
    assert _drive(monkeypatch, [0] * 5, state=state)[1] == [0] * quota


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"start_height": 1, "day": 0, "sent": "4",
                                                                     "answered": 0}),
                                     json.dumps({"start_height": 1, "day": 0, "sent": 2, "answered": 5}),
                                     json.dumps({"start_height": 1, "day": 0, "answered": 0})])
def test_an_unreadable_count_stops_the_generator_and_sends_nothing(monkeypatch, capsys, tmp_path, content):
    # A count not known is not a count of zero: read as zero, it would send the whole quota again.
    state = tmp_path / "sent.json"
    state.write_text(content, encoding="utf-8")
    _, served = _drive(monkeypatch, [0] * 5, state=str(state))
    assert served == [] and _drive.rc == 2
    assert "FATAL: the count of day 0 already sent is unreadable" in capsys.readouterr().out


def test_a_count_that_cannot_be_saved_is_said_and_the_day_goes_on(monkeypatch, capsys, tmp_path):
    def unwritable(*a):
        raise PermissionError("read-only volume")
    monkeypatch.setattr(G, "save_sent", unwritable)
    monkeypatch.setitem(RULES, "requests_per_day", 2)
    _, served = _drive(monkeypatch, [0] * 3)
    assert served == [0, 0]
    assert "the day's count is NOT saved (PermissionError: read-only volume)" in capsys.readouterr().out


# --- The season ends at a fixed instant of chain time (owner's decision of 2026-10-07) ---------------------

def test_the_generators_end_is_the_rules_end_time():
    # The end is READ from the rules, not written into the generator: 7 November 2026, 23:59 UTC, exclusive.
    import calendar
    import datetime
    from final_season_rules import end_epoch
    assert G.END == end_epoch() == calendar.timegm(datetime.datetime(2026, 11, 8, 0, 0, 0).timetuple())
    assert RULES["end_time"] == "2026-11-08T00:00:00Z" and "days" not in RULES


def test_on_the_last_day_the_requests_left_are_spread_over_the_time_left():
    half = 1 + DAY // 2
    day_paced = G.spacing_s(12, 2, half, 1)
    assert G.spacing_s(12, 2, half, 1, time_left_s=None) == day_paced
    assert G.spacing_s(12, 2, half, 1, time_left_s=10 ** 9) == day_paced     # more time than day: the day paces
    assert abs(G.spacing_s(12, 2, half, 1, time_left_s=1000.0) - 100.0) < 1e-9  # 10 left over 1000 s
    assert G.spacing_s(12, 2, half, 1, time_left_s=5.0) == 1.0               # floored, never a burst
    assert G.spacing_s(12, 2, half, 1, time_left_s=0.0) == 1.0
    assert G.spacing_s(12, 2, half, 1, time_left_s=-50.0) == 1.0             # past the margin: floored
    assert G.spacing_s(12, 12, half, 1, time_left_s=1000.0) is None          # quota reached: still none


def test_the_loop_paces_the_last_day_to_the_time_left_before_the_stop_margin(monkeypatch):
    # The time left is CHAIN time (the status's latest block time) up to the stop margin, not the end itself:
    # a request opened inside the margin would settle after the end and be paid by nothing.
    monkeypatch.setitem(RULES, "requests_per_day", 7)
    seen, real = [], G.spacing_s

    def spy(*a, **k):
        r = real(*a, **k)
        seen.append((k.get("time_left_s"), r))
        return r

    monkeypatch.setattr(G, "spacing_s", spy)
    _, served = _drive(monkeypatch, [2] * 3, status=lambda i: {"latest_block_time": G.END - 1000})
    left = 1000 - G.STOP_MARGIN_S
    assert served == [2, 2, 2]
    assert seen and all(t == left for t, _ in seen)
    assert seen[0][1] == left / 7 and seen[1][1] == left / 6                 # the time left, not the day's blocks


@pytest.mark.parametrize("field,value", [
    ("ended", _DROP), ("ended", None), ("ended", "false"), ("ended", 0), ("ended", 1), ("ended", "true"),
    ("latest_block_time", _DROP), ("latest_block_time", None), ("latest_block_time", str(G.END - RUNNING)),
    ("latest_block_time", float(G.END - RUNNING)), ("latest_block_time", True),
])
def test_a_status_that_cannot_say_the_season_is_running_sends_nothing_and_waits(monkeypatch, capsys,
                                                                                 field, value):
    # "ended" unread or malformed is NOT "still running", and a chain time not read is not "far from the end":
    # nothing is sent, and the generator waits instead of declaring the season over.
    _, served = _drive(monkeypatch, [0] * 5, status=lambda i: {field: value} if i < 3 else {})
    assert served == [0, 0]                          # the 3 unreadable polls sent nothing; the 2 readable did
    assert _drive.sleeps[:3] == [60] * 3 and _drive.rc is None
    assert "no more requests" not in capsys.readouterr().out


def test_an_ended_season_sends_nothing_says_its_last_day_once_and_stays_up(monkeypatch, capsys):
    quota = 7
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    urls, served = _drive(monkeypatch, [3] * 3 + [4] * 6,
                          status=lambda i: {} if i < 3 else {"ended": True, "latest_block_time": G.END + 60})
    assert served == [3, 3, 3]                       # nothing after the end, not even on a new day
    assert _drive.rc is None and len(urls) == 9 + 1  # never returned: it kept polling to the bench's stop
    assert _drive.sleeps == [600] * 6
    out = capsys.readouterr().out
    assert out.count("(the last day it sent requests on)") == 1
    assert f"day 3 (the last day it sent requests on): 3 of {quota} answered, 3 sent" in out
    assert out.count(f"the season ends at {RULES['end_time']}: no more requests") == 1
    assert "day 4" not in out


def test_a_season_already_ended_at_start_sends_nothing_and_names_no_day(monkeypatch, capsys):
    _, served = _drive(monkeypatch, [31] * 4, status=lambda i: {"ended": True, "latest_block_time": G.END + 5})
    assert served == [] and _drive.rc is None and _drive.sleeps == [600] * 4
    out = capsys.readouterr().out
    assert "(the last day it sent requests on)" not in out and out.count("no more requests") == 1


@pytest.mark.parametrize("before_end,sends", [
    (G.STOP_MARGIN_S + 1, True),                     # one second outside the margin: still sending
    (G.STOP_MARGIN_S, False), (G.STOP_MARGIN_S - 1, False), (1, False),
    (0, False), (-30, False),                        # chain time at/after the end while "ended" still reads false
])
def test_no_request_is_opened_within_the_stop_margin_of_the_end(monkeypatch, capsys, before_end, sends):
    # A request counts on the block it settles: opened within STOP_MARGIN_S of the end, it would be served
    # after it and paid by nothing.
    _, served = _drive(monkeypatch, [9] * 3, status=lambda i: {"latest_block_time": G.END - before_end})
    out = capsys.readouterr().out
    if sends:
        assert served == [9] * 3 and "no more requests" not in out
    else:
        assert served == [] and _drive.rc is None and _drive.sleeps == [600] * 3
        assert out.count("no more requests") == 1


def test_reaching_the_stop_margin_mid_day_stops_the_day_and_says_its_count_once(monkeypatch, capsys):
    quota = 7
    monkeypatch.setitem(RULES, "requests_per_day", quota)
    _, served = _drive(monkeypatch, [5] * 6,
                       status=lambda i: {} if i < 2 else {"latest_block_time": G.END - G.STOP_MARGIN_S // 2})
    assert served == [5, 5] and _drive.rc is None
    out = capsys.readouterr().out
    assert out.count("(the last day it sent requests on)") == 1 and f"day 5 (the last day it sent requests on): 2 of {quota} answered, 2 sent" in out
