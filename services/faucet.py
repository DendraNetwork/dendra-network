#!/usr/bin/env python3
"""Native Dendra faucet (replaces the ignite faucet).

POST {"address":"dendra1..."}  -> sends FAUCET_AMOUNT udndr from the FROM account (bob),
the only key in the faucet's own keyring (DENDRA_HOME: the faucet-keys volume, which no other service mounts).
Sends are serialized by a lock and each one waits for inclusion, so two sends never reuse a sequence.
GET /  -> health probe.

Env: DENDRA_NODE, DENDRA_CHAIN_ID (optional: read from the node and cross-checked), DENDRA_FAUCET_FROM=bob, DENDRA_FAUCET_AMOUNT=10000000,
     DENDRA_FAUCET_PORT=4500, DENDRA_HOME=/root/.dendra.

The amount of a drip. DENDRA_FAUCET_AMOUNT is a positive whole number of udndr, or the word `min_stake`: the
chain's `min_stake`, read from the chain (`dendrad query jobs params`) and re-read every MIN_STAKE_TTL_S,
which is exactly what one identity locks to register. A miner then registers with its whole drip and sends
its transactions at zero gas (the chain's validators run at a zero minimum gas price and the kit sends no
fee). A fixed 10 DNDR at a min_stake of 1 DNDR funds ten identities per drip. A min_stake that cannot be read,
or that reads 0 (proto3 omits a zero: absent is 0), refuses the drip (503) before any quota is spent: the
amount is never guessed, and a drip of 0 is not a drip. GET / says the amount and where it comes from.

A refusal names a cause, never dendrad's output. The 503 and the 502 carry one of the fixed CAUSE_* /
TRANSFER_* texts; what dendrad printed goes to this service's log (`_log_detail`), for the operator.

The kit floor. From DENDRA_KIT_DEADLINE (UTC) a request whose body declares a `kit_version`
below DENDRA_KIT_MIN is refused (403, REFUSAL_STALE_KIT, naming the update) before the amount is read and before
any quota is spent (`H.do_POST`, before `_rate_ok`). A request that declares no number is served: the wallet page,
the docs page, the gateway's own funding (gateway_fund.py) and deploy/testnet/join_validator.sh send none, and they are
not kits. A field that is present but not a whole number is refused as malformed (400, REFUSAL_BAD_KIT_VERSION),
so a stale kit cannot pass by writing its number badly. The two variables are the ones the relay reads and
`network-info.txt` publishes; both unset, the floor is dormant and every kit is served; one without the other,
or a malformed value, refuses to boot (`kit_policy_from_env`). The kit sends the field in its request body
(`KIT_VERSION_FIELD`, the name the relay's deposits use too, modea/relay_write.py); a declared number is declared,
not proven. Refusals are counted and printed in the log, and GET / counts them
with the requests let through without a number.
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from modea.exposure import listens_locally

from modea import kit_floor

NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
# The chain id is read (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
# Resolved lazily: miner imports this module for the PoW contract only, and must not query a node
# for a chain id it never uses.
def _chain_id():
    from modea import chain_id as _cid
    return _cid.chain_id(tuple(NODEF))
FROM = os.environ.get("DENDRA_FAUCET_FROM", "bob")
# udndr (10 DNDR), or "min_stake": see the module's header and `drip_amount`.
AMOUNT = os.environ.get("DENDRA_FAUCET_AMOUNT", "10000000").strip()
AMOUNT_MIN_STAKE = "min_stake"
MIN_STAKE_TTL_S = 300.0   # Chosen: a governed parameter changes by a vote, which takes far longer than this
PORT = int(os.environ.get("DENDRA_FAUCET_PORT", "4500"))
HOME = os.environ.get("DENDRA_HOME", "/root/.dendra")
DENOM = "udndr"
KB = ["--keyring-backend", "test", "--home", HOME]
NODEF = ["--node", NODE]
MAX_BODY = 8192          # a faucet request is an address plus a PoW; 8 KiB is already generous
_RE_ADDR = re.compile(r"^dendra1[0-9a-z]{38,70}$")
_LOCK = threading.Lock()

# --- Anti-abuse. The testnet pays rewards, so without caps the faucet is easy to drain with many identities.
#     Caps: per address (cooldown), per IP per day, and a global cap per day. A reached cap answers 429. State is
#     persisted, so a restart does not reopen the caps. An optional PoW tied to the address makes each new
#     address cost CPU. ---
ADDR_COOLDOWN = int(os.environ.get("DENDRA_FAUCET_ADDR_COOLDOWN", "86400"))  # 1 drip / address / 24 h
IP_DAILY = int(os.environ.get("DENDRA_FAUCET_IP_DAILY", "5"))                # max drips / IP / 24 h
DAILY_CAP = int(os.environ.get("DENDRA_FAUCET_DAILY_CAP", "2000"))           # Global cap / 24 h (anti-drain)
# Persistence: "" = RAM only (previous behavior). Default = under the DENDRA_HOME volume -> survives restart.
STATE_FILE = os.environ.get("DENDRA_FAUCET_STATE", os.path.join(HOME, "faucet-state.json"))
# PoW: 0 = off (closed testnet). >0 (e.g. 20) requires a 'pow' such that sha256(addr+':'+pow) has N leading zero bits.
POW_BITS = int(os.environ.get("DENDRA_FAUCET_POW_BITS", "0"))
_DAY = 86400.0
_RL_LOCK = threading.RLock()
_addr_last = {}    # addr -> ts of the last drip
_ip_hits = {}      # ip   -> [ts...] (24 h window)
_global_hits = []  # [ts...] global (24 h window)


# --- PoW contract: one definition, shared by both sides -------------------------------------------
# The expected token is a nonce `pow` such that sha256("<address>:<nonce>") carries at least POW_BITS
# leading zero bits. The verifier (server, `pow_ok`) and the solver (client, `solve_pow`) both derive
# from the same digest `pow_digest`: two separate implementations that merely resemble each other end
# up diverging on a separator or an encoding, and that divergence makes the faucet unreachable while
# neither side reports a fault — the client believes it is paying the requested CPU, the server sees
# an invalid token.
POW_SEP = ":"


def pow_digest(addr, nonce):
    """Digest of the (address, nonce) pair.

    The nonce is bound to this address: a token solved for another address is worthless, so every
    additional Sybil identity has to be paid for in CPU. That is the entire point of the faucet PoW.
    """
    return hashlib.sha256((str(addr) + POW_SEP + str(nonce)).encode()).digest()


def leading_zero_bits(digest):
    """Number of leading zero bits in the digest."""
    bits = 0
    for byte in digest:
        if byte:
            return bits + 8 - byte.bit_length()
        bits += 8
    return bits


def pow_ok(addr, nonce, bits):
    """Pure validity predicate for the token. `bits` <= 0 disarms the PoW (closed testnet)."""
    if bits <= 0:
        return True
    if not nonce:
        return False
    return leading_zero_bits(pow_digest(addr, nonce)) >= bits


def solve_pow(addr, bits, deadline_s=300.0, progress=None):
    """Solves the PoW on the client side. Returns the nonce, or "" when the time bound is reached.

    This is the client reference implementation: every faucet requester calls this, never a copy.
    `progress(attempts, seconds)` is invoked periodically so that a long wait does not look like a
    hang. It is time-bounded: a PoW that is too hard must produce a named failure, not an infinite
    loop the requester gets lost in.
    """
    if bits <= 0:
        return ""
    t0 = time.time()
    n = 0
    while True:
        nonce = format(n, "x")
        if leading_zero_bits(pow_digest(addr, nonce)) >= bits:
            return nonce
        n += 1
        if n % 20000 == 0:
            elapsed = time.time() - t0
            if elapsed >= deadline_s:
                return ""
            if progress:
                progress(n, elapsed)


def _pow_ok(addr, pow_str):
    """Service-side verifier: applies the configured difficulty (DENDRA_FAUCET_POW_BITS)."""
    return pow_ok(addr, pow_str, POW_BITS)


def _save_state():
    """Persist the anti-abuse state with an atomic write."""
    if not STATE_FILE:
        return
    now = time.time()
    with _RL_LOCK:
        data = {
            "addr_last": {a: t for a, t in _addr_last.items() if now - t < ADDR_COOLDOWN},
            "ip_hits": {ip: [t for t in ts if now - t < _DAY] for ip, ts in _ip_hits.items()},
            "global_hits": [t for t in _global_hits if now - t < _DAY],
        }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        raise


def _load_state():
    """Reload the anti-abuse state at startup, so a restart does not reset the caps."""
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    now = time.time()
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except Exception as e:
        # Fail closed, like the gateway quota: empty counters would let the faucet be drained again. The
        # global cap is marked reached for about 24 h. Fix or remove STATE_FILE to reopen.
        with _RL_LOCK:
            _global_hits.extend(now for _ in range(DAILY_CAP))
        print(f"[faucet] state file unreadable ({type(e).__name__}): failing closed, the global cap is "
              f"treated as reached. Fix or remove {STATE_FILE}.", flush=True)
        return
    with _RL_LOCK:
        for a, t in d.get("addr_last", {}).items():
            if now - float(t) < ADDR_COOLDOWN:
                _addr_last[a] = float(t)
        for ip, ts in d.get("ip_hits", {}).items():
            kept = [float(t) for t in ts if now - float(t) < _DAY]
            if kept:
                _ip_hits[ip] = kept
        _global_hits.extend(float(t) for t in d.get("global_hits", []) if now - float(t) < _DAY)
    print("[faucet] anti-abuse state reloaded (%s, %d addresses in cooldown)" % (STATE_FILE, len(_addr_last)), flush=True)


# The three refusals, as the wire carries them (the `info` of a 429). Named here, once, because the miner
# daemon classifies a refusal by comparing with these constants (it imports this module): a copy of the text
# on the other side would drift the day one of them is reworded, and an IP-quota refusal would then read as
# "unknown". The texts themselves are unchanged, so the wire format is.
REFUSAL_GLOBAL_CAP = "global daily cap reached (anti-drain)"
REFUSAL_ADDR_COOLDOWN = "address funded recently (cooldown)"
REFUSAL_IP_QUOTA = "too many requests from this IP (daily quota)"

# -- The kit floor -------------------------------------------------------------------
# The field of the request body that declares the kit: the same name the relay reads in a signed deposit
# (modea/relay_write.py, KIT_VERSION_FIELD) and the capacity report carries (capacity_server.py), so one kit declares
# one number under one name everywhere. The miner daemon writes it when it asks for a grant.
KIT_VERSION_FIELD = "kit_version"
# The field that carries the floor in force: in the 403 of a stale kit and under `kit_floor` of GET /stats. The
# miner daemon classes a refusal by this field read as an integer (`miner.faucet_reason`, the word
# `stale_kit`), never by the text of `error` or `info`.
KIT_MIN_FIELD = "kit_min"
KIT_DEADLINE_FORMAT = kit_floor.KIT_DEADLINE_FORMAT
# The refusal, as the wire carries it (the `info` of the 403); %d is the floor. It names the update, as the
# floor rule asks. The update line a kit below the floor prints ends in `git checkout -B main origin/main`: it leads to a tree
# no signature covers and, during a governed upgrade, to one that join.sh refuses on the network rules. So the text
# names the installer, which places the clone on the newest signed release of the rules the network declares.
REFUSAL_STALE_KIT = ("kit below the network's floor (kit_version %d or later required): update the kit with the "
                     "installer (download install.sh, read it, then run bash install.sh --yes with the options of "
                     "your first installation; it places the clone on the newest signed release and runs join.sh), "
                     "then ask again")
# The refusal of a field that is there but holds no kit number (the `info` of the 400). The kits write an integer
# or leave the field out (modea/kit_version.py::with_kit_version), so this names a client that writes it wrong.
REFUSAL_BAD_KIT_VERSION = ("kit_version must be a whole number (the kit number of a release), or left out")

# -- Where a refusal is explained ----------------------------------------------------------------------------
# A refusal a person reads carries the address of the section of the public site that settles it, in the field
# `help` of the reply. It is a field of its own that nobody reads: the `info` of a 429 is compared with the
# REFUSAL_* texts by the miner daemon (an older miner compares its own copy), so no link is added to a text.
# `help` comes before `info` in the reply, because the miner daemon logs only the first 300 bytes of a refusal
# and the stale-kit `info` alone is longer. Keyed by the refusal text (the stale-kit one is the template, not the
# filled line). A refusal without an entry is sent without a link and the bench in tests/test_refusal_help.py
# fails: no address is invented for it.
SITE_URL = "https://dendranetwork.com"
HELP_FIELD = "help"
HELP_FAUCET_LIMITS = SITE_URL + "/docs/#faucet-limits"
HELP_UPDATE_KIT = SITE_URL + "/update/#update"
REFUSAL_HELP = {
    REFUSAL_GLOBAL_CAP: HELP_FAUCET_LIMITS,
    REFUSAL_ADDR_COOLDOWN: HELP_FAUCET_LIMITS,
    REFUSAL_IP_QUOTA: HELP_FAUCET_LIMITS,
    REFUSAL_STALE_KIT: HELP_UPDATE_KIT,
    REFUSAL_BAD_KIT_VERSION: HELP_UPDATE_KIT,
}


def refusal_body(error, info, help_key, address, **fields):
    """The JSON of a refusal: ok, error, help (when the refusal has one), info, the extra fields, address.

    `help_key` is the refusal text that REFUSAL_HELP lists; `info` is what the requester reads, which differs
    from it only for the stale kit, whose text carries the floor."""
    body = {"ok": False, "error": error}
    link = REFUSAL_HELP.get(help_key)
    if link:
        body[HELP_FIELD] = link
    body["info"] = info
    body.update(fields)
    body["address"] = address
    return body


def kit_policy_from_env(min_raw, deadline_raw):
    """`(floor, deadline_ts, error)` from the two raw strings, read by `modea.kit_floor.policy_from_env` as the relay
    and the season rules read them: both empty is dormant `(None, None, None)`; both set is armed `(int, float, None)`
    from `deadline_ts` (UTC seconds); anything else is an error, and the daemon refuses to boot on it rather than
    guess a floor or a date."""
    return kit_floor.policy_from_env(min_raw, deadline_raw)


KIT_FLOOR, KIT_DEADLINE_TS, KIT_POLICY_ERROR = kit_policy_from_env(
    os.environ.get("DENDRA_KIT_MIN"), os.environ.get("DENDRA_KIT_DEADLINE"))
# Counts since boot, all three read only while the floor is in force: refusals under the floor and refusals of a
# malformed field (each also printed in the log), and requests let through that declared no number, which the
# floor does not see. GET / serves the three under `kit_floor`.
_STALE_REFUSED = [0]
_MALFORMED_REFUSED = [0]
_UNDECLARED_PASSED = [0]


def kit_floor_now(now=None):
    """The floor in force: None while dormant or before the deadline, else the integer. Read at each request, so
    the instant is crossed without a restart; the clock is this host's, in UTC, as it is for the relay."""
    return kit_floor.floor_in_force(KIT_FLOOR, KIT_DEADLINE_TS, time.time() if now is None else now)


