#!/usr/bin/env python3
"""Miner self-test: is THIS miner doing its job? Run INSIDE the miner container, where python3, dendrad,
the keyring and the service's environment already are -- the host needs Docker and nothing else.

    python3 /app/miner_selftest.py                # a report for a person
    python3 /app/miner_selftest.py --json         # one JSON document
    python3 /app/miner_selftest.py --host         # the line protocol deploy/testnet-miner/miner_health.sh reads
    python3 /app/miner_selftest.py --heartbeat    # the image's healthcheck: is the daemon's loop moving?
    python3 /app/miner_selftest.py --judge-role   # two lines for publish-capacity.sh: the judge role this
                                                  # identity declares (active | mute | off | unknown), and why

    --quick              skip the model probe (C7) and the relay check (C6)
    --write              let C6 re-deposit, signed, the key the chain anchors (without it C6 only reads)
    --no-write           skip C6 altogether
    --deadline-s S       the self-test's own bound: a check still running at S seconds is stopped and every
                         check after it is reported unmeasured (miner_health.sh passes it; default: none)
    --only C1,C3         run these checks only
    --reference-rpc URL  the network's public RPC (network-info's DENDRA_NODE), for C2
    --capacity-url URL   the capacity registry (the kit's DENDRA_CAPACITY_URL), for C8
    --node-id NAME       the name this machine publishes its capacity under (the kit's MINER_ID), for C8
    --id MID             the miner identity (default: what the daemon resolved, <keydir>/identite-resolue)
    --keydir DIR         the miner's key directory (default /data/keys)
    --height-wait S      how long C1 waits for a new block (default 30: a bound THIS check declares, the
                         chain fixes no block time)

THE CHECKS
    C1 node RPC          the node answers and its height climbs within --height-wait
    C2 node caught up    catching_up is false, and the lag behind the network's public RPC stays within
                         avail_deadline_blocks (or the whole window, avail_epoch_blocks, when the deadline
                         is 0): beyond it, the miner reads a challenge too late to answer it
    C3 registration      registered, by the operator this keyring signs as, with the VRF key this node
                         holds anchored on chain
    C4 presence          an accepted availability proof in the window that contains the current block or
                         the one before it -- the rule presence.go::minerPresentAt applies to every draw --
                         or a newcomer's grace
    C5 work queue        the relay's queue (GET /list) is readable
    C6 relay write       the relay's copy of this miner's encryption key does not differ from the chain's;
                         with --write, a SIGNED deposit of the anchored bytes is also accepted
    C7 model             one short generation and one embedding, through the same calls a job makes;
                         their latency is reported, never judged
    C8 capacity          this machine's line in the capacity registry is attributed on chain and fresh
    C9 processes         the daemon, and the workers this configuration starts, are running, and the
                         daemon's heartbeat is fresh
    C10 judge engine     when the judge role is on: the engine the verdicts run on answers and holds the model
                         this identity judges with -- the one its running judge worker resolved at its start
                         (one CPU judge serves every identity of a machine)
    C11 judge role       when the judge role is on: the role this identity DECLARES in its signed capacity
                         report -- active (its judge worker runs, its engine holds the model that worker judges
                         with, and that model is the chain's pin) or mute (the worker or the model is measured
                         absent: the chain draws it into juries and it says nothing)

    --compose CMD        the command that reaches this miner's compose project, written into the fixes
                         (default: slot 0's; miner_health.sh passes a slot k's)

ADVICE, APART FROM THE CHECKS: the keys kept in clear (A1), the recovery phrase still on this machine (A2),
the Final Testnet Season paying this machine's own key (A3). They are choices, not faults: each is listed
with the command that changes it, and none of them changes the exit code or raises an alert. A keyring
that does not OPEN with its passphrase is not advice: the miner cannot sign, and C3 is ko.

EVERY CHECK ANSWERS THE SAME SHAPE: {"id", "name", "state", "measured", "reason", "fix"}, and `state` is
one of THREE words. `ok` and `ko` are readings. `unmeasured` means the reading itself failed: a node that
did not answer a query, a document that is not what it should be. It is never folded into either of the
other two -- a reading that could not be made is not "fine", and it is not "broken" either.

THE RULE OF ZERO, PER DOCUMENT. The chain's answers are proto3: a field at its zero value is OMITTED, so
an absent `vrf_pubkey` IS the empty key (ko, not unmeasured), an absent `avail_epoch_blocks` IS 0
(presence disarmed), an absent transaction `code` IS 0 (accepted). CometBFT's /status and the capacity
registry are NOT proto3: they always write their fields, so an absent `catching_up` or
`registered_onchain` is a document this file does not recognise, and that is unmeasured -- never false.

WHAT IT WRITES: nothing, unless --write is given. Then at most one thing, the signed re-deposit of C6 --
the bytes of the encryption key the chain anchors for this miner, only when the relay's copy is absent or
equal to it, and only when the deposit can be SIGNED: an unsigned write is never sent. A relay copy that
DIFFERS from the chain is reported and never overwritten.

ONE AT A TIME, AND BOUNDED FROM INSIDE. A second self-test started while one runs in the same container
says so and runs nothing (a lock in the container's /tmp). --deadline-s bounds the run where it runs: a
bound applied to `docker exec` from the host only kills the client, and the run would go on in here.

Exit codes: 0 every check that ran is ok · 1 at least one ko · 2 no ko but at least one unmeasured, or
no check ran at all (zero checks is not a green).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import final_season_chain as fsc  # noqa: E402
import relay_client  # noqa: E402
from modea import crypto  # noqa: E402
from modea import heartbeat as hb  # noqa: E402
from modea import keyring  # noqa: E402
from modea import inference  # noqa: E402
from modea.relay_antireplay import REJEU  # noqa: E402

OK, KO, UNMEASURED = "ok", "ko", "unmeasured"
STATES = (OK, KO, UNMEASURED)
CREATE_ACTION = "/dendra.jobs.v1.MsgCreateMiner"
# THE LOAD THIS CHECK PUTS ON THE NODE IT READS, BOUNDED. The presence search covers two windows only,
# and reads at most this many pages of 100 transactions: a miner proves once per window, so two windows
# hold about two proofs per registered miner. A search that would need more stops and says so
# (unmeasured), rather than walking a public RPC for minutes once an hour.
PRESENCE_MAX_PAGES = 20
# The probe a job's own calls run (C7): the shortest generation the backend accepts, and one embedding.
PROBE_PROMPT = "Reply with one word: ready."
PROBE_TOKENS = 64
WORKERS = ("miner.py", "reveal_worker.py", "judge_worker.py")
# THE JUDGE ROLE THIS IDENTITY DECLARES (C11, --judge-role, and through it the signed capacity report).
# The chain knows no judge role: it draws every present miner into a jury (miner_vitality.go, audit_committee.go).
# Only a miner started with the role (DENDRA_MINER_JUDGE=1) runs judge_worker.py, and only a worker whose engine
# holds the model it judges with posts a verdict. Any other juror is drawn all the same and says nothing: a MUTE
# SEAT. Four words, closed, the same in this file, in deploy/testnet-miner/publish-capacity.sh and in the
# registry (capacity_server.py):
#   active   the role is requested, judge_worker.py runs, the judge engine holds the model THAT PROCESS judges
#            with (the one it resolved at its start and wrote in its judge state, modea/heartbeat.py), and that
#            model is the chain's pin (modelregistry audit_judge_model) or the operator's explicit choice
#   mute     the role is requested, and the worker or the model it judges with is MEASURED absent
#   off      the role is not requested -- the switch and the default of docker/entrypoint-services.sh, which
#            decided from this same environment whether the worker was started at all
#   unknown  the role is requested and what decides it could not be read -- the processes, the engine's answer,
#            the worker's own model, or whether that model is the pin: never folded into off nor into active
# A declaration about this container when it is read, not a proof: a judge is proven by a verdict it posted on chain.
JUDGE_ROLES = ("active", "mute", "off", "unknown")
JUDGE_ROLE_LINE = "DENDRA_JUDGE_ROLE "
JUDGE_ROLE_WHY_LINE = "DENDRA_JUDGE_ROLE_WHY "
# The bound --judge-role keeps where it runs, inside the container: one chain query (20 s: the pin, or a worker's
# model resolved now when its own is not readable) and one /api/tags read fit in it, and a reading stopped by it
# is unknown, never off.
JUDGE_ROLE_DEADLINE_S = 60


# ── the result shape ───────────────────────────────────────────────────────────────────────────────
def result(cid, name, state, measured="", reason="", fix=""):
    return {"id": cid, "name": name, "state": state, "measured": measured, "reason": reason, "fix": fix}


def aggregate(results) -> int:
    """0 every check ok · 1 at least one ko · 2 otherwise. A state that is none of the three words is
    NOT ok: it counts as unmeasured. And zero results is 2: a run that checked nothing proved nothing."""
    if not results:
        return 2
    states = [r.get("state") for r in results]
    if KO in states:
        return 1
    if any(s != OK for s in states):
        return 2
    return 0


def counts(results) -> dict:
    c = {OK: 0, KO: 0, UNMEASURED: 0}
    for r in results:
        s = r.get("state")
        c[s if s in (OK, KO) else UNMEASURED] += 1
    return c


# ── readers, one per document, each with ITS zero policy ───────────────────────────────────────────
def status_fields(res):
    """(height, catching_up) of a CometBFT /status `result`. Not proto3: CometBFT writes both fields on
    every answer, so an absent one -- or a `catching_up` that is not a boolean -- is a status this file
    does not recognise: None, never 0 and never False."""
    si = res.get("sync_info") if isinstance(res, dict) else None
    if not isinstance(si, dict):
        return None, None
    h = si.get("latest_block_height")
    height = None
    if isinstance(h, (str, int)) and not isinstance(h, bool):
        try:
            height = int(h)
        except ValueError:
            height = None
    cu = si.get("catching_up")
    return height, (cu if isinstance(cu, bool) else None)


def miner_fields(doc):
    """The record of `dendrad query jobs get-miner --output json`, or None when the answer is not one.
    proto3: an absent string IS the empty string, an absent number IS 0."""
    rec = doc.get("miner") if isinstance(doc, dict) else None
    if not isinstance(rec, dict):
        return None
    try:
        stake = int(rec.get("stake", 0) or 0)
    except (TypeError, ValueError):
        return None
    return {"operator": str(rec.get("operator", "") or ""), "vrf_pubkey": str(rec.get("vrf_pubkey", "") or ""),
            "enc_pubkey": str(rec.get("enc_pubkey", "") or ""), "stake": stake}


def tx_accepted(t) -> bool:
    """proto3 omits a zero `code`: a transaction WITHOUT the field is one the chain ACCEPTED. A code that
    is present and not an integer is not a reading: it raises, so the search is unmeasured."""
    try:
        return int(t.get("code", 0) or 0) == 0
    except (TypeError, ValueError) as e:
        raise fsc.ChainUnreadable(f"transaction with an unreadable code: {t.get('code')!r}") from e


def capacity_flags(row):
    """(registered_onchain, stale) of a capacity-registry row. Not proto3: the registry writes both flags
    on every row (capacity_server.py::_public_row), so a missing one or a non-boolean is None."""
    if not isinstance(row, dict):
        return None, None
    reg, stale = row.get("registered_onchain"), row.get("stale")
    return (reg if isinstance(reg, bool) else None), (stale if isinstance(stale, bool) else None)


# ── access to the node, the relay and the services ─────────────────────────────────────────────────
def dendrad(args, timeout=60):
    """(rc, stdout, stderr) of `dendrad <args>`; rc None when it could not run at all."""
    try:
        r = subprocess.run(["dendrad", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", f"{type(e).__name__}: {e}"


def http_get(url, headers=None, timeout=15.0):
    """(status, body) of a GET, an error status included; (None, why) when nothing answered. Redirects
    are refused (final_season_chain's opener) and the body is bounded."""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    deadline = time.monotonic() + 3 * timeout
    try:
        with fsc._OPENER.open(req, timeout=timeout) as r:
            return r.status, fsc._read_bounded(r, deadline)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096)
        except Exception:  # noqa: BLE001
            body = b""
        return e.code, body
    except Exception as e:  # noqa: BLE001 -- nothing answered
        return None, f"{type(e).__name__}: {e}"


