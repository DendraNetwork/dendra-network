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
from fractions import Fraction   # EXACT ordering over rationals: mirrors the chain's big.Int cross-product
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


# WHERE THE KEYRING LIVES. Building `dendrad tx ... --keyring-backend test` WITHOUT `--keyring-dir`
# and `--home` makes the binary look for keys in its default home (/root/.dendra) while they actually
# live in /data/keys/cosmos. The result is "key not found", `wait_tx` returning False, and a judge that
# prints "verdict NOT anchored (retrying)" forever without ever saying why.
# That failure stays INVISIBLE because the miner daemon SIGNS nothing: it serves inference through the
# relay, so the first component that needs a key is the judge. A machine can therefore mine, be paid
# and look healthy while being STRUCTURALLY unable to vote.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")  # e.g. /data/keys/cosmos
HOME_DIR = os.environ.get("DENDRA_HOME", "")            # e.g. /root/.dendra


def _node():
    return ["--node", NODE] if NODE else []


def _keys():
    """EXPLICIT keyring/home location. Empty means the binary's default home."""
    out = []
    if KEYRING_DIR:
        out += ["--keyring-dir", KEYRING_DIR]
    if HOME_DIR:
        out += ["--home", HOME_DIR]
    return out


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
           "--chain-id", CHAIN, "--yes", *_keys(), *_node()]
    o = ""
    for attempt in range(6):
        o = run(cmd)
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    # NEVER SWALLOW THE FAILURE REASON. The caller only receives a boolean through `wait_tx`; without
    # this output, a signing failure is indistinguishable from a sequence failure or from a silent
    # node, and the operator has nothing to work from.
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


def list_all(subcmd, field, t=60, max_pages=400):
    """FULL list of a paginated `query jobs <subcmd>`, page after page.

    WHY THIS EXISTS. `dendrad query jobs list-job` WITHOUT pagination stops at **100 entries**:
    ListJob calls `query.CollectionPaginate` (query_job.go) and the SDK applies `DefaultLimit = 100`
    (cosmos-sdk/types/query/pagination.go) when the request carries Limit=0. A single un-paginated
    call that calls itself "ALL jobs" therefore hands the exporter, the points ranking and The Proof a
    silent PREFIX of reality past 100 jobs. Truncation here can only ever produce GOOD
    news (fewer disputes, fewer slashes, fewer deferred audits): a measurement whose failure mode is
    indistinguishable from a clean result.

    Returns `None` if a page cannot be read — callers must treat None as "not measured", never as [].
    """
    out, key, pages = [], None, 0
    while pages < max_pages:
        cmd = ["dendrad", "query", "jobs", subcmd, "--page-limit", "500", "--output", "json", *_node()]
        if key:
            cmd += ["--page-key", key]
        d = out_json(cmd, t)
        if not isinstance(d, dict) or not d:
            return None
        out.extend(d.get(field, []) or d.get(field.capitalize(), []) or [])
        key = (d.get("pagination") or {}).get("next_key") or (d.get("pagination") or {}).get("nextKey")
        pages += 1
        if not key:
            return out
    sys.stderr.write(f"[client] {subcmd}: >{max_pages} pages -> INCOMPLETE read, refusing\n")
    return None