def declared_kit_version(body):
    """The kit number a request body declares, or None: the field absent, or anything but a whole number
    (a bool included), declares none. Nothing is read as a current kit by default. Whether a None came from
    an absent field or from a malformed one is `kit_version_malformed`."""
    v = body.get(KIT_VERSION_FIELD) if isinstance(body, dict) else None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v


def kit_version_malformed(body):
    """True when the body carries the field with a value that is not a kit number: text, a bool, a float, a
    negative number, a list or an object. A JSON null is no value, read as the field left out. This keeps
    "declares none", which is served, apart from "declares something unreadable", which is refused."""
    if not isinstance(body, dict) or body.get(KIT_VERSION_FIELD) is None:
        return False
    return declared_kit_version(body) is None


def kit_floor_status():
    """One line for the log at boot, and the shape the probe serves."""
    if KIT_POLICY_ERROR:
        return "error", KIT_POLICY_ERROR
    if KIT_FLOOR is None:
        return "dormant", "kit floor dormant: DENDRA_KIT_MIN and DENDRA_KIT_DEADLINE unset, every kit is served"
    when = datetime.datetime.fromtimestamp(KIT_DEADLINE_TS, tz=datetime.timezone.utc).strftime(KIT_DEADLINE_FORMAT)
    state = "armed" if kit_floor_now() is not None else "announced"
    return state, ("kit floor %s: from %s a request whose body declares %s below %d is refused (403), one whose %s "
                   "is not a whole number is refused (400), and one that declares none is served"
                   % (state, when, KIT_VERSION_FIELD, KIT_FLOOR, KIT_VERSION_FIELD))


