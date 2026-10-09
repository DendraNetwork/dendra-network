"""THE CHAIN ID IS READ, NEVER WRITTEN (ADR-048 item 8).

Five services wrote `CHAIN = "dendra"`, the faucet defaulted to it, and the compose file and the launcher
spelled it again. A relaunch under a new name (`dendra-testnet`) would have left every one of them signing
for a chain that no longer exists, and the error a signer gets is `signature verification failed`, which
says nothing about a name. So the name comes from two places only, and they are compared:

  * DENDRA_CHAIN_ID, when the operator sets it (the kit's network file does);
  * the node itself: `dendrad status` reports the network it serves, so a miner pointed at a node learns
    the name from the one machine that cannot be wrong about it.

Both present and DIFFERENT is a refusal, not a choice: the operator named one network and points at
another. Neither present is a refusal too. There is no default, because the only plausible default is the
name of the previous chain, and that is exactly the wrong one (the rule of zero: on a criterion
that decides what is signed, an absence is an error, never a fallback).
"""
from __future__ import annotations

import json
import os
import subprocess


class ChainIdError(RuntimeError):
    """The chain id could not be established, or the two sources disagree."""


def network_from_status(text: str) -> str:
    """The `network` field of `dendrad status`, or "" when the text carries none.

    The SDK has printed both `node_info` and `NodeInfo` across versions; both are read. Anything that is
    not JSON (a node that does not answer prints an error) yields "", which the caller treats as UNKNOWN.

    The text is read whole first, then line by line: the two output streams are joined (see `_default_run`),
    and a binary built with a newer Go toolchain prints a warning line on stderr. Measured: with the whole
    text as the only candidate, that one line made the chain id unknown. A line is only a candidate when it
    is a JSON object; a warning is never parsed into anything.
    """
    if not isinstance(text, str):
        return ""
    candidates = [text] + [ln for ln in text.splitlines() if ln.lstrip().startswith("{")]
    for cand in candidates:
        try:
            doc = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue
        for key in ("node_info", "NodeInfo"):
            info = doc.get(key)
            if isinstance(info, dict):
                net = info.get("network")
                if isinstance(net, str) and net.strip():
                    return net.strip()
    return ""


def _default_run(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    # `status` prints its JSON on stdout in some SDK versions and on stderr in others.
    return (r.stdout or "") + (r.stderr or "")


def resolve(node_flags=(), env=None, run=None) -> str:
    """Returns the chain id, or raises ChainIdError. Never returns a guess."""
    env = os.environ if env is None else env
    declared = (env.get("DENDRA_CHAIN_ID") or "").strip()
    try:
        served = network_from_status((run or _default_run)(["dendrad", "status", *node_flags]))
    except Exception:  # noqa: BLE001 — an unreachable node is UNKNOWN, decided below
        served = ""
    if declared and served and declared != served:
        raise ChainIdError(
            f"DENDRA_CHAIN_ID is {declared!r} but the node serves {served!r}: this process would sign for one "
            f"network while talking to another. Fix DENDRA_CHAIN_ID or DENDRA_NODE; nothing is guessed.")
    if declared:
        return declared
    if served:
        return served
    raise ChainIdError(
        "the chain id is unknown: DENDRA_CHAIN_ID is not set and the node did not answer `dendrad status`. "
        "Set DENDRA_CHAIN_ID (the network file carries it) or point DENDRA_NODE at a running node.")


_cache: dict = {}


def chain_id(node_flags=()) -> str:
    """Process-wide cached `resolve`. A network does not change name while a process runs; asking the node
    once is enough, and asking it on every transaction would turn a slow node into a slow signer."""
    key = tuple(node_flags)
    if key not in _cache:
        _cache[key] = resolve(node_flags)
    return _cache[key]
