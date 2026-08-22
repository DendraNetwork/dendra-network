#!/usr/bin/env python3
"""REVEAL (PRIMARY side), as a STANDALONE process (no change to miner).

Run by a miner ALONGSIDE `miner.py` (same --id/--keydir). Loop:
  1. discover via `list-job` the `+disputed` jobs where I am the primary (miner_id == me);
  2. fetch from the relay the sealed prompt (`req/<jid>__<me>`) AND my sealed answer (`res/<jid>__<me>`);
  3. RE-DECRYPT them with my X25519 key (same ECDH+AAD as handle_job: info = job_id) -- the cleartext
     is never re-read from disk, only re-derived in RAM long enough to re-seal;
  4. RE-SEAL (prompt + answer) to each committee member (pubs from the relay) and post `reveal/<jid>__<mid>`.

The committee (`judge_worker.py`) opens the reveal, recomputes, judges, and commits its verdict on-chain.
Confidentiality: revealing exposes content only for the ~10% AUDITED, sealed per member (an accepted trade-off).

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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea import dendrad_argv as da
from modea.crypto import Sealed
import relay_client as relay
import reveal_helpers as rv
import reveal_marker as rmk

NODE = os.environ.get("DENDRA_NODE", "")
CHAIN = "dendra"
# EXPLICIT keyring directory. Without it, `dendrad` looks in its default home, which is exactly how a
# batch of audits becomes unresolvable: the keys live elsewhere, the miner never signs, so nobody
# notices - the judge is the first component that SIGNS.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")
HOME_DIR = os.environ.get("DENDRA_HOME", "")


def _node():
    return ["--node", NODE] if NODE else []


def _keys():
    out = []
    if KEYRING_DIR:
        out += ["--keyring-dir", KEYRING_DIR]
    if HOME_DIR:
        out += ["--home", HOME_DIR]
    return out


def run(c, t=120):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t)
    return (r.stdout or "") + (r.stderr or "")


def tx_from(frm, sub, *positionals, flags=()):
    """A transaction signed by the miner. Robust NONCE handling: the account is shared between
    processes (miner, reveal_worker, judge_worker), so retry on "account sequence mismatch".

    Positionals go after `--` (modea/dendrad_argv.py): a Merkle root is hex today and cannot start
    with `-`, but the terminator is not conditioned on that — the sibling worker anchored nothing for
    weeks because one value could."""
    cmd = da.dendrad_argv(
        ("dendrad", "tx", "jobs"), sub, positionals,
        [*flags, "--from", frm, "--keyring-backend", "test", "--chain-id", CHAIN,
         "--gas", "auto", "--gas-adjustment", "1.6", "--yes", *_keys(), *_node()])
    o = ""
    for attempt in range(6):
        o = run(cmd)
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

    passphrase = os.environ.get("DENDRA_MINER_PASSPHRASE", "")
    skpath = Path(a.keydir) / f"{a.id}.sk"
    if not skpath.exists():
        print(f"[reveal] FATAL: X25519 key {skpath} is missing - run miner.py --id {a.id} first")
        sys.exit(3)
    sk = crypto.load_sk(str(skpath), passphrase)

    print(f"[reveal] reveal worker {a.id} ready (relay {a.relay}) - revealing the +disputed jobs where I am primary")
    print(f"[reveal] tier-1 marker: {'DISABLED (--no-marker)' if a.no_marker else f'epoch = {a.epoch_blocks} blocks'}")
    done = set()
    warned = set()    # job_id already named as unreadable at the relay (one line per job, not per round)
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
            for job_id, state, primary in _rows:
                if not is_disputed(state) or primary != a.id or job_id in done:
                    continue
                if not rv.safe_job_id(job_id):   # job_id from on-chain -> relay key/aad
                    print(f"[reveal] {a.id} NON-CONFORMING job_id ignored (defensive): {str(job_id)[:40]!r}")
                    done.add(job_id)
                    continue
                relay_key = f"{job_id}__{a.id}"   # relay storage key (req/res)
                aad = job_id.encode()  # handle_job: info/aad = BARE jobId (not the relay key) -> what the client sealed
                # prompt sealed by the CLIENT (req) -> client_eph_pk carried by the req envelope
                # `_decrypt_from_relay` returns None for BOTH a relay that does not answer and a blob
                # with no `ct`. Either way the reveal is impossible and the committee judges the job
                # INVALID - the primary is slashed for a silence it did not choose. So the cause is
                # named: an unreadable relay must not look like "nothing to do".
                # Named ONCE per job: the job stays open and the round retries every `--poll`, so an
                # unnamed repeat would drown the log that exists to carry the diagnosis.
                pr = _decrypt_from_relay(a.relay, "req", relay_key, sk, None, aad)
                if not pr:
                    if job_id not in warned:
                        warned.add(job_id)
                        print(f"[reveal] {a.id} req/{relay_key} UNREADABLE at the relay (absent, or no 'ct') "
                              f"- {job_id} CANNOT be revealed and will be judged invalid")
                    continue
                prompt, client_eph = pr
                # my answer (res): sealed by ME with the SAME session key (same client eph + aad)
                rr = _decrypt_from_relay(a.relay, "res", relay_key, sk, client_eph, aad)
                if not rr:
                    if job_id not in warned:
                        warned.add(job_id)
                        print(f"[reveal] {a.id} res/{relay_key} UNREADABLE at the relay (absent, or no 'ct') "
                              f"- {job_id} CANNOT be revealed and will be judged invalid")
                    continue
                answer, _ = rr
                _mi = list_miners()
                if _mi is None:
                    print(f"[reveal] {a.id} list-miner UNREADABLE - not sealing {job_id} against a partial registry")
                    continue
                pubs = rv.committee_pubs(a.relay, a.id, _mi)
                if not pubs:
                    print(f"[reveal] {a.id} no peer to reveal {job_id} to "
                          f"(no on-chain anchored key and no relay cache - empty miner registry?)")
                    continue
                # The salt is RE-DERIVED here rather than carried: the daemon that anchored the
                # commitment and this worker load the same key file, so they agree without any shared
                # state -- and a value that is never stored cannot be lost between the two.
                n = rv.reveal_job(a.relay, job_id, prompt, answer, pubs,
                                  author_id=a.id,
                                  psalt=crypto.prompt_salt(sk, job_id))
                # `reveal_job` returns the COUNT of ACCEPTED deposits, so zero is a TOTAL failure and
                # not a reveal. A count is never printed under the verb that asserts success without
                # being tested against zero, and `done` is never set on it: marking the job done would
                # retire the ONLY remaining chance to post the reveal, on the exact path that decides
                # whether the primary is paid or slashed. Kept open -> the next round retries.
                # A PARTIAL REVEAL IS NOT A REVEAL, so the job is only retired once EVERY member has
                # it. `reveal` is WRITE_ONCE: a member that refused the deposit can never be given it
                # later, and an audit that cannot read every share never concludes -- the primary's
                # held fee then stays frozen on a job it actually served. Retrying is safe and
                # converges, because a deposit already stored answers "exists", which counts as
                # accepted: only the missing members are attempted again.
                if n < len(pubs):
                    print(f"[reveal] {a.id} reveal INCOMPLETE for {job_id}: {n} of {len(pubs)} "
                          f"committee member(s) accepted the deposit - job KEPT, retried next round")
                    continue
                print(f"[reveal] {a.id} revealed {job_id} to {n} committee member(s)")
                done.add(job_id)
                if not a.no_marker and cur_epoch is not None:
                    pending.setdefault(cur_epoch, set()).add(job_id)
        except Exception as e:
            print(f"[reveal] {a.id} loop: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
