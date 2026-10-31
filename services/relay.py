#!/usr/bin/env python3
"""Untrusted network relay (L4): carries encrypted messages and public keys over HTTP
between the client and the miners. It never sees cleartext (only ciphertext + pubs).

Endpoints (JSON body):
  POST /pub/<mid>            {"pub": hex}                 -- a miner publishes its public key
  GET  /pub/<mid>            -> {"pub": hex}              -- the client fetches it
  POST /req/<jid>__<mid>     {"client_eph_pk","nonce","ct"} (hex) -- sealed request client->miner
  GET  /req/<jid>__<mid>     -> same
  POST /res/<jid>__<mid>     {"nonce","ct"} (hex)          -- sealed response miner->client
  GET  /res/<jid>__<mid>     -> same
  GET  /list                 -> {kind: [keys...]}          -- the work queue, open (keys only)
  GET  /list?suffix=<s>      -> same, only the keys ending with <s> (a miner asks for "__<its id>");
                                ETag + If-None-Match -> 304 while its slice of the queue is unchanged

Persistent storage (`relay_store.DiskStore`): RAM is only a hot cache, the disk is the truth.
Usage: python3 relay.py [port]   (default 8645, on 127.0.0.1).

Persistence is not optional. The sealed reveal is the only artifact that makes an audit judgeable
(the chain keeps only hashes of it). With an in-memory store a single restart would make every open
audit unjudgeable for good: the fee held forever, the client neither served nor refunded, the primary
neither paid nor slashed. So every deposit is written, the store is reloaded at startup, and the relay
refuses to start when the store is not writable, since starting without persistence would hide the
problem. `DENDRA_RELAY_STORE` chooses the location, never the existence (invariant: no environment
variable disables a guard).

Confidentiality is end to end, by Mode A: the relay is assumed hostile and only sees ciphertext. The
miner's public key at the relay is not the root of trust either: the client encrypts to the X25519 key
anchored on chain (signed by the miner's Cosmos key), so substituting a key at the relay achieves
nothing (the relay's /pub is only a cache).

Hardening:
  * A global lock: ThreadingHTTPServer with shared state and no data race (safe /list iteration).
  * The store is bounded per kind, in bytes (KIND_BUDGET): eviction by age only, a deposit beyond the
    budget is refused (507), and 80% of it raises an alarm in /stats.
  * A body size cap (MAX_BODY): no unbounded allocation.
  * A shared token (DENDRA_RELAY_TOKEN): once configured, the X-Dendra-Token header is required
    (empty, the local default, means no authentication).
  * A rate limit per IP (sliding window): a spammer can neither evict legitimate entries nor saturate
    the relay.
  * GET /list is open: it is how a miner learns a job is waiting for it, and its content (pairs of
    jobId and minerId) is already served to anyone by the chain. Writing it needs the token.
  Several machines: DENDRA_RELAY_TOKEN=<secret> on every node, and a WireGuard or SSH tunnel for the
  transport (the token authenticates; the tunnel encrypts and authenticates the channel). See the
  multi-machine deployment guide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_store  # noqa: E402  (sys.path must be set before this import)
from modea import (cosmos_addr, job_registry, kit_floor, registry_cache,  # noqa: E402  (same)
                   relay_antireplay, relay_canon, relay_carrier, relay_write)
from modea import relay_signature as _rs  # noqa: E402  (same)
from modea.exposure import (InvalidFlag, listens_locally,  # noqa: E402  (same)
                            public_enabled)

MAX_BODY = 1 << 20        # 1 MiB per request (well above a sealed prompt/response)
# -- The store budget: bytes per kind, not entries ------------------------------------------------
# The budget is a memory guard rail, and it never evicts. Eviction by count or by size would
# ignore that a reveal may still serve an open audit, so recent traffic could destroy the evidence of an
# older audit. Beyond the budget a deposit is refused (507); only age evicts.
#
# A count of entries is the wrong unit in both directions. Nothing expires before RETENTION, so a kind
# holds every deposit made within one window: a count low enough to bound memory refuses real traffic,
# and one high enough for the traffic bounds nothing (4000 bodies of MAX_BODY are 4 GiB per kind in RAM).
# Sizes measured on the public relay (read-only GETs of stored entries; /stats `bytes` over
# `occupation` gives the mean on any relay): sealed requests about 330 bytes, responses about 2 KB,
# reveals about 1.7 KB. A budget in bytes admits what the traffic weighs and bounds what the host holds.
# Worst case, every kind full: len(STORE) * KIND_BUDGET charged bytes.
#
# That worst case must fit the process's memory limit, or the budget never gets to refuse anything. The
# relay runs under a container memory limit (the relay service's `mem_limit` in docker-compose.yml) and
# reloads every record into RAM at boot, so a budget the container cannot hold turns a 507 into an
# out-of-memory kill, then a crash loop. The compiled floor keeps every kind full under that limit with
# room for the process; `verdict_budget_memory` compares the two at startup, from the limit read in the
# cgroup, and says so when a raised budget no longer fits.
#
# Every entry is charged a fixed cost on top of its key and body (ENTRY_OVERHEAD), for the Python objects
# and the file that hold it; without that charge ten thousand empty deposits would weigh nothing. It also
# bounds the count: KIND_BUDGET // ENTRY_OVERHEAD.
#
# The environment may raise the budget, never lower it: a value below the compiled floor would turn away
# the traffic the relay exists to carry (the same rule as RETENTION). An unreadable value keeps the floor
# and is reported at startup.
#
# The budget does not tell a hostile writer from an honest one: a token holder, or a registered miner
# signing under its own name, can still fill a kind (KIND_BUDGET / MAX_BODY deposits of maximal size).
# Only per-writer accounting would stop that, and this relay does not do it; it makes the filling visible
# (FILL_ALARM) before anyone is refused.
KIND_BUDGET_FLOOR = 64 << 20       # Compiled floor and default: 64 MiB per kind
ENTRY_OVERHEAD = 1024              # bytes charged per entry, on top of its key and its body
FILL_ALARM = 0.80                  # /stats lists a kind in `alarm` from this fill on
FILL_REARM = 0.75                  # ...and the log says it again only after it fell back under this


def _read_kind_budget(raw):
    """(budget in bytes, note for the startup banner). Never below the floor, never a guess.

    `raw` is MiB. Empty -> the floor, silently: that is the default. Anything else that does not
    raise the budget (unreadable, not finite, zero, negative, below the floor) -> the floor, and the
    note says which value was set aside and why."""
    if raw is None or not str(raw).strip():
        return KIND_BUDGET_FLOOR, ""
    try:
        mib = float(str(raw).strip())
    except ValueError:
        return KIND_BUDGET_FLOOR, (f"DENDRA_RELAY_KIND_BUDGET_MIB={str(raw)[:40]!r} is not a number "
                                   f"-> the floor applies")
    if not math.isfinite(mib) or mib <= 0:
        return KIND_BUDGET_FLOOR, (f"DENDRA_RELAY_KIND_BUDGET_MIB={str(raw)[:40]!r} is not a positive "
                                   f"size -> the floor applies")
    want = int(mib * (1 << 20))
    if want < KIND_BUDGET_FLOOR:
        return KIND_BUDGET_FLOOR, (f"DENDRA_RELAY_KIND_BUDGET_MIB={str(raw)[:40]!r} is below the "
                                   f"compiled floor ({KIND_BUDGET_FLOOR >> 20} MiB): the budget can be "
                                   f"raised, never lowered -> the floor applies")
    return want, ""


KIND_BUDGET, KIND_BUDGET_NOTE = _read_kind_budget(os.environ.get("DENDRA_RELAY_KIND_BUDGET_MIB"))
MEMORY_MARGIN = 0.80               # the store's worst case may take this share of the memory limit


def _cgroup_memory_limit(root="/"):
    """This process's memory limit in bytes, None when there is none, or "unknown" when it cannot be
    read. Three answers, never two: an unreadable limit is not "no limit".

    The process's own cgroup is read first (`/proc/self/cgroup`, the cgroup v2 line `0::<path>`): on a
    host the root of the hierarchy carries no `memory.max` at all, and inside a container the own
    cgroup is the root, so both cases land on the file that applies. `root` exists for the bench,
    which fabricates the two trees; the relay always reads "/"."""
    paths = []
    try:
        with open(os.path.join(root, "proc/self/cgroup")) as f:
            for line in f:
                if line.startswith("0::"):
                    own = line[3:].strip().strip("/")
                    if own:
                        paths.append(os.path.join(root, "sys/fs/cgroup", own, "memory.max"))
    except OSError:
        pass
    paths += [os.path.join(root, "sys/fs/cgroup/memory.max"),
              os.path.join(root, "sys/fs/cgroup/memory/memory.limit_in_bytes")]
    for path in paths:
        try:
            with open(path) as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            v = int(raw)
        except ValueError:
            return "unknown"
        return None if v >= (1 << 60) else v      # cgroup v1 writes a huge number for "unlimited"
    return "unknown"


def verdict_budget_memory(budget, kinds, limit):
    """Does every kind full fit the memory limit? Returns ("WARNING"|"OK", message).

    Pure, like the startup verdicts below, so that the bench can reach it. A warning and not a refusal: the
    worst case is a store full in every kind, which is not the state a relay boots in, and refusing to start
    over it would stop a relay that holds a few megabytes. It is reported at every start because the symptom, a
    relay that dies again at every reload, names nothing."""
    worst = int(budget) * int(kinds)
    if limit == "unknown":
        return ("OK", f"[relay] NOTICE: the memory limit of this process could not be read; the store's "
                      f"worst case ({kinds} kinds x {budget >> 20} MiB) is compared with nothing.")
    if limit is None or worst <= MEMORY_MARGIN * limit:
        return ("OK", "")
    return ("WARNING",
            f"[relay] WARNING: every kind full is {worst >> 20} MiB ({kinds} kinds x {budget >> 20} MiB) "
            f"against a memory limit of {limit >> 20} MiB: the process would be killed before the budget "
            f"refuses a deposit, and killed again at each reload. Raise the container's memory limit "
            f"together with DENDRA_RELAY_KIND_BUDGET_MIB.")
# The only eviction allowed is by age. An entry is discardable only once it has outlived the window in
# which an audit could still need it. 7 days is the same window as `miner_prune_blocks`, on which the
# release gate already alarms for held funds: one horizon to keep in mind, and it is the one that
# bounds the deferral loop (the deferral bound must stay under this horizon).
RETENTION_FLOOR = 86400            # Compiled floor: 24 h
RETENTION_DEFAULT = 604800         # 7 days
# The environment may lengthen the retention, never shorten it below the floor: otherwise
# `DENDRA_RELAY_RETENTION=1` would become a switch that disables the guard.
try:
    _RET = float(os.environ.get("DENDRA_RELAY_RETENTION", "") or RETENTION_DEFAULT)
except ValueError:
    _RET = RETENTION_DEFAULT
RETENTION = max(RETENTION_FLOOR, _RET)
TOKEN = os.environ.get("DENDRA_RELAY_TOKEN", "")          # shared auth (empty = off, local)
# Rotation without downtime. The token is shared, so changing it cuts off every client that has not
# migrated yet, and a rotation that keeps being postponed never happens. `DENDRA_RELAY_TOKEN_PREV` keeps
# the previous token valid during the switchover, with two guard rails: (1) empty has no effect at all
# (an empty variable opens nothing); (2) every use of the old token is counted and logged, which tells
# how many clients are left to migrate and so when the old token can be closed.
TOKEN_PREV = os.environ.get("DENDRA_RELAY_TOKEN_PREV", "")
_PREV_HITS = [0]
RATE_MAX = int(os.environ.get("DENDRA_RELAY_RATE", "120"))  # max req / window / IP
RATE_WINDOW = float(os.environ.get("DENDRA_RELAY_WINDOW", "10"))  # seconds
# Public exposure, read once through the shared reader (`modea.exposure`), so that the relay, the gateway,
# the judge and the content filter read `DENDRA_PUBLIC` the same way. An unexpected value is never guessed: it is recorded here and
# refused at startup by `main()`, never folded onto the permissive default.
PUBLIC = False
PUBLIC_ERR = ""
try:
    PUBLIC = public_enabled(os.environ.get("DENDRA_PUBLIC"))
except InvalidFlag as _e:
    PUBLIC_ERR = str(_e)
# Attestation gate for confidential jobs. When armed, the relay refuses a req/<jid>__<mid> until the
# miner <mid> has deposited a valid signed attestation (POST /attest/<mid>) whose measured hash is in
# the allow-list DENDRA_ATTEST_ALLOW (comma-separated sha256 of recognised binaries and configurations).
# It is deterrence (the miner must sign a recognised measured client), not a proof of execution.
#
# Public exposure does not imply attestation. `ATTEST_REQUIRE` reads its own variable and nothing else:
# derived from `PUBLIC`, it would force the gate on for any open network, and since an allow-list pins
# specific measured digests, an open network would have to publish the exact build every third-party
# miner must run, which excludes the independent operators the network exists to attract. The design states
# the consumer tier as hardened deterrence, not a cryptographic proof. So attestation is required when,
# and only when, someone asks for it, and two properties hold regardless:
#   · asking for the gate without an allow-list is refused at startup: a gate with nothing to check
#     against assigns no job at all, and failing at boot is better than a network answering silent 403s;
#   · a relay that does not enforce attestation says so at startup (see the banner below).
# Conversely, a guard that is not applied must not be announced: no public text may describe attestation
# as active while this is off.
ATTEST_ALLOW = {h.strip() for h in os.environ.get("DENDRA_ATTEST_ALLOW", "").split(",") if h.strip()}
ATTEST_REQUIRE = os.environ.get("DENDRA_ATTEST_REQUIRE", "0") == "1"

# -- Signed writes (the derived miner identity, applied to the relay) -----------------------------------------------
# The shared token says someone authorised is writing, never who. Since the deposit key encodes the miner
# (`req/<jid>__<mid>`), any token holder could write under any other operator's key once several
# operators share the network.
#
# Three states, and the default is `off` on purpose. The verification chain (canonical message ->
# signature -> attribution -> replay) is implemented and unit-tested, but the link that produces a
# signature runs `dendrad`, and arming a path whose first real execution is also its first test could
# refuse every deposit, with a symptom (an empty relay) that looks like a quiet network.
#
#   off      nothing changes: unsigned writes are accepted.
#   observe  signed writes are verified, attributed and counted; unsigned ones still pass, and so do
#            signed ones that no registry can attribute, on the token. This state measures on real
#            traffic whether signing works before anything is refused.
#   enforce  a write that is neither attributed nor carrying the token is refused (401).
#
# The registry, not the mode, decides whether a signature means anything. In every mode, a signature that
# cannot be attributed to the operator the chain records for `<mid>` authorises exactly as much as no
# signature at all: the deposit falls back to the token. See attribution below.
#
# The order matters: run `modea.relay_signature.autotest()` once, watch `observe` count valid writes, then
# arm `enforce`. Arming it directly tests in production a path whose failure mode is silence.
SIGN_MODE = (os.environ.get("DENDRA_RELAY_SIGN", "off") or "off").strip().lower()
if SIGN_MODE not in ("off", "observe", "enforce"):
    raise SystemExit(f"DENDRA_RELAY_SIGN={SIGN_MODE!r} — expected off | observe | enforce. "
                     f"Refusing to start rather than guess: a typo must not silently mean 'off'.")

# -- Attribution — a signature that names nobody authorises nobody ---------------------------------
# Verifying a signature and attributing a write are two different operations, and only the second says
# who wrote. Rebuilding the signed document from the address derived from the presented key makes every
# well-formed key verify for every deposit key: the check only says "someone holds a private key". Who
# that is comes from the registry alone: `address(pubkey)` must equal the operator the chain records for
# the miner `<mid>` the deposit key names (or, for `req`, the client the chain records as paying for
# `<jid>`).
#
# DENDRA_RELAY_REGISTRY is therefore what gives a signature its meaning. Unset, a signature stands for
# nothing and every deposit falls back to the shared token; that state is announced at startup, so that
# the guard is not assumed armed.
REGISTRY_URL = os.environ.get("DENDRA_RELAY_REGISTRY", "").strip().rstrip("/")
REGISTRY_TIMEOUT = 3.0                       # a request thread must not hang on a dead REST gateway
REGISTRY_EXPIRY = 300.0                      # beyond this the cache is stale, which does not refuse
# Floor between two refresh attempts. Without it, an unreachable gateway costs REGISTRY_TIMEOUT on
# every single write, and a guard whose cost grows with traffic becomes the reason someone removes it.
REGISTRY_RETRY = 30.0

# -- The kit floor ------------------------------------------------------------------
# From `KIT_DEADLINE`, a signed deposit of the miner's kinds whose body declares a `kit_version` below
# `KIT_MIN`, or declares none, is refused `STALE_KIT` (modea.relay_write). The two values are the ones
# `network-info.txt` publishes (deploy/testnet/publish_network.sh); the relay reads them from its own
# environment, so arming it is a deployment the operator makes on purpose, and the deadline is compared with
# this host's clock in UTC, since the relay has no chain head. Reads stay open on every route: a stale kit
# still reads what it needs to leave (the write policy is per route, see `Handler._policy`).
#
# Three states, no default. Both unset: dormant, every kit is served, and the start-up line says so. Both
# set: armed at the instant named. One set without the other, a floor that is not a kit number or an instant
# that is not `YYYY-MM-DDThh:mm:ssZ` (UTC, the `Z` required): a configuration error, and `main()` refuses to
# start rather than guess a floor or a date. A half-configured refusal that silently stayed dormant would be
# read as armed by whoever deployed it. The forms and the comparison are those of modea/kit_floor.py, which
# the faucet and the season rules read too.
KIT_DEADLINE_FORMAT = kit_floor.KIT_DEADLINE_FORMAT


def kit_policy_from_env(min_raw, deadline_raw):
    """`(floor, deadline_ts, error)` from the two raw strings (`modea.kit_floor.policy_from_env`). `error` is None
    when the pair is usable: `(None, None, None)` is dormant, `(int, float, None)` is armed from `deadline_ts`."""
    return kit_floor.policy_from_env(min_raw, deadline_raw)


KIT_FLOOR, KIT_DEADLINE_TS, KIT_POLICY_ERROR = kit_policy_from_env(
    os.environ.get("DENDRA_KIT_MIN"), os.environ.get("DENDRA_KIT_DEADLINE"))


def kit_floor_now(now=None):
    """The floor a signed write is held to at this instant, or None while the refusal is dormant: before
    the deadline, or with no floor published. A policy in error arms nothing here; `main()` refuses to
    start on it, which is where that error is meant to be met."""
    return kit_floor.floor_in_force(KIT_FLOOR, KIT_DEADLINE_TS, time.time() if now is None else now)


def verdict_kit_policy(error, floor, deadline_ts):
    """Is the relay allowed to start, as far as the kit floor goes? ("REFUSE"|"OK", message)."""
    if error:
        return "REFUSE", f"[relay] refused: {error}."
    if floor is None:
        return "OK", ("[relay] kit floor dormant: DENDRA_KIT_MIN and DENDRA_KIT_DEADLINE unset, every kit "
                      "version is served (the kit floor is not armed on this relay)")
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(deadline_ts, tz=_dt.timezone.utc).strftime(KIT_DEADLINE_FORMAT)
    state = "armed" if kit_floor_now() is not None else "announced"
    return "OK", (f"[relay] kit floor {state}: from {when} a signed deposit whose body declares kit_version "
                  f"below {floor}, or none, is refused STALE_KIT; reads stay open")


# -- The price of a signed request (C37, the second way in) ---------------------------------------------------
# A client with its own wallet opens the job on chain (the fee goes to escrow), then deposits the sealed request
# on `req` signed with the key that pays. The signature names the payer (`modea.relay_write`, role `client`);
# the price says whether what it escrowed is enough. One price per request, whatever the model, while the model
# that computes is not verified (decision G1).
#
# Read from the environment with no default. Unset: the signature path on `req` refuses every deposit (PRICE_UNSET)
# and the token path stays what it was. Set: a positive integer in udndr, compared with the `fee` the chain holds
# for the job. Anything else is a configuration error and `main()` refuses to start: a typo must not open the
# route at a price nobody chose.
REQUEST_PRICE_VAR = "DENDRA_REQUEST_PRICE_UDNDR"
PRICE_UNSET = "PRICE_UNSET"
UNDERPAID = "UNDERPAID"
FEE_UNREADABLE = "FEE_UNREADABLE"
# The key files the request under a miner the chain did not assign to the job, or the committee could not be
# read. Without this check one paid job would let its payer fill the `req` budget with invented miner ids.
UNASSIGNED_MINER = "UNASSIGNED_MINER"
COMMITTEE_UNREADABLE = "COMMITTEE_UNREADABLE"
NOT_ATTESTED = "NOT_ATTESTED"
# The job no longer waits for its request: settled, paid, expired and refunded, disputed. Its fee still reads, so
# without this check a payer could deposit on a job that is over, and once the relay evicts the old entries by
# age, deposit again under the same keys.
JOB_NOT_OPEN = "JOB_NOT_OPEN"
JOB_STATE_UNREADABLE = "JOB_STATE_UNREADABLE"
# The one state in which a job waits for its request. `msg_server_open_job.go::OpenJob` writes it, and every
# later transition appends a marker to it (`job_state.go`, `job_expiry.go::expireUnservedJob`), so the
# comparison is exact: a state that holds this word and a marker after it has moved on.
JOB_OPEN_STATE = "open"
# A `req` attributed to its payer and refused on one of these is counted as `request_refused`, neither as a bad
# signature nor as an unattributed write.
REQUEST_REFUSALS = (PRICE_UNSET, UNDERPAID, FEE_UNREADABLE, UNASSIGNED_MINER, COMMITTEE_UNREADABLE,
                    NOT_ATTESTED, JOB_NOT_OPEN, JOB_STATE_UNREADABLE)


def request_price_from_env(raw):
    """`(price, error)` from the raw string. `(None, None)` when unset, `(int, None)` when usable.

    The same form as `deploy/testnet/publish_network.sh`, which publishes the price under REQUEST_PRICE_UDNDR;
    `dendra_request_price_publish_test.sh` feeds both readers the same values. Nothing is trimmed: a value
    with spaces around it is a configuration error, never a price."""
    raw = raw or ""
    if not raw:
        return None, None
    # ASCII digits, no leading zero, never 0: a price of 0 would accept a job that escrowed nothing.
    if not (raw.isascii() and raw.isdigit() and raw[0] != "0"):
        return None, (f"{REQUEST_PRICE_VAR} must be a positive integer in udndr, in ASCII digits "
                      f"(got {raw!r})")
    return int(raw), None


REQUEST_PRICE, REQUEST_PRICE_ERROR = request_price_from_env(os.environ.get(REQUEST_PRICE_VAR))


def verdict_request_price(error, price):
    """Is the relay allowed to start, as far as the request price goes? ("REFUSE"|"OK", message)."""
    if error:
        return "REFUSE", f"[relay] refused: {error}."
    if price is None:
        return "OK", (f"[relay] signed requests closed: {REQUEST_PRICE_VAR} unset, a `req` deposit is accepted "
                      f"on the token only")
    return "OK", (f"[relay] signed requests open: a `req` deposit signed by the job's paying client is "
                  f"accepted when the job is open, escrowed at least {price} udndr, and the key names a miner "
                  f"of its assigned committee")


def job_fee_from_doc(raw, job_id):
    """The fee the chain escrowed for `job_id`, from the JSON of `/dendra/jobs/v1/job/{job_id}`, or None
    when the document does not establish one.

    proto3 omits a zero `fee`, so an absent field reads 0, which no price accepts. A document about another
    job, a `fee` that is not an unsigned integer, or no `job` object, is None: nothing was read, and the
    caller refuses on that as well, under its own name."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    d = json.loads(raw)
    job = d.get("job") if isinstance(d, dict) else None
    if not isinstance(job, dict) or job.get("job_id") != job_id:
        return None
    if "fee" not in job:
        return 0
    fee = job["fee"]
    if isinstance(fee, str) and fee.isascii() and fee.isdigit():
        return int(fee)
    if isinstance(fee, int) and not isinstance(fee, bool) and fee >= 0:
        return fee
    return None