def _alias_of(node):
    """The host of a tcp://host:port address when it is a container alias (no dot, no colon): the node
    kit's compose project, on this machine. "" for a public endpoint."""
    host = node.split("://", 1)[-1].rsplit(":", 1)[0] if node else ""
    return host if host and "." not in host and ":" not in host and host != "host.docker.internal" else ""


def _node_fix(node):
    alias = _alias_of(node)
    if "://host.docker.internal:" in node or node.startswith("host.docker.internal:"):
        return ("this kit reaches its node through host.docker.internal, as an older join.sh wrote it: re-run "
                "deploy/join.sh, which moves it to the node's alias on the dendra-chain network (it reads the "
                "CONFIG_URL the kit's .env names, or rewrites that line in place when there is none)")
    if alias:
        return (f"this miner reads its own node ({alias}): docker compose -p {alias} ps; "
                f"docker compose -p {alias} logs --tail 50 node")
    return (f"this miner reads {node or 'no node'}, which it does not run: ask on the network's channel, or run "
            f"your own node (re-run deploy/join.sh without --remote-rpc)")


class Ctx:
    """What the checks share: the environment, the options, and the readings one check makes for the
    next (C1's status, the chain's params, the miner record), each read once."""

    def __init__(self, opts, env):
        self.opts, self.env = opts, env
        self.node = env.get("DENDRA_NODE", "")
        self.rpc = fsc.rpc_url(self.node) if self.node else ""
        self.keydir = opts.keydir
        self.mid, self.mid_source = self._identity()
        self.status, self.height = None, None
        self._params = None
        self._miner = None
        self._workers = None
        self._engine = None
        self.heartbeat = hb.read()

    def workers(self):
        """The scripts running in this container (_running_workers), read once: C9 and C11 judge the same reading.
        A tuple in the cache, so that "nothing could be read" (None) is kept as read rather than read again."""
        if self._workers is None:
            self._workers = (_running_workers(self.opts.proc_root),)
        return self._workers[0]

    def judge_engine(self):
        """(C10 result, detail) of the judge engine, read once: C10 and C11 judge the same reading."""
        if self._engine is None:
            self._engine = judge_engine(self.env, self.opts.proc_root)
        return self._engine

    def _identity(self):
        if self.opts.id:
            return self.opts.id, "--id"
        try:
            with open(os.path.join(self.keydir, "identite-resolue"), encoding="utf-8") as f:
                v = "".join(f.read().split())
            if v.startswith("dm1"):
                return v, "identity resolved by the daemon (identite-resolue)"
        except OSError:
            pass
        for k in ("DENDRA_SIGN_KEY", "MINER_ID"):
            if self.env.get(k, "").strip():
                return self.env[k].strip(), f"{k} (the daemon has not resolved an identity yet)"
        return "", ""

    def node_flags(self):
        return ["--node", self.node] if self.node else []

    def params(self):
        """(params, None) or (None, why): the jobs module's params, read once."""
        if self._params is None:
            try:
                d = fsc._cli(["query", "jobs", "params"], self.node)
                p = d.get("params") if isinstance(d, dict) else None
                self._params = (p, None) if isinstance(p, dict) else (None, "the params answer carries no params")
            except fsc.ChainUnreadable as e:
                self._params = (None, str(e)[:240])
        return self._params

    def miner_record(self):
        """("ok", fields) | ("notfound", why) | ("unread", why) -- read once. NotFound is the chain's own
        answer that this identity is not in the registry; every other failure is a reading that failed."""
        if self._miner is None:
            if not self.mid:
                self._miner = ("unread", "no miner identity: the daemon has not resolved one, and none was given")
                return self._miner
            rc, out, err = dendrad(["query", "jobs", "get-miner", self.mid, "--output", "json", *self.node_flags()])
            if rc is None:
                self._miner = ("unread", err[:240])
            elif rc != 0:
                if "code = NotFound" in (err + out):
                    self._miner = ("notfound", f"the chain answers NotFound for {self.mid}")
                else:
                    self._miner = ("unread", (err or out).strip()[:240] or f"dendrad exited {rc}")
            else:
                try:
                    rec = miner_fields(json.loads(out))
                except ValueError:
                    rec = None
                self._miner = ("ok", rec) if rec is not None else ("unread", "get-miner answered something that is not a miner record")
        return self._miner


# ── the checks ─────────────────────────────────────────────────────────────────────────────────────
def c1_rpc(ctx):
    cid, name = "C1", "node RPC"
    if not ctx.node:
        return result(cid, name, KO, "DENDRA_NODE is empty", "the miner has no chain to read or to sign through",
                      "re-run deploy/join.sh: it writes DENDRA_NODE into the miner kit's .env")
    wait = float(ctx.opts.height_wait)
    try:
        st = fsc.status(ctx.rpc)
    except fsc.ChainUnreadable as e:
        return result(cid, name, KO, f"{ctx.rpc} did not answer /status", str(e)[:240], _node_fix(ctx.node))
    h0, _ = status_fields(st)
    if h0 is None:
        return result(cid, name, UNMEASURED, "/status carries no readable latest_block_height",
                      "CometBFT writes it on every answer: this is not a status this check can read", _node_fix(ctx.node))
    ctx.status, ctx.height = st, h0
    t0 = time.monotonic()
    deadline = t0 + wait
    while time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        try:
            st = fsc.status(ctx.rpc)
        except fsc.ChainUnreadable as e:
            return result(cid, name, KO, f"{ctx.rpc} stopped answering after height {h0}", str(e)[:240],
                          _node_fix(ctx.node))
        h1, _ = status_fields(st)
        if h1 is None:
            return result(cid, name, UNMEASURED, "/status carries no readable latest_block_height",
                          "CometBFT writes it on every answer: this is not a status this check can read",
                          _node_fix(ctx.node))
        ctx.status, ctx.height = st, h1
        if h1 > h0:
            return result(cid, name, OK, f"height {h0} -> {h1} in {time.monotonic() - t0:.0f} s ({ctx.node})")
    return result(cid, name, KO, f"height {h0}: no new block in {wait:g} s ({ctx.node})",
                  f"the node answers but its height does not move. {wait:g} s is a bound this check declares "
                  f"(--height-wait); the chain fixes no block time, so a slower chain needs a longer wait",
                  _node_fix(ctx.node))


def _bound(ctx):
    """(blocks, note): how far behind the network the node may be before an availability challenge is read
    too late. avail_deadline_blocks when it is set; else the whole window (avail_epoch_blocks), since a
    deadline of 0 lets the whole window count (msg_server_prove_availability.go::ProveAvailability). 0 =
    the chain measures no presence, so no bound is published. None = the params were not read."""
    p, why = ctx.params()
    if p is None:
        return None, f"the jobs params were not read ({why})"
    try:
        eb, dl = fsc.param_int(p, "avail_epoch_blocks"), fsc.param_int(p, "avail_deadline_blocks")
    except fsc.ChainUnreadable as e:
        return None, str(e)
    if dl > 0:
        return dl, f"avail_deadline_blocks = {dl}"
    if eb > 0:
        return eb, f"avail_deadline_blocks is 0, so the whole window counts: avail_epoch_blocks = {eb}"
    return 0, "avail_epoch_blocks is 0: the chain measures no presence and publishes no deadline"


