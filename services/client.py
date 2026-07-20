"""Dendra CLIENT engine: submit an inference request, track it, settle, and read network state.

Reused by the web dashboard and the CLI. Job flow (hash mode, local, deterministic Ollama):
  open-job (ESCROW of the fee) -> beacon -> assigned committee -> seals the prompt to each member and
  POSTs to the relay -> miners infer and return sealed responses + anchor their commit
  -> the client decrypts, pays the winners (payout from escrow) and slashes the divergent ones (finalize).
Content (prompt/response) travels ONLY ENCRYPTED (relay); the chain only sees metadata + hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# robustness: ensure `dendrad` is findable regardless of the launch context
os.environ["PATH"] = (os.environ.get("PATH", "") + os.pathsep
                      + os.path.expanduser("~/go/bin") + os.pathsep + "/snap/bin"
                      + os.pathsep + "/usr/local/bin" + os.pathsep + "/usr/bin")

from modea.client import Client
from modea.crypto import Sealed
import relay_client as relay

CHAIN = "dendra"
COMMITTEE_K = int(os.environ.get("DENDRA_COMMITTEE_K", "3"))
NODE = os.environ.get("DENDRA_NODE", "")  # e.g. "tcp://chain:26657" in a container; "" = local node
_RE_JID = re.compile(r"^[A-Za-z0-9_]+$")  # sane jid (no dendrad arg injection, no "__")


def _node():
    return ["--node", NODE] if NODE else []


def run(cmd, t=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
    return (r.stdout or "") + (r.stderr or "")


def out_json(cmd, t=60):
    try:
        return json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=t).stdout or "")
    except Exception:
        return {}


def tx_from(frm, *a):
    # robust NONCE: shared client account under concurrency -> retry on "account sequence
    # mismatch" (dendrad re-fetches the sequence each attempt). Also covers the debit-kit nonce (1 account/conc).
    cmd = ["dendrad", "tx", "jobs", *a, "--from", frm, "--keyring-backend", "test",
           "--chain-id", CHAIN, "--yes", *_node()]
    o = ""
    for attempt in range(6):
        o = run(cmd)
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    return o


def query(*a):
    return run(["dendrad", "query", "jobs", *a, *_node()])


def _ok(t):
    m = re.search(r'(^|\n)code: (\d+)', t)
    return bool(m) and m.group(2) == "0"


def wait_tx(o, timeout=24):
    if not _ok(o):
        return False
    h = re.search(r'txhash:\s*([A-Fa-f0-9]{64})', o)
    if not h:
        return False
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return _ok(q)
        time.sleep(2)
    return False


def _g(d, *keys, default=""):
    for k in keys:
        if k in d:
            return d[k]
    return default


def registered_miners():
    """List [{id, operator, stake, balance}] of registered miners."""
    d = out_json(["dendrad", "query", "jobs", "list-miner", "--output", "json"])
    out = []
    for m in d.get("miner", []) or d.get("Miner", []):
        mid = _g(m, "minerId", "miner_id")
        op = _g(m, "operator")
        stake = int(_g(m, "stake", default="0") or 0)
        out.append({"id": mid, "operator": op, "stake": stake, "balance": balance(op),
                    "demand": int(_g(m, "demand", "Demand", default="0") or 0),  # REAL cumulative client demand (non-recoverable)
                    "enc_pubkey": _g(m, "encPubkey", "enc_pubkey")})  # on-chain anchored pub
    return out


def balance(addr, denom="udndr"):
    if not addr:
        return 0
    d = out_json(["dendrad", "query", "bank", "balances", addr, "--output", "json", *_node()])
    for c in d.get("balances", []):
        if c.get("denom") == denom:
            return int(c["amount"])
    return 0


def list_jobs_full():
    """ALL jobs [{id, state, miner_id, client, fee}] via `list-job` (query_job.go::ListJob).
    Fields = proto Job (job.proto:10-14); tolerates snake_case AND camelCase (gogoproto/JSON conventions).
    Consumed by exporter (strict-finality metric). Single RPC call."""
    d = out_json(["dendrad", "query", "jobs", "list-job", "--output", "json", *_node()])
    rows = d.get("job", []) or d.get("Job", []) or []
    if d and not rows:  # unexpected format (updated binary?) -> WARN, otherwise silent 0 = false-red
        sys.stderr.write(f"[client] list-job : format inattendu (cles={list(d)[:6]}) -> 0 job vu\n")
    out = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        slashes = [{"miner_id": _g(s, "minerId", "miner_id"), "amount": int(_g(s, "amount", default="0") or 0)}
                   for s in (_g(j, "slashRecords", "slash_records", default=[]) or []) if isinstance(s, dict)]
        out.append({"id": _g(j, "jobId", "job_id"), "state": _g(j, "state"),
                    "miner_id": _g(j, "minerId", "miner_id"), "client": _g(j, "client"),
                    "fee": int(_g(j, "fee", default="0") or 0),
                    "slashes": slashes})  # The Proof: SlashRecords from the proto Job (clawback-able)
    return out


def pools():
    d = out_json(["dendrad", "query", "jobs", "get-pools", "--output", "json"])
    p = d.get("Pools") or d.get("pools") or d
    keys = ["minerPaid", "miner_paid", "validators", "team", "treasury", "minerLocked", "miner_locked"]
    out = {}
    for k in keys:
        if k in p:
            out[k.replace("_", "")] = int(p[k] or 0)
    return out


def committee_seed_health():
    """VRF bootstrap: health of the decentralized committee randomness. Normalized dict (snake/camel)
    {source, min, contributors, has_seed, height} -> the exporter derives dendra_committee_grinding_inactive."""
    d = out_json(["dendrad", "query", "jobs", "committee-seed-health", "--output", "json"]) or {}

    def pick(*ks):
        for k in ks:
            if k in d:
                return d[k]
        return None

    return {
        "source": int(pick("committee_seed_source", "committeeSeedSource") or 0),
        "min": int(pick("committee_min_vrf_contributors", "committeeMinVrfContributors") or 0),
        "contributors": int(pick("latest_contributors", "latestContributors") or 0),
        "has_seed": bool(pick("has_recent_seed", "hasRecentSeed") or False),
        "height": int(pick("current_height", "currentHeight") or 0),
    }


def height():
    d = out_json(["dendrad", "status", "--output", "json"]) or out_json(["dendrad", "status"])
    try:
        return int(d.get("sync_info", {}).get("latest_block_height", 0))
    except Exception:
        return 0


def network_state():
    return {"miners": registered_miners(), "pools": pools(), "height": height()}


def get_beacon(jid):
    m = re.search(r'seed:\s*"?([0-9]+:[0-9]+)"?', query("get-beacon", jid))
    return m.group(1) if m else ""


def committee(seed, miner_ids, k=COMMITTEE_K):
    return sorted(miner_ids, key=lambda m: hashlib.sha256(f"{seed}|{m}".encode()).digest())[:k]


def submit_job(prompt, fee, relay_url, client="alice", k=COMMITTEE_K, jid=None, max_out=0):
    """Opens a job (escrow), assigns the committee via the beacon, seals the prompt and POSTs to the relay.
    Returns {jid, committee, keys{mid:session_key}, prompt}. The `keys` must be kept
    by the caller to decrypt the responses."""
    jid = jid or f"job{int(time.time()*1000)}"
    if not _RE_JID.match(jid) or "__" in jid:   # safe jid BEFORE any dendrad call
        return {"error": "jid invalide (alphanumerique/underscore, sans '__')"}
    if not wait_tx(tx_from(client, "open-job", jid, str(int(fee)))):
        return {"error": "open-job (escrow) a echoue (solde du client insuffisant ?)"}
    beacon = get_beacon(jid)
    _miners = registered_miners()   # a single RPC call, reused below
    ids = [m["id"] for m in _miners]
    if len(ids) < k:
        return {"error": f"pas assez de mineurs enregistres ({len(ids)} < {k})"}
    comm = committee(f"{beacon}|{jid}", ids, k)
    cli = Client()
    keys = {}
    # The AUTHORITATIVE encryption pub is the one ANCHORED ON-CHAIN (signed by
    # the miner's Cosmos key), NOT the one announced to the relay (substitutable -> prompt MITM).
    onchain_pub = {m["id"]: (m.get("enc_pubkey") or "") for m in _miners}
    # on-chain pub REQUIRED BY DEFAULT -> anti-MITM crypto is active without configuration.
    # Local/legacy opt-out: DENDRA_REQUIRE_ONCHAIN_PUB=0 (falls back to the relay pub with a warning).
    strict = os.environ.get("DENDRA_REQUIRE_ONCHAIN_PUB", "1") != "0"
    for mid in comm:
        cpub = onchain_pub.get(mid, "")
        if cpub:
            rp = relay.get(relay_url, "pub", mid, retries=10) or {}
            if rp.get("pub") and rp["pub"] != cpub:
                print(f"[client] ALERT: relay pubkey != on-chain pubkey for {mid} -> relay ignored (anti-MITM)")
            pub_hex = cpub
        elif strict:
            return {"error": f"mineur {mid} sans pub ancree on-chain (mode strict) -> refuse"}
        else:
            rp = relay.get(relay_url, "pub", mid, retries=10)
            if not rp:
                return {"error": f"cle publique du mineur {mid} introuvable (ni on-chain ni relais)"}
            print(f"[client] AVERTISSEMENT : {mid} sans pub on-chain -> repli pub relais (non authentifiee)")
            pub_hex = rp["pub"]
        sub, key = cli.submit(jid, bytes.fromhex(pub_hex), prompt)
        keys[mid] = key
        relay.put(relay_url, "req", f"{jid}__{mid}",
                  {"client_eph_pk": sub.client_eph_pk.hex(),
                   "nonce": sub.sealed_prompt.nonce.hex(), "ct": sub.sealed_prompt.ct.hex(),
                   "max_out": max_out})   # requested output cap (client tier)
    return {"jid": jid, "committee": comm, "keys": keys, "prompt": prompt, "beacon": beacon}


def job_results(jid, committee_ids, keys, relay_url):
    """Decrypts the responses available at the relay. {mid: text|None}."""
    cli = Client()
    out = {}
    for mid in committee_ids:
        r = relay.get(relay_url, "res", f"{jid}__{mid}")
        if r:
            try:
                out[mid] = cli.open_result(jid, keys[mid], Sealed(bytes.fromhex(r["nonce"]), bytes.fromhex(r["ct"])))
            except Exception:
                out[mid] = None
        else:
            out[mid] = None
    return out


def canonical_answer(results):
    vals = [v for v in results.values() if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def settle(jid, reward, client="alice", threshold_bps=7000):
    """SEMANTIC settlement: pays the SAME-MEANING majority (from escrow) and slashes the truly
    aberrant ones. Robust to LLM non-determinism (two honest responses stay close in cosine).
    Idempotent."""
    ok = wait_tx(tx_from(client, "settle-semantic", jid, str(int(reward)), str(int(threshold_bps))))
    return {"settled": ok}


def _commit_anchored(text: str) -> bool:
    return ("resultCommit" in text) or ("result_commit" in text)


def _verification_mode() -> int:
    """0 = redundant k=3 (FULL committee required to settle); 1 = optimistic k=1 (only the PRIMARY commit
    is required). Read on-chain (`query jobs params`). ROBUST: default 0 if chain unreachable / JSON unreadable."""
    try:
        d = json.loads(query("params", "--output", "json"))
        p = d.get("params", d) if isinstance(d, dict) else {}
        return int(p.get("verification_mode") or p.get("verificationMode") or 0)
    except Exception:
        return 0


def settle_when_ready(jid, reward, committee_ids, client="alice", commit_wait=180):
    """Waits until the REQUIRED ON-CHAIN commits are anchored BEFORE settling, then settles. Fixes a RACE:
    the miner posts its response to the relay THEN anchors its commit on-chain; a client that settled as soon as
    the RELAY responses arrived got ahead of the commit -> SettleSemantic refused ("incomplete committee" / "primary commit missing").

    HARDENING v2:
      - MODE-AWARE THRESHOLD: in OPTIMISTIC mode (k=1) only the PRIMARY commit is required (need=1); previously we
        waited for the FULL committee -- never reached at k=1 (only the primary commits) -> the wait-loop expired on
        EVERY job for nothing, then settlement was tried only 5 times. In redundant mode (k=3): full committee.
      - UNIFIED poll->settle LOOP: we settle AS SOON AS `need` commits are anchored, and RE-TRY while the
        deadline is not reached (instead of a fixed wait followed by 5 attempts) -> settles as early as possible AND holds
        the whole window under load. Residual ROOT CAUSE = GPU contention on the bench (primary commit > deadline); in PROD
        miners are DISTRIBUTED (1 GPU each) -> no contention."""
    need = 1 if _verification_mode() == 1 else len(committee_ids)
    deadline = time.time() + commit_wait
    s = {"settled": False}
    while time.time() < deadline:
        anchored = sum(1 for mid in committee_ids
                       if _commit_anchored(query("get-commit", f"{jid}__{mid}")))
        if anchored >= need:
            s = settle(jid, reward, client=client)
            if s.get("settled"):
                return s
        time.sleep(3)
    return settle(jid, reward, client=client)  # last attempt (the commit may have arrived at the very end)


def quick(prompt, fee, reward, relay_url, client="alice", k=COMMITTEE_K, timeout=180):
    """Synchronous: submits, waits for the committee's responses, settles, returns the response + details."""
    sub = submit_job(prompt, fee, relay_url, client=client, k=k)
    if "error" in sub:
        return sub
    jid, comm, keys = sub["jid"], sub["committee"], sub["keys"]
    deadline = time.time() + timeout
    results = {}
    while time.time() < deadline:
        results = job_results(jid, comm, keys, relay_url)
        if all(results.get(m) for m in comm):
            break
        time.sleep(3)
    s = settle_when_ready(jid, reward, comm, client=client)
    return {"jid": jid, "committee": comm, "results": results,
            "answer": canonical_answer(results), "settle": s, "beacon": sub["beacon"]}


