"""Dendra client engine: submit an inference request, track it, settle, and read network state.

Reused by the web dashboard and the CLI. Job flow (hash mode, local, deterministic Ollama):
  open-job (escrow of the fee) -> beacon -> assigned committee -> seals the prompt to each member and
  POSTs to the relay -> miners infer and return sealed responses + anchor their commit
  -> the client decrypts, pays the winners (payout from escrow) and slashes the divergent ones (finalize).
Content (prompt/response) travels only encrypted (relay); the chain only sees metadata + hash.
"""
from __future__ import annotations

import hashlib
import calendar
import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
from collections import Counter
from fractions import Fraction   # Exact ordering over rationals: mirrors the chain's big.Int cross-product
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Make `dendrad` findable whatever the launch context.
os.environ["PATH"] = (os.environ.get("PATH", "") + os.pathsep
                      + os.path.expanduser("~/go/bin") + os.pathsep + "/snap/bin"
                      + os.pathsep + "/usr/local/bin" + os.pathsep + "/usr/bin")

from modea.client import Client
from modea import dendrad_argv as da
from modea import chain_id as _cid
from modea.crypto import Sealed
import relay_client as relay

# The chain id is read (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
COMMITTEE_K = int(os.environ.get("DENDRA_COMMITTEE_K", "3"))
NODE = os.environ.get("DENDRA_NODE", "")  # e.g. "tcp://chain:26657" in a container; "" = local node
_RE_JID = re.compile(r"^[A-Za-z0-9_]+$")  # sane jid (no dendrad arg injection, no "__")


# Where the keyring lives. Without `--keyring-dir` and `--home`, `dendrad tx ... --keyring-backend test`
# looks for keys in its default home (/root/.dendra) while they live in /data/keys/cosmos: "key not
# found", `wait_tx` returns False, and the judge retries forever without saying why. The miner daemon
# signs nothing through this path, so a machine could mine, be paid and look healthy while unable to vote.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")  # e.g. /data/keys/cosmos
HOME_DIR = os.environ.get("DENDRA_HOME", "")            # e.g. /root/.dendra


def _node():
    return ["--node", NODE] if NODE else []


def _keys():
    """Explicit keyring/home location. Empty means the binary's default home."""
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


def read_json(cmd, t=60):
    """The JSON object a query prints, or None when it could not be read.

    Two outcomes that `out_json` folds into one: a parsed object -- `{}` included, since proto3 omits
    every field at its zero value and an empty object is therefore a reading of zeros -- and None when
    the command failed, timed out, printed nothing, or printed something that is not a JSON object.
    A reader that answers `{}` for both lets "the node did not answer" pass for "there is nothing"."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def tx_from(frm, sub, *positionals, flags=()):
    # Nonce retries: the client account is shared under concurrency, so "account sequence mismatch" is
    # retried (dendrad reads the sequence again on each attempt). This also covers the throughput kit,
    # which uses one account per concurrent sender.
    # Positionals go after `--` (modea/dendrad_argv.py): amounts are non-negative today, and the
    # terminator does not rely on that staying true.
    # `--output json` is always requested: `_tx_code` parses that format, and inheriting it from the
    # local client configuration would make the verdict on a transaction depend on a file nobody reads.
    cmd = da.dendrad_argv(
        ("dendrad", "tx", "jobs"), sub, positionals,
        [*flags, "--from", frm, "--keyring-backend", "test",
         "--chain-id", _cid.chain_id(tuple(_node())), "--yes", "--output", "json", *_keys(), *_node()])
    o = ""
    for attempt in range(6):
        o = run(cmd)
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    # The output is returned even on failure: the caller only gets a boolean from `wait_tx`, and without
    # this text a signing failure looks the same as a sequence failure or a silent node.
    return o


def query(sub, *positionals, flags=()):
    return run(da.dendrad_argv(("dendrad", "query", "jobs"), sub, positionals, [*flags, *_node()]))


_TXHASH = re.compile(r"[A-Fa-f0-9]{64}")
_DIGITS = re.compile(r"[0-9]+")


def _tx_doc(o):
    """The transaction response carried by `o`, parsed, or None when `o` carries none.

    `o` is what `run()` returns: stdout then stderr. The command asks for `--output json`, so the
    response is the JSON object at the head of that text, and whatever follows it (a warning printed
    on stderr) is not part of it and is not read. A text that does not start with that object -- a
    usage error, an unreachable node, a key that is not in the keyring -- carries no response. Nor
    does an object without a 64-hex `txhash`: every transaction response names its hash, so an object
    without one is something else, and reading its absent `code` as a zero would accept it."""
    t = (o or "").lstrip()
    if not t.startswith("{"):
        return None
    try:
        d, _end = json.JSONDecoder().raw_decode(t)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    h = d.get("txhash")
    if not (isinstance(h, str) and _TXHASH.fullmatch(h)):
        return None
    return d


def _uint_field(d, name):
    """A non-negative integer field of a parsed response: its value; 0 when the field is absent
    (proto3 omits a field at its zero value, so absence is a reading of zero); None when the field
    is present but is not a number (nothing was read)."""
    if name not in d:
        return 0
    v = d[name]
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and _DIGITS.fullmatch(v):
        return int(v)
    return None


def _tx_code(o):
    """The result code of the transaction response in `o`, in three states, never two:
      * the number its `code` field carries;
      * 0 when the response carries no `code` -- the zero of that field is success, and proto3 omits
        it, so an absent code is a reading of zero, not a failure;
      * None when `o` is no transaction response at all, or its `code` is not a number: nothing was
        read, and an unread code is neither an acceptance nor a refusal.
    The field is parsed, never searched for in the text: `code: 0` searched for in an output reads the
    normal form of a success, the one without the field, as a failure."""
    d = _tx_doc(o)
    return None if d is None else _uint_field(d, "code")


def _ok(t):
    """True only for a code read as zero. An unreadable response is a failure, never a success."""
    return _tx_code(t) == 0


def wait_tx(o, timeout=24):
    """`wait_tx_reason` without its reason. One implementation, so the two can never judge apart."""
    return wait_tx_reason(o, timeout)[0]


def _g(d, *keys, default=""):
    for k in keys:
        if k in d:
            return d[k]
    return default


# -- In-process chain reads --------------------------------------------------------------------------------
# Every `dendrad query` is a process: about 0.35 CPU-s and 120 MB resident per read. On the small host that
# also carries the validator, the services' reads were a large share of the load. Reads that decode no
# transaction go to the node's REST (port 1317) and CometBFT RPC (port 26657) instead, over a kept-alive
# connection. Transaction search and anything that decodes a transaction stay on the CLI.
# DENDRA_CHAIN_READ=cli forces the CLI path everywhere, so the owner can fall back without a rebuild. There is
# no automatic fallback: a read that fails over REST is a failed read, exactly as it is on the CLI.
CHAIN_READ_ENV = "DENDRA_CHAIN_READ"
REST_URL_ENV = "DENDRA_REST_URL"     # overrides the base derived from DENDRA_NODE, e.g. http://chain:1317
RPC_URL_ENV = "DENDRA_RPC_URL"       # overrides the base derived from DENDRA_NODE, e.g. http://chain:26657
_REST_PORT = 1317
_RPC_PORT = 26657
_MAX_BODY = 64 * 1024 * 1024         # a 500-row page is well under 1 MB; this only stops a runaway answer
_JOBS = "/dendra/jobs/v1/"
_LIST_PATHS = ("job", "miner", "commit", "beacon")
_BANK_BALANCES = "/cosmos/bank/v1beta1/balances/"

_TLS = threading.local()             # one kept-alive connection per (scheme, host) and per thread
_NOTE_AT = [None]


def check_chain_read_setting():
    """The read path DENDRA_CHAIN_READ names: `rest` (also when empty) or `cli`. Any other value raises.

    A typo is not guessed at: falling back to either path would hide that the owner's choice was not
    applied. It runs at import, so every service that reads the chain refuses to start on a bad value."""
    mode = os.environ.get(CHAIN_READ_ENV, "").strip().lower()
    if mode not in ("", "rest", "cli"):
        raise ValueError(f"{CHAIN_READ_ENV}={mode!r} is not empty, `rest` or `cli`")
    return "cli" if mode == "cli" else "rest"


def _use_rest():
    """True when chain reads go over REST/RPC: unset or `rest`. `cli` forces the CLI; anything else raises."""
    return check_chain_read_setting() == "rest"


check_chain_read_setting()


def _note(msg):
    """A failed REST/RPC read, at most once in 30 s: the callers poll every few seconds."""
    now = time.monotonic()
    if _NOTE_AT[0] is not None and now - _NOTE_AT[0] < 30:
        return
    _NOTE_AT[0] = now
    sys.stderr.write(f"[client] chain read failed: {msg}\n")


def _node_parts():
    """(scheme, host, port or None) of the node DENDRA_NODE names (`tcp://chain:26657`); the local node when
    it is empty; None when it cannot be parsed."""
    raw = NODE.strip()
    if not raw:
        return "http", "127.0.0.1", None
    try:
        u = urllib.parse.urlsplit(raw if "://" in raw else "tcp://" + raw)
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if not host:
        return None
    return ("https" if u.scheme == "https" else "http"), host, port


def _base(env_name, default_port, use_node_port):
    over = os.environ.get(env_name, "").strip()
    if over:
        return over.rstrip("/")
    parts = _node_parts()
    if parts is None:
        return None
    scheme, host, port = parts
    p = (port or default_port) if use_node_port else default_port
    return f"{scheme}://[{host}]:{p}" if ":" in host else f"{scheme}://{host}:{p}"


def rest_base():
    """The node's REST base URL (port 1317 on the host DENDRA_NODE names), or None when unknown."""
    return _base(REST_URL_ENV, _REST_PORT, False)