def c2_sync(ctx):
    cid, name = "C2", "node caught up"
    if ctx.status is None and ctx.rpc:
        try:
            st = fsc.status(ctx.rpc)
            h, _ = status_fields(st)
            if h is not None:
                ctx.status, ctx.height = st, h
        except fsc.ChainUnreadable:
            pass
    if ctx.status is None:
        return result(cid, name, UNMEASURED, "the node's status was not read (see C1)")
    _, cu = status_fields(ctx.status)
    if cu is None:
        return result(cid, name, UNMEASURED, "catching_up is absent from /status, or not a boolean",
                      "CometBFT always writes it: an unread flag is never 'caught up'", _node_fix(ctx.node))
    flag = "catching up" if cu else "caught up (its own flag)"
    lag, lag_note = None, ""
    ref = (ctx.opts.reference_rpc or "").strip()
    if not ref:
        lag_note = "no reference RPC given (network-info's DENDRA_NODE, which miner_health.sh passes)"
    else:
        ref_url = fsc.rpc_url(ref)
        if ref_url.rstrip("/") == ctx.rpc.rstrip("/"):
            lag, lag_note = 0, "this miner reads the network's own RPC"
        else:
            try:
                lag = max(0, fsc.height(ref_url) - int(ctx.height))
            except fsc.ChainUnreadable as e:
                lag_note = f"the network's RPC ({ref}) did not answer: {str(e)[:160]}"
    bound, bnote = _bound(ctx)
    if lag is None:
        return result(cid, name, UNMEASURED, f"the node says it is {flag}; its lag behind the network was not measured",
                      lag_note, "run deploy/testnet-miner/miner_health.sh, which passes the network's RPC from network-info")
    measured = f"{flag}, {lag} block(s) behind the network"
    if bound is None:
        if lag == 0 and not cu:
            return result(cid, name, OK, measured)
        return result(cid, name, UNMEASURED, measured, f"no bound to judge the lag against: {bnote}")
    if bound == 0:
        if cu:
            return result(cid, name, KO, measured, f"{bnote}; the node itself says it is behind",
                          "wait for it to catch up, then run this again: " + _node_fix(ctx.node))
        return result(cid, name, OK, measured, f"{bnote}: the lag is reported, not judged")
    if lag <= bound:
        return result(cid, name, OK, measured + f" (within {bnote})",
                      "a node that is restarting catches up block by block; this lag still lets it answer a challenge in time" if cu else "")
    return result(cid, name, KO, measured, f"beyond {bnote}: a challenge read this late cannot be answered in its window",
                  "wait and run again; if the lag keeps growing: " + _node_fix(ctx.node))


def _keyring(ctx):
    """(Keyring, None) or (None, KeyringError): the keyring as the daemon resolves it (modea/keyring.py),
    read once."""
    if not hasattr(ctx, "_kr"):
        try:
            ctx._kr = (keyring.resolve(ctx.env.get("DENDRA_KEYRING_DIR") or None, env=ctx.env), None)
        except keyring.KeyringError as e:
            ctx._kr = (None, e)
    return ctx._kr


def _keyring_address(ctx):
    """(address, None, None) | (None, state, why). state is keyring.UNREADABLE or ABSENT -- the miner
    cannot sign, a reading -- or keyring.NOT_RUN -- nothing was read."""
    kr, err = _keyring(ctx)
    if kr is None:
        return None, keyring.UNREADABLE, f"{err} -- {err.hint}"
    state, val = keyring.key_state(kr, ctx.mid)
    if state == keyring.PRESENT:
        return val, None, None
    return None, state, val


def c3_registration(ctx):
    cid, name = "C3", "registration and keys"
    # THE KEYRING FIRST. A keyring that does not open with its passphrase is a READING, not a failure to
    # read: the miner cannot sign a commit, a proof or a deposit, so it is ko -- whatever the registration
    # says, and before it, since a daemon that cannot open its keys stops before it registers and would
    # otherwise be reported as merely "not registered". Only a dendrad that did not run leaves it unmeasured.
    addr, kstate, why = (_keyring_address(ctx) if ctx.mid else (None, keyring.NOT_RUN, "no miner identity"))
    if addr is None and kstate != keyring.NOT_RUN:
        return result(cid, name, KO, f"this miner CANNOT SIGN as {ctx.mid}: its key does not open", why,
                      "read the daemon's first lines (docker compose -p dendra-miner logs miner | grep -A4 'KEYS NOT "
                      "OPENED'); a lost passphrase file is recovered from its backup, or the key rebuilt from its "
                      "24-word recovery phrase")
    state, rec = ctx.miner_record()
    if state == "notfound":
        owner = ctx.env.get("DENDRA_MINER_OWNER", "").strip()
        return result(cid, name, KO, f"{ctx.mid} is NOT registered on chain", rec + ": a miner outside the registry is assigned no job",
                      (f"owner mode: the OWNER ({owner}) registers it, with the three commands the daemon prints: "
                       "docker compose -p dendra-miner logs miner | grep -A16 'OWNER MODE'") if owner else
                      ("the daemon registers itself at start; read why it did not: "
                       "docker compose -p dendra-miner logs miner | grep -iE 'regist|faucet'"))
    if state != "ok":
        return result(cid, name, UNMEASURED, f"the registration of {ctx.mid or '?'} was not read", rec, _node_fix(ctx.node))
    if addr is None:
        return result(cid, name, UNMEASURED, f"{ctx.mid} is registered; this keyring's address was not read", why)
    if rec["operator"] != addr:
        return result(cid, name, KO, f"{ctx.mid} is registered for {rec['operator'] or '(no operator)'}, this keyring signs as {addr}",
                      "the chain refuses every commit and every availability proof not signed by the miner's operator",
                      f"restore the key of {rec['operator']} into the keyring{SLOT0_NEW_ID_FIX}")
    vrf_chain = rec["vrf_pubkey"].strip().lower()
    if not vrf_chain:
        return result(cid, name, KO, f"{ctx.mid} has NO VRF key anchored on chain",
                      "under verification_mode=1 the chain refuses availability proofs from a miner without one, so it is never present",
                      "the daemon prints the anchoring command at start: docker compose -p dendra-miner logs miner | grep -A3 'VRF'")
    vpath = os.path.join(ctx.keydir, f"{ctx.mid}.vrf")
    kr, _ = _keyring(ctx)
    try:
        # The secret may be sealed under the keyring's passphrase (crypto.load_secret reads both forms).
        # Read only: this check never re-encrypts anything.
        sk = crypto.load_secret(vpath, kr.open_with if kr else "", crypto.AAD_VRF).decode("ascii").strip()
    except crypto.KeyEnvelopeError as e:
        return result(cid, name, KO, f"the VRF secret of {ctx.mid} is encrypted and does not open ({vpath})",
                      f"{e}: without it every availability proof fails",
                      "the passphrase file mounted at /run/dendra-secrets must be the one the key files were sealed with")
    except (OSError, UnicodeDecodeError):
        return result(cid, name, KO, f"the chain anchors a VRF key for {ctx.mid}; this node holds no VRF secret ({vpath})",
                      "every availability proof needs that secret: without it this miner can never be present",
                      "restore the miner-keys volume from its backup; a new key would have to be anchored again (rotate-miner-keys)")
    # THE SECRET GOES THROUGH THE ENVIRONMENT, NEVER argv: /proc/<pid>/cmdline is readable by every process.
    try:
        pk = subprocess.run(["dendra-vrf", "pubkey"], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "DENDRA_VRF_SK": sk}).stdout.strip().lower()
    except (OSError, subprocess.TimeoutExpired) as e:
        return result(cid, name, UNMEASURED, "dendra-vrf could not be run", f"{type(e).__name__}: {e}")
    if not re.fullmatch(r"[0-9a-f]{64}", pk or ""):
        return result(cid, name, UNMEASURED, "dendra-vrf gave no public key for this node's secret")
    if pk != vrf_chain:
        return result(cid, name, KO, f"VRF key MISMATCH for {ctx.mid}: the chain anchors {vrf_chain[:12]}..., this node holds {pk[:12]}...",
                      "every proof is made with the local key and verified against the anchored one: all are refused",
                      f"dendrad tx jobs rotate-miner-keys {ctx.mid} --new-vrf-pubkey {pk}")
    return result(cid, name, OK, f"{ctx.mid} registered by {addr}, stake {rec['stake']} udndr, VRF key anchored and held here")


def registration_height(node, mid, first, last, max_pages=PRESENCE_MAX_PAGES):
    """The height of the accepted MsgCreateMiner of `mid` in [first, last], or None. Filtered on the
    miner_id IN THE MESSAGE, as presence_proofs does, never on an event attribute."""
    q = f"message.action='{CREATE_ACTION}' AND tx.height>={int(first)} AND tx.height<={int(last)}"
    found, page, served = None, 1, 0
    while page <= max_pages:
        d = fsc._cli(["query", "txs", "--query", q, "--page", str(page), "--limit", "100"], node)
        txs = d.get("txs") or []
        served += len(txs)
        for t in txs:
            if not tx_accepted(t):
                continue
            h = int(t.get("height", 0) or 0)
            if not first <= h <= last:
                continue
            for m in ((t.get("tx") or {}).get("body") or {}).get("messages") or []:
                if m.get("@type") == CREATE_ACTION and (m.get("miner_id") or m.get("minerId")) == mid:
                    found = h if found is None else max(found, h)
        if fsc._pages_done(served, fsc._total_count(d), txs, q):
            return found
        page += 1
    raise fsc.ChainUnreadable(f"registration search did not end after {max_pages} pages")


def _last_attempt(ctx):
    doc, _ = ctx.heartbeat
    p = doc.get("presence") if isinstance(doc, dict) else None
    if not isinstance(p, dict):
        return ""
    when = p.get("at")
    ago = f"{int(time.time() - when)} s ago" if isinstance(when, int) and not isinstance(when, bool) else "at an unread time"
    why = f": {p.get('why')}" if p.get("why") else ""
    return f" The daemon's last attempt ({ago}): {p.get('result', '?')}{why}."


