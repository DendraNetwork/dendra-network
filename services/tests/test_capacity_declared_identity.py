"""THE "VERIFIED" BLOCK IS GATED ON A SELF-DECLARED IDENTITY, AND ITS OWN DOCUMENTATION SAYS OTHERWISE.

`aggregate()` computes a second set of totals restricted to nodes whose identity appears in the
on-chain miner registry, and `_provenance.verified_subset` tells the reader why that block is the one
to trust:

    "Inflating it requires staking first, so it is the figure public surfaces should display."

As implemented, inflating it requires NAMING a staked identity, not owning one. The match is

    matched = sorted(set(rep.get("miner_ids") or []) & onchain)

and `rep["miner_ids"]` is whatever the reporting node PUT IN ITS OWN REPORT. `do_POST` on /capacity
carries no signature and no authentication — only a per-IP rate limit and a body-size bound. And the
registry side of the set is public: one unauthenticated GET on the chain's REST gateway
(/dendra/jobs/v1/miner) returns every registered miner id.

MEASURED ON THE LIVE ENDPOINT, 2026-08-18, rather than reasoned about:
  · the chain served at api.dendranetwork.com holds exactly ONE registered miner id;
  · NO announced node_id matches it — the intersection of the two id sets is EMPTY;
  · yet one node, whose node_id is an operator-chosen name and not a miner id at all, carries
    `registered_onchain: true` and alone constitutes the whole `verified` block (1 machine, 1 GPU,
    1 judge-capable, 16384 MB VRAM).
That reading cannot tell an honest declaration from an impersonation — and neither can the server.
That is the defect: not that this particular node lied, but that the claim carries a guarantee the
mechanism does not provide.

⛔ THE ESCALATION IS WHAT MAKES IT WORTH FIXING. The store key is `machine::node_id`, and both halves
come from the report, so one party can hold unlimited distinct entries. Each may name the same staked
id. The `verified` totals sum every one of them, so the figure "public surfaces should display" can be
driven as high as its per-field bounds allow by a single anonymous poster — which is precisely what
the block exists to prevent.

⚠️ NOTHING HERE WAS TESTED AGAINST THE PRODUCTION SERVICE. Posting a fabricated report to a live
public endpoint would inflate a public page, so it was not attempted. These cases drive the SHIPPED
`aggregate()` in-process, with the registry reader stubbed, which is where the decision is actually
taken.

THE FIX IS THE ONE THE RELAY ALREADY LEARNED (2026-08-03, per-route policy): proving WHICH miner sent
something takes a SIGNATURE. That rule was written for relay writes and never carried across to this
endpoint — the same "a rule learned in one file protects only that file" this repository keeps paying
for. Until the fix lands, these cases hold the behaviour still and say plainly what it is.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capacity_server as cs

REGISTERED = "dm1xeup8w7knsfj8hwnewu499"   # the shape the chain really serves


def _report(node_id, miner_ids, vram=16384, cores=16, ram=32768):
    """A record in the shape `_clean()` produces, so these cases drive the same code the server does."""
    return {
        "node_id": node_id, "miner_ids": list(miner_ids), "machine": "box-" + node_id,
        "backend": "gpu", "gpu": "NVIDIA RTX 40 series", "gpu_count": 1,
        "vram_mb": vram, "ram_mb": ram, "cpu_cores": cores, "tier": 4,
        "model": "qwen3:14b", "can_judge": True, "judge_backend": "gpu",
        "ts": int(time.time()),
    }


def _aggregate_with_registry(monkeypatch, store, onchain):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(onchain))
    return cs.aggregate(store)


def _proven(report_, miner_id):
    """Marks a report as PROVEN, exactly as `do_POST` does after verifying the signature all the way
    to the miner OPERATOR. The cases below are about what the aggregation is ALLOWED TO ASSERT; the
    cryptographic verification itself is tested where it lives."""
    report_ = dict(report_)
    report_["_proven"] = miner_id
    return report_


def test_a_node_that_merely_NAMES_a_staked_id_is_no_longer_verified(monkeypatch):
    """The core: the reporter owns nothing. It NAMES a public id, and that is no longer enough."""
    store = {"k1": _report("anything-i-like", [REGISTERED])}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    v = out["verified"]
    assert v["machines"] == 0, "naming a staked id is not owning it"
    assert v["gpu_nodes"] == 0
    assert v["judge_capable"] == 0
    assert v["vram_total_mb"] == 0, "and its declared hardware no longer enters the trusted figure"
    assert out["nodes"][0]["registered_onchain"] is False

    # THE CONTROL, without which we would only have shown that we refuse everything: the SAME report,
    # PROVEN, enters the block. A gate that can no longer accept anything protects nothing.
    out2 = _aggregate_with_registry(monkeypatch, {"k1": _proven(store["k1"], REGISTERED)}, {REGISTERED})
    assert out2["verified"]["machines"] == 1
    assert out2["verified"]["vram_total_mb"] == 16384
    assert out2["nodes"][0]["registered_onchain"] is True

def test_a_node_naming_no_staked_id_is_not_counted(monkeypatch):
    """THE CONTROL, and the reason the case above means anything: the same fixture, one field changed,
    and nothing is counted. Without this half, "it was counted" could not be told apart from "this
    aggregate counts everything"."""
    store = {"k1": _report("anything-i-like", ["dm1someone-who-never-staked"])}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    v = out["verified"]
    assert v["machines"] == 0
    assert v["gpu_nodes"] == 0
    assert v["vram_total_mb"] == 0
    assert out["nodes"][0]["registered_onchain"] is False
    # ...and the declared totals DO move, so the case cannot pass by the aggregate having done nothing.
    assert out["known_nodes"] == 1 and out["vram_total_mb"] == 16384