def _rate_ok(addr, ip):
    """Authorizes the drip and records it. Returns (ok, reason). Fail-closed when a cap is reached.

    The most specific refusal comes first: the address's cooldown, then the IP's daily quota, then the global
    cap. The miner daemon classifies a refusal by these constants (`miner.faucet_reason`), so a funded
    address must not be told "global cap" once the day's budget is gone.
    The order spends nothing, because nothing is recorded before every check has passed. Whatever the order,
    ceil(DAILY_CAP / IP_DAILY) distinct IPs spend the day's budget; only the settings move that number, and a
    drip of `min_stake` (DENDRA_FAUCET_AMOUNT=min_stake) is what makes a higher DAILY_CAP affordable."""
    now = time.time()
    with _RL_LOCK:
        if now - _addr_last.get(addr, 0.0) < ADDR_COOLDOWN:
            return False, REFUSAL_ADDR_COOLDOWN
        hits = [t for t in _ip_hits.get(ip, []) if now - t < _DAY]
        if len(hits) >= IP_DAILY:
            return False, REFUSAL_IP_QUOTA
        _global_hits[:] = [t for t in _global_hits if now - t < _DAY]
        if len(_global_hits) >= DAILY_CAP:
            return False, REFUSAL_GLOBAL_CAP
        before = (_addr_last.get(addr), _ip_hits.get(ip))
        _addr_last[addr] = now
        hits.append(now)
        _ip_hits[ip] = hits
        _global_hits.append(now)
        try:
            _save_state()
        except Exception:
            # Memory stays what the disk holds: the caller answers 503 and nothing was granted.
            if before[0] is None:
                _addr_last.pop(addr, None)
            else:
                _addr_last[addr] = before[0]
            if before[1] is None:
                _ip_hits.pop(ip, None)
            else:
                _ip_hits[ip] = before[1]
            _global_hits.pop()
            raise
        return True, ""