def c4_presence(ctx):
    cid, name = "C4", "availability (presence)"
    p, why = ctx.params()
    if p is None:
        return result(cid, name, UNMEASURED, "the jobs params were not read", why, _node_fix(ctx.node))
    try:
        eb = fsc.param_int(p, "avail_epoch_blocks")
    except fsc.ChainUnreadable as e:
        return result(cid, name, UNMEASURED, "avail_epoch_blocks is not an integer", str(e))
    if eb == 0:
        return result(cid, name, OK, "presence is disarmed: avail_epoch_blocks is 0 (proto3 omits it at zero)",
                      "the chain does not measure presence, so it filters no draw on it (presence.go::minerPresentAt)")
    if not ctx.mid:
        return result(cid, name, UNMEASURED, "no miner identity to look for")
    state, rec = ctx.miner_record()
    if state == "notfound":
        return result(cid, name, KO, f"{ctx.mid} is not registered (see C3): it cannot be present")
    h = ctx.height
    if h is None:
        try:
            h = fsc.height(ctx.rpc)
        except fsc.ChainUnreadable as e:
            return result(cid, name, UNMEASURED, "the current height was not read", str(e)[:240])
    e = h // eb
    first = max(0, (e - 1) * eb)
    try:
        fsc.require_index_from(ctx.rpc, max(1, first))
    except fsc.ChainUnreadable as err:
        return result(cid, name, UNMEASURED, f"this node's history does not reach block {first}", str(err)[:300],
                      "a node restored by state sync keeps no history before its snapshot: this reads again once "
                      "two windows have passed")
    try:
        windows, _ = fsc.presence_proofs(ctx.node, first, h, eb, max_pages=PRESENCE_MAX_PAGES)
    except fsc.ChainUnreadable as err:
        return result(cid, name, UNMEASURED, f"the availability proofs of blocks [{first}, {h}] were not read", str(err)[:240])
    if ctx.mid in windows:
        return result(cid, name, OK, f"present: an accepted availability proof in blocks [{first}, {h}] "
                                     f"(windows {e - 1} and {e} of {eb} blocks)")
    try:
        reg_h = registration_height(ctx.node, ctx.mid, first, h)
    except fsc.ChainUnreadable as err:
        return result(cid, name, UNMEASURED, f"no proof in blocks [{first}, {h}]; the registration search failed", str(err)[:240])
    if reg_h is not None and reg_h // eb + 1 >= e:
        return result(cid, name, OK, f"newcomer's grace: registered at block {reg_h}, present through window {reg_h // eb + 1} "
                                     f"without a proof (presence.go::minerPresentAt)")
    return result(cid, name, KO, f"ABSENT: no accepted availability proof for {ctx.mid} in blocks [{first}, {h}]",
                  "the chain draws only present miners for work and juries: an absent miner gets nothing." + _last_attempt(ctx),
                  "docker compose -p dendra-miner logs miner | grep -i availability")


def c5_queue(ctx):
    cid, name = "C5", "work queue"
    relay = ctx.env.get("DENDRA_RELAY", "").rstrip("/")
    if not relay:
        return result(cid, name, KO, "DENDRA_RELAY is empty", "a miner without a relay is never handed a job",
                      "re-run deploy/join.sh: it writes DENDRA_RELAY into the miner kit's .env")
    hdr = {"X-Dendra-Token": ctx.env["DENDRA_RELAY_TOKEN"]} if ctx.env.get("DENDRA_RELAY_TOKEN") else {}
    code, body = http_get(relay + "/list", hdr)
    if code is None:
        return result(cid, name, KO, f"the relay {relay} did not answer", str(body)[:240],
                      "this miner learns that a job waits for it only from this queue: check the address, or ask on the network's channel")
    if code in (401, 403):
        return result(cid, name, KO, f"the relay refuses the work queue (GET /list -> HTTP {code})",
                      "a miner that cannot read the queue waits forever with no error",
                      "a current relay serves the queue to anyone: ask its operator to update")
    if code != 200:
        return result(cid, name, KO, f"GET /list -> HTTP {code}", "the queue was not served")
    try:
        d = json.loads(body)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return result(cid, name, KO, "GET /list answered something that is not a work queue")
    mine = [k for k in (d.get("req") or []) if ctx.mid and str(k).endswith("__" + ctx.mid)]
    return result(cid, name, OK, f"the work queue is readable (HTTP 200); {len(mine)} request(s) for this miner in it")


def c6_write(ctx):
    cid, name = "C6", "relay key copy and signed write"
    if not ctx.mid:
        return result(cid, name, UNMEASURED, "no miner identity")
    state, rec = ctx.miner_record()
    if state == "notfound":
        return result(cid, name, KO, f"{ctx.mid} is not registered (see C3): no anchored key to compare with")
    if state != "ok":
        return result(cid, name, UNMEASURED, "the anchored key was not read", rec)
    anchored = rec["enc_pubkey"]
    if not anchored:
        return result(cid, name, KO, f"no encryption key is anchored on chain for {ctx.mid}",
                      "clients seal every job to the key the chain anchors for the miner; with none, no job can be sealed to this one",
                      f"dendrad tx jobs rotate-miner-keys {ctx.mid} --new-enc-pubkey <the daemon's key>")
    relay = ctx.env.get("DENDRA_RELAY", "").rstrip("/")
    if not relay:
        return result(cid, name, UNMEASURED, "DENDRA_RELAY is empty (see C5)")
    code, raw = http_get(f"{relay}/pub/{ctx.mid}")
    if code is None:
        return result(cid, name, UNMEASURED, "the relay did not answer the read of this miner's key", str(raw)[:240])
    if code == 200:
        try:
            d = json.loads(raw)
        except ValueError:
            d = None
        stored = d.get("pub") if isinstance(d, dict) else None
        if not isinstance(stored, str):
            return result(cid, name, KO, f"the relay's pub/{ctx.mid} is not a key record; nothing was written",
                          "clients cross-check the relay's copy against the chain")
        # ⛔ A COPY THAT DIFFERS FROM THE CHAIN IS NEVER OVERWRITTEN HERE. It is either this machine's own
        # older key or somebody else's write, and both need a person: overwriting would erase the evidence.
        if stored.lower() != anchored.lower():
            return result(cid, name, KO, f"the relay serves a DIFFERENT key for {ctx.mid} than the chain anchors; nothing was written",
                          "clients cross-check the relay's copy against the chain and refuse a mismatch; this check never writes over it",
                          "find who wrote it before anything else: the daemon deposits its key at start (docker compose -p dendra-miner logs miner | grep pubkey)")
        obj = {"pub": stored}
        before = "the relay's copy equals the chain's"
        if not ctx.opts.write:
            return result(cid, name, OK, f"{before}; nothing was written",
                          "read only: the signed write path is tested on request (miner_health.sh --write)")
        if json.dumps(obj).encode() != raw:
            return result(cid, name, UNMEASURED, "the relay's copy equals the chain's key in a form this kit does not write; nothing was written",
                          "only identical bytes are ever re-deposited, so the write path was not measured")
    elif code == 404:
        # No copy at all: the relay forgets a deposit after its retention, and the daemon deposits only at
        # start. It is a cache, not the authority: clients seal to the key the chain anchors and only
        # cross-check the relay's copy (client.py, DENDRA_REQUIRE_ONCHAIN_PUB), so its absence is
        # said, not alarmed. The bytes --write deposits are the chain's own anchor: no wrong key can land.
        before = "the relay held no copy (a cache it forgets after its retention, or the start-up deposit was refused)"
        if not ctx.opts.write:
            return result(cid, name, OK, f"{before}; nothing was written",
                          "clients seal every job to the key the chain anchors; the relay's copy is only cross-checked. "
                          "To put it back, signed: bash deploy/testnet-miner/miner_health.sh --write")
        obj = {"pub": anchored}
    else:
        return result(cid, name, KO, f"the relay answered HTTP {code} to a read of pub/{ctx.mid}; nothing was written")
    relay_client.set_sign_key(ctx.mid)
    # require_signature: an unsigned deposit is NEVER sent -- a relay that still accepts one would take a
    # write nobody can attribute, and this check would have caused it.
    wcode, why, signed = relay_client.put_answer(relay, "pub", ctx.mid, obj, miner_id=ctx.mid, height=0,
                                                 require_signature=True)
    if not signed:
        return result(cid, name, KO, f"{before}; this miner could NOT sign the deposit, so nothing was written",
                      "an unsigned write is refused wherever the relay enforces signatures, and the same path carries this miner's answers (res) and reveals",
                      "the signing key is the keyring entry named after the miner identity: see C3, and the miner's log (grep 'cannot sign')")
    if wcode is None:
        return result(cid, name, UNMEASURED, f"{before}; the relay did not answer the signed write", why)
    if wcode == 200:
        return result(cid, name, OK, f"{before}; a signed deposit of those identical bytes was accepted")
    if wcode == 401 and why.startswith(REJEU):
        return result(cid, name, OK, f"{before}; the relay verified the signature, attributed it to this miner's operator, "
                                     f"and refused these exact bytes only as a replay (already accepted)")
    return result(cid, name, KO, f"{before}; the relay REFUSED the signed write (HTTP {wcode}): {why or 'no reason given'}",
                  "the same signed path carries this miner's sealed answers and reveals: a job could be computed and never delivered",
                  "read the relay's reason above; a signature refused for attribution means the key does not belong to the operator the chain records (see C3)")


# THE SERVICE THAT HOLDS THE MODEL THIS MINER SERVES, named in C7's fixes. deploy/join.sh decides it with the
# machine's engine (engine_decide): a card Docker can use -> the miner infers on the kit's `ollama`; no usable
# GPU -> the judge role on the CPU, whose override points the miner at `ollama-cpu`, where the served model and
# the embedder are pulled (write_cpu_judge_override). The container does not carry that decision as a word:
# DENDRA_MINER_JUDGE is the judge ROLE, which a miner on a card takes too while its model stays in `ollama`. It
# carries its EFFECT, the endpoint this backend calls -- so the service is READ from that endpoint, and a host
# that is none of the kit's services names none: a fix naming a guessed service would act on the wrong engine.
MODEL_SERVICES = ("ollama", "ollama-cpu")


def model_service(endpoint) -> str:
    """The kit service behind `endpoint` (one of MODEL_SERVICES), or "" when its host is none of them."""
    try:
        host = urllib.parse.urlsplit(str(endpoint or "")).hostname or ""
    except ValueError:
        return ""
    return host if host in MODEL_SERVICES else ""


def model_fixes(endpoint, emb_model=""):
    """(fix for a generation that fails, fix for an embedder that returns nothing), on the service the miner calls."""
    svc = model_service(endpoint)
    if not svc:
        where = (f"the model server at {endpoint} is none of the kit's services ({', '.join(MODEL_SERVICES)}), "
                 "so no compose command is written for it: ")
        return (where + "read its logs and its model list (ollama list) where it runs",
                where + f"pull {emb_model or 'the embedding model'} into it where it runs (ollama pull)")
    return (f"docker compose -p dendra-miner logs --tail 50 {svc}; docker compose -p dendra-miner exec {svc} ollama list",
            f"docker compose -p dendra-miner exec {svc} ollama pull {emb_model}")