def test_one_staked_id_named_by_many_reports_no_longer_multiplies_anything(monkeypatch):
    """The escalation is closed at both ends. Twenty-five reports NAMING the same staked id prove
    none of them; and on the deposit side the key now follows the PROOF, so a proven identity occupies
    ONE entry, its own, instead of an unbounded number chosen by the sender."""
    store = {"k%d" % i: _report("node-%d" % i, [REGISTERED]) for i in range(25)}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    v = out["verified"]
    assert v["machines"] == 0, "twenty-five declarations, zero proof"
    assert v["vram_total_mb"] == 0
    assert v["judge_capable"] == 0, "and the judge count is the one the launch narrative leans on"

    # The same party, proven: it counts ONCE, not twenty-five times.
    proven_set = {"k%d" % i: _proven(_report("node-%d" % i, [REGISTERED]), REGISTERED) for i in range(25)}
    out2 = _aggregate_with_registry(monkeypatch, proven_set, {REGISTERED})
    assert out2["verified"]["machines"] == 25, (
        "the aggregation does not de-duplicate: the STORAGE KEY is what bounds, and it is tested "
        "where it is computed (test_the_store_key_follows_the_PROOF)")

def test_the_node_id_shortcut_is_GONE(monkeypatch):
    """The quietest shortcut: `registered = ... or (node_id in onchain)`. `node_id` is a name the
    operator CHOOSES, so it was enough to choose one that looks like a staked id."""
    store = {"k1": _report(REGISTERED, [])}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    assert out["nodes"][0]["registered_onchain"] is False, (
        "a node_id that LOOKS LIKE a staked id proves nothing: it is a string the sender writes "
        "the sender writes itself")
    assert out["verified"]["machines"] == 0

def test_the_provenance_DESCRIBES_the_mechanism_that_now_exists(monkeypatch):
    """THE CLAIM ITSELF, pinned where it is SERVED.

    It exists to force the served text and the mechanism to move TOGETHER: the block is fed only by
    a signature that goes back to the operator, so the sentence must say that, and say what does NOT
    suffice. If either side changes, this case must be revisited in the same pass -- that is the point
    of asserting it."""
    out = _aggregate_with_registry(monkeypatch, {}, {REGISTERED})
    texte = out["_provenance"]["verified_subset"]
    assert "OPERATOR" in texte, "the text must name what is proven: the operator, not the id"
    assert "signature" in texte
    assert "Naming a staked id is not enough" in texte, (
        "and it must say explicitly what does NOT suffice, or a hurried reader takes away the opposite")
    assert "Unsigned reports are still" in texte, (
        "the fallback is part of the contract: an operator running yesterday is not cut off")


def test_the_store_key_follows_the_PROOF():
    """The other half of the escalation, and it does not live in the aggregation but in the KEY.

    `machine::node_id` is chosen by the sender at both ends: a hundred reports, a hundred entries, all
    naming the same staked miner. A proven identity occupies ONE key -- duplicating then costs as many
    operator keys as entries wanted, which is precisely the price this was meant to set."""
    raw = _report("nom-choisi", [REGISTERED])
    assert cs.storage_key(raw) == "box-nom-choisi::nom-choisi"

    hundred = {cs.storage_key(_report("nom-%d" % i, [REGISTERED])) for i in range(100)}
    assert len(hundred) == 100, "hundred noms choisis = hundred entrees : c est l escalade, telle quelle"

    proven_set = {cs.storage_key(_proven(_report("nom-%d" % i, [REGISTERED]), REGISTERED))
               for i in range(100)}
    assert proven_set == {"signe::" + REGISTERED}, (
        "a hundred reports PROVEN by the same identity occupy ONE key, not a hundred")
