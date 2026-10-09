#!/usr/bin/env python3
"""REVEAL (PRIMARY side), as a STANDALONE process (no change to miner).

Run by a miner ALONGSIDE `miner.py` (same --id/--keydir). Loop:
  1. discover via `list-job` the `+disputed` jobs where this miner is the primary (miner_id == me);
  2. read the jury ANCHORED on the job (`dendrad query jobs audit-committee`, `reveal_helpers.read_jury`)
     and decide (`reveal_helpers.decide_reveal`): seal to that jury, or seal NOTHING now -- jury
     unreadable, not anchored yet, empty, or a human dispute (ADR-033) -- and read it again later;
  3. fetch from the relay the sealed prompt (`req/<jid>__<me>`) AND my sealed answer (`res/<jid>__<me>`);
  4. RE-DECRYPT them with my X25519 key (same ECDH+AAD as handle_job: info = job_id) -- the cleartext
     is never re-read from disk, only re-derived in RAM long enough to re-seal;
  5. RE-SEAL (prompt + answer) to each anchored juror and post `reveal/<jid>__<juror>__<me>`. The key is
     the one the juror anchored on-chain; for a juror that anchored none, the key cached at the relay
     (`pub/<mid>`), which the relay serves without proof of who wrote it (`reveal_helpers.committee_pubs`).
     A registered miner outside the jury receives nothing.

The jurors (`judge_worker.py`) open the reveal, recompute, judge, and commit their verdicts on-chain.
Confidentiality: revealing exposes content only for the jobs the chain samples for audit
(`audit_sample_bps`), sealed per juror (an accepted trade-off). This worker ignores the miner's `drain`
file on purpose: a draining miner stops taking work, it does not stop owing the reveals of work it
already served.

Usage: python3 reveal_worker.py --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea import keyring as kring
from modea import dendrad_argv as da
from modea import chain_id as _cid
from modea.crypto import Sealed
import relay_client as relay
import reveal_helpers as rv
import reveal_marker as rmk

NODE = os.environ.get("DENDRA_NODE", "")
# ADR-048 item 8: the chain id is READ (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
# EXPLICIT keyring directory. Without it, `dendrad` looks in its default home, which is exactly how a
# batch of audits becomes unresolvable: the keys live elsewhere, the miner never signs, so nobody
# notices - the judge is the first component that SIGNS.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")
HOME_DIR = os.environ.get("DENDRA_HOME", "")


def _node():
    return ["--node", NODE] if NODE else []


def _keys():
    """The home flag. The keyring flags come from `_keyring()`: backend and directory, read from the disk."""
    return ["--home", HOME_DIR] if HOME_DIR else []


# THE KEYRING, AS THE DAEMON READS IT (modea/keyring.py): the backend from the state of the disk, and in
# `file` mode the passphrase, handed to dendrad on stdin -- the same source for every process that signs
# for this miner, so the three cannot disagree on which key is theirs.
_KR = None


def _keyring():
    global _KR
    if _KR is None:
        _KR = kring.resolve(KEYRING_DIR or HOME_DIR or None)
    return _KR


def run(c, t=120, stdin=""):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t, input=stdin)
    return (r.stdout or "") + (r.stderr or "")


def run_rc(c, t=60):
    """`(returncode, stdout, stderr)`, the three apart -- what the jury reader needs.

    `run()` glues the two streams, which suits a transaction whose output is searched and not a query
    whose JSON is parsed: any line on stderr (a toolchain warning, a deprecation notice) appended to the
    JSON makes it unparsable, and the NotFound status lives on stderr only. A command that cannot run
    at all answers `(None, "", reason)`, never an invented exit code."""
    try:
        r = subprocess.run(c, capture_output=True, text=True, timeout=t)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", f"{type(e).__name__}: {e}"
    return r.returncode, r.stdout or "", r.stderr or ""


def tx_from(frm, sub, *positionals, flags=()):
    """A transaction signed by the miner. Robust NONCE handling: the account is shared between
    processes (miner, reveal_worker, judge_worker), so retry on "account sequence mismatch".

    Positionals go after `--` (modea/dendrad_argv.py): a Merkle root is hex today and cannot start
    with `-`, but the terminator is not conditioned on that — the sibling worker anchored nothing for
    weeks because one value could."""
    cmd = da.dendrad_argv(
        ("dendrad", "tx", "jobs"), sub, positionals,
        [*flags, "--from", frm, *_keyring().flags(), "--chain-id", _cid.chain_id(tuple(_node())),
         "--gas", "auto", "--gas-adjustment", "1.6", "--yes", *_keys(), *_node()])
    o = ""
    for attempt in range(6):
        o = run(cmd, stdin=_keyring().stdin())
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    return o


def _tx_ok(t):
    m = re.search(r'(^|\n)code: (\d+)', t or "")
    return bool(m) and m.group(2) == "0"


def current_height():
    """The current block height, or None. None is NEVER treated as 0: without a height we do not know
    which epoch we are in, so we do not anchor. Better no marker at all than a marker filed under the
    wrong epoch, where no judge would find it."""
    out = run(["dendrad", "status", "--output", "json", *_node()], t=20)
    try:
        d = json.loads(out)
    except Exception:
        m = re.search(r'"?latest_block_height"?[:=]\s*"?(\d+)', out or "")
        return int(m.group(1)) if m else None
    for path in (("sync_info", "latest_block_height"), ("SyncInfo", "latest_block_height"),
                 ("sync_info", "latestBlockHeight")):
        node = d
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if node:
            try:
                return int(node)
            except (TypeError, ValueError):
                pass
    return None


def anchor_marker(miner_id, relay_url, epoch, job_ids):
    """TIER 1: anchors ONE marker for the elapsed epoch, and publishes the leaf list to the relay.

    The order is deliberate: the LIST first, the ROOT second. If anchoring fails, what remains is an
    orphaned list - harmless, since nobody looks for it. The other way round would leave an anchored,
    immutable root with no list, hence a permanently unverifiable marker, which would push every judge
    to `None` forever for that epoch.

    Idempotent: an already-anchored commit is refused by the chain (commits are immutable), so that
    refusal is not treated as an error.
    """
    jobs = sorted(set(job_ids))
    if not jobs:
        return False, "no reveal in this epoch: nothing to anchor (no empty marker)"
    root = rmk.merkle_root(jobs)
    payload = {"epoch": int(epoch), "miner": miner_id, "root": root, "jobs": jobs}
    if not relay.put(relay_url, "reveal", rmk.list_key(epoch, miner_id), payload):
        return False, "the relay REFUSED the list: an unverifiable root is not anchored"
    key = rmk.marker_key(epoch, miner_id)
    out = tx_from(miner_id, "create-commit", key, root, str(len(jobs)), rmk.MARKER_KIND)
    if _tx_ok(out):
        return True, f"{key} root {root[:16]}... ({len(jobs)} job(s))"
    low = " ".join((out or "").split()).lower()
    if "already" in low or "exists" in low:
        return True, f"{key} already anchored (commits are immutable) - nothing to redo"
    return False, " ".join((out or "(empty)").split())[:300]


def _norm_keys(d):
    """Adds snake_case aliases for the camelCase keys of a flat CLI dict (jobId->job_id,
    encPubkey->enc_pubkey). dendrad may emit camelCase; reading snake_case ONLY leaves the primary
    unable to find ITS OWN +disputed jobs, so the reveal is never posted and an honest miner is slashed
    over a spelling of case. Normalization happens at the edge, and enc_pubkey - the root of trust of
    the reveal - follows the same rule."""
    if not isinstance(d, dict):
        return d
    out = dict(d)
    for k, v in list(d.items()):
        snake = ""
        for ch in k:
            snake += ("_" + ch.lower()) if ch.isupper() else ch
        snake = snake.lstrip("_")
        if snake and snake not in out:
            out[snake] = v
    return out


def list_jobs():
    # PAGINATED and fail-closed: see `rv.query_all`. Without pagination, past 100 jobs the primary
    # stops seeing ITS OWN disputed audits, therefore does not reveal, therefore is slashed for a
    # silence it did not choose.
    rows = rv.query_all("list-job", "job", _node(), run)
    if rows is None:
        return None
    return [((jj := _norm_keys(j)).get("job_id", ""), jj.get("state", ""), jj.get("miner_id", "")) for j in rows]


def list_miners():
    """FULL miner-registry records ({miner_id, enc_pubkey, ...}).

    The WHOLE record is kept because `enc_pubkey` is the on-chain ANCHORED reveal key = the root of
    trust. Discarding it forces the reveal path onto the relay's volatile pub cache, where a single
    relay restart leaves honest miners unable to seal a reveal — and the committee charges them for
    that silence. The anchored key is already in this very response; dropping it buys nothing."""
    # Same defect, same remedy: `list-miner` also stops at 100. The current target is a few dozen
    # miners, so it is not imminent - but it is the SAME fault, and fixing it costs one line.
    rows = rv.query_all("list-miner", "miner", _node(), run)
    if rows is None:
        return None
    return [mm for mm in (_norm_keys(m) for m in rows) if mm.get("miner_id")]


def list_miner_ids():
    """Legacy id-only view (kept for callers that do not need the anchored keys)."""
    rows = list_miners()
    return [] if rows is None else [m.get("miner_id", "") for m in rows]


def is_disputed(state: str) -> bool:
    return "+disputed" in state and "+resolved" not in state


def _decrypt_from_relay(relay_url, kind, key, sk, eph_pk_hex, aad):
    """Re-decrypts an envelope {client_eph_pk?,nonce,ct} stored at the relay with MY X25519 key."""
    blob = relay.get(relay_url, kind, key)
    if not blob or "ct" not in blob:
        return None
    eph_hex = blob.get("client_eph_pk", eph_pk_hex)
    if not eph_hex:
        return None
    k = crypto.derive_session_key(sk, bytes.fromhex(eph_hex), info=aad)
    try:
        pt = crypto.decrypt(k, Sealed(bytes.fromhex(blob["nonce"]), bytes.fromhex(blob["ct"])), aad=aad)
        return pt.decode("utf-8", "replace"), eph_hex
    finally:
        crypto.zeroize(bytearray(k))


# Outcomes of `reveal_disputed_job` that are not a decision of `reveal_helpers.decide_reveal`.
REVEALED = "revealed"                 # every keyed juror holds a copy: the job is done
INCOMPLETE = "incomplete"             # some keyed jurors refused or missed: retried next round
REQ_UNREADABLE = "req-unreadable"     # the client's sealed prompt is not readable at the relay
RES_UNREADABLE = "res-unreadable"     # this miner's sealed answer is not readable at the relay
REGISTRY_UNREADABLE = "registry-unreadable"
NO_KEY = "no-key"                     # no anchored juror has a usable key anywhere: nothing can be sealed
JOB_ERROR = "job-error"               # the reveal of this job raised: retried after a back-off, alone


class Outcome(NamedTuple):
    action: str             # a `reveal_helpers` decision other than SEAL, or one of the outcomes above
    delivered: int          # deposits accepted by the relay (stored, or already stored)
    targets: tuple          # jurors a copy was sealed for
    unkeyed: tuple          # anchored jurors with no usable key anywhere: they will find no copy
    detail: str
    plan: object = None     # the SEAL plan this outcome was reached under (None when nothing was read)


def reveal_disputed_job(my_id, relay_url, sk, job_id, *, node_args=None, runner=None, miners_fn=None,
                        plan=None):
    """ONE disputed job where this miner is the primary: read the jury, then seal to it -- or not.

    The order is part of the guarantee. The JURY is read FIRST, before anything is decrypted: a job
    whose jury is unreadable, not anchored, empty or a human dispute never brings its cleartext back
    into memory, and no state of that read leads to a target list built from the registry.
    `node_args`, `runner` and `miners_fn` default to this module's own (`_node()`, `run_rc`,
    `list_miners`), looked up at CALL time so a bench drives the shipped path, not a copy of it.

    `plan` is a SEAL plan already read for THIS job (`main()` keeps it): the jury is then not read
    again. Anything else is ignored and the jury is read. Every outcome reached under a SEAL plan carries
    it back, so the caller can keep it.
    """
    node_args = _node() if node_args is None else node_args
    runner = runner or run_rc
    miners_fn = miners_fn or list_miners
    if not (isinstance(plan, rv.RevealPlan) and plan.action == rv.SEAL and plan.members):
        plan = rv.plan_reveal(job_id, my_id, node_args, runner)
    if plan.action != rv.SEAL:
        return Outcome(plan.action, 0, (), (), plan.reason)
    relay_key = f"{job_id}__{my_id}"   # relay storage key (req/res)
    aad = job_id.encode()  # handle_job: info/aad = BARE jobId (not the relay key) -> what the client sealed
    # prompt sealed by the CLIENT (req) -> client_eph_pk carried by the req envelope
    # `_decrypt_from_relay` returns None for BOTH a relay that does not answer and a blob with no `ct`.
    # Either way the reveal is impossible: the jurors find nothing to judge, and the primary is not paid
    # for a job it served -- or is slashed, where jurors vote a missing reveal invalid. So the cause is
    # named: an unreadable relay must not look like "nothing to do".
    pr = _decrypt_from_relay(relay_url, "req", relay_key, sk, None, aad)
    if not pr:
        return Outcome(REQ_UNREADABLE, 0, (), (), f"req/{relay_key} UNREADABLE at the relay (absent, or no 'ct')",
                       plan)
    prompt, client_eph = pr
    # my answer (res): sealed by ME with the SAME session key (same client eph + aad)
    rr = _decrypt_from_relay(relay_url, "res", relay_key, sk, client_eph, aad)
    if not rr:
        return Outcome(RES_UNREADABLE, 0, (), (), f"res/{relay_key} UNREADABLE at the relay (absent, or no 'ct')",
                       plan)
    answer, _ = rr
    mi = miners_fn()
    if mi is None:
        return Outcome(REGISTRY_UNREADABLE, 0, (), (), "list-miner UNREADABLE: the jurors' anchored keys are unknown",
                       plan)
    pubs = rv.committee_pubs(relay_url, my_id, mi, jury=plan.members)
    unkeyed = tuple(m for m in plan.members if m not in pubs)
    if not pubs:
        return Outcome(NO_KEY, 0, (), unkeyed, f"none of the {len(plan.members)} anchored juror(s) has a usable key",
                       plan)
    # The salt is RE-DERIVED here rather than carried: the daemon that anchored the commitment and this
    # worker load the same key file, so they agree without any shared state -- and a value that is
    # never stored cannot be lost between the two.
    n = rv.reveal_job(relay_url, job_id, prompt, answer, pubs, author_id=my_id,
                      psalt=crypto.prompt_salt(sk, job_id))
    # `reveal_job` returns the COUNT of ACCEPTED deposits, so zero is a TOTAL failure and not a reveal.
    # A PARTIAL REVEAL IS NOT A REVEAL: the job is only retired once EVERY keyed juror has it. `reveal`
    # is WRITE_ONCE, and an audit that cannot read every share never concludes -- the primary's held fee
    # then stays frozen on a job it actually served. Retrying is safe and converges, because a deposit
    # already stored answers "exists", which counts as accepted.
    action = REVEALED if n >= len(pubs) else INCOMPLETE
    return Outcome(action, n, tuple(sorted(pubs)), unkeyed, f"{n} of {len(pubs)} keyed juror(s)", plan)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--epoch-blocks", type=int, default=rmk.EPOCH_BLOCKS_DEFAULT,
                    help="size of the reveal-marker epoch, in blocks. The design is one tx per epoch "
                         "per miner, NEVER one tx per job. It must be IDENTICAL across every worker "
                         "and every judge, otherwise the epochs do not line up.")
    ap.add_argument("--no-marker", action="store_true",
                    help="anchor NO marker (an operational fallback; tier 1 is then absent and judges "
                         "fall back on tier 2 alone)")
    a = ap.parse_args()

    skpath = Path(a.keydir) / f"{a.id}.sk"
    if not skpath.exists():
        print(f"[reveal] FATAL: X25519 key {skpath} is missing - run miner.py --id {a.id} first")
        sys.exit(3)
    # The key file is opened with the KEYRING's passphrase (modea/keyring.py), the one the daemon sealed it
    # under; it is never re-encrypted from here -- one writer per file, the daemon.
    try:
        sk = crypto.load_sk(str(skpath), _keyring().open_with)
    except (kring.KeyringError, crypto.KeyEnvelopeError) as e:
        print(f"[reveal] FATAL: the keys cannot be opened -- {e} {getattr(e, 'hint', '')}".rstrip(), flush=True)
        sys.exit(3)

    print(f"[reveal] reveal worker {a.id} ready (relay {a.relay}) - revealing the +disputed jobs where I am primary, "
          f"sealed to the jury ANCHORED on each job and to nobody else")
    print(f"[reveal] tier-1 marker: {'DISABLED (--no-marker)' if a.no_marker else f'epoch = {a.epoch_blocks} blocks'}")
    done = set()
    noted = {}        # job_id -> last outcome printed: one line per CHANGE of outcome, not one per round
    attempts = {}     # job_id -> consecutive attempts that sealed nothing (drives the back-off)
    next_try = {}     # job_id -> monotonic time before which the job is not tried again
    # job_id -> the SEAL plan read for it. The audit anchor of a job has ONE writer
    # (`audit_sampling.go::runOptimisticAudit`, through `miner_vitality.go::anchorCommittee`) and is
    # removed only by `miner_vitality.go::clearAuditCommittee`, whose callers mark the job resolved in the
    # same step; a resolved job leaves this loop and its plan is dropped with it. So once read, the jury
    # is not read again: re-reading it would start one `dendrad` process per round, on top of the relay
    # read, for every job stuck at the relay. A plan that seals nothing is NOT kept -- that jury may
    # still be seated.
    juries = {}
    pending = {}      # epoch -> set(job_id) revealed in that epoch, not yet anchored
    cur_epoch = None
    while True:
        try:
            # --- TIER 1: anchor the ELAPSED epochs --------------------------------------------
            # The current epoch is never anchored: it can still receive reveals, and an immutable
            # marker placed too early would permanently exclude the jobs revealed after it.
            if not a.no_marker:
                h = current_height()
                if h is not None:
                    e = rmk.epoch_of(h, a.epoch_blocks)
                    if cur_epoch is not None and e != cur_epoch:
                        for closed in [x for x in sorted(pending) if x < e]:
                            ok, why = anchor_marker(a.id, a.relay, closed, pending[closed])
                            print(f"[reveal] {a.id} epoch {closed} marker: "
                                  f"{'anchored' if ok else 'FAILED'} - {why}")
                            if ok:
                                pending.pop(closed, None)
                            # On failure the epoch is KEPT and retried next round; otherwise a network
                            # incident would silently erase the proof of activity.
                    cur_epoch = e
            _rows = list_jobs()
            if _rows is None:
                print(f"[reveal] {a.id} list-job UNREADABLE - round skipped (not measured, not 'nothing to do')")
                # `--poll` is the only interval this parser declares. Using any other attribute name
                # raises AttributeError and KILLS the worker on the very path meant to survive a
                # transient read failure — printed warning first, silent death second. judge_worker.py
                # carries the same loop and must keep the same name.
                time.sleep(a.poll)
                continue
            mine = set()      # this round's open disputed jobs where this miner is the primary
            for job_id, state, primary in _rows:
                if not is_disputed(state) or primary != a.id or job_id in done:
                    continue
                if not rv.safe_job_id(job_id):   # job_id from on-chain -> relay key/aad
                    print(f"[reveal] {a.id} NON-CONFORMING job_id ignored (defensive): {str(job_id)[:40]!r}")
                    done.add(job_id)
                    continue
                mine.add(job_id)
                if next_try.get(job_id, 0.0) > time.monotonic():
                    continue
                try:
                    o = reveal_disputed_job(a.id, a.relay, sk, job_id, plan=juries.get(job_id))
                except Exception as e:
                    # ONE JOB'S FAILURE STAYS ITS OWN. Raised from here, it would reach the round's
                    # handler below and stop the reveal of every job listed after this one, at every
                    # round: one value the relay serves (a key, an envelope) would silence every other
                    # audit of this primary.
                    o = Outcome(JOB_ERROR, 0, (), (), f"{type(e).__name__}: {e}"[:200])
                if o.plan is not None:
                    juries[job_id] = o.plan
                if o.action == REVEALED:
                    # `done` is set on a COMPLETE delivery only: retiring the job earlier would retire
                    # the ONLY remaining chance to post the reveal, on the exact path that decides
                    # whether the primary is paid or slashed.
                    extra = (f"; {len(o.unkeyed)} anchored juror(s) with no usable key anywhere get no "
                             f"copy: {' '.join(o.unkeyed)}") if o.unkeyed else ""
                    print(f"[reveal] {a.id} revealed {job_id} to {o.delivered} anchored juror(s){extra}")
                    done.add(job_id)
                    for d in (noted, attempts, next_try, juries):
                        d.pop(job_id, None)
                    if not a.no_marker and cur_epoch is not None:
                        pending.setdefault(cur_epoch, set()).add(job_id)
                    continue
                if o.action == INCOMPLETE:
                    print(f"[reveal] {a.id} reveal INCOMPLETE for {job_id}: {o.detail} accepted the deposit "
                          f"- job KEPT, retried next round")
                    continue
                if o.action in (REQ_UNREADABLE, RES_UNREADABLE, REGISTRY_UNREADABLE):
                    # Retried every round, named ONCE per job and outcome: an unnamed repeat would drown
                    # the log that exists to carry the diagnosis.
                    if noted.get(job_id) != o.action:
                        noted[job_id] = o.action
                        print(f"[reveal] {a.id} {o.detail} - {job_id} CANNOT be revealed now; its jurors find "
                              f"no reveal to judge until it is; retried every round")
                    continue
                # Retried after a back-off, never abandoned, until the job is resolved: a jury-level
                # decision (a jury seated later by a deferred lottery must still be served), no juror
                # with a usable key (a juror can anchor one later), or a reveal that raised.
                k = attempts.get(job_id, 0)
                attempts[job_id] = k + 1
                next_try[job_id] = time.monotonic() + rv.recheck_delay(k, a.poll)
                if noted.get(job_id) != o.action:
                    noted[job_id] = o.action
                    if o.action == JOB_ERROR:
                        # Not "nothing sealed": the exception may have come after some deposits.
                        print(f"[reveal] {a.id} {job_id}: the reveal FAILED ({o.detail}) - job KEPT, retried "
                              f"after a back-off (up to {rv.RECHECK_CAP_S:.0f}s); the other jobs go on")
                    else:
                        print(f"[reveal] {a.id} {job_id}: NOTHING sealed ({o.action}) - {o.detail}; never "
                              f"sealed to the registry instead; retried after a back-off (up to "
                              f"{rv.RECHECK_CAP_S:.0f}s)")
            # Bookkeeping of a job that left the set (resolved, or no longer this miner's) is dropped:
            # a resolved job never comes back, and the maps stay bounded by the open audits.
            for d in (noted, attempts, next_try, juries):
                for k_ in [k_ for k_ in d if k_ not in mine]:
                    d.pop(k_, None)
        except Exception as e:
            print(f"[reveal] {a.id} loop: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