def c7_model(ctx):
    cid, name = "C7", "model"
    backend = ctx.env.get("BACKEND", "ollama")
    if backend != "ollama":
        return result(cid, name, UNMEASURED, f"backend '{backend}': this probe speaks to Ollama only")
    b = inference.OllamaBackend()
    t0 = time.monotonic()
    try:
        out = b.generate(PROBE_PROMPT, max_out=PROBE_TOKENS)
    except Exception as e:  # noqa: BLE001
        return result(cid, name, KO, f"{b.model} on {b.endpoint} did not answer a generation: {type(e).__name__}",
                      str(e)[:200] + " -- every job goes through this same call",
                      model_fixes(b.endpoint)[0])
    t_gen = time.monotonic() - t0
    if not str(out or "").strip():
        return result(cid, name, KO, f"{b.model} returned an empty answer", "a job answered this way is an empty answer")
    emb_model = ctx.env.get("DENDRA_EMBED_API_MODEL", "")
    if not emb_model:
        return result(cid, name, KO, "DENDRA_EMBED_API_MODEL is empty",
                      "every answer is embedded before it is committed (DENDRA_EMBED_MODE=backend); a backend that cannot embed fails the job",
                      "re-run deploy/join.sh: it writes the embedding model into the kit's .env")
    t1 = time.monotonic()
    v = b.embed("availability probe")
    t_emb = time.monotonic() - t1
    if not isinstance(v, list) or not v:
        return result(cid, name, KO, f"the embedding model {emb_model} returned no vector",
                      "every answer is embedded before it is committed; a backend that cannot embed fails the job",
                      model_fixes(b.endpoint, emb_model)[1])
    return result(cid, name, OK, f"{b.model}: {getattr(b, 'out_tok', '?')} token(s) in {t_gen:.1f} s; "
                                 f"{emb_model}: {len(v)} dimensions in {t_emb:.1f} s",
                  "latency is reported, not judged: this check reads no response budget to judge it against")


def c8_capacity(ctx):
    cid, name = "C8", "capacity registry"
    url = (ctx.opts.capacity_url or "").strip()
    node_id = (ctx.opts.node_id or ctx.env.get("DENDRA_SIGN_KEY", "")).strip()
    if not url:
        return result(cid, name, UNMEASURED, "no capacity registry address (the kit's DENDRA_CAPACITY_URL)",
                      "", "re-run deploy/join.sh, which writes it, or set DENDRA_CAPACITY_URL in the miner kit's .env")
    if not node_id:
        return result(cid, name, UNMEASURED, "no node name to look for (the kit's MINER_ID)")
    code, body = http_get(url)
    if code != 200:
        return result(cid, name, UNMEASURED, f"the registry {url} was not read ({'HTTP ' + str(code) if code else body})")
    try:
        d = json.loads(body)
    except ValueError:
        d = None
    nodes = d.get("nodes") if isinstance(d, dict) else None
    if not isinstance(nodes, list):
        return result(cid, name, UNMEASURED, "the registry answered without a list of nodes")
    rows = [r for r in nodes if isinstance(r, dict) and r.get("node_id") == node_id]
    if not rows:
        return result(cid, name, KO, f"no line for {node_id} in the registry",
                      "a machine without a fresh report is not counted, and the public chat reads that count",
                      "bash deploy/testnet-miner/publish-capacity.sh   (the hourly job does it; see the schedule check)")
    flags = [capacity_flags(r) for r in rows]
    if any(reg is None or stale is None for reg, stale in flags):
        return result(cid, name, UNMEASURED, f"the line of {node_id} carries no readable registered_onchain / stale flag")
    if any(reg and not stale for reg, stale in flags):
        return result(cid, name, OK, f"{node_id}: fresh, and attributed to the operator the chain records")
    if all(stale for _, stale in flags):
        return result(cid, name, KO, f"{node_id}: the report is STALE", "the registry stops counting a report after a day",
                      "bash deploy/testnet-miner/publish-capacity.sh, and check that its hourly job runs (crontab -l)")
    return result(cid, name, KO, f"{node_id}: fresh but NOT attributed on chain",
                  "only a report signed by the operator the chain records for this miner enters the verified count",
                  "bash deploy/testnet-miner/publish-capacity.sh   (it signs inside the miner container)")


