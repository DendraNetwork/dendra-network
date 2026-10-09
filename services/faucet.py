#!/usr/bin/env python3
"""Native Dendra faucet (replaces the ignite faucet).

POST {"address":"dendra1..."}  -> sends FAUCET_AMOUNT udndr from the FROM account (bob),
whose key lives in the shared keyring (volume /root/.dendra also mounted by the chain).
Sends are SERIALIZED (a lock) + we wait for tx inclusion -> no sequence collision.
GET /  -> health probe.

Env: DENDRA_NODE, DENDRA_CHAIN_ID (optional: read from the node and cross-checked), DENDRA_FAUCET_FROM=bob, DENDRA_FAUCET_AMOUNT=10000000,
     DENDRA_FAUCET_PORT=4500, DENDRA_HOME=/root/.dendra.

THE AMOUNT OF A DRIP. DENDRA_FAUCET_AMOUNT is a positive whole number of udndr, or the word `min_stake`: the
chain's `min_stake`, READ from the chain (`dendrad query jobs params`) and re-read every MIN_STAKE_TTL_S,
which is exactly what one identity locks to register. A miner then registers with its whole drip and sends
its transactions at zero gas (the chain's validators run at a zero minimum gas price and the kit sends no
fee). A fixed 10 DNDR at a min_stake of 1 DNDR funds ten identities per drip. A min_stake that cannot be read,
or that reads 0 (proto3 omits a zero: absent IS 0), refuses the drip (503) BEFORE any quota is spent: the
amount is never guessed, and a drip of 0 is not a drip. GET / says the amount and where it comes from.

A REFUSAL NAMES A CAUSE, NEVER DENDRAD'S OUTPUT. The 503 and the 502 carry one of the fixed CAUSE_* /
TRANSFER_* texts; what dendrad printed goes to this service's log (`_log_detail`), for the operator.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
# ADR-048 item 8: the chain id is READ (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
# Resolved LAZILY: miner imports this module for the PoW contract only, and must not query a node
# for a chain id it never uses.
def _chain_id():
    from modea import chain_id as _cid
    return _cid.chain_id(tuple(NODEF))
FROM = os.environ.get("DENDRA_FAUCET_FROM", "bob")
# udndr (10 DNDR), or "min_stake": see the module's header and `drip_amount`.
AMOUNT = os.environ.get("DENDRA_FAUCET_AMOUNT", "10000000").strip()
AMOUNT_MIN_STAKE = "min_stake"
MIN_STAKE_TTL_S = 300.0   # CHOSEN: a governed parameter changes by a vote, which takes far longer than this
PORT = int(os.environ.get("DENDRA_FAUCET_PORT", "4500"))
HOME = os.environ.get("DENDRA_HOME", "/root/.dendra")
DENOM = "udndr"
KB = ["--keyring-backend", "test", "--home", HOME]
NODEF = ["--node", NODE]
MAX_BODY = 8192          # a faucet request is an address plus a PoW; 8 KiB is already generous
_RE_ADDR = re.compile(r"^dendra1[0-9a-z]{38,70}$")
_LOCK = threading.Lock()

# --- ANTI-ABUSE (INCENTIVE testnet: without a cap, drain/Sybil is trivial). Cap per ADDRESS (cooldown) + per IP/day
#     + GLOBAL cap/day. Fail-closed (cap reached -> 429). PERSISTS state (survives restart = no re-drain).
#     OPTIONAL PoW tied to the address (anti-Sybil in public: each new address costs CPU). ---
ADDR_COOLDOWN = int(os.environ.get("DENDRA_FAUCET_ADDR_COOLDOWN", "86400"))  # 1 drip / address / 24 h
IP_DAILY = int(os.environ.get("DENDRA_FAUCET_IP_DAILY", "5"))                # max drips / IP / 24 h
DAILY_CAP = int(os.environ.get("DENDRA_FAUCET_DAILY_CAP", "2000"))           # GLOBAL cap / 24 h (anti-drain)
# Persistence: "" = RAM only (previous behavior). Default = under the DENDRA_HOME volume -> survives restart.
STATE_FILE = os.environ.get("DENDRA_FAUCET_STATE", os.path.join(HOME, "faucet-state.json"))
# PoW: 0 = off (closed testnet). >0 (e.g. 20) requires a 'pow' such that sha256(addr+':'+pow) has N leading zero bits.
POW_BITS = int(os.environ.get("DENDRA_FAUCET_POW_BITS", "0"))
_DAY = 86400.0
_RL_LOCK = threading.Lock()
_addr_last = {}    # addr -> ts of the last drip
_ip_hits = {}      # ip   -> [ts...] (24 h window)
_global_hits = []  # [ts...] global (24 h window)


# --- PoW CONTRACT: ONE DEFINITION, SHARED BY BOTH SIDES -------------------------------------------
# The expected token is a NONCE `pow` such that sha256("<address>:<nonce>") carries at least POW_BITS
# leading zero bits. The VERIFIER (server, `pow_ok`) and the SOLVER (client, `solve_pow`) both derive
# from the same digest `pow_digest`: two separate implementations that merely resemble each other end
# up diverging on a separator or an encoding, and that divergence makes the faucet unreachable while
# neither side reports a fault — the client believes it is paying the requested CPU, the server sees
# an invalid token.
POW_SEP = ":"


def pow_digest(addr, nonce):
    """Digest of the (address, nonce) pair.

    The nonce is bound TO THIS address: a token solved for another address is worthless, so every
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
    """PURE validity predicate for the token. `bits` <= 0 disarms the PoW (closed testnet)."""
    if bits <= 0:
        return True
    if not nonce:
        return False
    return leading_zero_bits(pow_digest(addr, nonce)) >= bits