def _rate_refund(addr):
    """Gives back only the address cooldown, after a drip that was authorised but never paid.

    `_rate_ok` records the drip before `fund()` runs, so without this a failed transfer would lock
    the requester out for ADDR_COOLDOWN with nothing received.
    The refund is deliberately narrow. The address cooldown stands for a payout, and none happened.
    The IP quota and the global cap stand for work this server did either way; refunding them would
    give unlimited attempts to anyone able to provoke a failure.
    """
    with _RL_LOCK:
        _addr_last.pop(addr, None)
        # `_rate_ok` saved the drip before the transfer, so the refund is saved too, or a restart would lock
        # the address again. A failed save keeps the refund in memory, which is what this process serves.
        try:
            _save_state()
        except Exception as e:  # noqa: BLE001
            _log_detail("state save failed after a refund", str(e))


def _run(cmd, t=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=t)


# -- What a requester is told, and what only the log is told -------------------------------------------------
# dendrad's output names the node, the keyring's home, the funding key and an account sequence. It is meant
# for the operator, and this endpoint is public. So a refusal carries one of the fixed causes below, and the
# detail goes to the faucet's log (`_log_detail`). The causes stay distinct only where the requester's next
# move differs.
CAUSE_MIN_STAKE_UNREAD = "min_stake could not be read from the chain"
CAUSE_MIN_STAKE_UNUSABLE = "the chain's min_stake is not a positive whole number of udndr"
CAUSE_MIN_STAKE_NOT_YET = "min_stake not read from the chain yet (it is read at the next drip)"
CAUSE_SETTING = "the faucet's DENDRA_FAUCET_AMOUNT setting is unusable"
# These three begin with "transfer failed", the words the 502 of a transfer that raised already began
# with, so a reader matching that prefix keeps matching. Not every 502 does: a transfer that was sent and
# then rejected or never included answers with `_wait_tx`'s own reason, which begins otherwise.
TRANSFER_NOT_SENT = "transfer failed: the faucet could not send it (the detail is in the faucet's log)"
TRANSFER_RAISED = "transfer failed: the faucet's transfer did not complete (the detail is in the faucet's log)"
TRANSFER_NO_HASH = "transfer failed: no transaction hash came back (the detail is in the faucet's log)"
LOG_REPEAT_S = 60.0       # the same detail is logged again at most this often, with how many it stood for
_LOG_LOCK = threading.Lock()
_log_last = {}            # what -> [detail, monotonic time it was printed, repeats since]