def _running_workers(root):
    """{script basename} of the processes visible under `root` (/proc), or None when nothing is readable."""
    try:
        pids = [p for p in os.listdir(root) if p.isdigit()]
    except OSError:
        return None
    seen, readable = set(), 0
    for pid in pids:
        try:
            with open(os.path.join(root, pid, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        readable += 1
        for a in raw.split(b"\0"):
            base = os.path.basename(a.decode("utf-8", "replace"))
            if base in WORKERS:
                seen.add(base)
    return seen if readable else None


def c9_processes(ctx):
    cid, name = "C9", "processes and heartbeat"
    seen = ctx.workers()
    if seen is None:
        return result(cid, name, UNMEASURED, f"no process could be read under {ctx.opts.proc_root}")
    # The same switches, with the same defaults, as docker/entrypoint-services.sh: revealing is ON unless
    # it is turned off, judging is OFF unless it is turned on.
    expected = ["miner.py"]
    if ctx.env.get("DENDRA_MINER_REVEAL", "1") == "1":
        expected.append("reveal_worker.py")
    if ctx.env.get("DENDRA_MINER_JUDGE", "0") == "1":
        expected.append("judge_worker.py")
    missing = [w for w in expected if w not in seen]
    if missing:
        costs = {"miner.py": "no job is served",
                 "reveal_worker.py": "a sampled job has no reveal to judge, so its fee stays held and this miner is neither paid nor slashed",
                 "judge_worker.py": "this miner's jury seats stay mute"}
        return result(cid, name, KO, "NOT running: " + ", ".join(missing),
                      "; ".join(f"{w}: {costs[w]}" for w in missing) + ". The container stays Up: only the daemon is waited on",
                      "docker compose -p dendra-miner restart miner; then docker compose -p dendra-miner logs --tail 80 miner")
    running = ", ".join(expected) + " running"
    doc, why = ctx.heartbeat
    if doc is None:
        return result(cid, name, UNMEASURED, f"{running}; the daemon's heartbeat was not read", why)
    age = hb.age_s(doc)
    if age is None:
        return result(cid, name, UNMEASURED, f"{running}; the heartbeat carries no readable written_at")
    bound = hb.max_age_s()
    if age > bound:
        return result(cid, name, KO, f"{running}, but the daemon's heartbeat is {age} s old",
                      f"the bound is {bound} s, two of the longest single step the loop takes (modea/heartbeat.py::max_age_s): "
                      f"the loop is not moving",
                      "docker compose -p dendra-miner logs --tail 80 miner; docker compose -p dendra-miner restart miner")
    notes = []
    loop_at = doc.get("loop_at")
    if isinstance(loop_at, int) and not isinstance(loop_at, bool):
        notes.append(f"last full pass {int(time.time() - loop_at)} s ago")
    else:
        notes.append(f"phase {doc.get('phase', '?')}")
    err = doc.get("loop_error")
    if isinstance(err, dict) and err.get("what"):
        notes.append(f"last loop error: {err.get('what')}")
    # The commit counters the HiveOS stats show as `ar` (miner._COMMITS): reported as read, never judged
    # -- a count is not a fault -- and left out when the heartbeat does not carry both as whole numbers (an
    # older daemon): an unread count is never shown as zero.
    ca, cr = doc.get("commits_anchored"), doc.get("commits_refused")
    if all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (ca, cr)):
        notes.append(f"create-commit transactions since the daemon started: {ca} anchored, {cr} refused")
    return result(cid, name, OK, f"{running}; heartbeat {age} s old (bound {bound} s); " + "; ".join(notes))


def c10_judge_engine(ctx):
    """THE ENGINE THIS IDENTITY JUDGES WITH, when its judge role is on. One CPU judge serves the whole machine:
    on a machine with one identity per card, slot 0's ollama-cpu is reached by every other slot under the alias
    dendra-judge-cpu, on a network of its own -- a dependency between two compose projects, so it has its own
    check. Three answers: the engine holds the model this identity judges with (ok); it does not answer, or it
    does not hold that model (ko: every jury seat of this identity is mute until it does); its answer cannot
    be read (unmeasured)."""
    return dict(ctx.judge_engine()[0])


def judge_engine(env, proc_root="/proc"):
    """(C10 result, detail): the reading of the judge engine, and what C11 needs to declare a role without
    reading the result's words:
        endpoint     "" when none is configured
        answered     False when nothing answered /api/tags, None when the engine was not asked
        names        the models the engine holds (a set), None when its answer was not read
        model        the model checked, and `source`, where it comes from (resolve_judge_model's own words)
        origin       "worker": that model is the RUNNING judge_worker.py's own (judge_worker_model);
                     "resolved now": its own was not readable, and this is the one a worker started now would use
        origin_why   why the worker's own model was not read ("" when it was)
        pinned       asked only of the worker's own model when the engine holds it (_pin_reading): True when it
                     is the chain's pin or the operator's explicit choice, False when that is not established
        pinned_why, pinned_fix"""
    detail = {"endpoint": "", "answered": None, "names": None, "model": "", "source": "", "origin": "",
              "origin_why": "", "pinned": None, "pinned_why": "", "pinned_fix": ""}
    return _judge_engine_reading(env, proc_root, detail), detail


def _whole(v):
    return isinstance(v, int) and not isinstance(v, bool)


def judge_worker_model(proc_root):
    """(model, source, None) as the RUNNING judge_worker.py resolved them at its start -- its judge state
    (modea/heartbeat.py::write_judge_state) -- or (None, None, why). The state is believed only when the
    process it names runs judge_worker.py AND started when the state says: a file outlives the process that
    wrote it, and a pid is reused, so a state left by an earlier worker would name a model nothing judges with."""
    p = hb.judge_state_path()
    doc, why = hb.read(p, what="judge state")
    if doc is None:
        return None, None, why
    pid, start, model, source = (doc.get(k) for k in ("pid", "starttime", "model", "source"))
    if not (_whole(pid) and pid > 0 and _whole(start) and isinstance(model, str) and model.strip()
            and source in hb.JUDGE_MODEL_SOURCES):
        return None, None, f"the judge state at {p} is not one this file recognises"
    base = os.path.join(proc_root, str(pid))
    try:
        with open(os.path.join(base, "cmdline"), "rb") as f:
            raw = f.read()
    except OSError:
        return None, None, f"the judge state names process {pid}, which does not run: it was written by an earlier worker"
    if "judge_worker.py" not in {os.path.basename(a.decode("utf-8", "replace")) for a in raw.split(bytes(1))}:
        return None, None, f"the judge state names process {pid}, which is not judge_worker.py: it was written by an earlier worker"
    try:
        with open(os.path.join(base, "stat"), encoding="utf-8", errors="replace") as f:
            now_start = hb.proc_starttime(f.read())
    except OSError:
        now_start = None
    if now_start is None:
        return None, None, f"the start time of process {pid} could not be read: the judge state cannot be tied to it"
    if now_start != start:
        return None, None, f"process {pid} started after the judge state was written (a pid reused): it was written by an earlier worker"
    return model.strip(), source, None


def _chain_pin(env):
    """(pin, None) -- the judge model the chain pins, "" when it pins none -- or (None, why) when it was not read.
    proto3 omits an empty string: an absent `audit_judge_model` IS the empty pin, a chain that pins nothing, and
    a query that fails is a pin nobody read. The two answers are kept apart. Same query, same node and same
    tolerance (a top-level params, camelCase) as judge_worker.py::resolve_judge_model."""
    node = env.get("DENDRA_NODE", "")
    rc, out, err = dendrad(["query", "modelregistry", "params", "--output", "json", *(["--node", node] if node else [])],
                           timeout=20)
    if rc != 0:
        return None, f"the chain's pin was not read (modelregistry params: rc={rc} {' '.join(str(err).split())[:160]})"
    try:
        d = json.loads(out)
    except ValueError:
        return None, "the chain's pin was not read (modelregistry params: the answer is not JSON)"
    p = d.get("params", d) if isinstance(d, dict) else None
    if not isinstance(p, dict):
        return None, "the chain's pin was not read (modelregistry params: no params in the answer)"
    v = p.get("audit_judge_model", p.get("auditJudgeModel", ""))
    if not isinstance(v, str):
        return None, f"the chain's pin was not read (audit_judge_model is not a string: {str(v)[:40]!r})"
    return v.strip(), None


def _pin_reading(env, model, source):
    """(pinned, why, fix): is `model` -- the one the running judge_worker.py judges with, from `source` -- the
    model the chain pins? The condition an ACTIVE role adds to a worker and an engine that holds its model.
        cli      an explicit choice (DENDRA_JUDGE_MODEL_OVERRIDE): the operator's, and the worker puts it before
                 the pin -- True, and the chain is not asked
        otherwise the pin is read NOW and compared: equal is True; another pin, or no pin at all, is False
        and when the pin cannot be read now, only a model the worker itself took from the chain at its start
                 is True. A fallback (env, default) the chain did not confirm is never pinned: the kit's own
                 model may differ from the pin, and a seat judging with it is not the seat the committee counts."""
    if source == "cli":
        return True, f"{model} is an explicit choice (DENDRA_JUDGE_MODEL_OVERRIDE), which the worker puts before the chain's pin", ""
    pin, why = _chain_pin(env)
    if pin is None:
        if source == "chain":
            return True, f"{model} is the chain's pin as judge_worker.py read it at its start ({why} now)", ""
        return False, (f"judge_worker.py judges with {model}, its {source} fallback, and {why}: whether that is the "
                       f"model the chain pins is not known"), ""
    if not pin:
        return False, ("the chain pins no judge model (modelregistry audit_judge_model is empty): no model is the "
                       "pinned one"), ""
    if pin == model:
        return True, f"{model} is the model the chain pins", ""
    return False, (f"the chain pins {pin}; judge_worker.py judges with {model}, which it resolved at its start "
                   f"({source})"), SLOT0_COMPOSE + "restart miner   (judge_worker.py reads the pin when it starts)"


def _judge_engine_reading(env, proc_root, detail):
    cid, name = "C10", "judge engine"
    from modea.judge import judge_endpoint
    ep = str(judge_endpoint() or "").rstrip("/")
    detail["endpoint"] = ep
    if not ep:
        return result(cid, name, KO, "no judge endpoint (DENDRA_JUDGE_ENDPOINT)", "a verdict has no engine to run on",
                      "re-run deploy/join.sh --judge, which writes it")
    # THE MODEL CHECKED IS THE ONE THE RUNNING WORKER JUDGES WITH. judge_worker.py resolves it ONCE, at its start,
    # and every verdict uses it; resolving it again here would read the chain of this moment, not the process --
    # a worker started while the chain did not answer judges with its fallback even after the chain answers. So
    # the worker's own judge state is read first. Only when it is not readable (no worker, one that has not
    # written it yet) is the model resolved now, by the worker's own function: C10 can still say whether a
    # worker started now would find its model, and C11 never declares a role from that model (judge_role).
    model, source, why = judge_worker_model(proc_root)
    if model is not None:
        detail["origin"] = "worker"
        whose = " -- the model judge_worker.py resolved at its start"
    else:
        try:
            import judge_worker as jw
            model, source = jw.resolve_judge_model(env.get("DENDRA_JUDGE_MODEL_OVERRIDE", "").strip())
        except Exception as e:  # noqa: BLE001
            return result(cid, name, UNMEASURED, f"the model this identity judges with could not be resolved: {type(e).__name__}",
                          str(e)[:200])
        detail["origin"], detail["origin_why"] = "resolved now", why
        whose = f" -- the model a worker started now would judge with; judge_worker.py's own was not read ({why})"
    detail["model"], detail["source"] = model, source
    shared = "" if "ollama-cpu" in ep else (" -- the machine's shared judge, on slot 0: "
                                            "bash deploy/testnet-miner/miner_health.sh --slot 0 checks it")
    code, body = http_get(f"{ep}/api/tags")
    detail["answered"] = code is not None
    if code is None:
        return result(cid, name, KO, f"the judge engine {ep} does not answer ({body})",
                      "every audit this identity is drawn for gets no verdict from it: a mute jury seat" + shared,
                      "bash deploy/testnet-miner/slots.sh run 0 ps ollama-cpu; bash deploy/testnet-miner/slots.sh run 0 logs --tail 50 ollama-cpu")
    if code != 200:
        return result(cid, name, UNMEASURED, f"the judge engine {ep} answered HTTP {code} to /api/tags")
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        d = None
    ms = d.get("models", []) if isinstance(d, dict) else None
    if not isinstance(ms, list):
        return result(cid, name, UNMEASURED, f"the judge engine {ep} answered /api/tags without a list of models")
    names = set()
    for m in ms:
        if isinstance(m, dict):
            names.update(str(m.get(k) or "") for k in ("name", "model"))
    names.discard("")
    detail["names"] = names
    want = {model, model + ":latest"} if ":" not in model else {model}
    if names & want:
        if detail["origin"] == "worker":
            detail["pinned"], detail["pinned_why"], detail["pinned_fix"] = _pin_reading(env, model, source)
        return result(cid, name, OK, f"{ep} holds {model} ({source}){whose}")
    return result(cid, name, KO, f"{ep} does not hold {model} ({source}){whose}: {len(names)} model(s) there",
                  "a verdict needs that model: until the engine holds it, every jury seat of this identity is mute" + shared,
                  "slot 0's judge-model-init pulls it: bash deploy/testnet-miner/slots.sh run 0 logs judge-model-init")


def judge_role(env, workers, engine):
    """(role, why, fix): the judge role this identity declares, one of JUDGE_ROLES (see there).

    `workers` and `engine` are CALLABLES -- the scripts running here (_running_workers: a set, or None when
    nothing could be read) and the judge engine's reading (judge_engine: (C10 result, detail)) -- called only
    when the role is requested: a role that is off asks nothing of /proc nor of the engine.

    ⛔ MUTE WINS OVER UNKNOWN, AND ACTIVE NEEDS EVERY PART READ. One necessary part measured absent is enough to
    know the seat says nothing, whatever the unread part would have answered; but a part that could not be read
    is never counted present. `fix` is the remedy of the FIRST measured cause: a worker that does not run is
    restarted before an engine is blamed.

    ⛔ ACTIVE IS DECLARED OF THE RUNNING WORKER'S OWN MODEL, AND OF THE PIN. The engine holding a model proves
    nothing of a worker that judges with another one: the model is the one the worker resolved at its start
    (detail origin "worker"), and it must be the chain's pin (_pin_reading). A model resolved NOW, a fallback the
    chain did not confirm, a pin that moved since the worker started: each is unknown, never active."""
    # The switch and the default of docker/entrypoint-services.sh, read in this same environment: not a default
    # chosen here, the reading of what the entrypoint did -- it starts judge_worker.py on "1" and on nothing else.
    if env.get("DENDRA_MINER_JUDGE", "0") != "1":
        return "off", "the judge role is not requested (DENDRA_MINER_JUDGE is not 1)", ""
    mute, unread, fixes, unread_fixes = [], [], [], []
    seen = workers()
    dead = False
    if seen is None:
        unread.append("no process could be read: whether judge_worker.py runs is not known")
    elif "judge_worker.py" not in seen:
        dead = True
        mute.append("judge_worker.py is NOT running")
        fixes.append(SLOT0_COMPOSE + "restart miner; then " + SLOT0_COMPOSE + "logs --tail 80 miner")
    res, detail = engine()
    st = res.get("state") if isinstance(res, dict) else None
    measured = (res.get("measured") if isinstance(res, dict) else "") or ""
    own = detail.get("origin") == "worker"
    # A ko silences THIS seat when it concerns the model the running worker judges with, or any model at all (no
    # endpoint, an engine that does not answer, one that holds nothing), or a worker that is dead anyway (the
    # model is then the one its restart will need). An engine lacking the model a worker started NOW would use
    # says nothing of a running worker whose own model was not read: that is unread, not silent.
    any_model = not detail.get("endpoint") or detail.get("answered") is False or detail.get("names") == set()
    if st == KO and (own or dead or any_model):
        mute.append(measured or "the judge engine cannot serve a verdict")
        if not detail.get("endpoint"):
            fixes.append("re-run deploy/join.sh --judge, which writes DENDRA_JUDGE_ENDPOINT")
        elif detail.get("answered") is False:
            # One CPU judge per machine, on slot 0 (its alias dendra-judge-cpu for every slot k): started there.
            fixes.append("bash deploy/testnet-miner/slots.sh run 0 --profile judge up -d ollama-cpu")
        else:
            fixes.append(f"bash deploy/testnet-miner/slots.sh run 0 exec ollama-cpu ollama pull {detail.get('model') or '<the model>'}")
    elif st == KO:
        unread.append(measured or "the judge engine lacks the model a worker started now would use")
    elif st != OK:
        unread.append(measured or "the judge engine was not read")
    elif not own:
        unread.append("the model judge_worker.py judges with was not read (" + (detail.get("origin_why") or "no judge state")
                      + "): the engine holding the one a worker started now would use says nothing of it")
    elif detail.get("pinned") is not True:
        unread.append(detail.get("pinned_why") or "whether the model judge_worker.py judges with is the chain's pin was not read")
        if detail.get("pinned_fix"):
            unread_fixes.append(detail["pinned_fix"])
    if mute:
        return "mute", "; ".join(mute), fixes[0] if fixes else ""
    if unread:
        return "unknown", "; ".join(unread), unread_fixes[0] if unread_fixes else ""
    return "active", "judge_worker.py running; " + (measured or "the judge engine holds its model") + "; " + str(detail.get("pinned_why") or ""), ""


def c11_judge_role(ctx):
    """THE ROLE THIS IDENTITY DECLARES, the word its signed capacity report carries (publish-capacity.sh reads it
    with --judge-role, in this container). Run only when the role is requested: off is neither a fault nor advice."""
    cid, name = "C11", "judge role (declared)"
    role, why, fix = judge_role(ctx.env, ctx.workers, ctx.judge_engine)
    if role == "active":
        return result(cid, name, OK, f"active: {why}",
                      "the capacity report declares this identity a judge; only a verdict it posts on chain proves it judges")
    if role == "mute":
        return result(cid, name, KO, f"MUTE: {why}",
                      "the chain draws this identity into juries all the same, and every audit it is drawn for gets no "
                      "verdict from it; the capacity report declares it mute", fix)
    if role == "off":
        return result(cid, name, OK, f"off: {why}")
    return result(cid, name, UNMEASURED, f"unknown: {why}",
                  "the capacity report declares the role unknown -- never off, never active", fix)


CHECKS = (("C1", c1_rpc), ("C2", c2_sync), ("C3", c3_registration), ("C4", c4_presence), ("C5", c5_queue),
          ("C6", c6_write), ("C7", c7_model), ("C8", c8_capacity), ("C9", c9_processes), ("C10", c10_judge_engine),
          ("C11", c11_judge_role))
NAMES = {"C1": "node RPC", "C2": "node caught up", "C3": "registration and keys", "C4": "availability (presence)",
         "C5": "work queue", "C6": "relay key copy and signed write", "C7": "model", "C8": "capacity registry",
         "C9": "processes and heartbeat", "C10": "judge engine", "C11": "judge role (declared)"}
# The command that reaches THIS miner's compose project, in the fixes the report prints. Slot 0's is written
# into them; on a machine with one identity per card, miner_health.sh passes slot k's (--compose) and every
# fix is retargeted to it, so that a remedy never acts on another identity.
SLOT0_COMPOSE = "docker compose -p dendra-miner "
# A remedy that only slot 0 has: `deploy/join.sh --id` names slot 0's identity, and a slot k's identity follows
# its own key. On a slot k it would act on ANOTHER identity, so retarget() replaces it with the one that holds
# for any slot: the key restored into that slot's keyring.
SLOT0_NEW_ID_FIX = ", or join under a new identity: deploy/join.sh --id <new-id>"
SLOT_K_KEY_FIX = (" of this slot (its own miner-keys volume), from that identity's 24-word recovery phrase; "
                  "deploy/join.sh --id names slot 0 only")


def retarget(doc, prefix):
    """The fixes, measures and reasons of `doc` (checks and advice) with slot 0's compose command replaced by
    `prefix`, and slot 0's own remedies by a slot k's. Nothing changes without a prefix. The judge's remedies
    name slot 0 through slots.sh on purpose: the machine's one judge engine is slot 0's, whatever identity
    reports it."""
    if not prefix:
        return doc
    for row in list(doc.get("checks") or []) + list(doc.get("advice") or []):
        if not isinstance(row, dict):
            continue
        for k in ("measured", "reason", "fix"):
            if isinstance(row.get(k), str):
                row[k] = row[k].replace(SLOT0_COMPOSE, prefix.rstrip() + " ").replace(SLOT0_NEW_ID_FIX, SLOT_K_KEY_FIX)
    return doc


# ── advice: what this miner's OWNER chose, or has not chosen yet ──────────────────────────────────────
# Three facts that are not faults: keys kept in clear, the recovery phrase still on this machine, the season
# paying this machine's own key. Each is a configuration someone may have chosen, so each is ADVICE, in its
# own section of the report, the --json document and the application's Health card, with the command that
# changes it. ⛔ ADVICE NEVER CHANGES THE EXIT CODE AND NEVER RAISES AN ALERT: an hourly alert on a choice
# teaches its reader to ignore alerts, and the next one would be a real fault.
def advice(ctx) -> list:
    out = []

    def add(aid, name, measured, fix):
        out.append({"id": aid, "name": name, "measured": measured, "fix": fix})
    kr, _ = _keyring(ctx)
    if kr is not None and kr.backend == keyring.TEST:
        add("A1", "keys at rest", "the miner's keyring is the TEST keyring: its key is stored IN CLEAR in the miner-keys "
                                  "volume, and in every backup of that volume",
            "bash deploy/testnet-miner/encrypt-keys.sh   (prints its plan and changes nothing; --yes encrypts)")
    elif kr is not None and ctx.mid:
        clear = [n for n, s in keyring.key_files(ctx.keydir, ctx.mid).items() if s == "clear"]
        if clear:
            add("A1", "keys at rest", "the keyring is encrypted, but these key files are still in clear: " + ", ".join(clear),
                "docker compose -p dendra-miner restart miner   (the daemon seals them at start)")
    rp = os.path.join(ctx.keydir, keyring.RECOVERY_NAME)
    if os.path.exists(rp):
        add("A2", "recovery phrase on this machine", f"the 24-word recovery phrase of this miner's key is still in {rp}",
            "open the Dendra application, type the three words it asks for, then 'I wrote it down — remove it from "
            "this machine'")
    rec = keyring.payout_record(ctx.keydir)
    owner = ctx.env.get("DENDRA_MINER_OWNER", "").strip()
    if ctx.mid and (not isinstance(rec, dict) or rec.get("miner_id") != ctx.mid):
        add("A3", "Final Testnet Season payout", f"no payout address is declared for {ctx.mid}: the season pays this "
                                                 "machine's own key, which every copy of this machine's keys can spend",
            # In owner mode the programme accepts the declaration from the owner's key only (it is not here).
            ("signed by the owner: docker compose -p dendra-miner exec -T miner python3 final_season_miner.py "
             "payout-prepare --address dendra1... > payout.json, sign it on the owner's machine with the command it "
             "prints, then payout-submit < payout.signed.json") if owner else
            ("bash deploy/join.sh --payout-address dendra1...   (an address whose key is NOT on this machine), or "
             "the application's 'Where rewards go'"))
    return out


class DeadlineExceeded(BaseException):
    """Raised inside a check, by the alarm, when the self-test's own deadline passes.

    ⛔ A BaseException, NOT an Exception: the readers a check calls catch Exception to turn a failure into a
    reading -- http_get returns "nothing answered" on any Exception. An alarm caught there became that reading:
    the judge engine "does not answer", a ko and a MUTE judge role, for a run that only ran out of time. The
    deadline is not an answer from anything; it must unwind to the one place that knows it is the deadline."""


# The alarm interrupts a CHECK, never the bookkeeping around it: outside a check it does nothing, and the
# loop sees the deadline itself before the next one.
_ALARM = {"in_check": False}


def _on_alarm(signum, frame):  # noqa: ARG001
    if _ALARM["in_check"]:
        raise DeadlineExceeded("the self-test's deadline passed")


def _arm(seconds):
    """Arms the alarm for `seconds` (> 0), or disarms it (0). Where the platform has no interval timer the
    deadline is still kept between checks, only not inside one."""
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, max(0.0, seconds))