def rpc_base():
    """The node's CometBFT RPC base URL (the port DENDRA_NODE names, else 26657), or None when unknown."""
    return _base(RPC_URL_ENV, _RPC_PORT, True)


class _BodyTooLarge(ValueError):
    """An answer past the size cap: the server is working, so asking again would only repeat it."""


def _read_capped(resp, deadline):
    """The whole body, bounded in size and in time. A socket timeout bounds each read, not the answer."""
    chunks, total = [], 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("answer not complete before the deadline")
        b = resp.read(65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)
        total += len(b)
        if total > _MAX_BODY:
            raise _BodyTooLarge("answer larger than the cap")


def _http_json(base, target, timeout):
    """GET `base + target` -> the JSON object it answers with, or None when it could not be read: no base,
    refused or reset connection, timeout, a status other than 200 (the gateway answers a failed query with
    an error status), a body that is not JSON or not an object. An empty object is a reading.

    The connection is kept for the next call. One that the server closed while idle fails on its first use;
    that, and only that, is retried once on a fresh connection (neither a timeout nor an answer past the size
    cap is retried)."""
    if not base or not target.startswith("/"):
        _note(f"no node address for {target[:60]}")
        return None
    try:
        u = urllib.parse.urlsplit(base)
        scheme, netloc, prefix = u.scheme, u.netloc, u.path.rstrip("/")
    except ValueError:
        return None
    if scheme not in ("http", "https") or not netloc:
        _note(f"unusable node address {base[:60]}")
        return None
    pool = getattr(_TLS, "pool", None)
    if pool is None:
        pool = _TLS.pool = {}
    key = (scheme, netloc)
    deadline = time.monotonic() + 3 * timeout
    for attempt in (0, 1):
        conn = pool.get(key)
        reused = conn is not None
        if conn is None:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = pool[key] = cls(netloc, timeout=timeout)
        else:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
        try:
            conn.request("GET", prefix + target, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            body = _read_capped(resp, deadline)
            status = resp.status
        except Exception as e:  # noqa: BLE001 -- every failure is an unread answer, decided below
            pool.pop(key, None)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            if (reused and attempt == 0 and not isinstance(e, (socket.timeout, TimeoutError, _BodyTooLarge))
                    and time.monotonic() < deadline):
                continue
            _note(f"{netloc}{target[:80]}: {type(e).__name__}: {e}")
            return None
        break
    if status != 200:
        _note(f"{netloc}{target[:80]}: HTTP {status}")
        return None
    try:
        d = json.loads(body.decode("utf-8"))
    except ValueError:
        _note(f"{netloc}{target[:80]}: the answer is not JSON")
        return None
    if not isinstance(d, dict):
        _note(f"{netloc}{target[:80]}: the answer is JSON but not an object")
        return None
    return d


def rest_json(target, t=20):
    """The JSON object the node's REST answers for `target` (a path with its query string), or None."""
    return _http_json(rest_base(), target, t)


def rpc_json(target, t=20):
    """The JSON object the node's CometBFT RPC answers for `target`, or None."""
    return _http_json(rpc_base(), target, t)


def reset_chain_read_state():
    """Forgets the cached params, the kept connections of this thread and the failure-note throttle."""
    _PARAMS.update(p=None, at=None)
    pool = getattr(_TLS, "pool", None) or {}
    for c in list(pool.values()):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    pool.clear()
    _NOTE_AT[0] = None


def _rest_list_path(subcmd):
    """The REST collection of a `list-*` subcommand, or None when REST has no route for it."""
    if isinstance(subcmd, str) and subcmd.startswith("list-") and subcmd[5:] in _LIST_PATHS:
        return subcmd[5:]
    return None


def _jobs_doc(rest_path, sub, t=60):
    """The object a `jobs` query without arguments answers, or None when it could not be read. Over REST
    this is the route `rest_path`; on the CLI the subcommand `sub`, whose return code and output decide:
    the output omits zero fields, so a legitimate `{}` must not pass for a failure, nor a failure for `{}`."""
    if _use_rest():
        return rest_json(_JOBS + rest_path, t)
    try:
        r = subprocess.run(["dendrad", "query", "jobs", sub, "--output", "json", *_node()],
                           capture_output=True, text=True, timeout=t)
    except Exception:
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def list_all(subcmd, field, t=60, max_pages=400):
    """Full list of a paginated `query jobs <subcmd>`, page after page.

    Why this exists. `dendrad query jobs list-job` without pagination stops at 100 entries:
    ListJob calls `query.CollectionPaginate` (query_job.go) and the SDK applies `DefaultLimit = 100`
    (cosmos-sdk/types/query/pagination.go) when the request carries Limit=0. A single un-paginated
    call that calls itself "all jobs" therefore hands the exporter, the points ranking and The Proof a
    silent prefix of reality past 100 jobs. Truncation here can only ever produce good
    news (fewer disputes, fewer slashes, fewer deferred audits): a measurement whose failure mode is
    indistinguishable from a clean result.

    Returns `None` if a page cannot be read — callers must treat None as "not measured", never as [].

    What "cannot be read" means, and what it does not. A page is unreadable when the command fails or
    prints no JSON object (`read_json`), or when the list or its continuation has a shape that is not
    theirs. A page that was read and carries no list is a page of zero entries: proto3 omits an empty
    repeated field, so `{}` is the normal answer of a chain with nothing to list, and calling it
    unreadable would turn an empty chain into a chain nobody can measure.

    The pages come from the node's REST (`pagination.limit=500`, `pagination.key`) unless the CLI is forced
    (DENDRA_CHAIN_READ=cli) or REST has no route for `subcmd`. Both are judged by the same rules above: the
    gateway emits zero fields and a null `next_key` on the last page, and neither changes a verdict.
    """
    out, key, pages = [], None, 0
    rest_path = _rest_list_path(subcmd) if _use_rest() else None
    while pages < max_pages:
        if rest_path is not None:
            q = f"{_JOBS}{rest_path}?pagination.limit=500"
            if key:
                q += "&pagination.key=" + urllib.parse.quote(key, safe="")   # base64 holds + / =
            d = rest_json(q, t)
        else:
            cmd = ["dendrad", "query", "jobs", subcmd, "--page-limit", "500", "--output", "json", *_node()]
            if key:
                cmd += ["--page-key", key]
            d = read_json(cmd, t)
        if d is None:
            return None
        rows = d[field] if field in d else d.get(field.capitalize())
        if rows is None:                # absent (or null): the zero of a repeated field
            rows = []
        if not isinstance(rows, list):
            sys.stderr.write(f"[client] {subcmd}: `{field}` is a {type(rows).__name__}, "
                             f"not a list -> unreadable\n")
            return None
        out.extend(rows)
        pg = d.get("pagination")
        if pg is None:
            pg = {}
        if not isinstance(pg, dict):
            return None
        key = pg.get("next_key") or pg.get("nextKey")
        if key and not isinstance(key, str):
            return None
        pages += 1
        if not key:
            return out
    sys.stderr.write(f"[client] {subcmd}: >{max_pages} pages -> incomplete read, refusing\n")
    return None


# The last registry read, and when (monotonic). Written only by a read of the registry that succeeded: it
# is what a caller falls back on when the next read fails, so it must never hold a failure read as a
# value. A balance folded to 0 by a failed bank query would be exactly that, a failure kept as a reading
# for as long as the fallback lasts -- which is why an operator balance that could not be read is kept as
# None (not measured), never as 0.
_MINERS_LAST = {"rows": None, "at": None}


def registered_miners(balances=True):
    """List [{id, operator, stake, balance, demand, enc_pubkey}] of registered miners (all pages), or
    None when the list could not be read.

    `balances=False` skips the operator balances: one bank query per miner is the cost of this call, and a
    caller that needs only the registry (the anchored keys of a submission, the points ranking, The Proof)
    should not pay it. Every row then carries `balance` None -- not measured, never 0. The exporter, which
    publishes the balances, keeps the default. Only a read with balances refreshes the last-good rows
    (`last_registered_miners`): a balances-free read would replace them with rows whose balances are all None.

    None, never [], and the difference is the whole point. This returned [] with a line on stderr, and
    every consumer turned it into a measurement: an exporter publishing zero miners, a leaderboard over
    nobody, a client that finds no anchored key for any miner. An empty list is a reading — the chain
    holds no miner — and an outage handed out in that form is indistinguishable from it, on the
    reassuring side. Each caller decides what not knowing means for it, never zero: a count or a gauge
    keeps the last list read (`last_registered_miners`); a key to seal a prompt to is never taken from
    it, since a key can be rotated after the reading (`submit_job`).

    A row that is not an object, whose numbers are not numbers or whose id and key are not text, makes
    the whole list unreadable, as in `list_jobs_full`: dropping it would publish a smaller registry than
    the one listed.

    An operator balance that could not be read (`balance_read`) keeps its row, with `balance` None. The chain
    does not check that a miner's `operator` is an address (`msg_server_miner.go::CreateMiner` checks the keys
    only), so one registration naming an operator the bank cannot query must not make the registry unreadable
    for the exporter, the leaderboard and the CLI. No reader needs the balance to know who is registered: None
    says it was not measured, never 0, and a sum over the balances says how many it left out."""
    rows = list_all("list-miner", "miner")
    if rows is None:
        sys.stderr.write("[client] list-miner unreadable or incomplete -> None returned (not measured)\n")
        return None
    out, unread, read_balances = [], 0, {}
    for m in rows:
        if not isinstance(m, dict):
            sys.stderr.write(f"[client] list-miner: a row is a {type(m).__name__}, not an object "
                             f"-> None returned (not measured)\n")
            return None
        mid = _g(m, "minerId", "miner_id")
        op = _g(m, "operator")
        try:
            stake = int(_g(m, "stake", "Stake", default="0") or 0)
            demand = int(_g(m, "demand", "Demand", default="0") or 0)  # Real cumulative client demand (non-recoverable)
        except (TypeError, ValueError):
            sys.stderr.write("[client] list-miner: a stake or demand is not a number "
                             "-> None returned (not measured)\n")
            return None
        pub = _g(m, "encPubkey", "enc_pubkey")      # on-chain anchored pub; absent is "" (none anchored)
        if not (isinstance(mid, str) and isinstance(op, str) and isinstance(pub, str)):
            sys.stderr.write("[client] list-miner: an id, operator or key is not text "
                             "-> None returned (not measured)\n")
            return None
        if balances:
            if op not in read_balances:         # one operator can run several miners: one query for all
                read_balances[op] = balance_read(op)
            bal = read_balances[op]
            if bal is None:
                unread += 1
        else:
            bal = None
        out.append({"id": mid, "operator": op, "stake": stake, "balance": bal,
                    "demand": demand, "enc_pubkey": pub})
    if unread:
        sys.stderr.write(f"[client] list-miner: {unread} operator balance(s) of {len(out)} could not be "
                         f"read -> kept as None (not measured), the registry itself was read\n")
    if balances:        # a read without balances must not replace rows that hold them
        _MINERS_LAST.update(rows=[dict(m) for m in out], at=time.monotonic())
    return out


def last_registered_miners():
    """(list, age in whole seconds) of the last registry read in this process, or (None, None) when no
    read has succeeded yet. A copy: a caller that edits it does not edit the next caller's fallback."""
    if _MINERS_LAST["rows"] is None:
        return None, None
    return ([dict(m) for m in _MINERS_LAST["rows"]],
            max(0, int(time.monotonic() - _MINERS_LAST["at"])))


def registered_miners_or_last(balances=True):
    """(list or None, age in seconds or None, read now?) — the registry read now, else the last one read.

    `read now` False with a list: this read failed and the list is the last one read, `age` seconds old.
    `read now` False with None: no read has ever succeeded in this process; nothing is known."""
    rows = registered_miners(balances=balances)
    if rows is not None:
        return rows, 0, True
    last, age = last_registered_miners()
    return last, age, False


def balance(addr, denom="udndr"):
    if not addr:
        return 0
    if _use_rest():
        d = rest_json(_BANK_BALANCES + urllib.parse.quote(addr, safe=""), 60) or {}
    else:
        d = out_json(["dendrad", "query", "bank", "balances", addr, "--output", "json", *_node()])
    for c in d.get("balances", []):
        if c.get("denom") == denom:
            return int(c["amount"])
    return 0


def balance_read(addr, denom="udndr"):
    """The `denom` balance of `addr` as read, or None when it could not be read.

    `balance` answers 0 for a query that failed as well as for an empty account; this one keeps the two
    apart. 0 when the bank answered and holds none of `denom` — proto3 omits an empty `balances`, so its
    absence is a reading of zero — and for an empty address, which names no account. None when the query
    failed or answered a shape that is not a balance list."""
    if not addr:
        return 0
    if _use_rest():
        # The operator is on-chain text that nothing checks to be an address: quoted, it can only be a
        # path segment of this one route.
        # 60 s as on the CLI path: on a swapping host a slow answer must not become an unread balance.
        d = rest_json(_BANK_BALANCES + urllib.parse.quote(addr, safe=""), 60)
    else:
        d = read_json(["dendrad", "query", "bank", "balances", addr, "--output", "json", *_node()])
    if d is None:
        return None
    rows = d.get("balances")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        return None
    for c in rows:
        if not isinstance(c, dict):
            return None
        if c.get("denom") == denom:
            return _uint_field(c, "amount")        # absent -> 0, not a whole number -> None
    return 0


def anchored_enc_pubkey(mid):
    """The encryption key miner `mid` has anchored on chain, read now from its own record (`query jobs
    get-miner`): the key, "" when the record holds none (proto3 omits an empty string: absent is ""), or
    None when the record could not be read.

    None also covers a record that names another miner, or none: a key is only ever attributed to the
    miner its record says it belongs to. A record that does not exist is not "a miner with no key" either;
    it is a record that could not be read, and the caller refuses on it like on an outage."""
    if _use_rest():
        # 60 s as on the CLI path: a slow answer must not become an unread key, which makes submit_job refuse.
        d = rest_json(_JOBS + "miner/" + urllib.parse.quote(mid, safe=""), 60)
    else:
        d = read_json(["dendrad", "query", "jobs", "get-miner", mid, "--output", "json", *_node()])
    rec = d.get("miner") if isinstance(d, dict) else None
    if not isinstance(rec, dict):
        return None
    rid = _g(rec, "minerId", "miner_id")
    pub = _g(rec, "encPubkey", "enc_pubkey")
    if rid != mid or not isinstance(pub, str):
        return None
    return pub


def list_jobs_full():
    """All jobs [{id, state, miner_id, client, fee}] via `list-job` (query_job.go::ListJob), or None.
    Fields = proto Job (job.proto:10-14); tolerates snake_case and camelCase (gogoproto/JSON conventions).
    Consumed by exporter (strict-finality metric), points_indexer, the_proof.
    "all" is literal: the read is page by page (see list_all), since one call stops at the SDK's 100-entry default.

    None when the listing could not be read, never []. An empty list is a measurement -- the chain holds
    no job -- and every consumer turns it into zeros it publishes: no settled job, no audit, no slash.
    Handing it out for "the node did not answer" turns an outage into a clean network, on the reassuring
    side, in a form nothing downstream can tell apart. Each caller decides what not knowing means for
    it (keep its last snapshot, keep its gauges, refuse to answer); none may read it as zero."""
    rows = list_all("list-job", "job", t=180)
    if rows is None:
        sys.stderr.write("[client] list-job unreadable or incomplete -> None returned (not measured)\n")
        return None
    out = []
    for j in rows:
        if not isinstance(j, dict):
            # A row that is not an object is not a job one can count: dropping it would publish a
            # smaller network than the one listed. The whole listing is unreadable instead.
            sys.stderr.write(f"[client] list-job: a row is a {type(j).__name__}, not an object "
                             f"-> None returned (not measured)\n")
            return None
        slashes = [{"miner_id": _g(s, "minerId", "miner_id"), "amount": int(_g(s, "amount", default="0") or 0)}
                   for s in (_g(j, "slashRecords", "slash_records", default=[]) or []) if isinstance(s, dict)]
        out.append({"id": _g(j, "jobId", "job_id"), "state": _g(j, "state"),
                    "miner_id": _g(j, "minerId", "miner_id"), "client": _g(j, "client"),
                    "fee": int(_g(j, "fee", default="0") or 0),
                    # The plurality of operators behind the decision (0 outside a
                    # resolved audit). A monitoring gate reads the min of distinct_voters and the
                    # largest single-operator share (max_operator_votes/voters < 50%) over the audits
                    # closed by quorum.
                    "audit_distinct_voters": int(_g(j, "auditDistinctVoters", "audit_distinct_voters", default="0") or 0),
                    "audit_voters": int(_g(j, "auditVoters", "audit_voters", default="0") or 0),
                    "audit_max_operator_votes": int(_g(j, "auditMaxOperatorVotes", "audit_max_operator_votes", default="0") or 0),
                    "slashes": slashes})  # The Proof: SlashRecords from the proto Job (clawback-able)
    return out


def pools():
    rest = _use_rest()
    if rest:
        d = rest_json(_JOBS + "pools", 60) or {}
    else:
        d = out_json(["dendrad", "query", "jobs", "get-pools", "--output", "json"])
    p = d.get("Pools") or d.get("pools") or d
    keys = ["minerPaid", "miner_paid", "validators", "team", "treasury", "minerLocked", "miner_locked"]
    out = {}
    for k in keys:
        if k in p:
            v = int(p[k] or 0)
            # The gateway emits a pool at zero, the CLI omits it: the callers see what they always saw.
            if v or not rest:
                out[k.replace("_", "")] = v
    return out


def committee_seed_health():
    """VRF bootstrap: health of the decentralized committee randomness. Normalized dict (snake/camel)
    {source, min, contributors, power_bps, has_seed, height, seed_height}, or None when the query failed.

    A failed query is not a reading of zeros. `out_json` turns any failure into {}, and every field
    below then defaulted to 0: an unreachable node read as "source 0, floor 0, no contributor", a state
    the chain can really be in. Inside an answer that was read, an absent field is a zero (proto3 omits
    zeros); a command that failed, or printed something that is not JSON, gives None for the whole."""
    d = _jobs_doc("committee_seed_health", "committee-seed-health")
    if d is None:
        return None

    def pick(*ks):
        for k in ks:
            if k in d:
                return d[k]
        return None

    # Two heights, and dropping one made the other a lie for every consumer.
    # `CommitteeSeedHealth` (query_committee_seed_health.go:29) tries height H, then H-1, and keeps the
    # first that carries a seed. So `latest_contributors` is the count at `latest_seed_height` — which
    # is H-1 whenever the current block has not written its seed yet — while `current_height` is merely
    # when the query ran. This normaliser exposed only the second and named it `height`, so a consumer
    # had no way to know which block the contributor count belonged to. The site then labelled that
    # count "this block", asserting a scope the payload could not support. Pass both through: a reading
    # that cannot say which block it describes is not a measurement.
    seed_h = pick("latest_seed_height", "latestSeedHeight")
    return {
        "source": int(pick("committee_seed_source", "committeeSeedSource") or 0),
        "min": int(pick("committee_min_vrf_contributors", "committeeMinVrfContributors") or 0),
        "contributors": int(pick("latest_contributors", "latestContributors") or 0),
        # The second bar. `committeeBaseSeedSourced` refuses a seed under either the head count or this
        # share (MinVrfContributorPowerBps of the commit power); a consumer reading only the count would
        # announce a usable seed the chain refuses.
        "power_bps": int(pick("contributor_power_bps", "contributorPowerBps") or 0),
        "has_seed": bool(pick("has_recent_seed", "hasRecentSeed") or False),
        "height": int(pick("current_height", "currentHeight") or 0),
        # None, never 0: with no seed the chain omits the field, and a height of 0 would claim the
        # genesis block carried the reading. Absent stays absent.
        # The gateway writes a missing seed height as "0", the CLI leaves it out: both are "no seed".
        "seed_height": (int(seed_h) if seed_h not in (None, "", 0, "0") else None),
    }


def _block_epoch(sync):
    """Epoch seconds of the last applied block, or None when it cannot be read.

    None, never 0: a zero here would date the last block to 1970 and make every chain look stopped,
    which is the same class of mistake as reading an absent field as a value.
    """
    t = (sync or {}).get("latest_block_time") or (sync or {}).get("latestBlockTime")
    if not isinstance(t, str) or not t:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})", t)
    if not m:
        return None
    try:
        return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
    except Exception:
        return None