def job_state_from_doc(raw, job_id):
    """The state the chain records for `job_id`, from the same JSON as `job_fee_from_doc`, or None when the
    document does not establish one.

    proto3 omits an empty `state`, so an absent field reads "", which is not the open state and is refused as
    a job that does not wait for its request. A document about another job, a `state` that is not a string,
    or no `job` object, is None: nothing was read."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    d = json.loads(raw)
    job = d.get("job") if isinstance(d, dict) else None
    if not isinstance(job, dict) or job.get("job_id") != job_id:
        return None
    if "state" not in job:
        return ""
    state = job["state"]
    return state if isinstance(state, str) else None


def committee_from_doc(raw):
    """The miners the chain assigned to a job, primary first, from the JSON of
    `/dendra/jobs/v1/assigned_committee/{job_id}`, or None when the document does not establish them.

    proto3 omits an empty list, so an absent `members` reads [], which names no miner. A `members` that is
    not a list of non-empty strings is None: nothing was read. The client seals to the first k members
    (`client.py::submit_job`); the relay accepts any member, which bounds the keys one paid job can
    fill to the size of its committee."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    d = json.loads(raw)
    if not isinstance(d, dict):
        return None
    if "members" not in d:
        return []
    members = d["members"]
    if not isinstance(members, list) or not all(isinstance(m, str) and m for m in members):
        return None
    return members


