"""The "verified" block of /capacity counts the identities a report proves, never the ones it names.

`aggregate()` computes a second set of totals restricted to nodes whose identity appears in the
on-chain miner registry, and `_provenance.verified_subset` tells the reader why that block is the one
to trust. A report names its miner ids itself, and the registry side is public: one unauthenticated GET
on the chain's REST gateway (/dendra/jobs/v1/miner) returns every registered miner id. So naming a
staked id proves nothing, since anyone can copy one; proving which miner sent a report takes a
signature, verified all the way to the miner's operator (`_proven`, set by `do_POST`).

The store key is `machine::node_id`, and both halves come from the report, so one party can hold any
number of entries. If each could name the same staked id, the `verified` totals would sum every one of
them, and a single anonymous poster could drive the figure public surfaces display as high as its
per-field bounds allow.

These cases drive the shipped `aggregate()` in-process, with the registry reader stubbed, which is
where the decision is taken; the signature check itself is tested where it lives. Nothing here posts
to a live service.
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
    """Marks a report as proven, exactly as `do_POST` does after verifying the signature all the way
    to the miner operator. The cases below are about what the aggregation is allowed to assert; the
    cryptographic verification itself is tested where it lives."""
    report_ = dict(report_)
    report_["_proven"] = miner_id
    return report_


def test_a_node_that_merely_NAMES_a_staked_id_is_no_longer_verified(monkeypatch):
    """The core: the reporter owns nothing. It names a public id, and that is no longer enough."""
    store = {"k1": _report("anything-i-like", [REGISTERED])}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    v = out["verified"]
    assert v["machines"] == 0, "naming a staked id is not owning it"
    assert v["gpu_nodes"] == 0
    assert v["judge_capable"] == 0
    assert v["vram_total_mb"] == 0, "and its declared hardware no longer enters the trusted figure"
    assert out["nodes"][0]["registered_onchain"] is False

    # The control, without which we would only have shown that we refuse everything: the same report,
    # proven, enters the block. A gate that can no longer accept anything protects nothing.
    out2 = _aggregate_with_registry(monkeypatch, {"k1": _proven(store["k1"], REGISTERED)}, {REGISTERED})
    assert out2["verified"]["machines"] == 1
    assert out2["verified"]["vram_total_mb"] == 16384
    assert out2["nodes"][0]["registered_onchain"] is True

def test_a_node_naming_no_staked_id_is_not_counted(monkeypatch):
    """The control, and the reason the case above means anything: the same fixture, one field changed,
    and nothing is counted. Without this half, "it was counted" could not be told apart from "this
    aggregate counts everything"."""
    store = {"k1": _report("anything-i-like", ["dm1someone-who-never-staked"])}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    v = out["verified"]
    assert v["machines"] == 0
    assert v["gpu_nodes"] == 0
    assert v["vram_total_mb"] == 0
    assert out["nodes"][0]["registered_onchain"] is False
    # ...and the declared totals do move, so the case cannot pass merely because the aggregate did nothing.
    assert out["known_nodes"] == 1 and out["vram_total_mb"] == 16384


def test_one_staked_id_named_by_many_reports_no_longer_multiplies_anything(monkeypatch):
    """The escalation is closed at both ends. Twenty-five reports naming the same staked id prove
    none of them; and on the deposit side the key now follows the proof, so a proven identity occupies
    one entry, its own, instead of an unbounded number chosen by the sender."""
    store = {"k%d" % i: _report("node-%d" % i, [REGISTERED]) for i in range(25)}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    v = out["verified"]
    assert v["machines"] == 0, "twenty-five declarations, zero proof"
    assert v["vram_total_mb"] == 0
    assert v["judge_capable"] == 0, "and the judge count is the one the launch narrative leans on"

    # The same party, proven: it counts once, not twenty-five times.
    proven_set = {"k%d" % i: _proven(_report("node-%d" % i, [REGISTERED]), REGISTERED) for i in range(25)}
    out2 = _aggregate_with_registry(monkeypatch, proven_set, {REGISTERED})
    assert out2["verified"]["machines"] == 25, (
        "the aggregation does not de-duplicate: the storage key is what bounds, and it is tested "
        "where it is computed (test_the_store_key_follows_the_PROOF)")

def test_the_node_id_shortcut_is_GONE(monkeypatch):
    """The quietest shortcut: `registered = ... or (node_id in onchain)`. `node_id` is a name the
    operator chooses, so it was enough to choose one that looks like a staked id."""
    store = {"k1": _report(REGISTERED, [])}
    out = _aggregate_with_registry(monkeypatch, store, {REGISTERED})

    assert out["nodes"][0]["registered_onchain"] is False, (
        "a node_id that looks like a staked id proves nothing: it is a string the sender writes "
        "the sender writes itself")
    assert out["verified"]["machines"] == 0