def solve_pow(addr, bits, deadline_s=300.0, progress=None):
    """Solves the PoW on the CLIENT side. Returns the nonce, or "" when the time bound is reached.

    This is the client reference implementation: every faucet requester calls THIS, never a copy.
    `progress(attempts, seconds)` is invoked periodically so that a long wait does not look like a
    hang. It is time-bounded: a PoW that is too hard must produce a NAMED failure, not an infinite
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
    """Persists the anti-abuse state (ATOMIC write). Best-effort: an error does not interrupt the service."""
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
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _load_state():
    """Reloads the anti-abuse state at startup -> a restart does not reopen the drain."""
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    now = time.time()
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except Exception as e:
        # FAIL CLOSED (aligned with the gateway quota). A corrupted state file must not return
        # silently: empty counters would restart the faucet FROM ZERO, reopening a free re-drain.
        # Instead the global cap is marked REACHED (~24 h, fresh timestamps) with an explicit log.
        # Fix or remove STATE_FILE to reopen.
        with _RL_LOCK:
            _global_hits.extend(now for _ in range(DAILY_CAP))
        print(f"[faucet] STATE unreadable ({type(e).__name__}) -> FAIL CLOSED (global cap treated as reached). "
              f"Fix or remove {STATE_FILE}.", flush=True)
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


# THE THREE REFUSALS, AS THE WIRE CARRIES THEM (the `info` of a 429). Named here, once, because the miner
# daemon classifies a refusal by comparing with THESE constants (it imports this module): a copy of the text
# on the other side would drift the day one of them is reworded, and an IP-quota refusal would then read as
# "unknown". The texts themselves are unchanged, so the wire format is.
REFUSAL_GLOBAL_CAP = "global daily cap reached (anti-drain)"
REFUSAL_ADDR_COOLDOWN = "address funded recently (cooldown)"
REFUSAL_IP_QUOTA = "too many requests from this IP (daily quota)"


def _rate_ok(addr, ip):
    """Authorizes the drip AND records it. Returns (ok, reason). Fail-closed when a cap is reached.

    THE MOST SPECIFIC REFUSAL FIRST: the address's cooldown, then the IP's daily quota, then the global cap.
    The global cap used to be tested first, so once the day's budget was gone an address already funded, or
    an IP past its own quota, was told "global cap" -- and the miner daemon, which classifies a refusal by
    these constants (`miner.faucet_reason`), waited on the wrong cause.
    WHAT THE ORDER DOES NOT CHANGE, said here because it is why the order was looked at: nothing is recorded
    before every check has passed, so the order spends nothing. The day's budget is spent by
    ceil(DAILY_CAP / IP_DAILY) distinct IPs whatever the order; only the settings move that number, and a
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
        _addr_last[addr] = now
        hits.append(now)
        _ip_hits[ip] = hits
        _global_hits.append(now)
        return True, ""