class PaidBeforeReplay:
    """The replay guard of a signed `req`, preceded by the checks of C37 (price, fee, committee).

    `relay_write` runs its replay guard last because that guard records the digest. These checks can refuse
    too, so they run just before it: a refused deposit leaves its digest unrecorded, and the client can send
    the same signed message again once the fee or the committee reads. The replay stage is also the first
    point where the writer is attributed to the job's payer, so a stranger never makes the relay read the
    REST gateway."""

    def __init__(self, refusal, antireplay):
        self._refusal, self._antireplay = refusal, antireplay

    def verify(self, writing_key, height, digest, head=None):
        refused = self._refusal()
        if refused is not None:
            reason, detail = refused
            return relay_antireplay.Verdict(False, None, reason, detail)
        return self._antireplay.verify(writing_key, height, digest, head=head)


def _read_rest(path: str) -> bytes:
    with urllib.request.urlopen(REGISTRY_URL + path, timeout=REGISTRY_TIMEOUT) as r:
        return r.read()


class _UnconfiguredRegistry:
    """The registry that is not configured. It answers NEVER_READ, which `relay_write` turns into a
    refusal — never into an acceptance.

    It carries no convenient default on purpose: a guard that cannot identify must refuse, and any other
    answer here would reopen the hole one layer lower."""

    def operator(self, miner_id):
        return None, registry_cache.NEVER_READ


REGISTRY = _UnconfiguredRegistry()
JOBS = None
# `job_id -> bytes` of the job read by id, the path pagination cannot truncate. None without a registry URL,
# and a signed request is then refused FEE_UNREADABLE: no fee can be read.
JOB_DOC_READER = None
# `job_id -> bytes` of the committee the chain assigned to the job. None without a registry URL, and a signed
# request is then refused COMMITTEE_UNREADABLE.
COMMITTEE_READER = None
JOB_LIST_PATH = "/dendra/jobs/v1/job?pagination.limit=1000&pagination.reverse=true"
if REGISTRY_URL:
    JOB_DOC_READER = lambda jid: _read_rest("/dendra/jobs/v1/job/" + urllib.parse.quote(jid, safe=""))  # noqa: E731
    COMMITTEE_READER = lambda jid: _read_rest(  # noqa: E731
        "/dendra/jobs/v1/assigned_committee/" + urllib.parse.quote(jid, safe=""))
    REGISTRY = registry_cache.RegistryCache(
        lambda: _read_rest("/dendra/jobs/v1/miner?pagination.limit=1000"), time.time,
        expiry=REGISTRY_EXPIRY)
    # `pagination.reverse=true`: the page holds the last 1000 job ids in key order, which for the
    # `job<milliseconds>` ids `client.py::submit_job` mints are the newest, the ones a deposit
    # names. The chain keys jobs by a caller-chosen string, so key order is not a guarantee of age: a
    # job outside the page is resolved by id and held in `JobRegistry._by_id` across refreshes.
    JOBS = job_registry.JobRegistry(
        lambda: _read_rest(JOB_LIST_PATH), time.time,
        expiry=REGISTRY_EXPIRY,
        job_reader=lambda jid: _read_rest("/dendra/jobs/v1/job/" + urllib.parse.quote(jid, safe="")))
# `retention` is not a free setting: a digest forgotten before its entry is evicted reopens exactly
# the replay window this object closes. It is read from RETENTION rather than restated.
ANTIREPLAY = relay_antireplay.Antireplay(time.time, retention=RETENTION)
_REG_LOCK = threading.Lock()
_REG_ATTEMPT = {"miner": 0.0, "job": 0.0}
# Reasons that do not establish a valid signature. Anything else means the signature verified and the
# attribution refused: one is a broken client, the other a write under someone else's name, and an
# operator must be able to tell them apart.
_BEFORE_SIGNATURE = (relay_write.MALFORMED, relay_write.INVALID_SIGNATURE,
                     relay_write.UNREADABLE_HEIGHT)
# Named like the reasons `relay_write` returns, because it lands in the same counter and the same
# refusal body: an operator reading `KEY_MISMATCH` next to `FOREIGN_KEY` must not have to work out
# which module spoke.
KEY_MISMATCH = "KEY_MISMATCH"
EMPTY_ATTRIBUTION = "EMPTY_ATTRIBUTION"
# Where a refusal is explained: the address of a section of the public site, in the field `help` of the refusal.
# The field sits beside `why` and `error` and is read by nobody; `why` keeps starting with the reason word, which
# the miner side and its self-test read. The table starts from the reasons `relay_write` and the replay guard
# return and adds the ones this file returns: the words of a write that is not verified, the two refusals of a
# bad key binding, and the refusals of a signed request. A reason that is not listed gets no link, and
# tests/test_refusal_help.py fails on the gap instead of the relay inventing an address for it.
HELP_FIELD = relay_write.HELP_FIELD
HELP_RELAY_REFUSALS = relay_write.HELP_RELAY_REFUSALS
REFUSAL_HELP = dict(relay_write.REFUSAL_HELP)
REFUSAL_HELP.update({word: HELP_RELAY_REFUSALS for word in (
    "off", "unsigned", "malformed",                 # the words of `_verify_write_request` that are not reasons
    KEY_MISMATCH, EMPTY_ATTRIBUTION,
    PRICE_UNSET, UNDERPAID, FEE_UNREADABLE, UNASSIGNED_MINER, COMMITTEE_UNREADABLE, NOT_ATTESTED,
    JOB_NOT_OPEN, JOB_STATE_UNREADABLE)})


def help_for_why(why):
    """The address that explains a refusal, from the reason word that starts its `why`, or None."""
    return relay_write.help_for(str(why).split(" ", 1)[0], REFUSAL_HELP)


def refusal_json(error, **fields):
    """The bytes of a refusal: `error`, the extra fields, and `help` last when a link is given as `help`."""
    doc = {"error": error}
    doc.update({k: v for k, v in fields.items() if v is not None})
    return json.dumps(doc, separators=(",", ":")).encode()
SIGN_STATS = {"signed_ok": 0, "signed_ko": 0, "unsigned": 0,
              # `signed_ok` keeps its meaning (the signature verified), because a counter whose meaning
              # changes invalidates every earlier reading of it. Attribution is counted next to it.
              # `stale_kit` counts a write that verified and was attributed, then refused on the kit floor:
              # neither a bad signature nor an unattributed write, so neither of those
              # counters moves for it.
              # `request_refused` counts a `req` attributed to its payer, then refused on the price, the fee,
              # the committee or the attestation (`REQUEST_REFUSALS`, C37). `attributed` counts only the
              # writes that were accepted.
              "attributed": 0, "unattributed": 0, "stale_kit": 0, "request_refused": 0}
SIGN_REFUSALS: dict = {}
# The key names the miner a deposit is filed under; the header names the miner the writer claims to be,
# and the registry is read against the header. Both enter the canonical message, so a signature is valid
# for a header of A over a key of B: a registered operator could deposit under another miner's key, be
# correctly attributed to itself, and take the key its owner needs, which write-once would then lock for
# good. The two names are compared here, in the relay, because the shape of a deposit key is route
# knowledge.
KEY_IS_THE_MINER = ("pub", "attest")     # `pub/<mid>`, `attest/<mid>`: the key is the identifier
# The kinds a signature may write on its own (`Handler._policy`). Every other kind, and every unknown route,
# takes the token.
SIGNED_KINDS = ("pub", "res", "reveal", "attest", "req")


def _miner_named_by_key(kind, key):
    """The miner the key files this deposit under, or None when the key names none.

    None is a refusal upstream, never a pass: a key whose shape says nothing about its owner cannot be
    compared with the writer, and a binding that cannot be enforced must not be reported as enforced."""
    if kind in KEY_IS_THE_MINER:
        return key or None
    return key.rsplit("__", 1)[1] if isinstance(key, str) and "__" in key else None


def _refresh_cache(cache, name, *, unknown_miner=False) -> bool:
    """Best-effort refresh; True when a read was attempted. `refresh()` never raises; a failed read is a
    fact the cache counts.

    A fresh copy is not re-read, except on `unknown_miner`: a miner registered after
    the last read is refused UNKNOWN_MINER until the copy expires, and its bootstrap deposits were lost
    until then. The re-read keeps the floor between two attempts, whatever its cause: a stream of invented
    identifiers earns one read of the REST gateway per REGISTRY_RETRY, never one per request."""
    if cache is None or not REGISTRY_URL:
        return False
    if cache.state() == registry_cache.FEES and not unknown_miner:
        return False
    now = time.monotonic()
    with _REG_LOCK:
        if now - _REG_ATTEMPT[name] < REGISTRY_RETRY:
            return False
        _REG_ATTEMPT[name] = now
    if unknown_miner:
        _count("registry_reread_unknown_miner")
    cache.refresh()
    return True


