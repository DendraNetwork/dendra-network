"""STARTUP REFUSALS under public exposure, measured on the real process.

⛔ WHY BY SUBPROCESS AND NOT BY IMPORT. These guards live in `main()` and end in `sys.exit`. A bench
that imports the module never crosses them, which is exactly what left the relay's exposure gate
without a single execution under test. What the operator experiences is an exit code and a message, so
that is what is measured here.

Each case requires TWO things of the refusal: exit code 2, and a message that NAMES what must be set.
A silent refusal gets worked around by disarming the guard.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The relay REFUSES to start without a writable store, so it is given a THROWAWAY one; otherwise the
# bench would measure that refusal instead of the one it targets, and write into the repository tree.
STORE = tempfile.mkdtemp(prefix="dendra-relay-store-test-")


def _start(script, env, args=(), timeout=90):
    """Start a service and return (exit code, output). A service that does NOT refuse starts
    listening: it is killed after the timeout and a code other than 2 is returned, which turns the
    test red as it should."""
    e = dict(os.environ)
    e.pop("DENDRA_PUBLIC", None)
    e.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            e.pop(k, None)
    def _txt(x):
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")
    try:
        p = subprocess.run([sys.executable, str(ROOT / script), *args], env=e, timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired as t:
        # ⛔ STDERR WAS DROPPED ON THIS PATH, AND IT MADE A WHOLE CLASS OF CASE UNTESTABLE.
        # Only the refusal path returned both streams; a service that STARTS reaches the timeout, and
        # this branch returned stdout alone. Every startup banner goes to stderr, so no test could
        # assert on what a service SAYS while coming up successfully — only on what it says while
        # dying. That is the wrong half: a refusal is loud by construction, an announcement made while
        # serving is exactly the kind that gets silently dropped and never noticed.
        # Measured: the notice "attestation NOT enforced" was printed by the real relay and invisible
        # here, so the test read an empty string and failed for a reason that had nothing to do with
        # the code under test.
        return (-1, _txt(t.stdout) + _txt(t.stderr))
    return p.returncode, _txt(p.stdout) + _txt(p.stderr)


# ── THE RELAY: same policy as the gateway on the same exposure ─────────────────────────────────────

def test_relay_refuses_an_open_listen_without_a_token():
    """⛔ THE DEFAULT CLOSED HERE. `docker-compose.yml` publishes the relay's port on the host and pins
    the listen address to `0.0.0.0`: starting without an environment file served ANONYMOUS WRITES, and
    therefore the overwriting of sealed envelopes — destroying the evidence that makes an audit
    judgeable — while the gateway refused to start in the very same case."""
    code, out = _start("relay.py", {"DENDRA_RELAY_HOST": "0.0.0.0", "DENDRA_RELAY_TOKEN": "",
                                             "DENDRA_RELAY_STORE": STORE}, args=["18646"])
    assert code == 2, f"expected a refusal, got {code}: {out[:400]}"
    assert "DENDRA_RELAY_TOKEN" in out and "127.0.0.1" in out, out[:400]


def test_relay_refuses_an_unexpected_public_flag_value():
    code, out = _start("relay.py", {"DENDRA_PUBLIC": "ture", "DENDRA_RELAY_HOST": "127.0.0.1",
                                             "DENDRA_RELAY_TOKEN": "a" * 32,
                                             "DENDRA_RELAY_STORE": STORE}, args=["18647"])
    assert code == 2, f"expected a refusal, got {code}: {out[:400]}"
    assert "DENDRA_PUBLIC" in out, out[:400]


def test_relay_refuses_to_REQUIRE_attestation_without_an_allow_list():
    """Asking for the gate with nothing to check against refuses EVERY miner: the relay would assign
    no job at all. The refusal is stated at startup rather than suffered as silent 403s once online.

    What is refused is the EMPTY ALLOW-LIST, not public exposure — see the case below."""
    code, out = _start("relay.py", {"DENDRA_PUBLIC": "1", "DENDRA_RELAY_HOST": "127.0.0.1",
                                             "DENDRA_RELAY_TOKEN": "a" * 32,
                                             "DENDRA_ATTEST_REQUIRE": "1",
                                             "DENDRA_ATTEST_ALLOW": "",
                                             "DENDRA_RELAY_STORE": STORE}, args=["18648"])
    assert code == 2, f"expected a refusal, got {code}: {out[:400]}"
    assert "DENDRA_ATTEST_ALLOW" in out, out[:400]


def test_public_relay_without_attestation_STARTS_and_says_so():
    """CONTRACT REVERSED, and this is the case that measures it on the REAL PROCESS.

    Public exposure used to arm the gate on its own, so this exact configuration — a public relay with
    no allow-list — refused to boot. It was found the hard way: it blocked a live launch, with zero
    miners registered, for want of a list of miner build digests that could not exist yet.

    The pure-function test cannot replace this one. It proves the notice is RETURNED; only starting the
    process proves someone PRINTS it — and the first version did not, because the caller dropped the
    message of any verdict that was not a refusal.
    """
    code, out = _start("relay.py", {"DENDRA_PUBLIC": "1", "DENDRA_RELAY_HOST": "127.0.0.1",
                                             "DENDRA_RELAY_TOKEN": "a" * 32,
                                             "DENDRA_ATTEST_ALLOW": "",
                                             "DENDRA_RELAY_STORE": STORE}, args=["18649"], timeout=12)
    assert code != 2, f"public exposure alone must NOT block the boot: {out[:400]}"
    assert "NOT enforced" in out, (
        "serving public without attestation has to be said OUT LOUD at startup, not left to be "
        f"discovered by reading the source: {out[:400]}")


# ── THE GATEWAY: the flag is read by the SAME function as the relay ────────────────────────────────

def test_gateway_refuses_an_unexpected_public_flag_value():
    """UNIFICATION WITNESS: before the shared reader, each service had its own vocabulary. The same
    `DENDRA_PUBLIC` must produce the same verdict on both sides of the stack."""
    code, out = _start("gateway.py", {"DENDRA_PUBLIC": "ture", "DENDRA_GW_HOST": "127.0.0.1",
                                              "DENDRA_API_KEY": "b" * 32, "DENDRA_GW_PORT": "18651"})
    assert code == 2, f"expected a refusal, got {code}: {out[:400]}"
    assert "DENDRA_PUBLIC" in out, out[:400]


def test_gateway_refuses_public_without_a_key():
    code, out = _start("gateway.py", {"DENDRA_PUBLIC": "yes", "DENDRA_GW_HOST": "127.0.0.1",
                                              "DENDRA_API_KEY": "", "DENDRA_GW_PORT": "18652"})
    assert code == 2, f"expected a refusal, got {code}: {out[:400]}"
    assert "DENDRA_API_KEY" in out, out[:400]


# ── THE JUDGE: the only path by which PLAINTEXT can reach a log ────────────────────────────────────

def test_judge_refuses_a_plaintext_trace_under_public_exposure():
    """⛔ CONFIDENTIALITY INVARIANT ENFORCED. `DENDRA_JUDGE_TRACE_PLAIN=1` writes the prompt and the
    answer in clear text to a JSONL file. "Bench only" was just a comment: nothing stopped a public
    deployment from setting it, and plaintext would then have reached a persistent log."""
    if not (ROOT / "judge_worker.py").exists():   # pragma: no cover
        pytest.skip("judge_worker.py is absent")
    code, out = _start("judge_worker.py", {"DENDRA_PUBLIC": "1", "DENDRA_JUDGE_TRACE": "1",
                                             "DENDRA_JUDGE_TRACE_PLAIN": "1"})
    assert code == 2, f"expected a refusal, got {code}: {out[:400]}"
    assert "DENDRA_JUDGE_TRACE_PLAIN" in out, out[:400]


def test_judge_without_public_exposure_does_NOT_refuse_for_that_reason():
    """WITNESS — without it, an unconditional refusal of `TRACE_PLAIN` would pass the test above and
    break the measurement bench, which has a legitimate need for plaintext on synthetic prompts. The
    judge stops here on the missing `--id`/`--relay` (argparse exit code 2), never on the trace."""
    code, out = _start("judge_worker.py", {"DENDRA_PUBLIC": None, "DENDRA_JUDGE_TRACE": "1",
                                             "DENDRA_JUDGE_TRACE_PLAIN": "1"})
    assert "DENDRA_JUDGE_TRACE_PLAIN" not in out, f"bench tracing must stay possible: {out[:400]}"