def head():
    """(height, epoch of the last block) from a single status read.

    Both come from one document on purpose. Asking twice would pair a height with the timestamp of a
    different block, and the age computed from that pair would be the age of neither. It also matters
    that the pair is what gets published: a height alone cannot tell a live chain from a stopped one --
    `catching_up=false` is answered identically by both -- and the age is what separates them.
    """
    if _use_rest():
        # 60 s as on the CLI path: a slow answer on a swapping host must not become an unread head.
        res = (rpc_json("/status", 60) or {}).get("result")  # the RPC wraps the document in `result`
        d = res if isinstance(res, dict) else {}
    else:
        d = out_json(["dendrad", "status", "--output", "json"]) or out_json(["dendrad", "status"])
    s = (d or {}).get("sync_info") or (d or {}).get("SyncInfo") or {}
    try:
        h = int(s.get("latest_block_height", 0))
    except Exception:
        h = 0
    return h, _block_epoch(s)


def height():
    return head()[0]


def network_state():
    """{miners, miners_read_now, miners_age_s, pools, height}.

    `miners` is the registry read now, else the last one read in this process (`miners_read_now`
    False, `miners_age_s` its age), else None when none was ever read. A consumer that only knows
    `miners` therefore keeps publishing the last measurement through an outage instead of a registry
    of zero miners; one that never got a reading gets None, which no count can be made of."""
    miners, age, now = registered_miners_or_last()
    if not now:
        sys.stderr.write("[client] network_state: the registry was not read -> "
                         + ("the last one read (%d s old) is kept\n" % age if miners is not None
                            else "none was ever read: miners=None (not measured)\n"))
    return {"miners": miners, "miners_read_now": now, "miners_age_s": age,
            "pools": pools(), "height": height()}


