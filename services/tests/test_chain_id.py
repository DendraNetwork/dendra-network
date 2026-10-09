"""Bench for `modea/chain_id.py` — the chain id is READ, never written (ADR-048 item 8).

Every case drives `resolve` with an injected environment and an injected `dendrad status` answer, so
the four states are all produced here: declared only, served only, both agreeing, both disagreeing — and
the fifth, neither, which must be a refusal and never a default. A bench that could only produce the
happy path would stay green on a resolver that returned "dendra" unconditionally.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MODEA = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(MODEA))

from modea import chain_id as cid  # noqa: E402


def _status(network, key="node_info"):
    return json.dumps({key: {"network": network, "moniker": "x"}, "sync_info": {}})


def _runner(text=None, exc=None):
    seen = []

    def run(argv):
        seen.append(list(argv))
        if exc:
            raise exc
        return text
    run.seen = seen
    return run


def _refused(**kw):
    try:
        cid.resolve(**kw)
    except cid.ChainIdError as e:
        return str(e)
    return None


def test_declared_only_when_the_node_is_silent():
    run = _runner(text="Error: post failed: connection refused")
    assert cid.resolve(env={"DENDRA_CHAIN_ID": "dendra-testnet"}, run=run) == "dendra-testnet"


def test_served_only_reads_the_node():
    assert cid.resolve(env={}, run=_runner(_status("dendra-testnet"))) == "dendra-testnet"


def test_a_warning_line_next_to_the_status_does_not_hide_it():
    # Both streams are joined: a toolchain warning on stderr, before or after the JSON, must not make the
    # chain id unknown. A warning alone is still no answer.
    warning = "go: warning: this binary was built with an experimental toolchain setting"
    for text in (warning + "\n" + _status("dendra-testnet"), _status("dendra-testnet") + "\n" + warning):
        assert cid.resolve(env={}, run=_runner(text)) == "dendra-testnet"
    assert cid.network_from_status(warning) == ""
    assert cid.network_from_status("{not json\n" + warning) == ""


def test_the_older_casing_is_read_too():
    assert cid.resolve(env={}, run=_runner(_status("dendra-testnet", key="NodeInfo"))) == "dendra-testnet"


def test_both_agreeing():
    assert cid.resolve(env={"DENDRA_CHAIN_ID": "dendra-testnet"}, run=_runner(_status("dendra-testnet"))) == "dendra-testnet"


def test_both_disagreeing_is_a_refusal_naming_both():
    msg = _refused(env={"DENDRA_CHAIN_ID": "dendra-testnet"}, run=_runner(_status("dendra")))
    assert msg is not None, "a process told one network and pointed at another must not sign"
    assert "dendra-testnet" in msg and "'dendra'" in msg


def test_neither_is_a_refusal_never_a_default():
    msg = _refused(env={}, run=_runner(text="not json"))
    assert msg is not None, "the only plausible default is the previous chain's name, which is the wrong one"
    assert "DENDRA_CHAIN_ID" in msg


def test_an_exploding_node_query_is_unknown_not_fatal():
    assert cid.resolve(env={"DENDRA_CHAIN_ID": "dendra-testnet"}, run=_runner(exc=OSError("no dendrad"))) == "dendra-testnet"
    assert _refused(env={}, run=_runner(exc=OSError("no dendrad"))) is not None


def test_a_blank_declaration_is_no_declaration():
    assert _refused(env={"DENDRA_CHAIN_ID": "   "}, run=_runner(text="")) is not None


def test_the_node_flags_reach_the_status_query():
    run = _runner(_status("dendra-testnet"))
    cid.resolve(node_flags=("--node", "tcp://chain:26657"), env={}, run=run)
    assert run.seen == [["dendrad", "status", "--node", "tcp://chain:26657"]]


def test_no_service_writes_the_chain_id_any_more():
    # The defect was five copies of one literal. Read the shipped files rather than trusting the edit.
    for name in ("miner.py", "judge_worker.py", "reveal_worker.py", "client.py", "faucet.py"):
        src = (MODEA / name).read_text(encoding="utf-8")
        assert 'CHAIN = "dendra"' not in src, name
        assert '"DENDRA_CHAIN_ID", "dendra"' not in src, name
        assert "chain_id" in src, f"{name} no longer reads the chain id at all"
