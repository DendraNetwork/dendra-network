"""Bench of the Final Testnet Season service, through real HTTP, with the signature and the chain
replaced by fakes.

What is faked, and why that does not hollow the bench: the SIGNATURE is the relay's (`verify_write`),
benched on its own with a real binary; here it is replaced by a verifier that accepts one known key, so
the bench can play a miner and a stranger. The CHAIN is a fixed height, the header time of its latest
block (ten days before the season's end unless a test says otherwise) and a registry of two miners. What
is NOT faked: the routes, the payout declaration and its limits, the private file of the answers to
programme requests and the draw of their sample, the evidence written, the grading routes and their
closing on a final or ranked day or after the season's end, the file routes, and the background workers
the service starts.
"""
import importlib
import json
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)

import pytest  # noqa: E402

OPERATORS = {"m1": "dendra1operatorone", "m2": "dendra1operatortwo"}
TOKEN = "t" * 40                                     # the internal token of the generator
GRADER = "g" * 40                                    # the grader's token
AUTH = {"Authorization": "Bearer " + GRADER}


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_DATA", str(tmp_path))
    monkeypatch.setenv("DENDRA_FINAL_SEASON_START_HEIGHT", "1")
    monkeypatch.setenv("DENDRA_TRUST_PROXY", "1")
    import final_season_server as S
    importlib.reload(S)

    class Reg:
        # The real registry's interface (`final_season_server._Registry`): both reads. Self-registered miners,
        # one key in both places, as the chain names them.
        def operator(self, mid):
            return (OPERATORS.get(mid), "FEES")

        def creator(self, mid):
            return (OPERATORS.get(mid), "FEES")

    S.REGISTRY = Reg()
    S.ST = S.State(str(tmp_path), 1)
    S.ST.height = 1000
    S.ST.latest_time = S.END - 10 * 86400     # a running season: the chain's time is read, before the end
    S._RATE.clear()

    def fake_signed(self, kind, key, body):
        mid = self.headers.get("X-Dendra-Miner")
        if self.headers.get("X-Test-Sig") != "valid":
            return None, "SIGNATURE_INVALIDE (test)"
        named = key.rsplit("__", 1)[1] if "__" in key else None
        if named != mid:
            return None, "key/header mismatch"
        return mid, OPERATORS[mid]

    monkeypatch.setattr(S.Handler, "_signed", fake_signed)
    httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/final-season/v1/"
    yield S, base
    httpd.shutdown()


def call(base, route, body=None, miner=None, sig="valid", ip="203.0.113.9", headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + route, data=data, method="POST" if data else "GET")
    req.add_header("X-Forwarded-For", ip)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if miner:
        req.add_header("X-Dendra-Miner", miner)
        req.add_header("X-Test-Sig", sig)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw[:1] == b"{" else raw)


def get_raw(base, route):
    """A published file as served, bytes untouched: a day of evidence is JSON lines, not one document."""
    try:
        with urllib.request.urlopen(base + route, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _internal(base, body, token, path="work_answer"):
    url = base.split("/final-season/")[0] + "/internal/" + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    if token is not None:
        req.add_header("X-Final-Season-Internal", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def work(job_id, miner_id="m1", prompt="p", answer="a"):
    return {"job_id": job_id, "miner_id": miner_id, "prompt": prompt, "answer": answer}


def good_address(first=0):
    from modea import cosmos_addr
    return cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes(range(first, first + 20)), 8, 5))


def _end_of_day(S, day):
    S.ST.height = S.day_bounds(day, 1)[1] + 1


def _rank(S, day):
    rdir = os.path.join(S.ST.data, "ranking")
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, f"day-{day:03d}.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    return os.path.join(rdir, f"day-{day:03d}.json")


class _Stop(Exception):
    pass


def _loop(S, monkeypatch, worker, iterations=1):
    """Runs a background worker for `iterations` turns of its loop: its sleep raises after that many."""
    calls = []

    def sleep(_s):
        calls.append(_s)
        if len(calls) > iterations:
            raise _Stop

    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=time.time, monotonic=time.monotonic))
    with pytest.raises(_Stop):
        worker()


def _sealed_day(S, base, monkeypatch, *docs):
    """Files `docs` as answers of the current day, ends the day and draws its sample."""
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    for d in docs:
        assert _internal(base, d, TOKEN)[0] == 200
    day = S.ST.day_now()
    _end_of_day(S, day)
    S.seal_work_day(day)
    return day


# ── status ──────────────────────────────────────────────────────────────────────────────────
def test_status_publishes_the_rules_and_their_fingerprint(srv):
    S, base = srv
    code, st = call(base, "status")
    assert code == 200
    assert st["season"] == S.RULES["season"] and st["start_height"] == 1 and st["height"] == 1000
    assert st["day"] == S.day_of_height(1000, 1) == 0
    assert st["rules"] == S.RULES and st["rules_fingerprint"] == S.fingerprint()
    # what a reader does with them: the rules served hash to the fingerprint served
    assert S.fingerprint(st["rules"]) == st["rules_fingerprint"]
    # and the rules served give the end the service applies; no day count is published as a rule
    assert S.end_epoch(st["rules"]) == S.END and "days" not in st["rules"]
    # Exactly these keys: no test is set, so nothing of one is published. `window_blocks` is the CHAIN's
    # availability window, read from its params (the launcher refuses a chain where it is 0). The end is
    # a reading of the chain: its latest header time, whether it is past the rule, and — once it is — the
    # season's last block and the day that holds it.
    # `unwound_audit_work_from_day`: the first day the rules served apply to (decision 18), fixed when the
    # service first started with them; a day before it was ranked under the rules before.
    assert set(st) == {"season", "start_height", "height", "day", "rules", "rules_fingerprint", "window_blocks",
                       "latest_block_time", "ended", "end_height", "last_day", "unwound_audit_work_from_day"}
    assert st["unwound_audit_work_from_day"] == 0                      # a fresh service: no day ranked yet
    assert st["latest_block_time"] == S.END - 10 * 86400 and st["ended"] is False
    assert st["end_height"] is None and st["last_day"] is None
    assert call(base, "")[1] == st


