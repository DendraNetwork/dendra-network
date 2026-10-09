"""THE CAPACITY REGISTRY READ THE CHAIN ONE PAGE AT A TIME — AND STOPPED AFTER THE FIRST.

WHAT THIS BENCH GUARDS, AND WHY IT IS NOT A MATTER OF FORM.

`capacity_server._onchain_miner_ids()` called `dendrad query jobs list-miner` **once**, with neither
`--page-limit` nor `--page-key`. `ListMiner` goes through `query.CollectionPaginate`, and the SDK
applies `DefaultLimit = 100` when the request carries no limit: past 100 registered miners the
response was a PREFIX of reality — no error, no warning.

The consequence: `registered_onchain` is the field that separates "identity staked on-chain" from
"anonymous hardware declaration" on the public /network page and in Prometheus. Every miner past the
hundredth was therefore displayed as **NOT REGISTERED** — a FALSE, PUBLIC claim produced by our own
truncation, on the exact axis that page exists to make trustworthy. The degradation is MONOTONE: the
larger the network, the more honest operators are presented as anonymous. A truncation whose effect
is to say "less" reads like a clean measurement.

The more important half of this bench is not "it paginates". It is that an INCOMPLETE read is treated
as a FAILURE. The module already took care not to overwrite the known set on an RPC outage, so as not
to produce a false claim born of our own unavailability. A pagination interrupted halfway is the same
fault: publishing what has been read so far would present a prefix as a total. So keep the previous
set, do not refresh the timestamp, and retry.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capacity_server as cs


def _page(ids, next_key=""):
    return json.dumps({"miner": [{"miner_id": i} for i in ids],
                       "pagination": {"next_key": next_key}})


class _Res:
    def __init__(self, out):
        self.stdout = out


def _wire(monkeypatch, responses):
    """Replaces subprocess.run; `responses` is a list of (output | Exception), one entry per call."""
    seen = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        i = len(seen) - 1
        r = responses[i] if i < len(responses) else responses[-1]
        if isinstance(r, Exception):
            raise r
        return _Res(r)

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    return seen


@pytest.fixture(autouse=True)
def _blank_cache():
    cs._MINERS["t"], cs._MINERS["ids"] = 0.0, set()
    yield
    cs._MINERS["t"], cs._MINERS["ids"] = 0.0, set()


# === 1. THE READ GOES ALL THE WAY ==============================================================

def test_all_pages_are_read(monkeypatch):
    seen = _wire(monkeypatch, [_page(["m1", "m2"], "K2"), _page(["m3"], "")])
    ids = cs._onchain_miner_ids()
    assert ids == {"m1", "m2", "m3"}, (
        "the registry stops at the first page: past the SDK limit, every additional miner is displayed "
        f"as NOT REGISTERED on the public page. Saw: {sorted(ids)}")
    assert len(seen) == 2, f"a single request was issued: the pagination loop does not exist ({seen})"
    assert "--page-key" in seen[1] and "K2" in seen[1], (
        "the second request does not carry the continuation key: it re-reads the same page")


def test_the_request_carries_an_explicit_limit(monkeypatch):
    """WITNESS: without `--page-limit` the SDK silently applies DefaultLimit=100.

    A correct pagination loop over an implicit limit stays correct — but it multiplies the round trips
    fivefold and, above all, it makes the original defect INVISIBLE in the emitted command. The limit
    must be written down: whatever the command does not state is decided by someone else's default.
    """
    seen = _wire(monkeypatch, [_page(["m1"], "")])
    cs._onchain_miner_ids()
    assert "--page-limit" in seen[0], f"request without an explicit limit: {seen[0]}"


# === 2. AN INCOMPLETE READ IS A FAILURE, NEVER A PARTIAL RESULT ================================

def test_a_missing_page_keeps_the_previous_set(monkeypatch):
    cs._MINERS["ids"] = {"m-ancien"}
    _wire(monkeypatch, [_page(["m1"], "K2"), RuntimeError("rpc cut")])
    ids = cs._onchain_miner_ids()
    assert ids == {"m-ancien"}, (
        "a PARTIAL set was published after an interrupted pagination. Publishing what was read so far "
        "presents a prefix as a total, and flips every miner on the unread pages to \"not registered\". "
        f"Saw: {sorted(ids)}")
    assert cs._MINERS["t"] == 0.0, (
        "the timestamp was refreshed despite the failure: the incomplete read would then be served for "
        "the whole cache lifetime instead of being retried on the next round")


def test_a_stalled_continuation_key_is_a_failure(monkeypatch):
    """The server returns the same key twice: it is not making progress.

    Looping forever would hold the lock; stopping while claiming completion would return a partial set.
    Both are worse than failing and retrying.
    """
    cs._MINERS["ids"] = {"m-ancien"}
    _wire(monkeypatch, [_page(["m1"], "MEME"), _page(["m2"], "MEME")])
    ids = cs._onchain_miner_ids()
    assert ids == {"m-ancien"}, f"partial set published on a pagination that is stuck: {sorted(ids)}"


def test_an_empty_but_read_list_is_a_success(monkeypatch):
    """THE DISTINCTION THIS BENCH PROTECTS: "read, and there is nothing" is not "not read".

    A fresh genesis legitimately has no miners. Treating that response as a failure would freeze the
    previous set forever on a reset chain — the opposite of the intent.
    """
    cs._MINERS["ids"] = {"m-ancien"}
    _wire(monkeypatch, [_page([], "")])
    assert cs._onchain_miner_ids() == set(), (
        "an EMPTY but VALID response must replace the previous set: it is a fact, not a failure")
    assert cs._MINERS["t"] > 0.0, "a success must refresh the timestamp"


def test_the_page_cap_refuses_rather_than_truncating(monkeypatch):
    """An endless stream of pages: refuse, do not return the first N."""
    cs._MINERS["ids"] = {"m-ancien"}
    monkeypatch.setattr(cs, "MAX_PAGES", 3)
    counter = {"n": 0}

    def fake_run(cmd, **kw):
        counter["n"] += 1
        return _Res(_page([f"m{counter['n']}"], f"K{counter['n']}"))

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    ids = cs._onchain_miner_ids()
    assert ids == {"m-ancien"}, f"truncated set published at the page cap: {sorted(ids)}"
    assert counter["n"] == 3, f"the cap did not bound the loop ({counter['n']} requests)"