def run_checks(opts, env):
    """(results, skipped). A check that raises is UNMEASURED with its exception named, never ok. What the
    libraries print while the checks run is kept off stdout: --json and --host must stay parseable.

    THE DEADLINE IS KEPT HERE, INSIDE THE CONTAINER. Each check runs under an alarm set to the time left; a
    check the alarm stops is unmeasured, and every check after it is reported unmeasured without running.
    The exception the alarm raises unwinds the check, so a `dendrad` or HTTP call it was waiting on is
    killed or closed with it (subprocess.run kills its child on any exception)."""
    ctx = Ctx(opts, env)
    only = {c.strip().upper() for c in opts.only.split(",") if c.strip()} if opts.only is not None else None
    skipped, results = [], []
    sink = io.StringIO()
    deadline = getattr(opts, "deadline_s", None)
    end = time.monotonic() + deadline if deadline else None
    old = signal.signal(signal.SIGALRM, _on_alarm) if end is not None and hasattr(signal, "SIGALRM") else None
    try:
        for cid, fn in CHECKS:
            if only is not None and cid not in only:
                continue
            if cid == "C7" and opts.quick:
                skipped.append({"id": cid, "why": "--quick"})
                continue
            if cid == "C6" and (opts.quick or opts.no_write):
                skipped.append({"id": cid, "why": "--quick" if opts.quick else "--no-write"})
                continue
            # The same switch, with the same default, as docker/entrypoint-services.sh: judging is OFF unless on.
            if cid == "C10" and env.get("DENDRA_MINER_JUDGE", "0") != "1":
                skipped.append({"id": cid, "why": "the judge role is off (DENDRA_MINER_JUDGE)"})
                continue
            # The role this identity declares: off is a choice, neither a ko nor advice -- skipped, and said so.
            if cid == "C11" and env.get("DENDRA_MINER_JUDGE", "0") != "1":
                skipped.append({"id": cid, "why": "the judge role is off (DENDRA_MINER_JUDGE): the capacity report declares off"})
                continue
            left = (end - time.monotonic()) if end is not None else None
            if left is not None and left <= 0:
                results.append(result(cid, NAMES[cid], UNMEASURED, f"not run: the self-test's deadline ({deadline:g} s) had passed",
                                      "an earlier check used the time this run was given (--deadline-s)"))
                continue
            try:
                if left is not None:
                    _ALARM["in_check"] = True
                    _arm(left)
                with contextlib.redirect_stdout(sink):
                    r = fn(ctx)
            except DeadlineExceeded:
                r = result(cid, NAMES[cid], UNMEASURED, f"stopped at the self-test's deadline ({deadline:g} s)",
                           "this check had not finished when the time this run was given ran out (--deadline-s)")
            except Exception as e:  # noqa: BLE001
                r = result(cid, NAMES[cid], UNMEASURED, f"the check itself failed: {type(e).__name__}", str(e)[:240])
            finally:
                _ALARM["in_check"] = False
                if left is not None:
                    _arm(0)
            results.append(r)
    finally:
        if old is not None:
            _arm(0)
            signal.signal(signal.SIGALRM, old)
    return ctx, results, skipped