def get_beacon(jid):
    """The anchored committee seed of this job, or "" when it is absent or not yet revealed.

    A stale pattern is a silent outage. A pattern accepting only the historical `height:index` form
    (`[0-9]+:[0-9]+`) stops matching entirely once the seed becomes a 64-character hex VRF output, and
    the `else ""` makes that failure indistinguishable from a genuinely absent beacon. The measured
    consequence: the client drew its committee from the empty seed and sent the work to miner X while
    the chain, which requires `Seed != ""`, reserved payment for miner Y. The honest miner inferred,
    anchored its proof, and was never paid; the client's escrow stayed locked and the job stayed
    `open` forever. It is invisible with a single registered miner, since every draw then agrees: the
    defect arms itself the day the network stops being one. Both formats are therefore accepted, and
    the caller must treat "" as a stop, never as a default."""
    m = re.search(r'seed:\s*"?([0-9a-fA-F]{16,}|[0-9]+:[0-9]+)"?', query("get-beacon", jid))
    return m.group(1) if m else ""


def wait_beacon(jid, timeout=45):
    """Waits for the seed to be revealed (the EndBlocker reveals it with a delay).

    Drawing a committee without a seed is worse than an error: it is a draw silently different from
    the chain's. So wait, then fail loudly rather than fall back to ""."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = get_beacon(jid)
        if b:
            return b
        time.sleep(2)
    return ""


def assigned_committee(jid, k=None, timeout=45, poll=2.0):
    """The assigned committee, read from the chain (`query jobs assigned-committee`) - the
    query that kills the replica: the client no longer re-derives the draw, it asks for it.
    members[0] is the primary, the one settleOptimistic pays; the committee of size k is the prefix,
    a property of the total order that the chain guarantees and tests.

    The chain refuses while the seed is unrevealed, so this polls until `timeout` and then fails
    loudly with a RuntimeError. There is never a silent fallback to a local draw: that fallback -
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
                f"(last reason: {last}). Refusing to re-derive locally: a local draw that diverged "
                f"would send the work to a miner the chain will never pay.")
        time.sleep(poll)