def _rate_refund(addr):
    """Gives back ONLY the address cooldown, after a drip that was authorised but never paid.

    THE QUOTA WAS SPENT BEFORE THE TRANSFER AND NEVER RETURNED. `_rate_ok` records the drip and
    then `fund()` runs; when the chain-side transfer fails, the requester was locked out for
    ADDR_COOLDOWN having received nothing, with no recourse and no line saying so.
    WHAT IS GIVEN BACK IS DELIBERATELY NARROW. The address cooldown stands for a PAYOUT, and no
    payout happened. The IP quota and the global cap stand for WORK this server actually did, and
    that work happened either way — refunding them would hand unlimited attempts to anyone able to
    provoke a failure, and the anti-drain caps would become decorative.
    """
    with _RL_LOCK:
        _addr_last.pop(addr, None)


def _run(cmd, t=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=t)


# ── THE AMOUNT OF A DRIP ────────────────────────────────────────────────────────────────────────────────
# ── WHAT A REQUESTER IS TOLD, AND WHAT ONLY THE LOG IS TOLD ─────────────────────────────────────────────────
# A refusal used to hand the requester dendrad's own output: the stderr of `query jobs params` in a 503, the
# stderr of `tx bank send` or an exception's text in a 502. That output is written for the operator — the
# node it reached, the keyring's home, the name of the funding key, an account sequence — and this endpoint
# is public. A requester can do nothing with it but read the operator's setup.
# So a refusal carries one of the FIXED causes below, and the detail goes to the faucet's log (`_log_detail`),
# where the operator who can act on it reads it. The causes stay distinct where the requester's next move
# differs, and nowhere else.
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

    TWO TEXTS FOR TWO READERS. `str(e)` is the DETAIL, for the log — it may quote dendrad. `e.public` is the
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
    value of 0 -- which is also what an ABSENT field means, proto3 omitting a zero -- is not a drip."""
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
    v = p.get("min_stake", 0)        # the query answered: an absent field IS 0 (rule of zero)
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
    `read=False` never queries the chain (the health probe): it answers the amount LAST read, however old --
    its age is `min_stake_read_age` -- and an amount never read is unknown there. Expiring it there after
    MIN_STAKE_TTL_S made a quiet faucet (no drip for five minutes) answer "not read yet" about a value it had
    read, and a check of the probe fail on a healthy faucet. A drip (`read=True`) reads it again once expired."""
    if AMOUNT != AMOUNT_MIN_STAKE:
        err = amount_setting_error()
        if err:
            raise AmountUnknown(err, CAUSE_SETTING)
        return int(AMOUNT), SETTING_SOURCE
    now = time.monotonic() if now is None else now
    # The read happens UNDER the lock: requests arriving together on an expired value wait for ONE query
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

    proto3 OMITS a field worth its zero value, and success IS code zero: an answer without `code` is
    therefore a SUCCESS, and defaulting the absent field to a non-zero value turns every such answer
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
    """(ok, why). Polls until the tx is readable on-chain; a refusal is FINAL, waiting cannot lift it."""
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
        # dendrad's own words go to the LOG, never to the requester (see WHAT A REQUESTER IS TOLD).
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
               "amount_source": MIN_STAKE_SOURCE if AMOUNT == AMOUNT_MIN_STAKE else SETTING_SOURCE}
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
            # BODY CEILING. `Content-Length` is supplied by the CLIENT: comparing it only against the
            # UPPER bound lets NEGATIVE values through, and `read(-1)` reads until EOF — unbounded
            # allocation despite the guard. It must be bounded on BOTH sides. (Reference form:
            # capacity_server.py.) The read happens BEFORE the rate limit, so without a ceiling a
            # single socket makes an unauthenticated endpoint swallow anything.
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid json"})
        # WELL-FORMED IS NOT WELL-SHAPED. `[]`, `null`, `1`, `"dendra1..."` all PARSE, so they cross
        # the try above untouched and only fail on the `.get` below — an AttributeError raised outside
        # any handler, which closes the socket with NO response at all: the caller gets a disconnect
        # instead of the named 400 this endpoint promises, and the log gets a traceback. The shape is
        # checked HERE, where it is known, exactly as `_tx_code` does after its own `json.loads`.
        if not isinstance(body, dict):
            return self._send(400, {"error": "body must be a JSON object"})
        addr = str(body.get("address", "")).strip()
        if not _RE_ADDR.match(addr):
            return self._send(400, {"error": "invalid address"})
        if not _pow_ok(addr, body.get("pow", "")):
            return self._send(400, {"error": "PoW required or invalid", "pow_bits": POW_BITS,
                                    "hint": "supply a 'pow' such that sha256(address+':'+pow) has >= %d leading zero bits" % POW_BITS})
        # THE AMOUNT BEFORE THE ACCOUNTING: an amount that cannot be known refuses the drip without spending
        # the address's cooldown, the IP's quota or the day's budget on a transfer that will not happen.
        try:
            amount, _source = drip_amount()
        except AmountUnknown as e:
            # The requester gets the CAUSE; the operator gets dendrad's words, in the log.
            _log_detail("drip refused (503, amount unknown)", str(e))
            return self._send(503, {"ok": False, "error": "drip amount unknown", "info": e.public,
                                    "address": addr})
        ip = self.client_address[0] if self.client_address else "?"
        rok, why = _rate_ok(addr, ip)
        if not rok:
            return self._send(429, {"ok": False, "error": "rate limited", "info": why, "address": addr})
        # `fund()` CARRIED NO try, AND THE ONE ABOVE COVERS ONLY THE BODY READ. `_run` lets
        # TimeoutExpired through (t=60, up to 21 calls via _wait_tx) and so does FileNotFoundError
        # when dendrad is missing. The exception reached ThreadingHTTPServer, which closed the
        # connection with NO response at all — neither 200 nor 502. From the requester's side that
        # is indistinguishable from a dead faucet, and the quota had already been spent.
        try:
            ok, info = fund(addr, amount)
        except Exception as e:
            # An exception's text names the command it ran (TimeoutExpired carries the whole argv: the
            # funding key, the keyring's home, the node). The log has it; the requester has the cause.
            _log_detail("transfer raised", "%s: %s" % (type(e).__name__, e))
            ok, info = False, TRANSFER_RAISED
        if ok:
            _save_state()  # persist after a successful drip (atomic) -> survives restart
        else:
            _rate_refund(addr)
        self._send(200 if ok else 502, {"ok": ok, "info": info, "address": addr})

    def log_message(self, *a):
        return