def test_status_reports_the_chain_window_and_none_before_it_is_read(srv, monkeypatch):
    S, base = srv
    S.ST.eb = None
    assert call(base, "status")[1]["window_blocks"] is None          # unknown, never a default of 0
    monkeypatch.setattr(S.C, "latest", lambda rpc: (1000, S.END - 10 * 86400))
    # before the end, the season's last block is not searched for
    monkeypatch.setattr(S.C, "season_end_height", lambda *a: (_ for _ in ()).throw(AssertionError(a)))
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {"avail_epoch_blocks": "288"})
    monkeypatch.setattr(S.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        S.watch_chain()
    assert call(base, "status")[1]["window_blocks"] == 288


def test_status_names_no_day_while_the_height_is_unknown(srv):
    S, base = srv
    S.ST.height = 0
    code, st = call(base, "status")
    assert code == 200 and st["height"] == 0 and st["day"] is None


def test_status_says_whether_the_season_is_over_in_three_states_never_two(srv):
    # An unread time is not "still running": `ended` is null until the chain's time is read, and false
    # is a reading, not a default.
    S, base = srv
    S.ST.latest_time = None
    st = call(base, "status")[1]
    assert st["latest_block_time"] is None and st["ended"] is None
    assert st["end_height"] is None and st["last_day"] is None
    S.ST.latest_time = S.END - 1                                      # one second before the end
    st = call(base, "status")[1]
    assert st["latest_block_time"] == S.END - 1 and st["ended"] is False
    S.ST.latest_time = S.END                                          # the end is EXCLUSIVE: at it, over
    st = call(base, "status")[1]
    assert st["ended"] is True
    # the season's last block is published only once found on the chain, never estimated meanwhile
    assert st["end_height"] is None and st["last_day"] is None
    S.ST.end_height = S.day_bounds(3, 1)[0] + 5
    S.ST.height = S.day_bounds(4, 1)[0]
    st = call(base, "status")[1]
    assert st["end_height"] == S.ST.end_height and st["last_day"] == 3
    # the day keeps counting past the end; `ended` and `last_day` say the season is over
    assert st["day"] == 4 and st["ended"] is True


# ── routes that do not exist ───────────────────────────────────────────────────────────────────
def test_no_route_serves_a_question_an_answer_or_an_exam(srv):
    # Owner's decisions of 2026-10-05: the programme sets no test of its own. Every one of these answers
    # 404, after the shape and height checks a real route would pass, and nothing is written for it.
    S, base = srv
    day = S.ST.day_now()
    assert call(base, "question?miner_id=m1")[0] == 404
    assert call(base, "question")[0] == 404
    assert call(base, "answer", {"window": 864, "miner_id": "m1", "answer": "x"}, miner="m1")[0] == 404
    assert call(base, "exam/start", {"miner_id": "m1"}, miner="m1")[0] == 404
    assert call(base, "exam", {"miner_id": "m1", "attempt": 1, "answers": {}}, miner="m1")[0] == 404
    assert call(base, "exam?miner_id=m1")[0] == 404
    assert S.ST.ev.read(day) == [] and not S.ST.ev.days()
    for name in ("close_windows", "close_window", "find_copies", "Q", "X"):
        assert not hasattr(S, name), name
    assert not hasattr(S.ST, "class_of") and not hasattr(S.ST, "exam_class")


def test_there_is_no_node_declaration(srv):
    # Owner's decision of 2026-10-04: no public-node reward in the Final Testnet Season, so no route, no
    # probe, no file.
    S, base = srv
    assert call(base, "node", {"miner_id": "m1", "rpc": "http://node.example:26657"}, miner="m1")[0] == 404
    assert not hasattr(S, "probe_nodes") and not hasattr(S.ST, "nodes")
    assert not os.path.exists(os.path.join(S.ST.data, "nodes.json"))


def test_there_is_no_trap_route(srv, monkeypatch):
    # Owner's decision of 2026-10-04: no trap promotion, so nothing can file a trap result.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    trap = {"miner_id": "m1", "job_id": "j1", "level": 3, "correct": True}
    assert _internal(base, trap, TOKEN, path="trap")[0] == 404
    assert not S.ST.ev.days()


# ── payout declaration ─────────────────────────────────────────────────────────────────────────
def test_payout_address_must_be_payable_and_declared_at_most_hourly(srv):
    S, base = srv
    from modea import cosmos_addr
    assert call(base, "payout", {"miner_id": "m1", "address": "cosmos1xyz"}, miner="m1")[0] == 400
    blocked = S.payable_address.__globals__["module_address"]("fee_collector")
    code, r = call(base, "payout", {"miner_id": "m1", "address": blocked}, miner="m1")
    assert code == 400 and "module" in r["error"]
    empty = cosmos_addr.bech32_encode("dendra", [])
    assert call(base, "payout", {"miner_id": "m1", "address": empty}, miner="m1")[0] == 400
    good = good_address()
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 200
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 429
    assert call(base, "payout", {"miner_id": "m2", "address": good}, miner="m2")[0] == 200   # per identity
    S.ST.height += S.DECLARE_EVERY_BLOCKS
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 200
    recs = [r for r in S.ST.ev.read(0) if r["miner_id"] == "m1"]
    assert recs == [{"type": "payout", "miner_id": "m1", "address": good, "height": 1000, "day": 0},
                    {"type": "payout", "miner_id": "m1", "address": good,
                     "height": 1000 + S.DECLARE_EVERY_BLOCKS, "day": 0}]
    # the record carries no address of the client: the programme pays nothing on one
    assert "203.0.113.9" not in open(S.ST.ev.path(0), encoding="utf-8").read()


def test_payout_address_is_stored_in_lowercase_and_a_capitalised_module_account_refused(srv):
    S, base = srv
    blocked = S.payable_address.__globals__["module_address"]("fee_collector")
    code, r = call(base, "payout", {"miner_id": "m1", "address": blocked.upper()}, miner="m1")
    assert code == 400 and "module" in r["error"]
    good = good_address()
    assert call(base, "payout", {"miner_id": "m1", "address": good.upper()}, miner="m1")[0] == 200
    rec = [r for r in S.ST.ev.read(S.ST.day_now()) if r["type"] == "payout"][-1]
    assert rec["address"] == good


def test_a_payout_declaration_is_signed_by_the_identity_it_names(srv):
    S, base = srv
    good = good_address()
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1", sig="bad")[0] == 401
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m2")[0] == 401
    assert call(base, "payout", {"miner_id": "m1", "address": good})[0] == 401
    # the signature is checked before anything the request carries: an unsigned bad address is a 401
    assert call(base, "payout", {"miner_id": "m1", "address": "cosmos1xyz"}, miner="m1", sig="bad")[0] == 401
    # a refused declaration writes nothing and spends nothing of the hourly allowance
    assert not S.ST.ev.days() and ("payout", "m1") not in S.ST.declared
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 200


def test_the_hourly_limit_survives_a_restart(srv):
    S, base = srv
    good = good_address()
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 200
    again = S.State(S.ST.data, 1)
    assert again.declared[("payout", "m1")] == 1000
    again.height = 1000 + S.DECLARE_EVERY_BLOCKS - 1
    S.ST = again
    # the chain's time and the season's last block are not kept across a restart: both are read again,
    # and nothing is declared before the time is
    assert again.latest_time is None and again.end_height is None
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 425
    again.latest_time = S.END - 10 * 86400
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 429
    S.ST.height += 1
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 200


def test_nothing_is_written_while_the_height_is_unknown_or_before_the_season(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    S.ST.height = 0
    code, r = call(base, "payout", {"miner_id": "m1", "address": good_address()}, miner="m1")
    assert code == 503 and "height" in r["error"]
    assert _internal(base, work("j0"), TOKEN)[0] == 425
    S.ST.height, S.ST.start = 1000, 5000                               # before the season's first block
    assert _internal(base, work("j1"), TOKEN)[0] == 425
    assert not S.ST.ev.days() and not S.ST.pending_days() and not S.ST.work_pending
    assert ("payout", "m1") not in S.ST.declared


def test_answers_and_declarations_wait_for_the_chains_time_and_stop_at_the_end(srv, monkeypatch):
    # 425 while the latest block's time is unread (the height alone does not say the season still runs),
    # 410 from the first block at or after the end: a declaration then applies to no day, and an answer
    # would join the last day's sample. Neither refusal writes anything or spends the hourly allowance.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    good = good_address()
    S.ST.latest_time = None
    code, r = call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")
    assert code == 425 and "time" in r["error"]
    assert _internal(base, work("j1"), TOKEN)[0] == 425
    S.ST.latest_time = S.END                                          # the end is EXCLUSIVE
    code, r = call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")
    assert code == 410 and "over" in r["error"]
    assert _internal(base, work("j2"), TOKEN)[0] == 410
    S.ST.latest_time = S.END + 3600
    assert call(base, "payout", {"miner_id": "m2", "address": good}, miner="m2")[0] == 410
    assert _internal(base, work("j3", miner_id="m2"), TOKEN)[0] == 410
    # after the end as before it, the signature and the address are checked first
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1", sig="bad")[0] == 401
    assert call(base, "payout", {"miner_id": "m1", "address": "cosmos1xyz"}, miner="m1")[0] == 400
    assert not S.ST.ev.days() and not S.ST.pending_days() and not S.ST.work_pending
    assert not S.ST.declared
    S.ST.latest_time = S.END - 1                                      # one second before: still running
    assert call(base, "payout", {"miner_id": "m1", "address": good}, miner="m1")[0] == 200
    assert _internal(base, work("j4"), TOKEN)[0] == 200
    assert [r["job_id"] for r in S.ST.read_pending(S.ST.day_now())] == ["j4"]


def test_a_malformed_miner_id_is_refused_before_anything_reads_it(srv, monkeypatch):
    S, base = srv
    seen = []
    monkeypatch.setattr(S.Handler, "_signed", lambda self, kind, key, body: seen.append(key) or (None, "x"))
    for bad in ("../../x", "", "m1/../m2", "-m1", "x" * 65):
        S.ST.height = 1000
        assert call(base, "payout", {"miner_id": bad, "address": good_address()}, miner="m1")[0] == 400, bad
        S.ST.height = 0                                                # the shape comes before the height
        assert call(base, "payout", {"miner_id": bad, "address": good_address()}, miner="m1")[0] == 400, bad
    assert seen == [] and not S.ST.ev.days()


def test_posts_are_rate_limited_per_address(srv):
    S, base = srv
    codes = [call(base, "payout", {"miner_id": "m1", "address": "x"}, miner="m1", ip="198.51.100.77")[0]
             for _ in range(S.POST_PER_MINUTE + 2)]
    assert codes[-1] == 429 and 429 not in codes[:S.POST_PER_MINUTE]
    assert call(base, "payout", {"miner_id": "m1", "address": "x"}, miner="m1", ip="198.51.100.78")[0] == 400


# ── answers to programme requests ──────────────────────────────────────────────────────────────
def _reordering_secret(S, jobs):
    """A draw key under which the sample is not the first answers to arrive. With the random key of the
    fixture, the draw of 3 among 5 is the first three one run in ten, and the bench could not tell the
    secret's order from the order of arrival."""
    for s in range(1, 256):
        S.ST.secret = bytes([s]) * 32
        drawn = sorted(jobs, key=S.ST.work_order)[:S.WORK_SAMPLE]
        if set(drawn) != set(jobs[:S.WORK_SAMPLE]):
            return drawn
    raise AssertionError("no key draws other than the first answers")


def test_work_answers_are_kept_privately_and_drawn_only_after_the_day(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    doc = work("job1", prompt="Explain a compass.", answer="x" * 9000)
    assert _internal(base, doc, None)[0] == 403
    assert _internal(base, doc, "wrong")[0] == 403
    assert _internal(base, dict(doc, job_id="a__b"), TOKEN)[0] == 400
    assert _internal(base, dict(doc, job_id="a/b"), TOKEN)[0] == 400
    assert _internal(base, dict(doc, miner_id="../x"), TOKEN)[0] == 400
    assert _internal(base, dict(doc, answer=None), TOKEN)[0] == 400
    assert _internal(base, {k: v for k, v in doc.items() if k != "prompt"}, TOKEN)[0] == 400
    assert not S.ST.pending_days()
    code, r = _internal(base, doc, TOKEN)
    assert code == 200 and r == {"ok": True}                            # the answer says nothing about a draw
    assert _internal(base, doc, TOKEN)[0] == 409                       # one job, one filing
    assert _internal(base, dict(doc, miner_id="m2"), TOKEN)[0] == 409  # whoever claims it
    jobs = [f"job{n}" for n in range(1, 6)]
    for jid in jobs[1:]:
        assert _internal(base, dict(doc, job_id=jid), TOKEN)[0] == 200
    assert _internal(base, dict(doc, job_id="other", miner_id="m2"), TOKEN)[0] == 200
    day = S.ST.day_now()
    # WHILE THE DAY RUNS, NOTHING IS PUBLIC: the evidence a miner can read carries no work answer at all,
    # so it cannot tell when its sample is complete.
    assert not [x for x in S.ST.ev.read(day) if x["type"] == "work_answer"]
    assert oct(os.stat(S.ST.pending_path(day)).st_mode & 0o777) == "0o600"
    # the private file lives outside the served evidence directory
    assert os.path.dirname(S.ST.pending_path(day)) != S.ST.ev.dir
    # ONCE THE DAY IS OVER, the draw is made in the secret's order, not in the order of arrival.
    want = _reordering_secret(S, jobs)
    _end_of_day(S, day)
    S.seal_work_day(day)
    recs = [x for x in S.ST.ev.read(day) if x["type"] == "work_answer"]
    m1 = [x["job_id"] for x in recs if x["miner_id"] == "m1"]
    assert sorted(m1) == sorted(want) and len(m1) == S.WORK_SAMPLE == 3
    assert [x["job_id"] for x in recs if x["miner_id"] == "m2"] == ["other"]
    one = next(x for x in recs if x["miner_id"] == "m1")
    assert len(one["answer"]) == S.MAX_ANSWER_CHARS and one["prompt"] == doc["prompt"]
    assert one["height"] == 1000 and one["day"] == day
    sealed = [x for x in S.ST.ev.read(day) if x["type"] == "work_sealed"]
    # the seal lists every job whose answer was received: the only programme requests that count as work
    assert sealed == [{"type": "work_sealed", "answers": 6, "drawn": 4, "day": day,
                       "answered": {"m1": sorted(jobs), "m2": ["other"]}}]
    assert not os.path.exists(S.ST.pending_path(day))                  # the private file is gone
    assert not S.ST.work_pending and day in S.ST.work_sealed
    assert _internal(base, dict(doc, job_id=want[0]), TOKEN)[0] == 409  # a drawn job is filed once
    # the sample survives a restart: the count, the filed jobs and the seal are rebuilt from the evidence
    again = S.State(S.ST.data, 1)
    assert again.work_count[(day, "m1")] == 3 and want[0] in again.work and day in again.work_sealed
    assert not again.work_pending


def test_the_draw_order_is_set_by_the_secret(srv):
    # A miner knows its job ids as they are served; an order computed from them alone would tell it which
    # of its answers are graded.
    S, _ = srv
    jobs = [f"job{n}" for n in range(8)]
    orders = set()
    for s in (1, 2, 3):
        S.ST.secret = bytes([s]) * 32
        orders.add(tuple(sorted(jobs, key=S.ST.work_order)))
    assert len(orders) == 3


def test_the_draw_key_is_private_and_kept_across_restarts(srv):
    S, _ = srv
    path = os.path.join(S.ST.data, "secret.bin")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    with open(path, "rb") as f:
        key = f.read()
    assert len(key) == 32 and S.State(S.ST.data, 1).secret == key == S.ST.secret
    with open(path, "wb") as f:
        f.write(key[:31])
    with pytest.raises(SystemExit):                                    # never replaced by a fresh one
        S.State(S.ST.data, 1)


def test_the_private_file_is_bounded_per_identity_and_the_answer_says_nothing(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    for n in range(S.WORK_PENDING_MAX + 2):
        assert _internal(base, work(f"j{n}"), TOKEN) == (200, {"ok": True})
    assert _internal(base, work("other", miner_id="m2"), TOKEN) == (200, {"ok": True})
    day = S.ST.day_now()
    kept = S.ST.read_pending(day)
    # every answer is recorded as received; only the first WORK_PENDING_MAX of an identity keep their text
    m1 = [r for r in kept if r["miner_id"] == "m1"]
    assert [r["job_id"] for r in m1] == [f"j{n}" for n in range(S.WORK_PENDING_MAX + 2)]
    assert [r["job_id"] for r in m1 if "answer" in r] == [f"j{n}" for n in range(S.WORK_PENDING_MAX)]
    assert [r["job_id"] for r in kept if r["miner_id"] == "m2" and "answer" in r] == ["other"]  # not crowded out
    # the seal draws only from answers with their text, and lists every one as answered
    _end_of_day(S, day)
    S.seal_work_day(day)
    seal = [x for x in S.ST.ev.read(day) if x["type"] == "work_sealed"][0]
    assert seal["answered"]["m1"] == sorted(r["job_id"] for r in m1) and seal["answers"] == len(kept)
    drawn = {x["job_id"] for x in S.ST.ev.read(day) if x["type"] == "work_answer"}
    assert drawn and not drawn & {f"j{S.WORK_PENDING_MAX}", f"j{S.WORK_PENDING_MAX + 1}"}


class _SpyLock:
    """`ST.lock` that says when a thread starts waiting for it."""

    def __init__(self):
        self.inner = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self):
        self.waiting.set()
        self.inner.acquire()
        return self

    def __exit__(self, *exc):
        self.inner.release()


def test_the_day_of_an_answer_is_read_under_the_lock_the_seal_takes(srv, monkeypatch):
    # An answer read as day d before a boundary and written after the seal of d would land in a file that
    # nobody seals again. The bench holds the lock, moves the chain past the boundary while the answer
    # waits for it, and requires the answer filed under the day it is written in.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    spy = _SpyLock()
    S.ST.lock = spy
    day = S.ST.day_now()
    got = []
    spy.inner.acquire()
    t = threading.Thread(target=lambda: got.append(_internal(base, work("jx"), TOKEN)))
    t.start()
    try:
        assert spy.waiting.wait(10)
        _end_of_day(S, day)
    finally:
        spy.inner.release()
    t.join(10)
    assert got == [(200, {"ok": True})]
    assert not os.path.exists(S.ST.pending_path(day))
    assert [r["job_id"] for r in S.ST.read_pending(day + 1)] == ["jx"]
    assert S.ST.work_pending["jx"] == (day + 1, "m1")


def test_a_torn_line_costs_one_answer_and_a_crashed_seal_files_nothing_twice(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert _internal(base, work("ja"), TOKEN)[0] == 200
    day = S.ST.day_now()
    with open(S.ST.pending_path(day), "a", encoding="utf-8") as f:
        f.write('{"job_id": "torn", "miner_id": "m1"')          # a crash in the middle of a line
    assert _internal(base, work("jb"), TOKEN)[0] == 200
    assert sorted(r["job_id"] for r in S.ST.read_pending(day)) == ["ja", "jb"]   # jb did not die with it
    # a restart reads the same two answers, not the torn line
    assert sorted(S.State(S.ST.data, 1).work_pending) == ["ja", "jb"]
    # a seal that stopped after filing "ja" but before recording itself: the next one files it once
    S.ST.ev.append(day, {"type": "work_answer", "job_id": "ja", "miner_id": "m1", "height": 1000,
                         "prompt": "p", "answer": "a"})
    S.ST.work["ja"] = (day, "m1")
    _end_of_day(S, day)
    S.seal_work_day(day)
    ids = [x["job_id"] for x in S.ST.ev.read(day) if x["type"] == "work_answer"]
    assert sorted(ids) == ["ja", "jb"] and ids.count("ja") == 1


def test_a_torn_evidence_line_does_not_swallow_the_next_record(srv):
    # The public evidence log: a crash mid-line leaves a torn last line; the next record is written on a
    # line of its own, so only the torn one is lost, and said.
    S, _ = srv
    S.ST.ev.append(0, {"type": "payout", "miner_id": "m1", "address": "a1", "height": 5})
    with open(S.ST.ev.path(0), "a", encoding="utf-8") as f:
        f.write('{"type": "payout", "miner_id": "m2"')
    S.ST.ev.append(0, {"type": "payout", "miner_id": "m3", "address": "a3", "height": 6})
    recs = S.ST.ev.read(0)
    assert [r["miner_id"] for r in recs if r["type"] == "payout"] == ["m1", "m3"]
    assert [r for r in recs if r["type"] == "_unreadable_lines"] == [{"type": "_unreadable_lines", "count": 1, "day": 0}]


def test_a_pending_answer_survives_a_restart_and_is_refused_twice(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert _internal(base, work("jr"), TOKEN)[0] == 200
    again = S.State(S.ST.data, 1)
    assert again.work_pending["jr"] == (S.ST.day_now(), "m1")
    again.height, again.latest_time = S.ST.height, S.ST.latest_time
    S.ST = again
    assert _internal(base, work("jr"), TOKEN)[0] == 409
    assert [r["job_id"] for r in S.ST.read_pending(S.ST.day_now())] == ["jr"]


def test_a_day_sealed_once_it_is_ranked_takes_nothing_more(srv, monkeypatch):
    # A ranking stands as published: a late seal must not add answers a reader recomputing it would not
    # have had.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert call(base, "payout", {"miner_id": "m1", "address": good_address()}, miner="m1")[0] == 200
    assert _internal(base, work("ja"), TOKEN)[0] == 200
    day = S.ST.day_now()
    _end_of_day(S, day)
    _rank(S, day)
    before = S.ST.ev.read(day)
    assert [r["type"] for r in before] == ["payout"]
    S.seal_work_day(day)
    assert S.ST.ev.read(day) == before
    assert "ja" not in S.ST.work and day in S.ST.work_sealed
    assert not os.path.exists(S.ST.pending_path(day)) and not S.ST.work_pending


def test_a_day_final_but_not_ranked_still_takes_its_seal(srv, monkeypatch):
    # A restart can leave a day unsealed past its finality. Its seal is the service's own record of the
    # answers it received during the day, which the ranking waits for (`rank_days`): without it the day
    # would pay no work at all.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert _internal(base, work("ja"), TOKEN)[0] == 200
    day = S.ST.day_now()
    S.ST.finality = 100
    S.ST.height = S.day_bounds(day, 1)[1] + 100
    S.seal_work_day(day)
    seal = [x for x in S.ST.ev.read(day) if x["type"] == "work_sealed"]
    assert seal and seal[0]["answered"] == {"m1": ["ja"]}
    assert day in S.ST.work_sealed and not os.path.exists(S.ST.pending_path(day))


def test_internal_route_is_closed_when_no_token_is_configured(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", "")
    assert _internal(base, work("j"), "")[0] == 403
    assert _internal(base, work("j"), None)[0] == 403
    assert not S.ST.pending_days()


# ── grading ───────────────────────────────────────────────────────────────────────────────────
def test_grading_routes_need_the_grader_token_and_take_work_grades_only(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    assert call(base, "payout", {"miner_id": "m1", "address": good_address()}, miner="m1")[0] == 200
    day = _sealed_day(S, base, monkeypatch, work("job1", prompt="Explain a compass.", answer="a"),
                      work("job2", miner_id="m2", prompt="q", answer="b"))
    assert {r["type"] for r in S.ST.ev.read(day)} == {"payout", "work_answer", "work_sealed"}
    assert call(base, f"grading/sample?day={day}")[0] == 403
    assert call(base, f"grading/sample?day={day}", headers={"Authorization": "Bearer wrong"})[0] == 403
    assert call(base, "grading/sample", headers=AUTH)[0] == 400          # the day is required
    assert call(base, "grading/sample?day=x", headers=AUTH)[0] == 400
    code, d = call(base, f"grading/sample?day={day}", headers=AUTH)
    # the sample lists the sampled answers to programme requests and nothing else of the day
    assert code == 200 and d["day"] == day
    assert sorted(d["sample"], key=lambda x: x["job_id"]) == [
        {"kind": "work", "job_id": "job1", "miner_id": "m1", "prompt": "Explain a compass.", "answer": "a"},
        {"kind": "work", "job_id": "job2", "miner_id": "m2", "prompt": "q", "answer": "b"}]
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": False, "model": "m"}
    assert call(base, "grading/result", res)[0] == 403
    assert call(base, "grading/result", res, headers={"Authorization": "Bearer wrong"})[0] == 403
    # a grade of a window: the programme sets no window, so it has no kind to file it under
    code, r = call(base, "grading/result", dict(res, kind="window", window=864), headers=AUTH)
    assert code == 400 and "work" in r["error"]
    assert call(base, "grading/result", {k: v for k, v in res.items() if k != "kind"}, headers=AUTH)[0] == 400
    assert call(base, "grading/result", dict(res, coherent="false"), headers=AUTH)[0] == 400   # not a boolean
    assert call(base, "grading/result", dict(res, miner_id="../x"), headers=AUTH)[0] == 400
    assert call(base, "grading/result", dict(res, miner_id="m2"), headers=AUTH)[0] == 404      # not m2's job
    assert call(base, "grading/result", dict(res, job_id="nope"), headers=AUTH)[0] == 404
    assert call(base, "grading/result", dict(res, job_id=7), headers=AUTH)[0] == 404
    assert not [x for x in S.ST.ev.read(day) if x["type"] == "work_grade"]
    assert call(base, "grading/result", res, headers=AUTH)[0] == 200
    assert call(base, "grading/result", res, headers=AUTH)[0] == 409
    assert call(base, "grading/result", dict(res, coherent=True), headers=AUTH)[0] == 409       # not revised
    g = [x for x in S.ST.ev.read(day) if x["type"] == "work_grade"]
    assert g == [{"type": "work_grade", "job_id": "job1", "miner_id": "m1", "coherent": False, "model": "m",
                  "day": day}]
    code, d = call(base, f"grading/sample?day={day}", headers=AUTH)
    assert [x["job_id"] for x in d["sample"]] == ["job2"]                # graded: not offered again
    # the grade survives a restart: a restarted service neither offers nor takes it again
    assert "job1" in S.State(S.ST.data, 1).work_graded


def test_grading_routes_are_closed_when_no_token_is_configured(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", "")
    for auth in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer"}):
        assert call(base, "grading/sample?day=0", headers=auth)[0] == 403
        res = {"kind": "work", "job_id": "j", "miner_id": "m1", "coherent": True}
        assert call(base, "grading/result", res, headers=auth)[0] == 403


def test_a_ranked_or_final_day_takes_no_more_grades(srv, monkeypatch):
    # A published day must recompute identically from its published evidence: a grade filed after the
    # ranking would change what a reader recomputes.
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    day = _sealed_day(S, base, monkeypatch, work("job1"))
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": False}
    ranked = _rank(S, day)
    code, r = call(base, f"grading/sample?day={day}", headers=AUTH)
    assert code == 409 and "ranked" in r["error"]
    assert call(base, "grading/result", res, headers=AUTH)[0] == 409
    os.remove(ranked)
    # final but not ranked yet: closed too (the ranking may already be reading the evidence)
    S.ST.finality = 100
    S.ST.height = S.day_bounds(day, 1)[1] + 100
    assert call(base, f"grading/sample?day={day}", headers=AUTH)[0] == 409
    assert call(base, "grading/result", res, headers=AUTH)[0] == 409
    assert not [x for x in S.ST.ev.read(day) if x["type"] == "work_grade"]
    assert "job1" not in S.ST.work_graded                              # a refusal spends nothing
    S.ST.height -= 1                                                   # one block before: still open
    assert call(base, f"grading/sample?day={day}", headers=AUTH)[0] == 200
    assert call(base, "grading/result", res, headers=AUTH)[0] == 200


def test_once_the_ranking_has_read_a_final_day_its_grades_are_refused(srv, monkeypatch):
    # The ranking's read and a grade's filing share one lock: a grade lands before the read (and is ranked)
    # or after it (and is refused), never in the gap between the read and the published file.
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    day = _sealed_day(S, base, monkeypatch, work("job1"))
    S.ST.height = S.day_bounds(day, 1)[1] + 50
    assert S.ST.finality is None
    height, records = S.ranking_inputs(day, 50)
    assert height == S.ST.height and any(r["type"] == "work_answer" for r in records)
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": False}
    assert call(base, "grading/result", res, headers=AUTH)[0] == 409


def test_the_last_day_is_final_at_the_seasons_last_block_and_no_day_follows_it(srv, monkeypatch):
    # The day that holds the season's last block is cut there: it is final `finality` blocks after THAT
    # block — the window the ranking uses — not after its full 17 280 blocks. A day after it is no
    # season day: refused, never offered as an empty one.
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert _internal(base, work("job1"), TOKEN)[0] == 200
    assert _internal(base, work("job2", miner_id="m2"), TOKEN)[0] == 200
    day = S.ST.day_now()
    # the chain passes the end at block 1501: the season's last block is 1500, inside day 0
    S.ST.latest_time, S.ST.end_height, S.ST.height = S.END, 1500, 1501
    assert S.ST.last_day() == day == 0
    S.seal_work_day(day)
    S.ST.finality = 100
    for after in (day + 1, day + 7):
        code, r = call(base, f"grading/sample?day={after}", headers=AUTH)
        assert code == 409 and "after the season's end" in r["error"], after
    S.ST.height = 1500 + 100 - 1                                      # one block before: still open
    assert S.ST.day_closed(day) == ""
    assert call(base, f"grading/sample?day={day}", headers=AUTH)[0] == 200
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": False}
    assert call(base, "grading/result", res, headers=AUTH)[0] == 200
    S.ST.height = 1500 + 100
    # under the full day it would still be open: the cut is what closes it
    assert S.ST.height < S.day_bounds(day, 1)[1] + S.ST.finality
    code, r = call(base, f"grading/sample?day={day}", headers=AUTH)
    assert code == 409 and "final" in r["error"]
    code, r = call(base, "grading/result", dict(res, job_id="job2", miner_id="m2"), headers=AUTH)
    assert code == 409 and "final" in r["error"]
    assert [x["job_id"] for x in S.ST.ev.read(day) if x["type"] == "work_grade"] == ["job1"]
    assert "job2" not in S.ST.work_graded


def test_only_the_day_that_holds_the_end_is_cut(srv):
    S, _ = srv
    S.ST.finality = 100
    S.ST.end_height = S.day_bounds(1, 1)[0] + 5                       # the season ends early in day 1
    full = S.day_bounds(0, 1)[1]
    S.ST.height = full + 100 - 1
    assert S.ST.day_closed(0) == ""                                    # day 0 keeps its full window
    S.ST.height = full + 100
    assert "final" in S.ST.day_closed(0)
    assert S.ST.day_closed(1) == ""                                    # day 1: final at its cut + 100
    S.ST.height = S.ST.end_height + 100
    assert "final" in S.ST.day_closed(1)
    assert "after the season's end" in S.ST.day_closed(2)
    # a season whose end came before its first block holds no day at all
    S.ST.end_height = S.ST.start - 1
    assert S.ST.last_day() == -1
    assert "after the season's end" in S.ST.day_closed(0)


# ── published files ───────────────────────────────────────────────────────────────────────────
def test_published_files_are_served_and_nothing_else(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert call(base, "payout", {"miner_id": "m1", "address": good_address()}, miner="m1")[0] == 200
    assert _internal(base, work("jp", answer="a private answer"), TOKEN)[0] == 200
    day = S.ST.day_now()
    code, raw = get_raw(base, f"evidence/day-{day:03d}.jsonl")
    with open(S.ST.ev.path(day), "rb") as f:
        assert code == 200 and raw == f.read()
    assert b"private" not in raw
    assert get_raw(base, "evidence/day-009.jsonl")[0] == 404
    assert get_raw(base, f"ranking/day-{day:03d}.json")[0] == 404        # not ranked yet
    with open(_rank(S, day), "w", encoding="utf-8") as f:
        f.write('{"day": 0}\n')
    assert get_raw(base, f"ranking/day-{day:03d}.json") == (200, b'{"day": 0}\n')
    # The climb out of the served directory: the system refuses it on its own unless a directory named
    # "day-.." exists, so the bench plants one and leaves the route's own check as the only barrier
    # between the request and the private file of the day's answers.
    os.makedirs(os.path.join(S.ST.ev.dir, "day-.."))
    assert os.path.exists(os.path.join(S.ST.ev.dir, "day-..", "..", "..", "work-pending", f"day-{day:03d}.jsonl"))
    for route in (f"evidence/day-../../../work-pending/day-{day:03d}.jsonl",
                  f"evidence/day-..%2F..%2F..%2Fwork-pending%2Fday-{day:03d}.jsonl",
                  "evidence/day-..%2Fsecret.bin.jsonl",
                  "ranking/day-../../../secret.bin.json",
                  "evidence/../secret.bin", "secret.bin", "work-pending/day-000.jsonl"):
        code, raw = get_raw(base, route)
        assert code == 404 and b"private" not in raw, route


# ── the service's own workers ──────────────────────────────────────────────────────────────────
def test_the_service_starts_the_chain_watch_and_the_seal_and_ranks_only_with_a_generator(srv, monkeypatch):
    S, _ = srv
    started = []

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            started.append(self.target)

    class Server:
        def __init__(self, addr, handler):
            self.daemon_threads = False

        def serve_forever(self):
            return None

    monkeypatch.setattr(S, "threading", types.SimpleNamespace(Thread=Thread, Lock=threading.Lock))
    monkeypatch.setattr(S, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(S, "GENERATOR", "")
    assert S.main() == 0
    assert started == [S.watch_chain, S.seal_days]
    started.clear()
    monkeypatch.setattr(S, "GENERATOR", "dendra1generator")
    assert S.main() == 0
    assert started == [S.watch_chain, S.seal_days, S.rank_days]


def test_the_seal_loop_draws_each_day_that_is_over_and_only_those(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    S.ST.height = S.day_bounds(2, 1)[0] + 10
    assert _internal(base, work("d2"), TOKEN)[0] == 200
    _end_of_day(S, 2)
    assert _internal(base, work("d3"), TOKEN)[0] == 200                  # the running day
    # a private file left by a run that stopped after sealing day 1 but before deleting it
    os.makedirs(os.path.dirname(S.ST.pending_path(1)), exist_ok=True)
    with open(S.ST.pending_path(1), "w", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": "d1", "miner_id": "m1", "height": 5, "prompt": "p", "answer": "a"}) + "\n")
    S.ST.work_sealed.add(1)

    class Stop(Exception):
        pass

    def loop_once():
        calls = []

        def sleep(_s):
            calls.append(_s)
            if len(calls) > 1:
                raise Stop

        monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=time.time, monotonic=time.monotonic))
        with pytest.raises(Stop):
            S.seal_days()

    height = S.ST.height
    S.ST.height = 0                                                    # nothing is sealed on an unknown height
    loop_once()
    assert S.ST.pending_days() == [1, 2, 3] and not S.ST.ev.days()
    S.ST.height = height
    loop_once()
    assert S.ST.pending_days() == [3]
    assert S.ST.ev.read(1) == []                                       # sealed already: removed, not redrawn
    assert [r["job_id"] for r in S.ST.ev.read(2) if r["type"] == "work_answer"] == ["d2"]
    assert [r["type"] for r in S.ST.ev.read(2)] == ["work_answer", "work_sealed"]
    assert S.ST.ev.read(3) == [] and S.ST.work_pending == {"d3": (3, "m1")}


def test_the_seal_loop_draws_the_last_day_once_the_chain_is_past_the_seasons_last_block(srv, monkeypatch):
    # The day that holds the season's last block never "ends" by the next day starting soon: waiting for
    # its full 17 280 blocks would seal it late, or never if the chain stops after the season.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    S.ST.height = S.day_bounds(2, 1)[0] + 10
    assert _internal(base, work("last"), TOKEN)[0] == 200
    _loop(S, monkeypatch, S.seal_days)
    assert S.ST.pending_days() == [2] and not S.ST.ev.days()          # the end is not known: day 2 runs
    S.ST.end_height = S.ST.height                                      # the latest block IS the last one
    _loop(S, monkeypatch, S.seal_days)
    assert S.ST.pending_days() == [2] and not S.ST.ev.days()          # not past it yet
    S.ST.height += 1
    S.ST.latest_time = S.END
    assert S.ST.day_now() == S.ST.last_day() == 2                     # still day 2 by its blocks
    _loop(S, monkeypatch, S.seal_days)
    assert S.ST.pending_days() == [] and 2 in S.ST.work_sealed and not S.ST.work_pending
    assert [r["job_id"] for r in S.ST.ev.read(2) if r["type"] == "work_answer"] == ["last"]
    assert [r["type"] for r in S.ST.ev.read(2)] == ["work_answer", "work_sealed"]


def _published(S, day, paid=0):
    rdir = os.path.join(S.ST.data, "ranking")
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, f"day-{day:03d}.json"), "w", encoding="utf-8") as f:
        json.dump({"total_paid_udndr": paid, "inputs": {"season_end_height": None}}, f)


def test_the_ranking_loop_stops_after_the_seasons_last_day_and_never_at_a_count(srv, monkeypatch, capsys):
    S, _ = srv
    ranked = []

    def rank_day(d, records, *a, **k):
        ranked.append(d)
        raise ValueError("not final")                                   # the loop goes on, says nothing

    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "finality_blocks", lambda params, rules: 100)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})
    # 31 days published and the end not known yet: day 31 is ranked like any other. The season was
    # "30 days" until its end became a date; no count written down in advance stops it now.
    for d in range(31):
        _published(S, d)
    _loop(S, monkeypatch, S.rank_days)
    assert ranked == [31]
    # the chain has passed the end inside day 31: day 31 is the last day, and still ranked
    S.ST.end_height = S.day_bounds(31, 1)[0] + 5
    assert S.ST.last_day() == 31
    ranked.clear()
    _loop(S, monkeypatch, S.rank_days)
    assert ranked == [31]
    # once it is published, no day after it is ranked, and that is said once, not every turn
    _published(S, 31)
    ranked.clear()
    capsys.readouterr()
    _loop(S, monkeypatch, S.rank_days, iterations=3)
    assert ranked == []
    assert capsys.readouterr().out.count("season over") == 1


def test_the_ranking_loop_ranks_under_the_first_day_of_decision_18_the_service_fixed(srv, monkeypatch):
    # The day decision 18 applies from is the one the service fixed at its start and publishes in its status --
    # never a default inside the loop, which would choose in silence which work a day pays.
    S, base = srv
    seen = []

    def rank_day(d, records, *a, **k):
        seen.append(k)
        raise ValueError("not final")

    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "finality_blocks", lambda params, rules: 100)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})
    S.ST.unwound_from = 7
    _loop(S, monkeypatch, S.rank_days)
    assert seen == [{"unwound_from": 7}]
    assert call(base, "status")[1]["unwound_audit_work_from_day"] == 7


def test_the_ranking_loop_takes_the_finality_of_the_days_own_rules(srv, monkeypatch):
    # A day before decision 18 is final when the rules it is ranked under said (unwind + 200, as the service
    # published those days), a day under it one audit deadline later: the loop never hands one finality to all.
    S, _ = srv
    finality = []

    def rank_day(d, records, node, gen, start, paid, fin, *a, **k):
        finality.append((d, fin))
        raise ValueError("not final")

    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {"audit_unwind_blocks": "17280",
                                                          "audit_resolve_timeout": "240"})
    S.ST.unwound_from = 1
    _loop(S, monkeypatch, S.rank_days)                                  # day 0: before decision 18
    _published(S, 0)
    _loop(S, monkeypatch, S.rank_days)                                  # day 1: under it
    assert finality == [(0, 17_480), (1, 17_720)]
    assert S.ST.finality == 17_720


def test_the_ranking_loop_waits_for_the_days_seal(srv, monkeypatch):
    # Ranked before its seal, a day would pay no work: the seal is the list of answers received.
    S, base = srv
    monkeypatch.setattr(S, "INTERNAL_TOKEN", TOKEN)
    assert _internal(base, work("ja"), TOKEN)[0] == 200
    day = S.ST.day_now()
    _end_of_day(S, day)
    ranked = []

    def rank_day(d, records, *a, **k):
        ranked.append([r["type"] for r in records])
        raise ValueError("not final")                                   # the loop goes on, says nothing

    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "finality_blocks", lambda params, rules: 100)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})

    class Stop(Exception):
        pass

    def loop_once():
        calls = []

        def sleep(_s):
            calls.append(_s)
            if len(calls) > 1:
                raise Stop

        monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=time.time, monotonic=time.monotonic))
        with pytest.raises(Stop):
            S.rank_days()

    loop_once()
    assert ranked == []                                                 # the answers are not sealed yet
    S.seal_work_day(day)
    loop_once()
    assert ranked and "work_sealed" in ranked[0]