def audit_deferred():
    """Audits selected but deferred, plus the opening counter.

    -> {"job_ids": [...], "audits_opened": int}, or None when not measurable (query absent on an old
    binary, or node unreachable). The discriminant is the return code and stdout, not an empty dict:
    the CLI output is snake_case and omits empty fields, so "zero deferred" arrives as {} - which
    would be indistinguishable from an error if it went through out_json.
    None is not []: "not measured" must never propagate as "zero", because a false zero propagates."""
    d = _jobs_doc("audit_deferred", "audit-deferred")
    if d is None:
        return None
    return {"job_ids": list(d.get("job_ids") or []),
            "audits_opened": int(d.get("audits_opened") or 0)}


def held_summary():
    """Fee-hold retentions: {"held_total", "held_count", "oldest_age_blocks", "oldest_job_id"}.

    "Retained is not lost" is the central promise of the sub-quorum path, and these two numbers are
    what can contradict it: a total that keeps growing means the escrow is stuck, and an age that only
    ever rises means the network is not healing.

    "oldest_age_blocks" understates itself for the first blocks after a restart from a fresh
    genesis, and the number cannot be fixed where it is read. Ages leave through genesis as elapsed
    blocks and are re-anchored on import as `since = height - elapsed`, clamped at 0. Measured on one
    held fee exported with an age of 60 and read 29 blocks later, the faithful answer being 89 every
    time: height 1 gives 30, height 30 gives 59, height 200 gives 89. Only the third is right.
    The clamp is not a bug to route around: a uint64 anchor cannot express "60 blocks old" at height 1,
    so the loss is in the state shape, and storing the age instead of an anchor is what would remove
    it (accepted and declared rather than migrated).
    => Read this figure as a lower bound while the chain height is still below the ages it carries.
    It is exact from the moment the height exceeds the oldest age it reports. None when not measurable (query absent, node
    unreachable) - same return-code/stdout discriminant as audit_deferred, because the CLI output
    omits zero fields, so a legitimate {} is indistinguishable from an error through out_json."""
    d = _jobs_doc("held_summary", "held-summary")
    if d is None:
        return None
    # The `pockets_measured` sentinel is always non-zero chain-side, so it is always emitted despite
    # the codec omitting zeros. It is what makes "0, measured" provable at field level: absent (0)
    # means a binary older than pockets 2 and 3, whose zeros are not measurements, hence None and
    # never a false "healthy zero"; >= n means the zeros of pocket n are measurements.
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
            # The second pocket: dispute bonds held in escrow for open disputes.
            "bond_escrowed_total": _pocket(2, "bond_escrowed_total"),
            "bond_escrowed_count": _pocket(2, "bond_escrowed_count"),
            # The third pocket: jurors retained by the exit guard, i.e. seats anchored to unresolved
            # audits. "Locked is not seized" is measured here; whether to allow exit-after-vote is a
            # decision to be made on these numbers from an open testnet, not on intuition.
            "jurors_locked_count": _pocket(3, "jurors_locked_count"),
            "oldest_lock_age_blocks": _pocket(3, "oldest_lock_age_blocks"),
            "oldest_lock_job_id": _pocket(3, "oldest_lock_job_id", is_str=True)}