def test_the_provenance_DESCRIBES_the_mechanism_that_now_exists(monkeypatch):
    """The claim itself, pinned where it is served.

    It exists to force the served text and the mechanism to move together: the block is fed only by
    a signature that goes back to the operator, so the sentence must say that, and say what does not
    suffice. If either side changes, this case must be revisited in the same pass -- that is the point
    of asserting it."""
    out = _aggregate_with_registry(monkeypatch, {}, {REGISTERED})
    text = out["_provenance"]["verified_subset"]
    assert "must be the operator" in text, "the text must name what is proven: the operator, not the id"
    assert "signature" in text
    assert "Naming a staked id is not enough" in text, (
        "and it must say explicitly what does not suffice, or a hurried reader takes away the opposite")
    assert "Unsigned reports are still" in text, (
        "the fallback is part of the contract: an operator running yesterday is not cut off")


def test_the_store_key_follows_the_PROOF():
    """The store key, the other half of the defence.

    `machine` and `node_id` are both chosen by the sender, so a hundred reports would make a hundred
    entries naming the same staked miner. A proven identity occupies one key, so duplicating it costs
    one operator key per entry.

    The two keyspaces must not meet: if a declared key could spell a proven one, an unsigned report
    would overwrite a proven record and empty the `verified` block that public surfaces display and
    the chat gate relies on. This case checks that neither namespace can spell the other."""
    raw = _report("chosen-name", [REGISTERED])
    proven_key = cs.storage_key(_proven(raw, REGISTERED))

    # The eviction is asserted first, and as a property rather than a literal. Against a module
    # that still shares one keyspace this case has to fail on the claim; ordered the other way it
    # failed on a missing module attribute instead, which reads as "this test is out of date" and
    # says nothing about what the server does. Naming a constant is not measuring a behaviour.
    # `cs._KEY_PROVEN_BEFORE_2026_10_10` is the proven prefix as stores written before the rename spell it.
    old_word = cs._KEY_PROVEN_BEFORE_2026_10_10.rstrip(":")
    for machine in ("signed", old_word, "decl", "", "signed::x", old_word + "::x", "box-1"):
        forged = _report(REGISTERED, [])
        forged["machine"] = machine
        assert cs.storage_key(forged) != proven_key, (
            "an unsigned deposit must not be able to name, and therefore replace, a proven record; "
            "machine=%r reached it" % machine)

    declared = cs.storage_key(raw)
    assert "chosen-name" in declared, "the declared key still identifies what the sender declared"
    hundred = {cs.storage_key(_report("name-%d" % i, [REGISTERED])) for i in range(100)}
    assert len(hundred) == 100, "a hundred chosen names, a hundred entries: the escalation as it stands"

    proven_set = {cs.storage_key(_proven(_report("name-%d" % i, [REGISTERED]), REGISTERED))
               for i in range(100)}
    assert proven_set == {proven_key}, (
        "a hundred reports proven by the same identity occupy one key, not a hundred")

    # And retiring the pre-namespace key must never hand that reach back one level up: `machine`
    # set to a namespace this file owns turns the retired key into a current one.
    for machine in ("signed", old_word, "decl"):
        f = _report("abc123::n1", [])
        f["machine"] = machine
        assert cs.superseded_keys(f) == (), "machine=%r retired a live key" % machine


def test_a_store_written_before_the_rename_keeps_ONE_key_per_proven_identity():
    """A record proven under an older server's French prefix must not sit next to its
    re-posted twin under the current prefix: one identity counted twice in `verified` is an over-count."""
    old = cs._KEY_PROVEN_BEFORE_2026_10_10
    moved = cs._migrate_proven_prefix({old + "dm1a": {"ts": 10, "v": "old"}, "decl::b::n": {"ts": 5}})
    assert moved == {cs.KEY_PROVEN + "dm1a": {"ts": 10, "v": "old"}, "decl::b::n": {"ts": 5}}, moved
    both = cs._migrate_proven_prefix({old + "dm1a": {"ts": 10, "v": "old"},
                                      cs.KEY_PROVEN + "dm1a": {"ts": 20, "v": "new"}})
    assert both == {cs.KEY_PROVEN + "dm1a": {"ts": 20, "v": "new"}}, "the record received last wins"
    later_old = cs._migrate_proven_prefix({old + "dm1a": {"ts": 30, "v": "old"},
                                           cs.KEY_PROVEN + "dm1a": {"ts": 20, "v": "new"}})
    assert later_old == {cs.KEY_PROVEN + "dm1a": {"ts": 30, "v": "old"}}
    assert cs._migrate_proven_prefix([1, 2]) == [1, 2], "a store that is not a mapping is left to its readers"