def test_the_chain_watch_finds_the_seasons_last_block_once(srv, monkeypatch, capsys):
    # The watch reads the height and the header time from ONE status read. The first time it sees a block
    # at or after the end, it searches the season's last block over the signed header times — once: a
    # later read never moves it. A search that cannot read the chain leaves it unknown, and is retried.
    S, base = srv
    S.ST.latest_time = None
    reads = [S.C.ChainUnreadable("node down"),
             (1000, S.END - 1),                                         # before the end: no search
             (1001, S.END),                                             # at it: search, chain unreadable
             (1001, S.END),                                             # retried: found
             (1002, S.END + 7)]                                         # found already: not again
    searched, seen = [], []

    def latest(rpc):
        assert rpc == S.RPC
        if not reads:
            raise _Stop
        r = reads.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def season_end_height(rpc, end, lo, hi):
        searched.append((rpc, end, lo, hi))
        if len(searched) == 1:
            raise S.C.ChainUnreadable("block 1001 unreadable")
        return 1000

    def sleep(_s):
        seen.append((S.ST.height, S.ST.latest_time, S.ST.ended(), S.ST.end_height))

    monkeypatch.setattr(S.C, "latest", latest)
    monkeypatch.setattr(S.C, "season_end_height", season_end_height)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {"avail_epoch_blocks": "288"})
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=time.time, monotonic=time.monotonic))
    with pytest.raises(_Stop):
        S.watch_chain()
    assert seen == [(1000, None, None, None),                          # unread: the time stays unknown
                    (1000, S.END - 1, False, None),
                    (1001, S.END, True, None),                         # over, its last block not known yet
                    (1001, S.END, True, 1000),
                    (1002, S.END + 7, True, 1000)]
    assert searched == [(S.RPC, S.END, 1, 1001)] * 2                    # from the start height to the latest
    assert "chain unreadable" in capsys.readouterr().out
    st = call(base, "status")[1]
    assert st["ended"] is True and st["end_height"] == 1000 and st["last_day"] == 0
    assert st["latest_block_time"] == S.END + 7 and st["height"] == 1002