PARAMS_TTL_S = 60
_PARAMS = {"p": None, "at": None}


def jobs_params():
    """The on-chain `jobs` params as a dict, or None when they could not be read.

    Read at most once in PARAMS_TTL_S seconds: `_verification_mode` asks before every submission and every
    settlement wait, and the params change at an epoch change, not per job. A failed read is never kept, so
    the next call asks the node again."""
    now = time.monotonic()
    if _PARAMS["p"] is not None and now - _PARAMS["at"] < PARAMS_TTL_S:
        return dict(_PARAMS["p"])
    if _use_rest():
        d = rest_json(_JOBS + "params", 60)       # 60 s as on the CLI path, like the balance read
    else:
        try:
            d = json.loads(query("params", flags=("--output", "json")))
        except Exception:
            d = None
    p = d.get("params", d) if isinstance(d, dict) else None
    if not isinstance(p, dict):
        return None
    _PARAMS.update(p=dict(p), at=now)
    return dict(p)


def prune_window_blocks():
    """The eviction window for inert miners (miner_prune_blocks), read from the on-chain params.
    0 or omitted falls back to the compiled constant (~7 days = 604800). It is the threshold of the
    retention-age warning: if funds stay retained longer than it takes to evict a dead miner, the
    network is not healing. None when the params are unreadable - no threshold is ever invented."""
    p = jobs_params()
    if p is None:
        return None
    try:
        v = int(p.get("miner_prune_blocks") or p.get("minerPruneBlocks") or 0)
        return v if v > 0 else 604800
    except Exception:
        return None