def job_tokens(jid, committee_ids, relay_url):
    """Tokens (input/output) REPORTED by the miners to the relay (committee median). 0 if absent."""
    ins, outs = [], []
    for mid in committee_ids:
        r = relay.get(relay_url, "res", f"{jid}__{mid}")
        if r:
            try:
                ins.append(int(r.get("in_tok", 0)))
                outs.append(int(r.get("out_tok", 0)))
            except Exception:
                pass
    med = lambda a: sorted(a)[len(a) // 2] if a else 0
    return med(ins), med(outs)


def quick_metered(prompt, base, per_token, out_allow, relay_url, client="alice", k=COMMITTEE_K, timeout=240):
    """Pricing by REAL WORK, in 2 PHASES:
      1) ESCROW a MAX = base + price/token x (input_tokens + CAPPED output);
      2) once the response is obtained, we SETTLE on the EFFECTIVE output (tokens actually produced).
    The miner is thus paid per token actually generated (a long prompt AND a long response cost
    more). The escrow covers the max; any surplus stays in the module (refund = future work).
    """
    in_tok = max(1, len(prompt) // 4)                       # ~4 characters / token
    fee_max = base + per_token * (in_tok + out_allow)       # escrowed upper bound
    sub = submit_job(prompt, fee_max, relay_url, client=client, k=k, max_out=out_allow)
    if "error" in sub:
        return sub
    jid, comm, keys = sub["jid"], sub["committee"], sub["keys"]
    deadline = time.time() + timeout
    results = {}
    while time.time() < deadline:
        results = job_results(jid, comm, keys, relay_url)
        if all(results.get(m) for m in comm):
            break
        time.sleep(3)
    answer = canonical_answer(results)
    # We BILL on what the CLIENT can VERIFY (it sent the prompt and
    # DECRYPTED the response), NOT on the miner's self-declaration. Miner in_tok/out_tok travel
    # OUTSIDE the AEAD -> a malicious committee or a MITM relay would inflate them up to the escrow
    # cap to bill the maximum. The miner figures are now used only for TELEMETRY.
    rep_in, rep_out = job_tokens(jid, comm, relay_url)      # telemetry only (NOT billed)
    in_tok = max(1, len(prompt) // 4)                       # the client knows the prompt
    bill_out = max(1, len(answer) // 4) if answer else 1   # the client decrypted the response
    truncated = bill_out >= out_allow                      # client-verifiable proxy for the cap being reached
    out_tok = min(out_allow, bill_out)
    actual_fee = min(base + per_token * (in_tok + out_tok), fee_max)   # never > escrow
    reward = max(1, actual_fee // k)                        # pays the work (CLIENT measure) per miner
    s = settle_when_ready(jid, reward, comm, client=client)
    return {"jid": jid, "committee": comm, "results": results, "answer": answer, "settle": s,
            "beacon": sub["beacon"], "in_tok": in_tok, "out_tok": out_tok, "truncated": truncated,
            "rep_in_tok": rep_in, "rep_out_tok": rep_out,  # miner self-declared: TELEMETRY, never the money
            "fee_escrow": fee_max, "fee_actual": reward * k}