def registered_miners():
    """List [{id, operator, stake, balance}] of registered miners (ALL pages)."""
    rows = list_all("list-miner", "miner")
    if rows is None:
        sys.stderr.write("[client] list-miner unreadable -> 0 miners RETURNED (not measured)\n")
        rows = []
    out = []
    for m in rows:
        mid = _g(m, "minerId", "miner_id")
        op = _g(m, "operator")
        stake = int(_g(m, "stake", "Stake", default="0") or 0)
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
    Consumed by exporter (strict-finality metric), points_indexer, the_proof.
    "ALL" is literal: the read is page by page (see list_all), since one call stops at the SDK's 100-entry default."""
    rows = list_all("list-job", "job", t=180)
    if rows is None:  # unreadable -> WARN, never a silent 0 (a false-green)
        sys.stderr.write("[client] list-job unreadable/incomplete -> 0 jobs RETURNED (not measured)\n")
        rows = []
    out = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        slashes = [{"miner_id": _g(s, "minerId", "miner_id"), "amount": int(_g(s, "amount", default="0") or 0)}
                   for s in (_g(j, "slashRecords", "slash_records", default=[]) or []) if isinstance(s, dict)]
        out.append({"id": _g(j, "jobId", "job_id"), "state": _g(j, "state"),
                    "miner_id": _g(j, "minerId", "miner_id"), "client": _g(j, "client"),
                    "fee": int(_g(j, "fee", default="0") or 0),
                    # ADR-034 partition: the plurality of OPERATORS behind the decision (0 outside a
                    # resolved audit). A monitoring gate reads the MIN of distinct_voters AND the
                    # largest single-operator share (max_operator_votes/voters < 50 %) over the audits
                    # closed by quorum.
                    "audit_distinct_voters": int(_g(j, "auditDistinctVoters", "audit_distinct_voters", default="0") or 0),
                    "audit_voters": int(_g(j, "auditVoters", "audit_voters", default="0") or 0),
                    "audit_max_operator_votes": int(_g(j, "auditMaxOperatorVotes", "audit_max_operator_votes", default="0") or 0),
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

    # ⛔ TWO HEIGHTS, AND DROPPING ONE MADE THE OTHER A LIE FOR EVERY CONSUMER.
    # `CommitteeSeedHealth` (query_committee_seed_health.go:29) tries height H, then H-1, and keeps the
    # FIRST that carries a seed. So `latest_contributors` is the count at `latest_seed_height` — which
    # is H-1 whenever the current block has not written its seed yet — while `current_height` is merely
    # when the query ran. This normaliser exposed only the second and named it `height`, so a consumer
    # had no way to know which block the contributor count belonged to. The site then labelled that
    # count "this block", asserting a scope the payload could not support. Pass BOTH through: a reading
    # that cannot say which block it describes is not a measurement.
    seed_h = pick("latest_seed_height", "latestSeedHeight")
    return {
        "source": int(pick("committee_seed_source", "committeeSeedSource") or 0),
        "min": int(pick("committee_min_vrf_contributors", "committeeMinVrfContributors") or 0),
        "contributors": int(pick("latest_contributors", "latestContributors") or 0),
        "has_seed": bool(pick("has_recent_seed", "hasRecentSeed") or False),
        "height": int(pick("current_height", "currentHeight") or 0),
        # None, never 0: with no seed the chain omits the field, and a height of 0 would claim the
        # genesis block carried the reading. Absent stays absent.
        "seed_height": (int(seed_h) if seed_h not in (None, "") else None),
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
    """The ANCHORED committee seed of this job, or "" when it is absent or not yet revealed.

    A STALE PATTERN IS A SILENT OUTAGE. A pattern accepting only the historical `height:index` form
    (`[0-9]+:[0-9]+`) stops matching entirely once the seed becomes a 64-character hex VRF output, and
    the `else ""` makes that failure indistinguishable from a genuinely absent beacon. The measured
    consequence: the client drew its committee from the EMPTY seed and sent the work to miner X while
    the chain, which requires `Seed != ""`, reserved payment for miner Y. The honest miner inferred,
    anchored its proof, and was NEVER paid; the client's escrow stayed locked and the job stayed
    `open` forever. It is invisible with a SINGLE registered miner, since every draw then agrees: the
    defect arms itself the day the network stops being one. Both formats are therefore accepted, and
    the caller must treat "" as a STOP, never as a default."""
    m = re.search(r'seed:\s*"?([0-9a-fA-F]{16,}|[0-9]+:[0-9]+)"?', query("get-beacon", jid))
    return m.group(1) if m else ""


def wait_beacon(jid, timeout=45):
    """Waits for the seed to be REVEALED (the EndBlocker reveals it with a delay).

    Drawing a committee without a seed is worse than an error: it is a draw SILENTLY different from
    the chain's. So wait, then fail LOUDLY rather than fall back to ""."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = get_beacon(jid)
        if b:
            return b
        time.sleep(2)
    return ""


def assigned_committee(jid, k=None, timeout=45, poll=2.0):
    """The ASSIGNED committee, READ from the chain (`query jobs assigned-committee`, ADR-034) - the
    query that KILLS THE REPLICA: the client no longer re-derives the draw, it ASKS for it.
    members[0] is the PRIMARY, the one settleOptimistic pays; the committee of size k is the PREFIX,
    a property of the total order that the chain guarantees and tests.

    The chain REFUSES while the seed is unrevealed, so this POLLS until `timeout` and then fails
    LOUDLY with a RuntimeError. There is NEVER a silent fallback to a local draw: that fallback -
    empty seed first, then pure-hash versus hash/stake ordering - is what cost the network every
    settlement it had."""
    deadline = time.time() + timeout
    last = ""
    while True:
        r = subprocess.run(["dendrad", "query", "jobs", "assigned-committee", jid,
                            "--output", "json", *_node()], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and (r.stdout or "").strip():
            try:
                members = list(json.loads(r.stdout).get("members") or [])
            except Exception:
                members = []
            if members:
                if k is not None and len(members) < k:
                    raise RuntimeError(
                        f"assigned-committee {jid}: incomplete committee ({len(members)} < {k}) - "
                        f"not enough registered miners to seal at k={k}.")
                return members[:k] if k else members
            last = "response with no members (`members` field empty or omitted)"
        else:
            last = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:300] or "(empty output)"
        if time.time() >= deadline:
            raise RuntimeError(
                f"assigned-committee {jid}: the chain returned no committee within {timeout}s "
                f"(last reason: {last}). REFUSING to re-derive locally: a local draw that diverged "
                f"would send the work to a miner the chain will never pay.")
        time.sleep(poll)


def audit_deferred():
    """Audits SELECTED but DEFERRED, plus the opening counter (ADR-034 partition).

    -> {"job_ids": [...], "audits_opened": int}, or None when NOT MEASURABLE (query absent on an old
    binary, or node unreachable). The discriminant is the return code and stdout, NOT an empty dict:
    the CLI output is snake_case and OMITS empty fields, so "zero deferred" arrives as {} - which
    would be indistinguishable from an error if it went through out_json.
    None is not []: "not measured" must never propagate as "zero", because a false zero propagates."""
    try:
        r = subprocess.run(["dendrad", "query", "jobs", "audit-deferred", "--output", "json", *_node()],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    return {"job_ids": list(d.get("job_ids") or []),
            "audits_opened": int(d.get("audits_opened") or 0)}


def held_summary():
    """Fee-hold retentions: {"held_total", "held_count", "oldest_age_blocks", "oldest_job_id"}.

    "Retained is not lost" is the central promise of the sub-quorum path, and these two numbers are
    what can CONTRADICT it: a total that keeps growing means the escrow is stuck, and an age that only
    ever rises means the network is not healing. None when NOT MEASURABLE (query absent, node
    unreachable) - same return-code/stdout discriminant as audit_deferred, because the CLI output
    omits zero fields, so a legitimate {} is indistinguishable from an error through out_json."""
    try:
        r = subprocess.run(["dendrad", "query", "jobs", "held-summary", "--output", "json", *_node()],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    # The `pockets_measured` SENTINEL is ALWAYS non-zero chain-side, so it is always EMITTED despite
    # the codec omitting zeros. It is what makes "0, measured" PROVABLE at FIELD level: absent (0)
    # means a binary older than pockets 2 and 3, whose zeros are NOT measurements, hence None and
    # never a false "healthy zero"; >= n means the zeros of pocket n ARE measurements.
    pockets = int(d.get("pockets_measured") or 0)

    def _pocket(n, key, is_str=False):
        if pockets < n:
            return None  # pocket not measurable on this binary: None is not 0, at field level
        return (d.get(key) or "") if is_str else int(d.get(key) or 0)

    return {"held_total": int(d.get("held_total") or 0),
            "held_count": int(d.get("held_count") or 0),
            "oldest_age_blocks": int(d.get("oldest_age_blocks") or 0),
            "oldest_job_id": d.get("oldest_job_id") or "",
            "pockets_measured": pockets,
            # The SECOND pocket: dispute bonds held in escrow for open disputes.
            "bond_escrowed_total": _pocket(2, "bond_escrowed_total"),
            "bond_escrowed_count": _pocket(2, "bond_escrowed_count"),
            # The THIRD pocket: jurors RETAINED by the exit guard, i.e. seats anchored to unresolved
            # audits. "Locked is not seized" is measured here; whether to allow exit-after-vote is a
            # decision to be made on these numbers from an open testnet, not on intuition.
            "jurors_locked_count": _pocket(3, "jurors_locked_count"),
            "oldest_lock_age_blocks": _pocket(3, "oldest_lock_age_blocks"),
            "oldest_lock_job_id": _pocket(3, "oldest_lock_job_id", is_str=True)}


def prune_window_blocks():
    """The eviction window for inert miners (miner_prune_blocks), READ from the on-chain params.
    0 or omitted falls back to the compiled constant (~7 days = 604800). It is the THRESHOLD of the
    retention-age warning: if funds stay retained longer than it takes to evict a dead miner, the
    network is not healing. None when the params are unreadable - no threshold is ever invented."""
    try:
        d = json.loads(query("params", "--output", "json"))
        p = d.get("params", d) if isinstance(d, dict) else {}
        v = int(p.get("miner_prune_blocks") or p.get("minerPruneBlocks") or 0)
        return v if v > 0 else 604800
    except Exception:
        return None


def committee(seed, miner_ids, k=COMMITTEE_K, stakes=None):
    """A reference REPLICA for parity tests and fixture extraction - NOT the submission path.

    Since the `assigned-committee` query exists (ADR-034), `submit_job` ASKS the chain for the
    committee (`assigned_committee()` above) and the client holds no model of the draw. This function
    survives ONLY as an executable reference for the arithmetic (h/stake, exact cross-product), used
    by parity tests and to extract fixtures.

    It is an EXACT replica of `selectCommittee`/`lessByStakeScore`
    (chain/x/jobs/keeper/committee.go). Any divergence from the chain means the client sends work
    to a miner the chain will not pay: free work and a locked escrow.

    The chain sorts by h/stake with an EXACT big.Int cross-product; Fraction is exact too. Floats are
    never used: one rounding is enough to swap two neighbours. Zero stake is relegated to the end and
    ties are broken by id, exactly as on-chain.

    `stakes` ABSENT means the "equal stakes" assumption, i.e. ordering by pure hash. That is correct
    only if all stakes really are equal, so callers that COMMIT work or money must supply them."""
    st = {m: int((stakes or {}).get(m, 0) or 0) for m in miner_ids}
    weighted = any(v > 0 for v in st.values())

    # ANTI-DIVERGENCE: never fall back to PURE-hash ordering SILENTLY when the caller SUPPLIED stakes.
    # Supplying `stakes` means this draw commits work and money. If NO stake is > 0, that is almost
    # never a network genuinely at zero stake - every miner holds a real bond >= min_stake - it is the
    # JSON `stake` field being read as 0 because its name or case changed. Pure-hash ordering then
    # diverges from the chain's hash/stake ordering, the served miner is never paid and the escrow is
    # locked. Refuse loudly rather than diverge without a word. (stakes=None remains the assumed
    # "equal stakes" hypothesis, for read-only display.)
    if stakes is not None and miner_ids and not weighted:
        raise ValueError(
            "committee(): stakes were supplied but ALL of them are 0 -> the 'stake' field is probably "
            "missing or renamed in the output of `query jobs list-miner`. A pure-hash draw would DIVERGE "
            "from the chain's hash/stake ordering (served miner never paid, escrow locked). Refusing. "
            "Check the JSON of list-miner.")

    def sort_key(m):
        h = int.from_bytes(hashlib.sha256(f"{seed}|{m}".encode()).digest(), "big")
        if not weighted:
            return (0, Fraction(h), m)
        s = st.get(m, 0)
        if s <= 0:
            return (1, Fraction(0), m)      # zero stake: never prioritized
        return (0, Fraction(h, s), m)

    return sorted(miner_ids, key=sort_key)[:k]


def submit_job(prompt, fee, relay_url, client="alice", k=None, jid=None, max_out=0):
    """Opens a job (escrow), assigns the committee via the beacon, seals the prompt and POSTs to the relay.
    Returns {jid, committee, keys{mid:session_key}, prompt}. The `keys` must be kept
    by the caller to decrypt the responses. `k=None` -> the sealed size is READ from the chain's mode
    (k=1 under optimistic settlement)."""
    if k is None:
        k = _submit_committee_k()  # seal to k=1 under optimistic settlement; the client holds no k=3 model
    jid = jid or f"job{int(time.time()*1000)}"
    if not _RE_JID.match(jid) or "__" in jid:   # safe jid BEFORE any dendrad call
        return {"error": "invalid jid (alphanumeric/underscore, no '__')"}
    if not wait_tx(tx_from(client, "open-job", jid, str(int(fee)))):
        return {"error": "open-job (escrow) failed (client balance too low?)"}
    # The committee is READ from the chain (ADR-034); there is NO local re-derivation left.
    # The query refuses while the seed is unrevealed, so its polling REPLACES wait_beacon and the
    # failure is LOUD - never a local fallback draw, which diverges from the chain in silence.
    try:
        comm = assigned_committee(jid, k)
    except RuntimeError as e:
        return {"error": str(e)}
    _miners = registered_miners()   # a single RPC call, reused below (on-chain pubkeys)
    beacon = get_beacon(jid)        # informational only, for display and diagnostics: the draw comes from the chain
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
            return {"error": f"miner {mid} has no on-chain anchored pubkey (strict mode) -> refused"}
        else:
            rp = relay.get(relay_url, "pub", mid, retries=10)
            if not rp:
                return {"error": f"public key of miner {mid} not found (neither on-chain nor at the relay)"}
            print(f"[client] WARNING: {mid} has no on-chain pubkey -> falling back to the relay pubkey (unauthenticated)")
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


def _tx_reason(o):
    """A READABLE reason for a refused tx: raw_log when present, otherwise the code."""
    m = re.search(r'raw_log:\s*"?([^\n"]+)', o) or re.search(r'(code:\s*\d+[^\n]*)', o)
    r = (m.group(1).strip() if m else "")
    return r or (o.strip()[:200] or "(empty output)")


def wait_tx_reason(o, timeout=24):
    """Like `wait_tx`, but PROPAGATES the reason instead of losing it -> (ok, reason).

    `wait_tx` returns a boolean, which makes a business refusal indistinguishable from a slow network.
    That is what made the "primary commit missing" refusal INVISIBLE across every job on the
    network."""
    if not _ok(o):
        return False, _tx_reason(o)
    h = re.search(r'txhash:\s*([A-Fa-f0-9]{64})', o)
    if not h:
        return False, "no txhash in the broadcast response"
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return (True, "") if _ok(q) else (False, _tx_reason(q))
        time.sleep(2)
    return False, f"tx {h.group(1)[:12]}... never included (timeout {timeout * 2}s)"


_SETTLE_WARNED = set()


def _warn_settle_failure(jid, why):
    """A LOUD ASSERTION ON DRAW PARITY.

    The committee is computed TWICE: here, off-chain, and by the chain at settlement. Two copies of a
    consensus arithmetic eventually diverge - and these two already had, once on an empty client-side
    seed and once on pure-hash versus hash/stake ordering, masked while all stakes were equal. When
    they diverge, the chain reserves payment for a miner that did no work: the honest miner is NEVER
    paid and the client's escrow stays locked forever, without a single message.

    Where the comparison cannot be made BEFOREHAND, the gap is made loud AFTERWARDS: the settlement
    refusal is the observable symptom of the divergence. Printed once per job."""
    if jid in _SETTLE_WARNED:
        return
    _SETTLE_WARNED.add(jid)
    print(f"[client] SETTLEMENT REFUSED  job={jid}: {why}", flush=True)
    low = why.lower()
    if "primaire" in low or "primary" in low or "comite" in low or "committee" in low:
        print("[client]   ^^ DRAW DIVERGENCE: the chain designated a DIFFERENT primary from the one "
              "that served the job. The miner that did the work WILL NOT be paid and the escrow stays "
              "locked. Known causes: a committee seed absent or stale on the client side, or a "
              "desynchronized stake weighting. Check `query jobs get-beacon <jid>` and draw parity.",
              flush=True)


def settle(jid, reward, client="alice", threshold_bps=7000):
    """SEMANTIC settlement: pays the SAME-MEANING majority (from escrow) and slashes the truly
    aberrant ones. Robust to LLM non-determinism (two honest responses stay close in cosine).
    Idempotent."""
    out = tx_from(client, "settle-semantic", jid, str(int(reward)), str(int(threshold_bps)))
    ok, why = wait_tx_reason(out)
    if not ok:
        _warn_settle_failure(jid, why)
    return {"settled": ok, "reason": why}


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


def _submit_committee_k() -> int:
    """The committee size to SEAL to, READ from the chain's mode. Under OPTIMISTIC settlement (k=1)
    the chain pays ONLY the primary, so sealing to COMMITTEE_K=3 makes two miners infer for NOTHING:
    GPU work burned, their commits never settled, and a third commit exposed to the k=3 slash paths.
    Sealing to 1 also turns `reward = actual_fee // k` (quick_metered) into `actual_fee // 1`, so the
    primary is paid for REAL WORK - the chain applies the 85 % fee / 15 % protocol split - instead of
    a third by construction. The client DOES NOT HOLD the protocol model, it READS it. It falls back
    to COMMITTEE_K only when the mode cannot be determined."""
    return 1 if _verification_mode() == 1 else COMMITTEE_K


def settle_when_ready(jid, reward, committee_ids, client="alice", commit_wait=180):
    """Waits until the REQUIRED ON-CHAIN commits are anchored BEFORE settling, then settles. Closes a RACE:
    the miner posts its response to the relay THEN anchors its commit on-chain, so a client that settles as soon as
    the RELAY responses arrive runs ahead of the commit -> SettleSemantic refuses ("incomplete committee" / "primary commit missing").

    Two properties this loop holds:
      - MODE-AWARE THRESHOLD: in OPTIMISTIC mode (k=1) only the PRIMARY commit is required (need=1), because at
        k=1 the primary is the only one that commits -- a threshold set on the FULL committee is never reached
        there and the wait-loop expires on EVERY job for nothing. In redundant mode (k=3): full committee.
      - UNIFIED poll->settle LOOP: settlement fires AS SOON AS `need` commits are anchored and RE-TRIES while the
        deadline holds, rather than a fixed wait followed by a fixed number of attempts -> settles as early as possible AND holds
        the whole window under load. Residual ROOT CAUSE of a late primary commit = GPU contention on the bench (primary commit > deadline); in PROD
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


def quick(prompt, fee, reward, relay_url, client="alice", k=None, timeout=180):
    """Synchronous: submits, waits for the committee's responses, settles, returns the response + details."""
    if k is None:
        k = _submit_committee_k()  # k=1 under optimistic settlement
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


def quick_metered(prompt, base, per_token, out_allow, relay_url, client="alice", k=None, timeout=240):
    """Pricing by REAL WORK, in 2 PHASES:
      1) ESCROW a MAX = base + price/token x (input_tokens + CAPPED output);
      2) once the response is obtained, we SETTLE on the EFFECTIVE output (tokens actually produced).
    The miner is thus paid per token actually generated (a long prompt AND a long response cost
    more). The escrow covers the max; any surplus stays in the module (refund = future work).
    """
    if k is None:
        k = _submit_committee_k()  # k=1 under optimistic settlement -> reward = actual_fee//1 (real work, not a third)
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