def _canon(kind, key, body, miner_id, height):
    """`relay_write` needs the message and its digest; `relay_canon` exposes them separately and builds the
    message again for the digest. One call, one message: the digest must be that of the bytes that were
    verified, not of a second construction that happens to agree."""
    message = relay_canon.canonical_message(kind, key, body, miner_id, height)
    return message, hashlib.sha256(message).digest()


LOCK = threading.Lock()
STORE = {"pub": OrderedDict(), "req": OrderedDict(), "res": OrderedDict(),
         "reveal": OrderedDict(),    # Sealed reveal primary->committee on an audited job
         "attest": OrderedDict()}    # signed software attestation of the miner (key = <mid>)
# -- Store version, for the /list cache --------------------------------------------------------------
# Every change to the keys of STORE bumps this integer, under LOCK, in the same critical section as the
# change. A cached /list body is served only while the version it was built at is still current, so a
# missed bump serves a stale queue and a miner never sees its job: every function that adds, removes,
# reorders or reloads keys calls `_bump_store_version_locked`, and test_relay_list_cache.py removes each
# call in turn to show a bench fails without it.
_STORE_VERSION = 0


def _bump_store_version_locked():
    """Mark the keys of STORE as changed. Caller holds LOCK."""
    global _STORE_VERSION
    _STORE_VERSION += 1
# -- Write-once -----------------------------------------------------------------------------------
# `_put` overwrites, and three of the five kinds hold a per-job sealed artifact that exists in one
# copy: the sealed request (`req`), the sealed response (`res`) and the sealed reveal (`reveal`). The
# chain keeps only hashes of them, so replacing one destroys the evidence an audit concludes on: the held
# fee never resolves, and the symptom looks like a quiet network.
#
# `pub` and `attest` are deliberately absent. They carry an identity, not evidence: a miner rotates its
# X25519 key and republishes a measured client after every upgrade, and write-once would freeze it on
# its first key.
#
# Nobody replaces an artifact, not even its author. Letting an attributed author replace its own would
# give the primary a way to swap the bytes a jury is judging, and would make the answer depend on an
# ownership `relay_store` does not persist, so the same request would be refused or accepted depending
# on whether the relay had restarted. A retry whose `200` was lost is covered by the identical-bytes case
# below instead.
WRITE_ONCE = ("req", "res", "reveal")


class Conflict(RuntimeError):
    """A different content offered for a key that already holds a sealed artifact.

    Byte-identical is not a conflict: a retry that re-sends what is already stored destroys nothing,
    and refusing it would strand a worker whose first `200` was lost on the way back."""
# -- Aggregate counters — observability without the paths -----------------------------------------
# The access log is silent by design (see `log_message`): the path of a request is an identifier
# (`<kind>/<jobId>__<minerId>`). No paths does not have to mean no visibility: without these counters a
# store-filling attack (a third party saturating the store so that legitimate deposits are refused) would
# be invisible. Only numbers live here: no jobId, no minerId, no key.
#
# They are counted in `_send` and nowhere else. A counter placed branch by branch is forgotten on the next
# branch added, while every response goes through `_send`, so a new refusal code is counted without anyone
# thinking about it, under its own name (`http_418` if a 418 is ever introduced).
COUNTERS = {"evictions_age": 0}
COUNTERS_LOCK = threading.Lock()


def _count(name, n=1):
    with COUNTERS_LOCK:
        COUNTERS[name] = COUNTERS.get(name, 0) + n


def _legacy_entry_cap(occupation, used):
    """`legacy_entry_cap` of /stats: the count that keeps its readers' saturation true.

    The field was the count ceiling, and its readers compute saturation as max(occupation) /
    legacy_entry_cap, alarming near 1 (exporter.py::refresh_relay). An older relay spelled
    the /stats keys in French (this one, `counters`, `prev_token_uses`, `tracked_ips`); the exporter ships
    in the same image as this relay, so the two move together, and an older exporter
    reads none of them (a missing cap reads as zero saturation). The ceiling is in bytes now: kept as a
    constant count bound, that ratio would understate the fill by the ratio of ENTRY_OVERHEAD to an
    entry's real cost, and the only saturation gauge on /metrics would read safe at the very deposit
    refused (test_relay_list_filter.py runs the shipped exporter against this relay).

    So it is the count at which max(occupation) reads as the fill of the fullest kind (`fill_max`),
    rounded down so that the ratio is never below that fill; never below 1, so that a fill above the
    budget reads above 1 instead of dividing by zero; and, with nothing charged, the count bound itself.
    A reader that wants the budget reads `fill_max` and `budget_bytes_per_kind`; one that wants the count
    bound reads `entries_max_per_kind`."""
    bound = KIND_BUDGET // ENTRY_OVERHEAD
    fmax = max((_fill(u) for u in used.values()), default=0.0)
    nmax = max(occupation.values(), default=0)
    if fmax <= 0 or nmax <= 0:
        return bound
    return max(1, min(bound, int(nmax / fmax)))


def _stats():
    """Aggregate snapshot of the relay. Nothing here may ever carry a key: the `kind` names are fixed
    protocol categories (pub/req/res/reveal/attest), not identifiers."""
    with LOCK:
        occupation = {k: len(v) for k, v in STORE.items()}
        used = dict(USED)
        list_cache = {**LIST_CACHE_STATS, "entries": len(_LIST_CACHE)}
    with COUNTERS_LOCK:
        counters = dict(COUNTERS)
    fill = {k: round(_fill(n), 4) for k, n in used.items()}
    return {
        "counters": counters,
        "occupation": occupation,
        # A legacy field, derived at each read, not a constant (`_legacy_entry_cap`).
        "legacy_entry_cap": _legacy_entry_cap(occupation, used),
        # The most entries a kind can ever hold: every entry is charged at least ENTRY_OVERHEAD.
        "entries_max_per_kind": KIND_BUDGET // ENTRY_OVERHEAD,
        # The budget, in the unit it is enforced in. `fill` is bytes / budget per kind; `fill_max` is the
        # fullest kind, because an average would dilute the very kind about to refuse; `alarm` names
        # every kind at or above `alarm_threshold`.
        "budget_bytes_per_kind": KIND_BUDGET,
        "entry_overhead_bytes": ENTRY_OVERHEAD,
        "bytes": used,
        "fill": fill,
        "fill_max": max(fill.values()) if fill else 0.0,
        "alarm_threshold": FILL_ALARM,
        "alarm": sorted(k for k, f in fill.items() if f >= FILL_ALARM),
        "retention_s": RETENTION,
        "prev_token_uses": _PREV_HITS[0],
        # Signed writes: the mode is published next to its counters on purpose, since `signed_ok: 0` means
        # "nobody signs yet" under `off` and "nothing gets through" under `enforce`.
        # `registry` sits in the same block for the same reason: a high `signed_ok` with `attributed` at
        # zero means signatures name nobody and every write rides on the token.
        "signature": {"mode": SIGN_MODE, **SIGN_STATS,
                      "registry": REGISTRY.state() if REGISTRY_URL else "NOT_CONFIGURED",
                      "jobs": JOBS.state() if JOBS is not None else "NOT_CONFIGURED",
                      # The price a signed `req` is held to; null while unset, and the signed path then
                      # refuses every request (`refusals.PRICE_UNSET`).
                      "request_price_udndr": REQUEST_PRICE,
                      "refusals": dict(SIGN_REFUSALS)},
        # The kit floor: what is published and whether it applies at this instant.
        # `refusals.STALE_KIT` above counts its effect; this block says what the count was taken against.
        "kit_floor": {"kit_min": KIT_FLOOR, "deadline_ts": KIT_DEADLINE_TS,
                      "armed": kit_floor_now() is not None},
        # The /list cache: `hits` answered from a cached body, `builds` copied the store keys again.
        "list_cache": list_cache,
        "rate_max": RATE_MAX,
        "rate_window_s": RATE_WINDOW,
        "tracked_ips": len(RATE),
        "uptime_s": round(time.time() - _T0, 1),
    }


_T0 = time.time()
RATE = {}                 # ip -> deque[timestamps] (bounded by window)
RATE_LOCK = threading.Lock()