def document(ctx, results, skipped, adv=None):
    now = time.time()
    c = counts(results)
    return {"schema": 1, "tool": "miner_selftest",
            "generated_at": datetime.datetime.fromtimestamp(int(now), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_epoch": int(now), "miner_id": ctx.mid, "identity_source": ctx.mid_source,
            "rc": aggregate(results), "summary": {OK: c[OK], KO: c[KO], UNMEASURED: c[UNMEASURED], "checks": len(results)},
            "skipped": skipped, "checks": results, "advice": list(adv or [])}


def text_lines(doc):
    out = []
    for r in doc["checks"]:
        tag = {OK: "[ok]", KO: "[KO]"}.get(r.get("state"), "[??]")
        out.append(f"  {tag:5} {r.get('id')} {r.get('name', ''):32} : {r.get('measured', '')}")
        if r.get("state") != OK:
            if r.get("reason"):
                out.append(f"          why : {r['reason']}")
            if r.get("fix"):
                out.append(f"          fix : {r['fix']}")
        elif r.get("reason"):
            out.append(f"          note: {r['reason']}")
    for s in doc["skipped"]:
        out.append(f"  [--]  {s['id']} {NAMES[s['id']]:32} : skipped ({s['why']})")
    if doc.get("advice"):
        out.append("  advice (choices this machine's owner can change; they do not change the exit code):")
        for a in doc["advice"]:
            out.append(f"  [advice] {a.get('id')} {a.get('name', ''):28} : {a.get('measured', '')}")
            out.append(f"          do  : {a.get('fix', '')}")
    s = doc["summary"]
    out.append(f"  self-test: {s[OK]} ok, {s[KO]} ko, {s[UNMEASURED]} unmeasured, of {s['checks']} check(s)"
               + ("" if s["checks"] else " -- NO check ran: this is not a green"))
    return out


def heartbeat_probe() -> int:
    """The image's healthcheck: 0 when the daemon wrote its heartbeat within hb.max_age_s(), 1 otherwise.
    Two states only, because Docker has two: it is a display, never the verdict (miner_health.sh is)."""
    doc, why = hb.read()
    if doc is None:
        print(f"unhealthy: {why}")
        return 1
    age = hb.age_s(doc)
    if age is None:
        print("unhealthy: the heartbeat carries no readable written_at")
        return 1
    bound = hb.max_age_s()
    if age > bound:
        print(f"unhealthy: the daemon last wrote its heartbeat {age} s ago (bound {bound} s)")
        return 1
    print(f"healthy: heartbeat {age} s old, phase {doc.get('phase', '?')}")
    return 0


def judge_role_probe(opts) -> int:
    """--judge-role: the judge role this identity declares, read HERE, in the miner container -- where the
    environment that started (or did not start) judge_worker.py, the processes and the judge engine's network
    are. deploy/testnet-miner/publish-capacity.sh asks it before signing the capacity report, and puts the word
    in the report; the host never guesses it. Two lines, always, and nothing else on stdout:
        DENDRA_JUDGE_ROLE <active|mute|off|unknown>
        DENDRA_JUDGE_ROLE_WHY <one line>
    Bounded where it runs (JUDGE_ROLE_DEADLINE_S): a bound on `docker exec` would only kill the client. A
    reading stopped by it, or one that raises, is unknown -- never off. Exit 0 whenever the two lines are
    printed: the word is the answer, unknown included."""
    role, why = "unknown", "not read"
    sink = io.StringIO()
    old = signal.signal(signal.SIGALRM, _on_alarm) if hasattr(signal, "SIGALRM") else None
    env = dict(os.environ)
    try:
        _ALARM["in_check"] = True
        _arm(JUDGE_ROLE_DEADLINE_S)
        with contextlib.redirect_stdout(sink):
            role, why, _fix = judge_role(env, lambda: _running_workers(opts.proc_root),
                                         lambda: judge_engine(env, opts.proc_root))
    except DeadlineExceeded:
        role, why = "unknown", f"not read within {JUDGE_ROLE_DEADLINE_S} s (the bound this reading keeps in the container)"
    except Exception as e:  # noqa: BLE001 -- a reading that fails is unknown, said with its cause
        role, why = "unknown", f"the reading itself failed: {type(e).__name__}: {e}"
    finally:
        _ALARM["in_check"] = False
        _arm(0)
        if old is not None:
            signal.signal(signal.SIGALRM, old)
    if role not in JUDGE_ROLES:
        role, why = "unknown", f"the reading returned a word that is not a role ({str(role)[:40]!r})"
    print(JUDGE_ROLE_LINE + role)
    print(JUDGE_ROLE_WHY_LINE + " ".join(str(why).split())[:300])
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Dendra miner self-test (runs inside the miner container)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--host", action="store_true")
    mode.add_argument("--heartbeat", action="store_true")
    mode.add_argument("--judge-role", action="store_true",
                      help="print the judge role this identity declares (two lines, for publish-capacity.sh)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--deadline-s", type=float, default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--reference-rpc", default="")
    ap.add_argument("--capacity-url", default="")
    ap.add_argument("--node-id", default="")
    ap.add_argument("--id", default="")
    ap.add_argument("--keydir", default="/data/keys")
    ap.add_argument("--height-wait", type=float, default=30.0)
    ap.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    ap.add_argument("--compose", default="", help="the command that reaches this miner's compose project, for the "
                                                  "fixes the report prints (miner_health.sh passes it for a slot k)")
    a = ap.parse_args(argv)
    if "\n" in a.compose or "\r" in a.compose:
        ap.error("--compose is one command line")
    if a.height_wait < 1:
        ap.error("--height-wait must be at least 1 second")
    if a.deadline_s is not None and a.deadline_s < 1:
        ap.error("--deadline-s must be at least 1 second")
    if a.write and a.no_write:
        ap.error("--write and --no-write are two answers to one question: pick one")
    if a.only is not None:
        bad = [c for c in a.only.split(",") if c.strip() and c.strip().upper() not in NAMES]
        if bad:
            ap.error(f"--only: unknown check(s) {', '.join(bad)} (C1..C11)")
    return a


LOCK_FILE_DEFAULT = "/tmp/dendra-miner-selftest.lock"


def _one_at_a_time():
    """An open lock file this process holds, None when another self-test holds it, or False when no lock
    can be taken here at all (no fcntl, an unwritable /tmp) -- then the run goes on, unguarded, as before
    the lock existed. The lock dies with the process: no stale file ever blocks a later run."""
    try:
        import fcntl
    except ImportError:
        return False
    path = os.environ.get("DENDRA_SELFTEST_LOCK") or LOCK_FILE_DEFAULT
    try:
        f = open(path, "a+")  # noqa: SIM115 -- held for the whole run
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main(argv=None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    if opts.heartbeat:
        return heartbeat_probe()
    if opts.judge_role:
        return judge_role_probe(opts)
    lock = _one_at_a_time()
    if lock is None:
        msg = "another self-test is already running in this container"
        print(("DENDRA_SELFTEST_BUSY " if opts.host else "") + msg)
        return 2
    ctx, results, skipped = run_checks(opts, dict(os.environ))
    # Advice is read after the checks and kept OUT of them: it has no state, so it cannot move `rc`.
    try:
        adv = advice(ctx)
    except Exception as e:  # noqa: BLE001 -- advice that cannot be read is said, never a failed check
        adv = [{"id": "A0", "name": "advice", "measured": f"not read ({type(e).__name__}: {e})"[:240], "fix": ""}]
    doc = retarget(document(ctx, results, skipped, adv), opts.compose)
    rc = doc["rc"]
    if opts.json:
        print(json.dumps(doc, ensure_ascii=True))
    elif opts.host:
        # THE LINE PROTOCOL OF miner_health.sh, which runs on a host that may carry no JSON parser: the
        # document on one line, the report as text, and a last line with the counts, as words. The host
        # never reads the JSON; it carries it, and reads the counts from the END line.
        print("DENDRA_SELFTEST_JSON " + json.dumps(doc, ensure_ascii=True))
        for line in text_lines(doc):
            print("DENDRA_SELFTEST_TEXT " + line)
        s = doc["summary"]
        print(f"DENDRA_SELFTEST_END rc={rc} ok={s[OK]} ko={s[KO]} unmeasured={s[UNMEASURED]} checks={s['checks']}")
    else:
        print(f"== Dendra miner self-test: {ctx.mid or '?'} ({ctx.mid_source or 'identity unknown'}) ==")
        for line in text_lines(doc):
            print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