def _log_detail(what, detail, now=None):
    """Write the operator's detail to the log: once per change, and a repeat at most every LOG_REPEAT_S.

    A refusal that repeats on every request (a node that stays down) must not write a line per request: a
    public endpoint would then decide how fast the log fills. The text is flattened to one line and bounded,
    because it is a subprocess's output and nobody chose its shape."""
    flat = " ".join(str(detail).split())[:400]
    now = time.monotonic() if now is None else now
    with _LOG_LOCK:
        last = _log_last.get(what)
        if last and last[0] == flat and now - last[1] < LOG_REPEAT_S:
            last[2] += 1
            return False
        repeats = last[2] if (last and last[0] == flat) else 0
        _log_last[what] = [flat, now, 0]
    print("[faucet] %s: %s%s" % (what, flat, " (and %d more like it)" % repeats if repeats else ""),
          flush=True)
    return True


class AmountUnknown(RuntimeError):
    """The amount of a drip cannot be known: the drip is refused, never sent with a guessed amount.

    Two texts for two readers. `str(e)` is the detail, for the log — it may quote dendrad. `e.public` is the
    cause a requester is told, one of the CAUSE_* constants and never anything dendrad printed."""

    def __init__(self, detail, public=CAUSE_MIN_STAKE_UNREAD):
        super().__init__(detail)
        self.public = public


_MS_LOCK = threading.Lock()
_ms_cache = {"value": None, "at": 0.0}    # the chain's min_stake as last read, and when (monotonic)
MIN_STAKE_SOURCE = "min_stake, read from the chain"
SETTING_SOURCE = "DENDRA_FAUCET_AMOUNT"


def amount_setting_error(setting=None):
    """'' when DENDRA_FAUCET_AMOUNT is usable, else why not. The service refuses to start on it."""
    s = AMOUNT if setting is None else str(setting).strip()
    if s == AMOUNT_MIN_STAKE:
        return ""
    if not (s.isascii() and s.isdigit()):
        return "DENDRA_FAUCET_AMOUNT=%r is neither a whole number of udndr nor %r" % (s, AMOUNT_MIN_STAKE)
    if int(s) <= 0:
        return "DENDRA_FAUCET_AMOUNT=%r is zero: a drip of nothing funds nobody" % s
    return ""