# --- Persistence --------------------------------------------------------------------------------
# The location is configurable, the existence is not: no value of DENDRA_RELAY_STORE makes the relay
# volatile. Default = `relay-store/` next to the script.
STORE_DIR = (os.environ.get("DENDRA_RELAY_STORE", "").strip()
             or os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay-store"))
TS = {k: OrderedDict() for k in STORE}   # kind -> key -> deposit time (eviction is by age)
# kind -> bytes charged against KIND_BUDGET (`_cost` of every entry held). Read and written under LOCK
# only, in the same critical section as the entry it accounts for, so that the total always describes
# the store as it is.
USED = {k: 0 for k in STORE}
ALARMED = set()      # kinds whose crossing of FILL_ALARM has been reported and not yet taken back
DISK = None          # relay_store.DiskStore — the disk is authoritative, RAM is only a hot cache
BOOT_ERR = ""        # non-empty => the store is unusable => the relay refuses to serve
BOOT_STATS = {}


def _cost(key, data):
    """Bytes one entry is charged: its key, its body, and ENTRY_OVERHEAD. `surrogatepass` because a
    key reloaded from disk is whatever JSON held, and a budget that raises on a strange key would stop
    the relay from booting over one record."""
    return len(key.encode("utf-8", "surrogatepass")) + len(data or b"") + ENTRY_OVERHEAD


def _fill(used):
    """Fraction of the budget `used` represents. A budget of zero is full, never empty."""
    return used / KIND_BUDGET if KIND_BUDGET > 0 else 1.0


def _fill_transition_locked(kind):
    """The line to log when `kind` crosses the alarm, or "". Caller holds LOCK.

    Said once per crossing and taken back once it falls under FILL_REARM: a fill hovering on the
    threshold must not write a line per deposit, and a fill that came back down must not leave the
    last word to an alarm that is over. Only the category and numbers are printed, never a key."""
    f = _fill(USED[kind])
    if f >= FILL_ALARM and kind not in ALARMED:
        ALARMED.add(kind)
        return (f"[relay] store alarm: kind {kind} holds {USED[kind]} of {KIND_BUDGET} bytes "
                f"({f:.0%}, threshold {FILL_ALARM:.0%}). Past the budget every new {kind} deposit is "
                f"refused (507) until entries age out ({RETENTION:g}s). Raise "
                f"DENDRA_RELAY_KIND_BUDGET_MIB, or find who is filling it: /stats counts per code.")
    if f < FILL_REARM and kind in ALARMED:
        ALARMED.discard(kind)
        return (f"[relay] store alarm over: kind {kind} is back to {f:.0%} of its budget.")
    return ""


def _boot_store():
    """Open the store and reload the RAM cache from disk. Returns {kind: n_reloaded}.

    FIFO order is rebuilt from each record's persisted rank, so after a restart eviction is still oldest
    first. Raises `StoreUnavailable` if the store is not writable, never a silent in-memory fallback.

    The byte total is rebuilt from what was reloaded, never persisted on its own: a counter stored beside
    the records could disagree with them. Nothing is discarded for being over the budget; the next
    deposit is refused instead.
    """
    global DISK
    DISK = relay_store.DiskStore(STORE_DIR, list(STORE.keys()))
    loaded = DISK.load(retention=RETENTION)     # purge by age at startup, never by count or size
    stats = {}
    with LOCK:
        ALARMED.clear()
        for kind, items in loaded.items():
            d, t = STORE[kind], TS[kind]
            d.clear()
            t.clear()
            used = 0
            for key, data, _n, ts in items:  # items already sorted from oldest to newest
                if key in d:
                    used -= _cost(key, d[key])
                d[key] = data
                t[key] = ts
                used += _cost(key, data)
            USED[kind] = used
            if _fill(used) >= FILL_ALARM:
                ALARMED.add(kind)        # said by main()'s banner, not twice by the first deposit
            stats[kind] = len(d)
        # A reload replaces every key, whatever the version was: a /list body cached before it is void.
        _bump_store_version_locked()
    return stats


def _startup_alarm_lines():
    """The lines main() prints for every kind reloaded above the alarm. A store reloaded there says so
    at startup: no deposit may arrive to trigger the line, and the first one that does must not say it
    a second time (`_boot_store` marks the kind as said)."""
    with LOCK:
        hot = {k: _fill(USED[k]) for k in sorted(ALARMED)}
    return [f"[relay] store alarm at startup: kind {k} reloaded at {f:.0%} of its budget"
            for k, f in hot.items()]


try:
    BOOT_STATS = _boot_store()
except relay_store.StoreUnavailable as _e:
    BOOT_ERR = str(_e)


def _attest_ok(mid: str) -> bool:
    """Is a miner attested to receive a confidential job? Verifies the signature of the deposited
    attestation and the membership of the measured hash in the allow-list. If the `cryptography` or
    `modea` library cannot be imported on the relay side, the answer is no (fail closed while the gate
    is active). Without a configured allow-list the gate cannot recognise a good client, so it refuses
    too (an inconsistent configuration closes)."""
    if not ATTEST_ALLOW:
        return False
    raw = _get("attest", mid)
    if not raw:
        return False
    try:
        att = json.loads(raw)
    except Exception:
        return False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from modea import confine
    except Exception:
        return False
    # `expected_pubkey` is left out on purpose. `verify_attestation` compares it with
    # `att["attest_pubkey"]`, and passing that same value would compare it with itself: a check that can
    # refuse nothing while reading like a binding to an identity. The relay has no source for the
    # attestation key: the registry holds Cosmos operator addresses (secp256k1), while an attestation is
    # signed by an Ed25519 key that appears nowhere on chain. What is proven is what remains: a valid
    # signature, a measure that matches its hash, and a hash on the allow-list.
    ok, _ = confine.verify_attestation(att, allowed_hashes=ATTEST_ALLOW, expected_pubkey=None)
    return ok and att.get("miner_id") == mid


def _put(kind: str, key: str, data: bytes) -> None:
    """Deposit: disk first, then RAM. The order matters.

    If the disk write fails, nothing is put in RAM and the exception propagates (503 on the HTTP side):
    a deposit accepted but not persisted would answer "ok" to a miner whose reveal is not safe.

    The write-once guard lives here rather than in the HTTP layer: this is the only function that
    writes, so a caller added later inherits the guard instead of having to remember it.
    """
    if DISK is None:
        raise relay_store.StoreUnavailable(BOOT_ERR or "store not opened")
    now = time.time()
    evicted = []
    with LOCK:
        d, t = STORE[kind], TS[kind]
        # (1) Eviction by age, and by age only. `t` is ordered by insertion, hence by date: stop at
        # the first entry that is still young.
        while d:
            k0 = next(iter(d))
            if now - t.get(k0, now) <= RETENTION:
                break
            USED[kind] -= _cost(k0, d.pop(k0, None))
            t.pop(k0, None)
            evicted.append(k0)
        if evicted:
            # Bumped here and not only by the write below: a deposit that ends in 409, 507 or an
            # identical-bytes retry still evicted these keys.
            _bump_store_version_locked()
        said = _fill_transition_locked(kind) if evicted else ""
    if said:
        print(said, flush=True)
    if evicted:
        _count("evictions_age", len(evicted))
    for k in evicted:
        DISK.remove(kind, k)

    # Check and write under the same lock, or the write-once guard does not hold. This is a
    # ThreadingHTTPServer: if the key were tested under the lock and written after releasing it, two
    # deposits of different bytes on the same fresh key could both see it free and the second would
    # overwrite the first, destroying a sealed artifact the chain only keeps a hash of.
    # The disk write stays inside the critical section for the same reason. Concurrent deposits then
    # serialise on one I/O, which is acceptable at a handful of artifacts per job.
    with LOCK:
        d, t = STORE[kind], TS[kind]
        # (2) Write-once on the kinds that hold a sealed artifact, decided before the budget: a deposit
        # that can never be accepted must say so (409) rather than ask its sender to retry later (507).
        # Two outcomes, and the first keeps an honest worker from being stranded:
        #   · identical bytes: nothing to do and nothing destroyed. A worker whose `200` was lost on the
        #     way back re-sends exactly what is stored, and accepting it changes nothing on disk or in the
        #     budget;
        #   · different bytes: refused, whoever writes. The chain keeps only a hash of this artifact, and
        #     replacing it would leave the audit nothing to conclude on.
        stored = d.get(key)
        if kind in WRITE_ONCE and stored is not None:
            if stored == data:
                return
            raise Conflict(
                f"{kind}/{key}: this key already holds a sealed artifact and the deposit carries "
                f"different bytes. Refused rather than replaced - the chain keeps only a hash of it, so "
                f"an audit that loses it can never conclude and its fee stays held for good.")
        # (3) Budget: when it would be exceeded while everything is still young, the write is refused.
        # Erasing evidence that is still useful to make room cannot be undone, while a refused deposit is
        # retried. The total is read again here rather than carried from the block above, whose lock has
        # since been released. A replacement (`pub`, `attest`) is charged the difference, so a rotated key
        # costs what it adds and is never counted twice.
        cost = _cost(key, data)
        after = USED[kind] - (_cost(key, stored) if stored is not None else 0) + cost
        if after > KIND_BUDGET and after > USED[kind]:
            raise relay_store.StoreFull(
                f"{kind}: budget of {KIND_BUDGET} bytes reached ({USED[kind]} held, this deposit "
                f"costs {cost}) and none has outlived the retention window ({RETENTION:g}s) - "
                f"deposit refused rather than erasing evidence that is still useful")
        DISK.write(kind, key, data, ts=now)
        if key in d:
            d.move_to_end(key)
            t.pop(key, None)
        d[key] = data
        t[key] = now
        USED[kind] = after
        _bump_store_version_locked()
        said = _fill_transition_locked(kind)
    if said:
        print(said, flush=True)


def _get(kind: str, key: str):
    with LOCK:
        return STORE[kind].get(key)


def _snapshot_keys(suffix=None):
    """{kind: [keys...]} — every kind is named, even empty: a reader tells "the queue was read and holds
    nothing for me" from "nothing was read" by that (`miner.py::main` keys on a non-empty
    answer). With `suffix`, only the keys ending with it."""
    with LOCK:      # copied under the lock, for a safe iteration
        return _keys_locked(suffix)


def _keys_locked(suffix):
    """The body of `_snapshot_keys`, for a caller that already holds LOCK."""
    if not suffix:
        return {k: list(v.keys()) for k, v in STORE.items()}
    return {k: [x for x in v.keys() if x.endswith(suffix)] for k, v in STORE.items()}


# -- The filtered work queue ----------------------------------------------------------------------
# Every miner reads `/list` every few seconds and keeps only the keys ending with its own id, so the full
# queue would carry every key of the network to every miner, at a cost that grows with the store.
# `/list?suffix=<s>` returns the slice a miner actually reads, and its ETag makes an unchanged slice cost
# a 304 with no body.
#
# The parameter is the literal suffix (a miner sends `__<its id>`), matched with `endswith` exactly as
# the miner already filters. It filters nothing the relay keeps secret: `/list` is open and its content
# public on chain, so the route takes the same policy as `/list` and no other.
#
# The relay echoes the suffix it applied (`X-Dendra-List-Suffix`). A 200 without it means the filter was
# not applied (an older relay never answers 200 here, but a proxy that drops the query string would), and
# the client then filters what it got and says so. A parameter this route does not read is refused (400),
# never ignored, which would hand the full queue to a caller that believes it asked for a slice.
LIST_SUFFIX_HEADER = "X-Dendra-List-Suffix"
_RE_SUFFIX = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _list_suffix(query):
    """(suffix or None, error or ""). An empty query string is no filter."""
    if not query:
        return None, ""
    try:
        q = urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return None, "unreadable query string"
    if set(q) - {"suffix"}:
        return None, "/list reads one parameter, `suffix`, and nothing else"
    vals = q.get("suffix", [])
    if len(vals) != 1 or not _RE_SUFFIX.fullmatch(vals[0]):
        return None, "`suffix` must appear once, 1 to 128 characters of [A-Za-z0-9_.:-]"
    return vals[0], ""


def _etag(body):
    """Strong validator of the exact bytes served: an ETag that matched different bytes would answer
    304 over a changed queue, and a miner would never see the job that changed it."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


# -- The /list cache ---------------------------------------------------------------------------------
# Every miner polls /list every few seconds, and building an answer copies every key of the store, then
# serialises and hashes it. The store changes far less often than it is polled, so the body and its ETag
# are kept per suffix together with the store version they were built at, and served only while that
# version is current. Bounded in entries and in bytes: the suffix is chosen by the caller, so a stream of
# distinct suffixes must evict old slots, never grow the map.
LIST_CACHE_MAX_ENTRIES = 256
LIST_CACHE_MAX_BYTES = 8 << 20
_LIST_CACHE = OrderedDict()          # suffix ("" for the full queue) -> (version, body, etag)
_LIST_CACHE_BYTES = 0
LIST_CACHE_STATS = {"hits": 0, "builds": 0}


def _list_answer(suffix=None):
    """(body, etag) of /list for `suffix`, from the cache while the store version is unchanged.

    The keys are copied in the same critical section that reads the version, so a body is always filed
    under the version it describes; a mutation that lands while it is serialised makes it a miss, never
    a stale hit."""
    global _LIST_CACHE_BYTES
    slot = suffix or ""
    with LOCK:
        version = _STORE_VERSION
        hit = _LIST_CACHE.get(slot)
        if hit is not None and hit[0] == version:
            _LIST_CACHE.move_to_end(slot)
            LIST_CACHE_STATS["hits"] += 1
            return hit[1], hit[2]
        keys = _keys_locked(suffix)       # the same reader as `_snapshot_keys`: every kind named
        LIST_CACHE_STATS["builds"] += 1
    body = json.dumps(keys).encode()
    tag = _etag(body)
    if len(body) > LIST_CACHE_MAX_BYTES:
        return body, tag
    with LOCK:
        old = _LIST_CACHE.get(slot)
        # Never replace a body built at a later version by one built at an earlier one.
        if old is None or old[0] < version:
            if old is not None:
                _LIST_CACHE_BYTES -= len(old[1])
            _LIST_CACHE[slot] = (version, body, tag)
            _LIST_CACHE.move_to_end(slot)
            _LIST_CACHE_BYTES += len(body)
            while (len(_LIST_CACHE) > LIST_CACHE_MAX_ENTRIES
                   or _LIST_CACHE_BYTES > LIST_CACHE_MAX_BYTES):
                _gone, (_v, gone_body, _t) = _LIST_CACHE.popitem(last=False)
                _LIST_CACHE_BYTES -= len(gone_body)
    return body, tag


def _etag_matches(header, etag):
    """Does an If-None-Match header name `etag`? A weak `W/` prefix compares on the opaque tag.
    `*` is not honoured: on a queue it would answer 304 to a changed slice, for ever."""
    for tag in (header or "").split(","):
        tag = tag.strip()
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def _rate_ok(ip: str) -> bool:
    """Sliding window per IP. True if the request is allowed."""
    now = time.monotonic()
    with RATE_LOCK:
        dq = RATE.get(ip)
        if dq is None:
            dq = deque()
            RATE[ip] = dq
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_MAX:
            return False
        dq.append(now)
        # opportunistic purge of inactive IPs (bounds the memory of this table)
        if len(RATE) > 8192:
            for k in [k for k, v in RATE.items() if not v or now - v[-1] > RATE_WINDOW][:4096]:
                RATE.pop(k, None)
        return True


def _read_timeout():
    """Seconds a connection may stay silent before the server drops it. Never None, never 0."""
    raw = os.environ.get("DENDRA_RELAY_TIMEOUT", "")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 30.0
    # A zero or a negative value would mean "no limit" to the socket layer; an operator who writes one
    # means to relax the bound, not to remove it.
    return v if v > 0 else 30.0


class Handler(BaseHTTPRequestHandler):
    # Without a timeout a silent socket would hold its thread for ever. `StreamRequestHandler.setup()` arms
    # a socket deadline only when `self.timeout` is not None, and the inherited value is None, so a client
    # that sends nothing (or announces a `Content-Length` and goes quiet) would pin a thread, unseen by the
    # rate limiter, which counts requests. A few hundred such sockets would be enough, with no token.
    # The value can be set from the environment, since a relay behind a slow link may need patience, but
    # it cannot be removed: a `0` or an unreadable value falls back to the default.
    timeout = _read_timeout()

    def _send(self, code, body=b"", headers=None):
        _count("http_%d" % code)          # see the COUNTERS block: one place, so no branch is forgotten
        self.send_response(code)
        if code != 304:                  # a 304 carries no body, hence no type and no length
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body and code != 304:
            self.wfile.write(body)

    def _route(self):
        """The PATH segments, the query string set aside in `self._query`.

        The policy is decided on the path alone: splitting the query off is what lets `GET /list?suffix=`
        reach the `list` policy instead of the strictest one an unknown segment gets. Only the query is
        taken off, so no other route changes how it is read."""
        path, _sep, self._query = self.path.partition("?")
        return [p for p in path.strip("/").split("/") if p != ""]

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _authed(self) -> bool:
        if not TOKEN:
            return True   # no token configured: local mode, no authentication
        got = self.headers.get("X-Dendra-Token", "")
        # Constant-time comparison (== would leak the length of the matching prefix)
        cur = hmac.compare_digest(got, TOKEN)
        if not TOKEN_PREV:
            return cur
        # Both are always evaluated (no short circuit): returning earlier on the current token would
        # make the response time depend on which token was presented.
        prev = hmac.compare_digest(got, TOKEN_PREV)
        if prev and not cur:
            _PREV_HITS[0] += 1
            if _PREV_HITS[0] in (1, 10, 100) or _PREV_HITS[0] % 500 == 0:
                print(f"[relay] previous token still in use ({_PREV_HITS[0]} requests) from "
                      f"{self._client_ip()} - this client is not migrated. Do not remove "
                      f"DENDRA_RELAY_TOKEN_PREV while this counter keeps rising.", flush=True)
        return cur or prev

    # Access policy is decided per route: one policy for the whole service would make this relay
    # impossible to join and protect the wrong thing.
    #
    # `DENDRA_RELAY_TOKEN` is a shared secret that `network-info.txt` does not carry and no self-service
    # procedure issues. Required on every route, it would gate a permissionless network on a secret handed
    # out by hand: a joiner would pass the hardware and genesis-digest checks of `join.sh`, then get 401 on
    # every relay call.
    #
    # Publishing the token would not fix that. A POST to `res/<jid>__<mid>` lands on the only artifact that
    # makes an audit judgeable (see the module docstring). `_put` refuses a second, different content on that
    # key (409), which closes the erasure, but a first write on an unused key still needs a named writer.
    # Write-once and attribution do not replace each other: write-once protects an artifact that exists,
    # attribution decides who may create one.
    #
    # The routes do not share a threat model, so they do not share a policy:
    #   · reading content is already assumed hostile: bodies are sealed to the X25519 key anchored on chain,
    #     and a substitution at the relay achieves nothing. A token in front of ciphertext protects nothing
    #     and blocks a joiner from pulling its own work: open.
    #   · writing miner-owned content (`pub`, `res`) needs the writer to be that miner, which a shared secret
    #     cannot express and a signature can; `relay_client` signs these deposits: a signature, or the token
    #     as the transition path.
    #   · writing `req` hands a miner the sealed prompt of a job someone paid for. The gateway writes it with
    #     the token; a client with its own wallet signs it with the key that paid (C37). So: a signature,
    #     attributed to the job's paying client, on a job still open, over a fee of at least
    #     `DENDRA_REQUEST_PRICE_UDNDR` and filed under a miner of the job's assigned committee, or the token,
    #     read exactly as before.
    #   · `stats` is aggregate counters, and nothing depends on it to work: token.
    #   · `list` returns keys shaped `<jobId>__<minerId>`, and the chain already serves both halves to anyone
    #     without a secret (`/dendra/jobs/v1/job` gives job_id and miner_id, and
    #     `/dendra/jobs/v1/assigned_committee/<jobId>` the drawn members in order). It is also the work queue:
    #     `miner.py::main` opens its loop with `relay.listing()`, and no other call tells a miner a
    #     job is waiting. Behind a token a joiner would get 401 on every round and wait for jobs forever:
    #     open. test_relay_access.py holds both halves.
    #
    # DENDRA_RELAY_SIGN=enforce alone is not the answer: it is global, so it would refuse the gateway's
    # unsigned `req` deposits and stop the network. The issue is applying one mode to routes with different
    # threats, which is why the token path of `req` keeps its own reading in `do_POST`.
    POLICY_OPEN, POLICY_TOKEN, POLICY_SIGNED_OR_TOKEN = "open", "token", "signed-or-token"

    def _policy(self, parts, method):
        """Access policy for this route. Unknown routes get the strictest one: a path nobody
        anticipated must not inherit the permissions of the most open class."""
        # `list` is the work queue and its content is already public on chain (see above); `stats` stays
        # on the token, since nothing depends on it to receive work. Reading the queue is open, writing it
        # is not: this function answers for every method, and an unqualified `OPEN` would give `POST /list`
        # the permission of the GET.
        if parts and parts[0] == "list":
            return self.POLICY_OPEN if method == "GET" else self.POLICY_TOKEN
        if parts and parts[0] == "stats":
            return self.POLICY_TOKEN
        if len(parts) == 2 and parts[0] in STORE:
            if method == "GET":
                return self.POLICY_OPEN
            # The criterion is who writes. Four kinds are written by the miner: `pub`, `res`, `reveal` and
            # `attest` (deposited by reveal_helpers.py, reveal_worker.py and miner.py). For those a
            # signature names the miner that wrote, which a shared secret cannot express, so it stands on
            # its own. `reveal` matters most: it is the primary's obligation, and a sampled job with no
            # reveal has no artifact to judge, so its held fee never resolves.
            # `req` is written by whoever pays for the job: the gateway with the token, or a client that
            # signs with its own wallet, attributed to the payer the chain records (C37).
            return self.POLICY_SIGNED_OR_TOKEN if parts[0] in SIGNED_KINDS else self.POLICY_TOKEN
        return self.POLICY_TOKEN

    def _guard(self, policy=None) -> bool:
        """Rate limit and authentication. Returns False (having already responded) when rejected.

        The rate limit applies to every route, the open ones included: opening a route removes the need
        for a secret, never the need for a bound."""
        if not _rate_ok(self._client_ip()):
            self._send(429, refusal_json("rate limited", help=HELP_RELAY_REFUSALS))
            return False
        if policy == self.POLICY_OPEN:
            return True
        if not self._authed():
            self._send(401, refusal_json("unauthorized (X-Dendra-Token)", help=HELP_RELAY_REFUSALS))
            return False
        return True

    def _verify_write_request(self, kind, key, body):
        """(address|None, reason). The address is the one the chain names for this deposit key.

        The four stages and their order are the guard (shape, signature, attribution, replay), so they are
        not re-implemented here: `modea.relay_write.verify_write` composes them and is tested as a
        composition. Another implementation of the same chain would be another place for a stage to go
        missing.

        Nothing is verified in `off`: it would cost a signature check (about 475 us) on input nothing
        requires yet, and counters of a guard that cannot refuse get read as decoration. `observe` exists
        so that the counters mean something before they can block.
        """
        if SIGN_MODE == "off":
            return None, "off"
        missing = [h for h in _rs.HEADERS if not self.headers.get(h)]
        if missing:
            SIGN_STATS["unsigned"] += 1
            # Named, not "invalid signature": an operator whose client is only too old reads which field is
            # absent instead of searching through its key material.
            return None, "unsigned (missing: " + ", ".join(missing) + ")"
        try:
            pub = base64.b64decode(self.headers.get(_rs.HEADER_PUBKEY), validate=True)
            sig = base64.b64decode(self.headers.get(_rs.HEADER_SIG), validate=True)
            miner = self.headers.get(_rs.HEADER_MINER)
            height = int(self.headers.get(_rs.HEADER_HEIGHT))
            verif = relay_carrier.verifier(self.headers.get(_rs.HEADER_ACCT),
                                           self.headers.get(_rs.HEADER_SEQ))
        except Exception as e:  # noqa: BLE001 — malformed input is a refusal, never an outage
            SIGN_STATS["signed_ko"] += 1
            return None, f"malformed ({type(e).__name__})"
        # Stage (1) continued: the two names this deposit carries must be one name. Checked here rather than
        # in `relay_write`, because `<jid>__<mid>` is the shape of a relay route, which a module that verifies
        # signatures should not have to know.
        target = _miner_named_by_key(kind, key)
        if target is None or target != miner:
            SIGN_STATS["signed_ko"] += 1
            SIGN_REFUSALS[KEY_MISMATCH] = SIGN_REFUSALS.get(KEY_MISMATCH, 0) + 1
            return None, (f"{KEY_MISMATCH} (the key files this deposit under {target!r}, the header "
                          f"announces {miner!r})")
        _refresh_cache(REGISTRY if REGISTRY_URL else None, "miner")
        _refresh_cache(JOBS, "job")
        # `head=None`: the relay has no chain head, so the replay guard runs in its degraded mode (high-water
        # mark and digest, without the freshness window), as `relay_antireplay` states on purpose: a halted
        # chain must not destroy audit evidence.
        # A signed `req` meets the checks of C37 inside the replay stage, before the digest is recorded
        # (`PaidBeforeReplay`): a request refused on its price or its committee can be sent again unchanged.
        antireplay = (PaidBeforeReplay(lambda: self._request_refusal(key), ANTIREPLAY)
                      if kind == "req" else ANTIREPLAY)

        def _verify():
            return relay_write.verify_write(
                kind, key, body, miner, height, pub, sig, REGISTRY, antireplay, None,
                canon=_canon, verify_signature=verif,
                address_from_key=cosmos_addr.address_from_pubkey,
                read_height=relay_canon.message_height,
                job_registry=JOBS, job_from_key=job_registry.job_from_key,
                kit_floor=kit_floor_now())
        r = _verify()
        # A miner the copy does not know may have registered since the last read. One
        # re-read, under the REGISTRY_RETRY floor, then the same verdict again; an identifier the chain does
        # not know is refused a second time. The first verdict stopped at attribution, so nothing was
        # written and the second pass is the first that can reach the replay guard.
        if (not r.accepted and r.reason == relay_write.UNKNOWN_MINER
                and _refresh_cache(REGISTRY if REGISTRY_URL else None, "miner", unknown_miner=True)):
            r = _verify()
        if r.accepted:
            address = r.attributed_to()
            SIGN_STATS["signed_ok"] += 1
            if not address:
                # An acceptance without an address is not an attribution. The empty string is the shape that
                # would slip through: it is falsy but not None, and `address != None` would read it as a name.
                SIGN_STATS["unattributed"] += 1
                SIGN_REFUSALS[EMPTY_ATTRIBUTION] = SIGN_REFUSALS.get(EMPTY_ATTRIBUTION, 0) + 1
                return None, f"{EMPTY_ATTRIBUTION} (accepted with no address for role {r.role!r})"
            SIGN_STATS["attributed"] += 1
            return address, "signed"
        SIGN_REFUSALS[r.reason] = SIGN_REFUSALS.get(r.reason, 0) + 1
        if r.reason in _BEFORE_SIGNATURE:
            SIGN_STATS["signed_ko"] += 1
        elif r.reason == relay_write.STALE_KIT:
            # Verified and attributed, refused on the kit floor: its own counter, so that `unattributed`
            # keeps naming writes filed under a name that is not the writer's.
            SIGN_STATS["signed_ok"] += 1
            SIGN_STATS["stale_kit"] += 1
        elif kind == "req" and r.reason in REQUEST_REFUSALS:
            # Verified and attributed to the payer, refused on what the job paid or where it is filed.
            SIGN_STATS["signed_ok"] += 1
            SIGN_STATS["request_refused"] += 1
        else:
            # The signature verified and the binding refused. Counting this as a bad signature would send an
            # operator to check its key material over a write filed under a name that is not its own.
            SIGN_STATS["signed_ok"] += 1
            SIGN_STATS["unattributed"] += 1
        return None, f"{r.reason} ({r.detail[:120]})"

    def _request_refusal(self, key):
        """None when a `req` deposit attributed to the job's paying client may be written, else
        `(reason, detail)`.

        Attribution says who pays. This says whether the job still waits for its request (the open state,
        read from the same document as the fee), whether it paid the price, and whether the key files the
        request under a miner the chain assigned to that job: `job_from_key` only checks that the key's tail
        is the header's miner, so without this check one paid job would let its payer file deposits under
        invented miner ids until the `req` budget is full, and the gateway's deposits would be refused 507.
        It runs in the replay stage of `relay_write` (`PaidBeforeReplay`), after attribution, so only the
        payer of a job makes the relay read that job, and before the digest is recorded."""
        if REQUEST_PRICE is None:
            return PRICE_UNSET, f"{REQUEST_PRICE_VAR} is unset on this relay"
        # The shape `<jid>__<mid>` was checked by attribution (`job_registry.job_from_key`).
        jid, mid = key.rsplit("__", 1)
        # One read of the job serves the fee and the state, so the two cannot describe different moments.
        fee = state = None
        try:
            doc = JOB_DOC_READER(jid) if JOB_DOC_READER is not None else None
            if doc is not None:
                fee, state = job_fee_from_doc(doc, jid), job_state_from_doc(doc, jid)
        except Exception:  # noqa: BLE001 -- an unreadable job is a refusal, never an outage
            fee = state = None
        if fee is None:
            return FEE_UNREADABLE, f"the fee of job {jid!r:.40} could not be read"
        if state is None:
            return JOB_STATE_UNREADABLE, f"the state of job {jid!r:.40} could not be read"
        if state != JOB_OPEN_STATE:
            return JOB_NOT_OPEN, f"job {jid!r:.40} is {state!r:.60}, it no longer waits for its request"
        if fee < REQUEST_PRICE:
            return UNDERPAID, f"job escrowed {fee} udndr, the price is {REQUEST_PRICE} udndr"
        try:
            members = committee_from_doc(COMMITTEE_READER(jid)) if COMMITTEE_READER is not None else None
        except Exception:  # noqa: BLE001 -- an unreadable committee is a refusal, never an outage
            members = None
        if members is None:
            return COMMITTEE_UNREADABLE, f"the committee of job {jid!r:.40} could not be read"
        if mid not in members:
            return UNASSIGNED_MINER, f"{mid!r:.40} is not in the committee the chain assigned to {jid!r:.40}"
        # The attestation gate of `do_POST` runs after the write is authorised, which for a signature is
        # after the digest is recorded; checked here as well, an unattested miner leaves the message reusable.
        if ATTEST_REQUIRE and not _attest_ok(mid):
            return NOT_ATTESTED, "miner not attested (DENDRA_ATTEST_REQUIRE)"
        return None

    def do_POST(self):
        parts = self._route()
        pol = self._policy(parts, "POST")
        # A signed write needs no shared secret, so for `signed-or-token` the token check waits until it is
        # known whether the deposit is signed: running it first would make the token mandatory.
        if not self._guard(self.POLICY_OPEN if pol == self.POLICY_SIGNED_OR_TOKEN else pol):
            return
        # No write reads a parameter. Ignoring one would file `POST /res/<key>?x` under `<key>`, a key the
        # sender did not write, so the request is refused.
        if self._query:
            self._send(400, b'{"error":"a write takes no query parameter"}')
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        # Body cap. `Content-Length` comes from the client, and an upper bound alone would let a negative
        # value through, where `read(-1)` reads until EOF: it is bounded on both sides.
        if n < 0 or n > MAX_BODY:
            self._send(413, b'{"error":"body too large"}')
            return
        data = self.rfile.read(n) if n else b""
        if len(parts) == 2 and parts[0] in STORE:
            # The signature is checked before the write: `_put` touches the disk and the replay set, and
            # running it on a deposit that will be refused would let an unauthenticated caller fill both.
            # For `req` the price and the committee are part of this verdict (`_request_refusal`).
            _writer, _sig_why = self._verify_write_request(parts[0], parts[1], data)
            if pol == self.POLICY_SIGNED_OR_TOKEN:
                # An attributed write is stronger than the shared token, so it stands alone. The
                # strength is in the attribution: a signature only proves that someone holds a private
                # key, and the verifier rebuilds the signed document from the address derived from that
                # key, so any well-formed key verifies for any deposit key. What names a writer is the
                # registry comparison: `address(pubkey)` equal to the operator the chain records for
                # `<mid>` (or the client that pays for `<jid>`). `_writer` is that address; a valid
                # signature the registry cannot attribute leaves it None and counts as unsigned.
                # The token remains a fallback, so that an operator whose client cannot sign yet is not
                # cut off; a joiner whose signature can be attributed needs no secret at all.
                #
                # Under `enforce` the fallback requires that a token is configured and was presented.
                # `_authed()` alone answers True when no token is configured, which is right for a route
                # guarded only by the token, but here it would make `enforce` refuse nothing on a relay
                # without a token. `off` and `observe` keep letting unsigned deposits through, as they
                # are meant to, so they read the token guard alone.
                #
                # `req` is the exception: its token path is the one it had as a token route, `_authed()` in
                # every mode, so that adding the signature path changes nothing for the gateway.
                if SIGN_MODE == "enforce" and parts[0] != "req":
                    _token_fallback = bool(TOKEN) and self._authed()
                else:
                    _token_fallback = self._authed()
                if not _writer and not _token_fallback:
                    refusal = {
                        "error": "unauthorized",
                        "why": _sig_why,
                        "how": ("sign this deposit with the key that paid for the job, over an open job "
                                "that escrowed at least the relay's request price, under a miner of the "
                                "job's assigned committee, or send X-Dendra-Token"
                                if parts[0] == "req" else
                                "sign this deposit with the key of the operator this miner is "
                                "registered under, or send X-Dendra-Token"),
                    }
                    link = help_for_why(_sig_why)
                    if link:
                        refusal[HELP_FIELD] = link
                    self._send(401, json.dumps(refusal).encode())
                    return
            # Attestation gate on the deposit of a confidential job (req/<jid>__<mid>). After the
            # authorisation, so that a caller who may not write cannot read from a 403 whether a miner is
            # attested.
            if ATTEST_REQUIRE and parts[0] == "req":
                mid = parts[1].split("__", 1)[1] if "__" in parts[1] else ""
                if not (mid and _attest_ok(mid)):
                    self._send(403, refusal_json("miner not attested (DENDRA_ATTEST_REQUIRE)",
                                                 help=HELP_RELAY_REFUSALS))
                    return
            # A kind under `POLICY_TOKEN` refuses nothing on the signature: `_guard` has already required the
            # token, which is that route's policy. The verification still runs and counts, so the signed path
            # is exercised on real traffic before anything depends on it.
            try:
                _put(parts[0], parts[1], data)
            except Conflict as e:
                # 409: the key already holds a sealed artifact and this deposit carries different
                # bytes. Refusing is the only outcome that keeps the audit judgeable.
                self._send(409, json.dumps({"error": str(e)}).encode())
                return
            except relay_store.StoreFull as e:
                # 507: the store is full of evidence that is still useful. Refused rather than erased: a
                # refused deposit is retried, destroyed evidence is gone.
                self._send(507, json.dumps({"error": str(e)}).encode())
                return
            except (relay_store.StoreUnavailable, OSError) as e:
                # Refuse the deposit rather than return an "ok" the disk does not guarantee.
                self._send(503, json.dumps({"error": f"store unavailable: {type(e).__name__}"}).encode())
                return
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"route"}')

    def do_GET(self):
        parts = self._route()
        if not self._guard(self._policy(parts, "GET")):
            return
        if parts == ["list"]:
            # The work queue. A miner learns here, and nowhere else, that a job is waiting for it
            # (`miner.py` opens its loop with `relay.listing`). Open on GET whether or not a token is
            # configured: it returns key pairs the chain already publishes to anyone. The filtered form is
            # the same route under the same policy (see the filtered work queue).
            suffix, err = _list_suffix(self._query)
            if err:
                self._send(400, json.dumps({"error": err}).encode())
                return
            body, tag = _list_answer(suffix)
            hdrs = {"ETag": tag, "Cache-Control": "no-cache"}
            if suffix:
                hdrs[LIST_SUFFIX_HEADER] = suffix
            if _etag_matches(self.headers.get("If-None-Match"), tag):
                self._send(304, b"", hdrs)
            else:
                self._send(200, body, hdrs)
            return
        # Only `/list` reads a parameter. Anywhere else one is refused rather than ignored: answering
        # `GET /req/<key>?x` for `<key>` would serve the caller something it did not ask for.
        if self._query:
            self._send(400, b'{"error":"this route takes no query parameter"}')
            return
        if parts == ["stats"]:
            # Guarded by `_guard` like everything else: the counters map nothing, but no public surface is
            # opened only because it looks harmless.
            self._send(200, json.dumps(_stats()).encode())
            return
        if len(parts) == 2 and parts[0] in STORE:
            d = _get(parts[0], parts[1])
            self._send(404, b'{"error":"not found"}') if d is None else self._send(200, d)
        else:
            self._send(404, b'{"error":"route"}')

    # This silence is a guard, not an oversight. An HTTP access log writes the request path, and in this
    # relay the path is the identifier, `<kind>/<jobId>__<minerId>`: enabling `log_message` would create a
    # lasting leak (log rotation, shipping to a collector), against the invariant that no plaintext
    # appears on chain, at the relay or in logs.
    #
    # Observability comes from aggregate counters instead (refusals by reason, evictions, store occupancy,
    # latency), none of which needs a `jobId`: numbers, never paths. They are exported by
    # `exporter.py`.
    #
    # A static check verifies this override and a test locks it (`tests/test_relay_hardening.py`); both
    # fail if this no-op is removed.
    def log_message(self, *a):
        pass  # silent by design — read the block above before touching this line


def verdict_exposure(token: str, token_prev: str, host: str, public: bool):
    """Is the relay allowed to start? Returns ("REFUSE"|"WARNING"|"OK", message).

    Pure so that it can be tested: the relay bench instantiates `ThreadingHTTPServer` directly and never
    calls `main()`, so a decision inside `main()` could not be reached by any test.

    Two criteria of different kinds:
    · the listen address is a fact: non-local without a token means refusal, whether `DENDRA_PUBLIC` is set
      or not, as on the gateway. The relay carries the sealed envelopes, and an anonymous write would
      overwrite audit evidence for good;
    · `DENDRA_PUBLIC` is a declaration: it adds requirements on the strength of the secret, which an address
      cannot measure.

    The 16-character floor applies to both tokens, since `_authed()` also accepts `DENDRA_RELAY_TOKEN_PREV`
    whatever its length and a weak previous token would be a back door for the whole rotation window. An
    empty previous token has no effect (guard rail 1 of the rotation)."""
    if public:
        if not token:
            return ("REFUSE", "[relay] refused: DENDRA_PUBLIC=1 without DENDRA_RELAY_TOKEN "
                             "(auth required in public).")
        if len(token) < 16:
            return ("REFUSE", "[relay] refused: DENDRA_PUBLIC=1 with a DENDRA_RELAY_TOKEN that is too "
                             "short (<16 chars). Generate `openssl rand -hex 24`.")
        if token_prev and len(token_prev) < 16:
            return ("REFUSE", "[relay] refused: DENDRA_PUBLIC=1 with a DENDRA_RELAY_TOKEN_PREV that is "
                             "too short (<16 chars). The previous token is accepted by _authed(): a "
                             "weak one is a public back door for the whole rotation window.")
        return ("OK", "")
    # Non-local bind without a token: refusal, whether `DENDRA_PUBLIC` is set or not, as guard (1) of the
    # gateway does. `docker-compose.yml` publishes the relay port on the host, so a start without an
    # environment file would offer a relay open to anonymous writes, which could overwrite the sealed
    # envelopes an audit is judged on.
    if not listens_locally(host) and not token:
        return ("REFUSE", f"[relay] refused: non-local bind ({host or '0.0.0.0'}) without "
                         "DENDRA_RELAY_TOKEN -> anonymous writes could overwrite sealed reveals. "
                         "Set DENDRA_RELAY_TOKEN=`openssl rand -hex 24` (and the same value on the "
                         "gateway/miners), or bind DENDRA_RELAY_HOST=127.0.0.1.")
    return ("OK", "")


def verdict_attestation(public: bool, require: bool, allow_list):
    """Is the attestation gate usable? Returns ("REFUSE"|"OK", message).

    Pure, like `verdict_exposure`, and for the same reason: the bench never calls `main()`.

    The refusal is about asking for the gate with nothing to check against, not about being public:
    `_attest_ok` refuses every miner when the allow-list is empty, so requiring attestation without one
    assigns no job at all. Either the gate is armed and usable, or the start is refused; never a gate that
    shuts the network down without a word.

    Being public does not arm the gate on its own: an allow-list pins specific measured digests, and
    forcing it on an open network would exclude the independent operators it needs. Public exposure makes
    the state visible instead: the caller prints a banner when the relay serves without attestation.
    """
    if require and not allow_list:
        return ("REFUSE", "[relay] refused: DENDRA_ATTEST_REQUIRE=1 with an empty DENDRA_ATTEST_ALLOW. "
                         "The gate refuses every miner without an allow-list, which would assign no "
                         "job at all. Set DENDRA_ATTEST_ALLOW=<sha256[,sha256...]> (see "
                         "modea_attest.py), or leave DENDRA_ATTEST_REQUIRE unset.")
    if public and not require:
        # A state rather than a failure, and one that must be visible without reading the source.
        return ("OK", "[relay] NOTICE: serving publicly with attestation not enforced "
                      "(DENDRA_ATTEST_REQUIRE unset). Miners are not required to present a signed "
                      "measured client. Do not describe attestation as active anywhere public while "
                      "this is the case.")
    return ("OK", "")


def verdict_signature(sign_mode: str, registry_url: str, token: str):
    """Can a signed write ever be attributed here? Returns ("REFUSE"|"OK", message).

    Pure, like the two verdicts above, and for the same reason: the bench never calls `main()`.

    The refusal is about a relay that could accept nothing. Under `enforce` a deposit passes on an
    attributed signature or on the token, and on nothing else; without a registry no signature can be
    attributed, and without a token there is no fallback, so every write would be refused while the relay
    looked like a quiet network. As with an attestation gate armed on an empty allow-list, the start fails
    where it can be read.

    The notice is the other half, and the one usually printed: a relay with no registry still verifies and
    counts signatures but cannot attribute one, so a joiner holding no token cannot write, however
    correctly it signs.
    """
    if sign_mode == "off":
        return ("OK", "")
    if not registry_url:
        if sign_mode == "enforce" and not token:
            return ("REFUSE",
                    "[relay] refused: DENDRA_RELAY_SIGN=enforce without DENDRA_RELAY_REGISTRY and "
                    "without DENDRA_RELAY_TOKEN. A signature can only be attributed against the "
                    "registry, and there is no token to fall back on: every write would be refused. "
                    "Set DENDRA_RELAY_REGISTRY=http://<node>:1317, or keep DENDRA_RELAY_TOKEN.")
        return ("OK",
                "[relay] NOTICE: DENDRA_RELAY_REGISTRY is unset -> a signature is verified but "
                "attributed to nobody, so it authorises no write on its own and every deposit falls "
                "back to DENDRA_RELAY_TOKEN. A joiner that holds no token cannot write, however "
                "correctly it signs. Set DENDRA_RELAY_REGISTRY=http://<node>:1317 to let a signed "
                "deposit stand alone.")
    return ("OK", "")


def main():
    # Fail closed on persistence, before any other guard: a relay serving without a store would keep the
    # audit evidence in the RAM of one process. It refuses, names the path and the system error, and
    # invents no fallback.
    if BOOT_ERR:
        print(f"[relay] refused: store not writable -> {BOOT_ERR}\n"
              f"[relay] The relay cannot start without persistence: the sealed reveal is the only\n"
              f"[relay] artifact that makes an audit judgeable, and losing it freezes the held fee for good.\n"
              f"[relay] Fix the permissions, or pick another location with DENDRA_RELAY_STORE=<dir>.",
              file=sys.stderr)
        sys.exit(2)
    # An unrecognised value of `DENDRA_PUBLIC` is a refusal, never a fallback to "not public", which would
    # expose the relay with its guards disarmed because of a typo.
    if PUBLIC_ERR:
        print(f"[relay] refused: {PUBLIC_ERR}", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8645
    # Several machines: DENDRA_RELAY_HOST=0.0.0.0 accepts remote miners (default 127.0.0.1). Before exposing
    # on 0.0.0.0, set DENDRA_RELAY_TOKEN (authentication) and a WireGuard or SSH tunnel (transport).
    host = os.environ.get("DENDRA_RELAY_HOST", "127.0.0.1")
    auth = "ON" if TOKEN else "OFF(local)"
    for verdict, reason in (verdict_exposure(TOKEN, TOKEN_PREV, host, PUBLIC),
                           verdict_attestation(PUBLIC, ATTEST_REQUIRE, ATTEST_ALLOW),
                           verdict_signature(SIGN_MODE, REGISTRY_URL, TOKEN),
                           verdict_kit_policy(KIT_POLICY_ERROR, KIT_FLOOR, KIT_DEADLINE_TS),
                           verdict_request_price(REQUEST_PRICE_ERROR, REQUEST_PRICE)):
        if verdict == "REFUSE":
            print(reason, file=sys.stderr)
            sys.exit(2)
        # An "OK" that carries a message still has something to say, so the branch keys on the presence of
        # a message, not on the verdict class; otherwise ("OK", notice) would lose its text, including the
        # notice that attestation is not enforced. Unit tests of the pure functions cannot see that gap.
        elif reason:
            print(reason, file=sys.stderr)
    srv = ThreadingHTTPServer((host, port), Handler)
    n = sum(BOOT_STATS.values())
    corrupt = getattr(DISK, "last_load_corrupt", 0)
    expired = getattr(DISK, "last_load_expired", 0)
    print(f"[relay] store {STORE_DIR} - {n} record(s) reloaded "
          f"({' '.join(f'{k}={v}' for k, v in sorted(BOOT_STATS.items()) if v)or 'empty'})"
          + (f"  {corrupt} unreadable ignored" if corrupt else "")
          + (f"  {expired} expired purged" if expired else ""))
    print(f"[relay] eviction by age only - retention {RETENTION:g}s "
          f"({RETENTION/86400:.1f} d); budget {KIND_BUDGET >> 20} MiB/kind "
          f"({ENTRY_OVERHEAD} bytes charged per entry on top of key and body) is a guard rail, "
          f"beyond it deposits are refused (507) instead of erasing evidence; alarm at "
          f"{FILL_ALARM:.0%} in /stats")
    if KIND_BUDGET_NOTE:
        print(f"[relay] WARNING: {KIND_BUDGET_NOTE}", file=sys.stderr)
    _verdict_mem, _said_mem = verdict_budget_memory(KIND_BUDGET, len(STORE), _cgroup_memory_limit())
    if _said_mem:
        print(_said_mem, file=sys.stderr)
    for line in _startup_alarm_lines():
        print(line, flush=True)
    # The state of the attestation gate is announced, so that nobody describes it as armed by assumption.
    print(f"[relay] attestation {'required' if ATTEST_REQUIRE else 'not required'} - "
          f"{len(ATTEST_ALLOW)} recognized digest(s) (deterrence: a signed measured client, "
          f"not a proof of execution)")
    # The attribution registry is announced next to the mode: "signatures are on" and "signatures name
    # someone" are two different statements, and only the second guards anything.
    print(f"[relay] signed writes {SIGN_MODE} - attribution registry "
          f"{REGISTRY_URL or 'not configured (a signature attributes to nobody)'} - "
          f"write-once on {'/'.join(WRITE_ONCE)} (a second, different content is refused 409)")
    print(f"[relay] listening on http://{host}:{port}  auth={auth}  rate={RATE_MAX}/{RATE_WINDOW:g}s/IP  (encrypted transport)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