if __name__ == "__main__":
    _load_state()
    # Host CONFIGURABLE (default 0.0.0.0 kept = reachable from the compose network,
    # nothing breaks) + FAIL-CLOSED under public exposure, exactly mirroring the gateway / relay:
    # DENDRA_PUBLIC=1 without anti-Sybil PoW (DENDRA_FAUCET_POW_BITS>0) = REFUSE to boot. A public faucet
    # without PoW = free Sybil drain; this knob is now ENFORCED by the code, not just documented.
    host = os.environ.get("DENDRA_FAUCET_HOST", "0.0.0.0")
    if os.environ.get("DENDRA_PUBLIC", "") == "1" and POW_BITS <= 0:
        print("[faucet] FATAL: DENDRA_PUBLIC=1 requires DENDRA_FAUCET_POW_BITS > 0 (anti-Sybil). Refusing to boot.",
              flush=True)
        raise SystemExit(2)
    _bad = amount_setting_error()
    if _bad:
        print("[faucet] FATAL: %s. Refusing to boot." % _bad, flush=True)
        raise SystemExit(2)
    if AMOUNT == AMOUNT_MIN_STAKE:
        # Read once now so the health probe can say it; a failure is said, and every drip reads it again.
        try:
            print("[faucet] drip amount: %dudndr (%s)" % (drip_amount()[0], MIN_STAKE_SOURCE), flush=True)
        except AmountUnknown as e:
            print("[faucet] drip amount NOT known yet (%s): every drip is refused (503) until it is read" % e,
                  flush=True)
    print("[faucet] listening on %s:%d  from=%s  amount=%s  node=%s  pow_bits=%d  state=%s"
          % (host, PORT, FROM, AMOUNT if AMOUNT == AMOUNT_MIN_STAKE else AMOUNT + DENOM, NODE, POW_BITS,
             STATE_FILE or "RAM"), flush=True)
    ThreadingHTTPServer((host, PORT), H).serve_forever()
