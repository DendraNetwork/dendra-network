"""Integration tests for the hardened relay: token authentication and per-IP rate limiting.
Starts a real ThreadingHTTPServer on an ephemeral port and issues real HTTP requests."""
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Throwaway store, set before the first import of the relay. Without it, every (re)load creates
# `relay-store/` inside the repository tree, and `test_relay_store.py` — which exists precisely to
# check that no bench leaves residue — goes red because of this file. A test that dirties the tree
# turns another one red: cleanliness is part of the measurement, not a comfort.
os.environ.setdefault("DENDRA_RELAY_STORE", tempfile.mkdtemp(prefix="dendra-relay-hardening-"))


def _boot(env):
    """(Re)load relay under a given environment and start the server on an ephemeral port."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import relay
    importlib.reload(relay)
    srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return srv, port


def _req(port, path, method="GET", headers=None, body=None):
    url = f"http://127.0.0.1:{port}/{path}"
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_token_auth_required_when_configured():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": "s3cret", "DENDRA_RELAY_RATE": "1000"})
    try:
        # POST /pub without a token -> 401
        code, _ = _req(port, "pub/m1", "POST", body=json.dumps({"pub": "ab"}).encode())
        assert code == 401, f"expected 401 without a token, got {code}"
        # with the right token -> 200
        code, _ = _req(port, "pub/m1", "POST", headers={"X-Dendra-Token": "s3cret"},
                       body=json.dumps({"pub": "ab"}).encode())
        assert code == 200, f"expected 200 with a token, got {code}"
        # `GET /list` is the work queue: `relay.listing()` in miner is a miner's only discovery
        # call, so a token there would leave a joined miner waiting forever with no error. The chain
        # already publishes the same assignment (`assigned_committee/<jobId>`), so reading the queue
        # stays open even when a token is configured.
        code, _ = _req(port, "list")
        assert code == 200, f"expected 200 on GET /list without a token (the work queue is open), got {code}"
        # The other half, which is what a token still buys: writing to that queue stays shut.
        code, _ = _req(port, "list", "POST", body=b"{}")
        assert code == 401, f"expected 401 on POST /list without a token, got {code}"
        # `/stats` is not on the path to receiving work, so it stays on the token.
        code, _ = _req(port, "stats")
        assert code == 401, f"expected 401 on /stats without a token, got {code}"
    finally:
        srv.shutdown()


def test_no_token_backward_compatible():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": None, "DENDRA_RELAY_RATE": "1000"})
    try:
        # no token configured -> local access works without a header (backward compatible)
        code, _ = _req(port, "list")
        assert code == 200, f"expected 200 in local mode, got {code}"
    finally:
        srv.shutdown()


def test_rate_limit_blocks_flood():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": None, "DENDRA_RELAY_RATE": "5", "DENDRA_RELAY_WINDOW": "100"})
    try:
        codes = [_req(port, "list")[0] for _ in range(12)]
        assert 429 in codes, f"expected a 429 under flood, got {codes}"
        assert codes[:5] == [200] * 5, f"the first 5 should pass, got {codes[:5]}"
    finally:
        srv.shutdown()


# ==================================================================================================
# The public exposure gate. `_boot()` above builds `ThreadingHTTPServer` directly and never calls
# `main()`, so the fail-closed decision that separates a public relay from an open one is tested
# through the pure function `main()` calls, `verdict_exposure`.
# ==================================================================================================
import relay as rs  # noqa: E402  (deliberate late import: after the sys.path setup above)

STRONG = "a" * 32
STRONG2 = "b" * 32


def test_public_without_a_token_refuses_startup():
    v, m = rs.verdict_exposure("", "", "0.0.0.0", True)
    assert v == "REFUSE", "a public relay without auth exposes /list (network mapping) and anonymous PUTs"
    assert "without DENDRA_RELAY_TOKEN" in m, f"the reason must name the missing variable: {m}"


def test_public_token_too_short_refuses():
    v, m = rs.verdict_exposure("court", "", "0.0.0.0", True)
    assert v == "REFUSE"
    assert "too short" in m and "openssl rand" in m, f"the reason must say what to do: {m}"


def test_public_previous_token_too_short_refuses():
    """`_authed()` also accepts `TOKEN_PREV`, so the minimum length applies to it too: otherwise a
    strong current token with `TOKEN_PREV=a` would let a public relay accept "a" for the whole
    rotation window."""
    v, m = rs.verdict_exposure(STRONG, "a", "0.0.0.0", True)
    assert v == "REFUSE", "a weak previous token is a public back door, not a transition"
    assert "TOKEN_PREV" in m, f"the reason must point at the previous token: {m}"


def test_an_empty_previous_token_stays_inert():
    """Witness — without it, a minimum length that refused every `TOKEN_PREV` would pass this bench.
    The rotation rule says: empty means strictly no effect. That must hold."""
    v, _ = rs.verdict_exposure(STRONG, "", "0.0.0.0", True)
    assert v == "OK", "an empty variable opens nothing and must refuse nothing either"


def test_public_with_two_strong_tokens_starts():
    v, _ = rs.verdict_exposure(STRONG, STRONG2, "0.0.0.0", True)
    assert v == "OK", "a correctly armed rotation must be able to start"


def test_an_open_bind_without_a_token_refuses_even_without_public():
    """Same rule as the gateway. `docker-compose.yml` publishes the relay's port and listens on
    `0.0.0.0`, and an anonymous write can overwrite a sealed envelope an audit needs. `DENDRA_PUBLIC`
    is a declaration and the listen address is a fact; in both services the fact decides."""
    v, m = rs.verdict_exposure("", "", "0.0.0.0", False)
    assert v == "REFUSE", "a non-local bind without a token means anonymous writes over the evidence"
    assert "DENDRA_RELAY_TOKEN" in m and "127.0.0.1" in m, f"the reason must say what to do: {m}"


def test_an_open_bind_WITH_a_token_starts():
    """Witness — the refusal is about the absence of a token, not about the open bind. Without it, a
    refusal that blocked every non-local bind would pass this bench and break multi-host deployment."""
    v, _ = rs.verdict_exposure(STRONG, "", "0.0.0.0", False)
    assert v == "OK", "a correctly authenticated multi-host relay must start"


def test_localhost_without_a_token_stays_ok():
    v, _ = rs.verdict_exposure("", "", "127.0.0.1", False)
    assert v == "OK", "local mode without a token is the accepted backward-compatible default"


def test_an_empty_address_is_a_network_listen_not_a_loopback():
    """`bind("")` means INADDR_ANY. Classifying the empty string as loopback silenced the guard exactly
    where it matters: a listener on every interface mistaken for a developer workstation."""
    v, _ = rs.verdict_exposure("", "", "", False)
    assert v == "REFUSE", "an empty DENDRA_RELAY_HOST listens on all interfaces"


def test_the_public_trigger_is_tested_too():
    """A gate whose body is tested but never its trigger is only half proven."""
    for armed in ("1", " 1 ", "true", "TRUE", "yes", "Yes", "on"):
        assert rs.public_enabled(armed) is True, f"{armed!r} must arm public exposure"
    for inert in (None, "", "0", "no", "false", "off", " OFF "):
        assert rs.public_enabled(inert) is False, f"{inert!r} must not arm public exposure"


def test_an_unexpected_flag_value_REFUSES_instead_of_falling_back_to_permissive():
    """On a security flag, an unrecognised value never means "not public". Falling back to the
    permissive default would turn a typo (`DENDRA_PUBLIC=ture`) into a silent exposure: the service
    starts with its guards disarmed while believing itself public."""
    import pytest
    # "oui" (French yes) and "ture" (a typo) are deliberate: the vocabulary is closed.
    for absurd in ("2", "oui", "ture", "true!", "-1"):
        with pytest.raises(rs.InvalidFlag):
            rs.public_enabled(absurd)


# -- The attestation gate — asked for or not, but never armed with nothing to check against ---------
#
# Public exposure alone does not arm the gate: an allow-list pins specific measured digests, so an open
# network would have to publish the exact build every third-party miner must run. What is refused is
# asking for the gate with nothing to check against, the state that assigns no job at all.

def test_requiring_without_an_allow_list_refuses_startup():
    """Required on an empty allow-list, the gate refuses every miner: the relay would assign no job at
    all and nothing would say so. The refusal is stated at startup, naming the variable."""
    v, m = rs.verdict_attestation(public=True, require=True, allow_list=set())
    assert v == "REFUSE"
    assert "DENDRA_ATTEST_ALLOW" in m, f"the reason must name the variable to set: {m}"


def test_requiring_without_an_allow_list_refuses_even_outside_public():
    """The defect is the empty allow-list, not the exposure: a closed stack asking for the gate with
    nothing to check against is just as unable to assign a job."""
    v, _ = rs.verdict_attestation(public=False, require=True, allow_list=set())
    assert v == "REFUSE"


def test_requiring_with_an_allow_list_starts():
    v, _ = rs.verdict_attestation(public=True, require=True, allow_list={"a" * 64})
    assert v == "OK", "an allow-list that is set makes the gate usable"


def test_public_without_the_requirement_starts_but_says_so():
    """Witness — being public no longer arms the gate, and the relay must say it serves without
    attestation. A guard that is not applied must not be discoverable only by reading the source,
    because the counterpart of turning it off is never announcing it as active."""
    v, m = rs.verdict_attestation(public=True, require=False, allow_list=set())
    assert v == "OK", "public exposure alone must not block the launch"
    assert m, "serving public without attestation has to be stated out loud"
    assert "not enforced" in m, f"the notice must say the gate is off, plainly: {m}"


def test_a_closed_stack_with_nothing_stays_silent():
    """Witness against noise — a closed stack that asks for nothing gets no warning. A banner printed
    in every situation stops being read in the one situation that matters."""
    v, m = rs.verdict_attestation(public=False, require=False, allow_list=set())
    assert v == "OK"
    assert m == "", f"no notice expected on a closed stack: {m}"


def _set_env(env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _reload(env):
    """Reload `relay` under `env`, return (ATTEST_REQUIRE, PUBLIC, PUBLIC_ERR), then restore it.

    The gate constants are read at import time: exercising them requires reloading the module, and not
    contaminating the following tests requires reloading it a second time in its original state.
    """
    before = {k: os.environ.get(k) for k in env}
    try:
        _set_env(env)
        importlib.reload(rs)
        return (rs.ATTEST_REQUIRE, rs.PUBLIC, rs.PUBLIC_ERR)
    finally:
        _set_env(before)
        importlib.reload(rs)


def test_public_alone_does_NOT_arm_attestation():
    """Public exposure does not arm the gate by itself.

    An allow-list pins specific measured digests, so it would exclude the third-party operators an open
    network needs, and a public relay with no miners yet could not boot. ADR-011 describes the consumer
    tier as hardened deterrence without a cryptographic proof. The knob is explicit, and a gate that is
    not applied is never announced as active.
    """
    require, public, err = _reload({"DENDRA_PUBLIC": "1", "DENDRA_ATTEST_REQUIRE": None})
    assert (public, err) == (True, ""), (public, err)
    assert require is False, "DENDRA_PUBLIC=1 alone must not arm the attestation gate"


def test_the_variable_arms_the_gate_wherever_we_are():
    """Anti-decoration witness: without it, a hard-coded `ATTEST_REQUIRE = False` would satisfy the
    case above. The variable has to actually do something, public or not."""
    require, public, _ = _reload({"DENDRA_PUBLIC": "1", "DENDRA_ATTEST_REQUIRE": "1"})
    assert (public, require) == (True, True), "the variable must arm the gate under public exposure"
    require, public, _ = _reload({"DENDRA_PUBLIC": None, "DENDRA_ATTEST_REQUIRE": "1"})
    assert (public, require) == (False, True), "and on a closed stack too"


def test_a_closed_stack_leaves_the_gate_to_the_operator():
    """Outside public exposure and without the variable, the default stays backward compatible."""
    require, public, _ = _reload({"DENDRA_PUBLIC": None, "DENDRA_ATTEST_REQUIRE": None})
    assert public is False and require is False, "a closed stack does not arm the gate by default"


def test_an_unexpected_flag_value_is_HELD_for_refusal_at_startup():
    """The import cannot raise (the module is also loaded outside the service), so the error is held
    and `main()` turns it into a refusal. Without `PUBLIC_ERR`, a typo would once again become a
    silent start in non-public mode."""
    require, public, err = _reload({"DENDRA_PUBLIC": "ture", "DENDRA_ATTEST_REQUIRE": None})
    assert public is False and "DENDRA_PUBLIC" in err, (public, err)


def test_the_previous_token_is_really_accepted_and_counted():
    """Proof that the refusal above is not decorative: without it, this short "a" really would open
    the relay. Measured on the real server, not on the pure function."""
    srv, port = _boot({"DENDRA_RELAY_TOKEN": STRONG, "DENDRA_RELAY_TOKEN_PREV": "a",
                       "DENDRA_RELAY_RATE": "1000"})
    try:
        import relay as live
        live._PREV_HITS[0] = 0
        # This probe used `GET /list`, which no longer proves anything: that route is open now, so
        # `_authed()` is never reached and the counter legitimately stays at 0 — a green route turning
        # this case into a false negative about the previous token, which is what it exists to measure.
        # `/stats` is a read that still requires a token, so it exercises the same code path the old
        # probe did. The lesson is general: a test that borrows a route it does not care about breaks
        # the day that route's policy changes, and it breaks silently, on the wrong subject.
        code, _ = _req(port, "stats", headers={"X-Dendra-Token": "a"})
        assert code == 200, f"the short previous token is accepted by _authed(): got {code}"
        assert live._PREV_HITS[0] == 1, "and every use of the previous token must be counted"
        code, _ = _req(port, "stats", headers={"X-Dendra-Token": "z"})
        assert code == 401, "an unknown token must stay refused"
    finally:
        srv.shutdown()


def test_the_access_log_is_SILENT_and_that_is_a_guard():
    """Invariant: `log_message` must remain a no-op.

    The path of a relay request is an identifier (`<kind>/<jobId>__<minerId>`): re-enabling the access
    log would manufacture the very leak our checks hunt for, and a persistent one. This test is the
    lock on the suite side; the launch gate carries the same check as a static read.
    It does not test "the log is empty" — that would be a green by absence, true even if the
    property fell. It tests the property: the call writes nothing, whatever it is passed."""
    import io
    import relay as rs
    from contextlib import redirect_stderr, redirect_stdout

    assert "log_message" in rs.Handler.__dict__, (
        "the override is gone: BaseHTTPRequestHandler would log the PATH, hence the identifier")
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        # feed it exactly what would leak: a path carrying identifiers
        rs.Handler.log_message(object(), '"%s %s %s"', "GET", "/req/job1785333936418__miner7", "200")
    assert o.getvalue() == "" and e.getvalue() == "", (
        f"the access log wrote: stdout={o.getvalue()!r} stderr={e.getvalue()!r}")


# ==================================================================================================
# Observability without the paths — the saturation counters
#
# Request silence and observability are not in opposition: granularity decides. Without counters, a
# saturation attack in progress (filling the store so that legitimate deposits are refused) is
# invisible — the network goes dark and nothing says so. With paths, we would manufacture the leak
# that `log_message`'s silence avoids. These tests hold both ends.
# ==================================================================================================

def test_the_counters_rise_by_CODE_without_being_set_branch_by_branch():
    """The counting lives in `_send`: every refusal is counted, including a code nobody planned for."""
    srv, port = _boot({"DENDRA_RELAY_TOKEN": STRONG, "DENDRA_RELAY_RATE": "1000"})
    try:
        import relay as live
        live.COUNTERS.clear(); live.COUNTERS["evictions_age"] = 0
        # The refused probe is `/stats`, not `/list`: reading the work queue is open, so it would
        # answer 200 and this case would measure nothing while still looking green.
        _req(port, "stats")                                                    # 401 (no token)
        _req(port, "list", headers={"X-Dendra-Token": STRONG})                 # 200
        _req(port, "route/unknown/xxx", headers={"X-Dendra-Token": STRONG})   # 404
        c = live._stats()["counters"]
        assert c.get("http_401") == 1, f"the auth refusal must be counted: {c}"
        assert c.get("http_200", 0) >= 1 and c.get("http_404") == 1, c
    finally:
        srv.shutdown()


def test_stats_exposes_the_F1_signal_occupancy_and_cap():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": STRONG, "DENDRA_RELAY_RATE": "1000"})
    try:
        code, body = _req(port, "stats", headers={"X-Dendra-Token": STRONG})
        assert code == 200, code
        st = json.loads(body)
        # The saturation signal: is the store filling up, and how far is it from the cap?
        assert "occupation" in st and "legacy_entry_cap" in st, st
        assert st["legacy_entry_cap"] > 0
        assert set(st["occupation"]) >= {"pub", "req", "res"}, st["occupation"]
        for k in ("counters", "retention_s", "uptime_s", "prev_token_uses"):
            assert k in st, f"{k} is missing: {st}"
    finally:
        srv.shutdown()


def test_stats_is_GUARDED_by_the_token():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": STRONG, "DENDRA_RELAY_RATE": "1000"})
    try:
        code, _ = _req(port, "stats")
        assert code == 401, f"expected 401 on /stats without a token, got {code}"
    finally:
        srv.shutdown()


def test_NO_identifier_leaks_through_the_counters():
    """The witness that matters. A key carrying identifiers is deposited, then no fragment of that
    key may appear in /stats. Without this test, "counters" would turn into "aggregated paths" at the
    first evolution, rebuilding the leak that log_message's silence exists to avoid."""
    srv, port = _boot({"DENDRA_RELAY_TOKEN": STRONG, "DENDRA_RELAY_RATE": "1000"})
    try:
        jid, mid = "job1785333936418", "miner7"
        code, _ = _req(port, f"pub/{jid}__{mid}", "POST",
                       headers={"X-Dendra-Token": STRONG}, body=json.dumps({"pub": "ab"}).encode())
        assert code == 200, code
        code, body = _req(port, "stats", headers={"X-Dendra-Token": STRONG})
        raw = body.decode()
        assert code == 200
        for leak in (jid, mid, f"{jid}__{mid}"):
            assert leak not in raw, f"/stats carries an identifier ({leak!r}): {raw[:200]}"
        # ...and it did see the deposit: the witness is only worth something if the measurement happened.
        assert json.loads(raw)["occupation"]["pub"] >= 1, "the deposit was not counted"
    finally:
        srv.shutdown()