def _read_min_stake():
    """The chain's `min_stake`, read now. Raises AmountUnknown: a query that fails is not a value, and a
    value of 0 (which is also what an absent field means, since proto3 omits a zero) is not a drip."""
    try:
        r = _run(["dendrad", "query", "jobs", "params", "--output", "json", *NODEF], t=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AmountUnknown("query jobs params: %s" % type(e).__name__)
    if r.returncode != 0:
        raise AmountUnknown("query jobs params: rc=%d %s" % (r.returncode, (r.stderr or "").strip()[:160]))
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise AmountUnknown("query jobs params: the answer is not JSON")
    p = d.get("params") if isinstance(d, dict) else None
    if not isinstance(p, dict):
        raise AmountUnknown("query jobs params: an answer without `params`")
    v = p.get("min_stake", 0)        # the query answered: an absent field is 0 (rule of zero)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise AmountUnknown("min_stake=%r is not a whole number" % (v,), CAUSE_MIN_STAKE_UNUSABLE)
    if v <= 0:
        raise AmountUnknown("min_stake reads %d on this chain: a drip of it would fund nobody" % v,
                            CAUSE_MIN_STAKE_UNUSABLE)
    return v


def drip_amount(read=True, now=None):
    """(udndr per drip, where the amount comes from). Raises AmountUnknown when it cannot be known.
    `read=False` never queries the chain (the health probe): it answers the amount last read, however old --
    its age is `min_stake_read_age` -- and an amount never read is unknown there. Expiring it there after
    MIN_STAKE_TTL_S made a quiet faucet (no drip for five minutes) answer "not read yet" about a value it had
    read, and a check of the probe fail on a healthy faucet. A drip (`read=True`) reads it again once expired."""
    if AMOUNT != AMOUNT_MIN_STAKE:
        err = amount_setting_error()
        if err:
            raise AmountUnknown(err, CAUSE_SETTING)
        return int(AMOUNT), SETTING_SOURCE
    now = time.monotonic() if now is None else now
    # The read happens under the lock: requests arriving together on an expired value wait for one query
    # instead of each starting its own dendrad process.
    with _MS_LOCK:
        if _ms_cache["value"] is not None and (not read or now - _ms_cache["at"] < MIN_STAKE_TTL_S):
            return _ms_cache["value"], MIN_STAKE_SOURCE
        if not read:
            raise AmountUnknown(CAUSE_MIN_STAKE_NOT_YET, CAUSE_MIN_STAKE_NOT_YET)
        v = _read_min_stake()
        _ms_cache["value"], _ms_cache["at"] = v, now
    return v, MIN_STAKE_SOURCE


def min_stake_read_age(now=None):
    """Whole seconds since min_stake was last read from the chain, or None when it never was."""
    now = time.monotonic() if now is None else now
    with _MS_LOCK:
        if _ms_cache["value"] is None:
            return None
        return max(0, int(now - _ms_cache["at"]))


def _txhash(out):
    try:
        return json.loads(out).get("txhash")
    except Exception:
        m = re.search(r"txhash:\s*([0-9A-Fa-f]{64})", out or "")
        return m.group(1) if m else None


def _tx_code(out):
    """Result code carried by a tx answer, or None when the answer could not be read.

    proto3 omits a field worth its zero value, and success is code zero: an answer without `code` is
    therefore a success, and defaulting the absent field to a non-zero value turns every such answer
    into a failure. `None` is a separate value on purpose -- "we could not read it" must never share a
    value with "it worked" nor with "the chain refused it", because the three earn different answers.
    """
    try:
        d = json.loads(out)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    try:
        return int(d.get("code", 0))
    except (TypeError, ValueError):
        return None


def _wait_tx(h, tries=20):
    """(ok, why). Polls until the tx is readable on-chain; a refusal is final, waiting cannot lift it."""
    for _ in range(tries):
        r = _run(["dendrad", "query", "tx", h, "--output", "json", *NODEF])
        if r.returncode == 0:
            c = _tx_code(r.stdout)
            if c == 0:
                return True, ""
            if c is not None:
                return False, "tx rejected by the chain (code %d)" % c
        time.sleep(1.5)
    return False, "tx not included"


def fund(addr, amount=None):
    """Sends one drip of `amount` udndr (the handler resolves it, `drip_amount`, before any quota is spent;
    None resolves it here). A non-positive amount is refused: a transfer of nothing is not a drip."""
    if amount is None:
        amount = drip_amount()[0]
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return False, "drip amount %r is not a positive number of udndr" % (amount,)
    with _LOCK:
        cmd = ["dendrad", "tx", "bank", "send", FROM, addr, str(amount) + DENOM,
               *KB, *NODEF, "--chain-id", _chain_id(), "-y",
               "--fees", "0" + DENOM, "--gas", "auto", "--gas-adjustment", "1.5",
               "--output", "json", "--broadcast-mode", "sync"]
        r = _run(cmd)
        # dendrad's own words go to the log, never to the requester (see what a requester is told).
        if r.returncode != 0:
            _log_detail("transfer not sent (rc=%d)" % r.returncode, r.stderr or r.stdout or "no output")
            return False, TRANSFER_NOT_SENT
        h = _txhash(r.stdout)
        if not h:
            _log_detail("transfer sent, no txhash in the answer", r.stdout or r.stderr or "no output")
            return False, TRANSFER_NO_HASH
        # From here the reasons are this module's own words over on-chain facts (a code, a hash).
        included, why = _wait_tx(h)
        if not included:
            return False, why + " (" + h + ")"
        return True, h


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        # The health probe never queries the chain: in min_stake mode it says the amount last read and how long
        # ago (the next drip reads it again once it is older than MIN_STAKE_TTL_S), or null with the reason
        # until one has been read. Where the amount comes from is always said.
        out = {"status": "ok", "from": FROM, "pow_bits": POW_BITS,
               "amount_source": MIN_STAKE_SOURCE if AMOUNT == AMOUNT_MIN_STAKE else SETTING_SOURCE,
               # The kit floor, for the operator and the kit: its state, the floor and the deadline (UTC seconds),
               # and the counts since boot, the requests served without a number among them.
               "kit_floor": {"state": kit_floor_status()[0], KIT_MIN_FIELD: KIT_FLOOR, "deadline_ts": KIT_DEADLINE_TS,
                             "field": KIT_VERSION_FIELD, "refused_stale": _STALE_REFUSED[0],
                             "refused_malformed": _MALFORMED_REFUSED[0], "passed_undeclared": _UNDECLARED_PASSED[0]}}
        try:
            out["amount"] = "%d%s" % (drip_amount(read=False)[0], DENOM)
            if AMOUNT == AMOUNT_MIN_STAKE:
                out["amount_read_s_ago"] = min_stake_read_age()
        except AmountUnknown as e:
            out["amount"], out["amount_unknown"] = None, e.public
        self._send(200, out)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            # Body ceiling. `Content-Length` is supplied by the client: comparing it only against the
            # upper bound lets negative values through, and `read(-1)` reads until EOF — unbounded
            # allocation despite the guard. It must be bounded on both sides. (Reference form:
            # capacity_server.py.) The read happens before the rate limit, so without a ceiling a
            # single socket makes an unauthenticated endpoint swallow anything.
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid json"})
        # Well-formed is not well-shaped. `[]`, `null`, `1`, `"dendra1..."` all parse, so they cross
        # the try above untouched and only fail on the `.get` below — an AttributeError raised outside
        # any handler, which closes the socket with no response at all: the caller gets a disconnect
        # instead of the named 400 this endpoint promises, and the log gets a traceback. The shape is
        # checked here, where it is known, exactly as `_tx_code` does after its own `json.loads`.
        if not isinstance(body, dict):
            return self._send(400, {"error": "body must be a JSON object"})
        addr = str(body.get("address", "")).strip()
        if not _RE_ADDR.match(addr):
            return self._send(400, {"error": "invalid address"})
        # The kit floor, before the proof of work, the amount and the quotas: a stale kit is
        # told what to do and spends nothing, neither its own work nor the faucet's budget. Dormant or before the
        # deadline, the field is not read. From the deadline: a number below the floor is refused, a field that
        # holds no number is refused as malformed, and a request without the field is served, since the wallet
        # page, the docs page, the gateway and the validator script are not kits and send none.
        floor = kit_floor_now()
        if floor is not None:
            if kit_version_malformed(body):
                _MALFORMED_REFUSED[0] += 1
                print("[faucet] refused: %s is not a whole number (%.40r), %d malformed refusal(s) so far"
                      % (KIT_VERSION_FIELD, body.get(KIT_VERSION_FIELD), _MALFORMED_REFUSED[0]), flush=True)
                return self._send(400, refusal_body("invalid kit_version", REFUSAL_BAD_KIT_VERSION,
                                                    REFUSAL_BAD_KIT_VERSION, addr))
            declared = declared_kit_version(body)
            if declared is not None and declared < floor:
                _STALE_REFUSED[0] += 1
                print("[faucet] refused: %s declared %d (floor %d), %d refusal(s) under the floor so far"
                      % (KIT_VERSION_FIELD, declared, floor, _STALE_REFUSED[0]), flush=True)
                return self._send(403, refusal_body("stale kit", REFUSAL_STALE_KIT % floor, REFUSAL_STALE_KIT, addr,
                                                    **{KIT_MIN_FIELD: floor, "declared": declared}))
            if declared is None:
                _UNDECLARED_PASSED[0] += 1
        if not _pow_ok(addr, body.get("pow", "")):
            return self._send(400, {"error": "PoW required or invalid", "pow_bits": POW_BITS,
                                    "hint": "supply a 'pow' such that sha256(address+':'+pow) has >= %d leading zero bits" % POW_BITS})
        # The amount before the accounting: an amount that cannot be known refuses the drip without spending
        # the address's cooldown, the IP's quota or the day's budget on a transfer that will not happen.
        try:
            amount, _source = drip_amount()
        except AmountUnknown as e:
            # The requester gets the cause; the operator gets dendrad's words, in the log.
            _log_detail("drip refused (503, amount unknown)", str(e))
            return self._send(503, {"ok": False, "error": "drip amount unknown", "info": e.public,
                                    "address": addr})
        ip = self.client_address[0] if self.client_address else "?"
        try:
            rok, why = _rate_ok(addr, ip)
        except Exception as e:
            _log_detail("state save failed", str(e))
            return self._send(503, {"ok": False, "error": "state unavailable", "address": addr})
        if not rok:
            return self._send(429, refusal_body("rate limited", why, why, addr))
        # `fund()` is wrapped because `_run` lets TimeoutExpired (t=60, up to 21 calls via _wait_tx) and
        # FileNotFoundError (dendrad missing) through. Unhandled, the exception would reach
        # ThreadingHTTPServer, which closes the connection with no response, so the requester could not
        # tell it from a dead faucet, and the quota would already be spent.
        try:
            ok, info = fund(addr, amount)
        except Exception as e:
            # An exception's text names the command it ran (TimeoutExpired carries the whole argv: the
            # funding key, the keyring's home, the node). The log has it; the requester has the cause.
            _log_detail("transfer raised", "%s: %s" % (type(e).__name__, e))
            ok, info = False, TRANSFER_RAISED
        if not ok:
            _rate_refund(addr)
        self._send(200 if ok else 502, {"ok": ok, "info": info, "address": addr})

    def log_message(self, *a):
        return


def exposure_check(host, public_raw, pow_bits):
    """(fatal, warning) for the listen address and DENDRA_PUBLIC, each a log line or None.

    DENDRA_PUBLIC armed without PoW refuses to boot: a public faucet without PoW can be drained by many
    identities. A host beyond loopback without PoW only warns. docker-compose.yml listens on 0.0.0.0 so the
    gateway and the relay reach the faucet over the compose network, with DENDRA_FAUCET_POW_BITS 0 unless the
    .env sets it, and the testnet's faucet runs that way; refusing there would stop it at the next redeploy.
    Only the exact value "1" arms the refusal, as before this function: the host's .env is not read here, and a
    wider vocabulary could stop the running faucet at the next redeploy."""
    public = (public_raw or "") == "1"
    if pow_bits > 0:
        return None, None
    if public:
        # The words of the refusal from before this function: tests and launch_env_check.sh read them.
        return "[faucet] FATAL: DENDRA_PUBLIC=1 requires DENDRA_FAUCET_POW_BITS > 0 (anti-Sybil). Refusing to boot.", None
    if not listens_locally(host):
        return None, (f"[faucet] WARNING: listening on {host or 'every interface'}, beyond loopback, with "
                      f"DENDRA_FAUCET_POW_BITS=0: whoever reaches this port draws drips bounded only by the address "
                      f"cooldown, the per-IP quota and the daily cap. Set DENDRA_FAUCET_POW_BITS (20 for a public "
                      f"launch) in the .env; the kit's clients solve it.")
    return None, None


if __name__ == "__main__":
    _load_state()
    host = os.environ.get("DENDRA_FAUCET_HOST", "127.0.0.1")
    _fatal, _warning = exposure_check(host, os.environ.get("DENDRA_PUBLIC"), POW_BITS)
    if _fatal:
        print(_fatal, flush=True)
        raise SystemExit(2)
    if _warning:
        print(_warning, flush=True)
    _bad = amount_setting_error()
    if _bad:
        print("[faucet] FATAL: %s. Refusing to boot." % _bad, flush=True)
        raise SystemExit(2)
    # The kit floor is armed on purpose or not at all: a half-configured floor that stayed dormant would be read
    # as armed by whoever deployed it.
    if KIT_POLICY_ERROR:
        print("[faucet] FATAL: %s. Refusing to boot." % KIT_POLICY_ERROR, flush=True)
        raise SystemExit(2)
    print("[faucet] %s" % kit_floor_status()[1], flush=True)
    if AMOUNT == AMOUNT_MIN_STAKE:
        # Read once now so the health probe can say it; a failure is logged, and every drip reads it again.
        try:
            print("[faucet] drip amount: %dudndr (%s)" % (drip_amount()[0], MIN_STAKE_SOURCE), flush=True)
        except AmountUnknown as e:
            print("[faucet] drip amount not known yet (%s): every drip is refused (503) until it is read" % e,
                  flush=True)
    print("[faucet] listening on %s:%d  from=%s  amount=%s  node=%s  pow_bits=%d  state=%s"
          % (host, PORT, FROM, AMOUNT if AMOUNT == AMOUNT_MIN_STAKE else AMOUNT + DENOM, NODE, POW_BITS,
             STATE_FILE or "RAM"), flush=True)
    ThreadingHTTPServer((host, PORT), H).serve_forever()