def committee(seed, miner_ids, k=COMMITTEE_K, stakes=None):
    """A reference replica for parity tests and fixture extraction - not the submission path.

    Since the `assigned-committee` query exists, `submit_job` asks the chain for the
    committee (`assigned_committee()` above) and the client holds no model of the draw. This function
    survives only as an executable reference for the arithmetic (h/stake, exact cross-product), used
    by parity tests and to extract fixtures.

    It is an exact replica of `selectCommittee`/`lessByStakeScore`
    (chain/x/jobs/keeper/committee.go). Any divergence from the chain means the client sends work
    to a miner the chain will not pay: free work and a locked escrow.

    The chain sorts by h/stake with an exact big.Int cross-product; Fraction is exact too. Floats are
    never used: one rounding is enough to swap two neighbours. Zero stake is relegated to the end and
    ties are broken by id, exactly as on-chain.

    `stakes` absent means the "equal stakes" assumption, i.e. ordering by pure hash. That is correct
    only if all stakes really are equal, so callers that commit work or money must supply them."""
    st = {m: int((stakes or {}).get(m, 0) or 0) for m in miner_ids}
    weighted = any(v > 0 for v in st.values())

    # Anti-divergence: never fall back to pure-hash ordering silently when the caller supplied stakes.
    # Supplying `stakes` means this draw commits work and money. If no stake is > 0, that is almost
    # never a network genuinely at zero stake - every miner holds a real bond >= min_stake - it is the
    # JSON `stake` field being read as 0 because its name or case changed. Pure-hash ordering then
    # diverges from the chain's hash/stake ordering, the served miner is never paid and the escrow is
    # locked. Refuse loudly rather than diverge without a word. (stakes=None remains the assumed
    # "equal stakes" hypothesis, for read-only display.)
    if stakes is not None and miner_ids and not weighted:
        raise ValueError(
            "committee(): stakes were supplied but all of them are 0 -> the 'stake' field is probably "
            "missing or renamed in the output of `query jobs list-miner`. A pure-hash draw would diverge "
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
    by the caller to decrypt the responses. `k=None` -> the sealed size is read from the chain's mode
    (k=1 under optimistic settlement)."""
    if k is None:
        k = _submit_committee_k()  # seal to k=1 under optimistic settlement; the client holds no k=3 model
    jid = jid or f"job{int(time.time()*1000)}"
    if not _RE_JID.match(jid) or "__" in jid:   # safe jid before any dendrad call
        return {"error": "invalid jid (alphanumeric/underscore, no '__')"}
    # The refusal is reported as the chain or the node wrote it (`raw_log`, else the code, else the output):
    # a guessed cause would send the operator after the wrong fault.
    opened, why = wait_tx_reason(tx_from(client, "open-job", jid, str(int(fee))))
    if not opened:
        return {"error": f"open-job (escrow) refused: {why}"}
    # The committee is read from the chain; there is no local re-derivation left.
    # The query refuses while the seed is unrevealed, so its polling replaces wait_beacon and the
    # failure is loud - never a local fallback draw, which diverges from the chain in silence.
    try:
        comm = assigned_committee(jid, k)
    except RuntimeError as e:
        return {"error": str(e)}
    # The authoritative encryption pub is the one anchored on-chain (signed by
    # the miner's Cosmos key), not the one announced to the relay (substitutable -> prompt MITM).
    # The keys are read now, never remembered.
    # An unread registry is not an empty one: read as [], every committee member had "no anchored key",
    # which strict mode refused under the wrong cause and the legacy opt-out answered by sealing to the
    # relay's key — an outage turned into the very downgrade the anchored key exists to prevent.
    # And a registry read earlier is not the registry now: an operator replaces its key with
    # `rotate-miner-keys` (msg_server_miner.go::RotateMinerKeys), after losing it or after it leaked,
    # and a prompt sealed to the key it replaced is one its miner cannot open and the old key's holder
    # can. So: the registry read now; when it cannot be read, each committee member's own record read now
    # (`anchored_enc_pubkey`); when one of those cannot be read either, nothing is sealed.
    _miners = registered_miners(balances=False)     # only the keys are used: no bank query per miner
    if _miners is not None:
        onchain_pub = {m["id"]: (m.get("enc_pubkey") or "") for m in _miners}
    else:
        onchain_pub = {}
        for mid in comm:
            pub = anchored_enc_pubkey(mid)
            if pub is None:
                return {"error": f"neither the miner registry nor the record of committee member {mid} "
                                 f"could be read: its on-chain encryption key is unknown -> the prompt was "
                                 f"not sealed (job {jid} is open on chain, its fee escrowed)"}
            onchain_pub[mid] = pub
        print(f"[client] job {jid}: the miner registry could not be read -> each committee member's key "
              f"was read from its own on-chain record", flush=True)
    beacon = get_beacon(jid)        # informational only, for display and diagnostics: the draw comes from the chain
    cli = Client()
    keys = {}
    undelivered = []   # committee members the sealed prompt never reached; empty is a measurement
    # on-chain pub required by default -> anti-MITM crypto is active without configuration.
    # Local/legacy opt-out: DENDRA_REQUIRE_ONCHAIN_PUB=0 (falls back to the relay pub with a warning).
    strict = os.environ.get("DENDRA_REQUIRE_ONCHAIN_PUB", "1") != "0"
    for mid in comm:
        cpub = onchain_pub.get(mid, "")
        if cpub:
            rp = relay.get(relay_url, "pub", mid, retries=10) or {}
            if rp.get("pub") and rp["pub"] != cpub:
                print(f"[client] WARNING: relay pubkey != on-chain pubkey for {mid} -> relay ignored (anti-MITM)")
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
        # The fee is already escrowed on chain when this runs, and this deposit is what hands the sealed
        # prompt to the miner. Its result is checked so that a refused deposit is never reported as a
        # job some miner is working on.
        #
        # "exists" counts as delivered. The relay's store is write-once, so a retry after a lost
        # response answers 409 while the sealed prompt is sitting there perfectly readable; reading
        # that as a failure would report an undelivered job that is on its way.
        st = "refused"
        for _ in range(3):     # bounded: a transient relay hiccup must not cost an escrowed job
            st = relay.put_status(relay_url, "req", f"{jid}__{mid}",
                                  {"client_eph_pk": sub.client_eph_pk.hex(),
                                   "nonce": sub.sealed_prompt.nonce.hex(),
                                   "ct": sub.sealed_prompt.ct.hex(),
                                   "max_out": max_out})   # requested output cap (client tier)
            if st in ("ok", "exists"):
                break
        if st not in ("ok", "exists"):
            undelivered.append(mid)
            print(f"[client] the relay refused the sealed request for {mid} on job {jid}: this miner "
                  f"has not been handed the prompt and cannot answer. The fee is already escrowed on "
                  f"chain.", flush=True)
    # The keys are returned even when a deposit failed, and that is deliberate: whatever did reach a
    # miner can still be decrypted, and dropping the keys here would make a partially delivered job
    # unreadable on top of being partial. `undelivered` is what the caller reads to know the
    # difference; it is empty on the nominal path, never absent.
    if undelivered:
        print(f"[client] job {jid}: {len(undelivered)}/{len(comm)} committee member(s) were never "
              f"handed the prompt -> {', '.join(undelivered)}", flush=True)
    return {"jid": jid, "committee": comm, "keys": keys, "prompt": prompt, "beacon": beacon,
            "undelivered": undelivered}


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
    """A readable reason for a refused or unread tx: the response's `raw_log` when it carries one,
    otherwise its code (and codespace); when `o` is no transaction response, the head of the output
    as it was read -- a guessed cause would send the operator after the wrong fault."""
    d = _tx_doc(o)
    if d is not None:
        log = d.get("raw_log")
        if isinstance(log, str) and log.strip():
            return log.strip()[:300]
        code = _uint_field(d, "code")
        if code is None:
            return f"unreadable `code` field ({str(d.get('code'))[:40]!r})"
        space = d.get("codespace")
        return f"code {code}" + (f" (codespace {space})" if isinstance(space, str) and space else "")
    return (o or "").strip()[:200] or "(empty output)"


def wait_tx_reason(o, timeout=24):
    """Like `wait_tx`, but propagates the reason instead of losing it -> (ok, reason).

    `wait_tx` returns a boolean, which makes a business refusal indistinguishable from a slow network.
    That is what made the "primary commit missing" refusal invisible across every job on the
    network.

    Both readings go through `_tx_code`: the broadcast response, then the included transaction,
    each requested as JSON. True only for a code read as zero at both; an output that carries no
    readable response is a failure with that output as its reason, never a success."""
    if _tx_code(o) != 0:
        return False, _tx_reason(o)
    h = _tx_doc(o)["txhash"]
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h, "--output", "json", *_node()])
        d = _tx_doc(q)
        # Not found yet answers an error and no JSON: that is "keep waiting", never a verdict. A
        # response for another hash is not this transaction's either.
        if d is not None and d["txhash"].upper() == h.upper() and (_uint_field(d, "height") or 0) > 0:
            return (True, "") if _tx_code(q) == 0 else (False, _tx_reason(q))
        time.sleep(2)
    return False, f"tx {h[:12]}... never included (timeout {timeout * 2}s)"


_SETTLE_WARNED = set()


def _warn_settle_failure(jid, why):
    """A loud assertion on draw parity.

    The committee is computed twice: here, off-chain, and by the chain at settlement. Two copies of a
    consensus arithmetic eventually diverge - and these two already had, once on an empty client-side
    seed and once on pure-hash versus hash/stake ordering, masked while all stakes were equal. When
    they diverge, the chain reserves payment for a miner that did no work: the honest miner is never
    paid, and the client's escrow comes back only when the job expires unserved, without a single
    message.

    Where the comparison cannot be made beforehand, the gap is made loud afterwards: the settlement
    refusal is the observable symptom of the divergence. Printed once per job."""
    if jid in _SETTLE_WARNED:
        return
    _SETTLE_WARNED.add(jid)
    print(f"[client] settlement refused  job={jid}: {why}", flush=True)
    low = why.lower()
    # Matched against the chain's refusal text, which is English (no non-test Go file of chain/x
    # carries a French word): the French spellings once listed here could match nothing.
    if "primary" in low or "committee" in low:
        print("[client]   ^^ draw divergence: the chain designated a different primary from the one "
              "that served the job. The miner that did the work will not be paid and the escrow stays "
              "locked. Known causes: a committee seed absent or stale on the client side, or a "
              "desynchronized stake weighting. Check `query jobs get-beacon <jid>` and draw parity.",
              flush=True)


def settle(jid, reward, client="alice", threshold_bps=7000):
    """Semantic settlement: pays the majority that gave the same meaning (from escrow) and slashes the
    truly aberrant answers. It tolerates LLM non-determinism, since two honest responses stay close in
    cosine. Idempotent."""
    out = tx_from(client, "settle-semantic", jid, str(int(reward)), str(int(threshold_bps)))
    ok, why = wait_tx_reason(out)
    if not ok:
        _warn_settle_failure(jid, why)
    return {"settled": ok, "reason": why}


def _commit_anchored(text: str) -> bool:
    return ("resultCommit" in text) or ("result_commit" in text)


def _verification_mode() -> int:
    """0 = redundant k=3 (full committee required to settle); 1 = optimistic k=1 (only the primary commit
    is required). Read on chain (`query jobs params`, kept 60 s by `jobs_params`); 0 when the chain is
    unreachable or the JSON unreadable."""
    p = jobs_params()
    if p is None:
        return 0
    try:
        return int(p.get("verification_mode") or p.get("verificationMode") or 0)
    except Exception:
        return 0


def _submit_committee_k() -> int:
    """The committee size to seal to, read from the chain's mode. Under optimistic settlement (k=1)
    the chain pays only the primary, so sealing to COMMITTEE_K=3 makes two miners infer for nothing:
    GPU work burned, their commits never settled, and a third commit exposed to the k=3 slash paths.
    Sealing to 1 also turns `reward = actual_fee // k` (quick_metered) into `actual_fee // 1`, so the
    primary is paid for real work - the chain applies the 85% fee / 15% protocol split - instead of
    a third by construction. The client does not hold the protocol model, it reads it. It falls back
    to COMMITTEE_K only when the mode cannot be determined."""
    return 1 if _verification_mode() == 1 else COMMITTEE_K


def settle_when_ready(jid, reward, committee_ids, client="alice", commit_wait=180):
    """Waits until the required on-chain commits are anchored before settling, then settles. Closes a race:
    the miner posts its response to the relay then anchors its commit on-chain, so a client that settles as soon as
    the relay responses arrive runs ahead of the commit -> SettleSemantic refuses ("incomplete committee" / "primary commit missing").

    Two properties this loop holds:
      - Mode-aware threshold: in optimistic mode (k=1) only the primary commit is required (need=1), because at
        k=1 the primary is the only one that commits -- a threshold set on the full committee is never reached
        there and the wait-loop expires on every job for nothing. In redundant mode (k=3): full committee.
      - Unified poll->settle loop: settlement fires as soon as `need` commits are anchored and re-tries while the
        deadline holds, rather than a fixed wait followed by a fixed number of attempts -> settles as early as possible and holds
        the whole window under load. Residual root cause of a late primary commit = GPU contention on the bench (primary commit > deadline); in prod
        miners are distributed (1 GPU each) -> no contention."""
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
    """Tokens (input/output) reported by the miners to the relay (committee median). 0 if absent."""
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


def quick_metered(prompt, base, per_token, out_allow, relay_url, client="alice", k=None, timeout=240,
                  on_answer=None):
    """Pricing by real work, in 2 phases:
      1) Escrow a max = base + price/token x (input_tokens + capped output);
      2) once the response is obtained, settle on the tokens actually produced.
    The miner is thus paid per token actually generated (a long prompt and a long response cost
    more). The escrow covers the max; any surplus stays in the module (refund = future work).
    `on_answer(jid, committee, answer)`, when given, is called with the decrypted answer before the
    settlement (the Final Testnet Season generator records it there, so its record never lands after the
    job's own day); whatever it raises is caught, and the settlement goes ahead.
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
    # Bill on what the client can verify: it sent the prompt and decrypted the response. The miner's
    # in_tok/out_tok travel outside the AEAD, so a malicious committee or relay could inflate them up
    # to the escrow cap. The miner's figures are used for telemetry only.
    rep_in, rep_out = job_tokens(jid, comm, relay_url)      # telemetry only (not billed)
    in_tok = max(1, len(prompt) // 4)                       # the client knows the prompt
    bill_out = max(1, len(answer) // 4) if answer else 1   # the client decrypted the response
    truncated = bill_out >= out_allow                      # client-verifiable proxy for the cap being reached
    out_tok = min(out_allow, bill_out)
    actual_fee = min(base + per_token * (in_tok + out_tok), fee_max)   # never > escrow
    reward = max(1, actual_fee // k)                        # pays the work (client measure) per miner
    reported = None
    if on_answer is not None:
        try:
            on_answer(jid, comm, answer)
            reported = True
        except Exception as e:  # noqa: BLE001
            reported = f"{type(e).__name__}: {e}"
    s = settle_when_ready(jid, reward, comm, client=client)
    return {"jid": jid, "committee": comm, "results": results, "answer": answer, "settle": s,
            "reported": reported,
            "beacon": sub["beacon"], "in_tok": in_tok, "out_tok": out_tok, "truncated": truncated,
            "rep_in_tok": rep_in, "rep_out_tok": rep_out,  # miner self-declared: telemetry, never the money
            "fee_escrow": fee_max, "fee_actual": reward * k}
