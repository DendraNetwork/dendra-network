#!/usr/bin/env python3
"""final_season_miner.py — the miner's side of the Final Testnet Season (ADR-047): where its rewards go.

    python3 final_season_miner.py payout  [--miner <id>] --address dendra1... [--lock]
    python3 final_season_miner.py status
    python3 final_season_miner.py payout-prepare --owner dendra1<owner> --address dendra1... > payout.json
    python3 final_season_miner.py payout-submit < payout.signed.json

The programme sets the miner no test. Everything it pays is read from the chain — the programme's
requests the miner served and that were verified, its verdicts as a drawn juror, the availability windows
it proved (the daemon proves them on its own, `miner.prove_availability_once`) — so the only thing
a miner says to the programme is where its rewards go. Without a declaration they go to its operator.

The declaration is signed with the miner's operator key, exactly like a relay deposit
(`relay_client._signature`): the service attributes it to the operator the chain names for that miner.
Inside the miner's container the identity defaults to the one the daemon resolved
(`/data/keys/identite-resolue`, a `dm1...` id): the daemon RENAMES the keyring entry to it, so the name
`DENDRA_SIGN_KEY` was set to at start-up no longer exists, and signing must follow the rename.

A MINER IN OWNER MODE DECLARES WITH ITS OWNER'S KEY, WHICH IS NOT ON THIS MACHINE (`deploy/join.sh --owner`).
The programme accepts a declaration only from the miner's on-chain creator when the creator and the
operator differ (final_season_server.py), so `payout` -- signed by the operator key -- is refused there.
Three steps instead, and only the middle one needs the owner key:
  payout-prepare  reads the height the programme reports and the owner account's number and sequence,
                  writes the transaction to sign on STDOUT (the very document `modea.relay_signature`
                  signs for a deposit: a chain id no chain accepts, so it can never be broadcast) and keeps
                  what the submission needs in the miner's volume; the sign command goes to stderr;
  (owner's machine) dendrad tx sign ... --offline -- the command is printed complete;
  payout-submit   reads the signed transaction on STDIN, checks that it signs THIS declaration and that
                  the key that signed it is the owner's, then sends it.
The programme refuses a signature whose height has left its replay window (blocks, read from
`relay_antireplay.Antireplay`'s window, not a time): a declaration prepared and submitted much later is
prepared again.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROGRAMME = os.environ.get("DENDRA_FINAL_SEASON_URL", "").rstrip("/")
RESOLVED = "/data/keys/identite-resolue"     # written by miner once the identity is resolved
# What payout-submit needs to send a declaration the owner signed elsewhere. Nothing secret: the body, the
# height, the owner's address and the account number and sequence the signature covers. In the miner's
# volume, next to the identity it is about.
PENDING = "payout-pending.json"


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None, timeout=20):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        # unreachable, timed out, or an answer that is not JSON (a proxy's page): said, never a traceback
        return 0, {"error": f"{url}: {type(e).__name__}: {e}"}


def _sign(kind: str, key: str, body: bytes, miner_id: str, height: int) -> dict:
    import relay_client
    return relay_client._signature(kind, key, body, miner_id, height)


def status(base: str) -> tuple:
    return _http("GET", f"{base}/status")


def _payout_body(miner_id: str, address: str, lock: bool = False) -> bytes:
    doc = {"address": address, "miner_id": miner_id}
    if lock:
        # INSIDE the signed body: a lock that travelled beside the signature could be added or removed
        # by whoever relays the request.
        doc["lock"] = True
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def declare_payout(base: str, miner_id: str, address: str, sign=_sign, lock: bool = False) -> tuple:
    """(http code, answer). Signed at the chain height the service reports, like a relay deposit.

    `lock=True` asks the programme to LOCK the address: once filed, any other address is refused (409) for
    this identity, and the lock cannot be lifted (final_season_server.py, the payout route). The programme
    grants it only on the identity's FIRST declaration, or to its owner's key in owner mode when every earlier
    declaration was made in owner mode too: otherwise it answers 409 with `"lock_refused": true` and files
    nothing."""
    body = _payout_body(miner_id, address, lock)
    code, st = status(base)
    h = st.get("height") if code == 200 else None
    if not isinstance(h, int) or h <= 0:
        # The signature commits to a height; one guessed here would be signed and refused, or worse,
        # accepted at a height the service never reported. Nothing is signed without the reading.
        return 0, {"error": f"the service's height is unknown (status {code}): nothing was signed"}
    headers = sign("fspay", f"payout__{miner_id}", body, miner_id, h)
    if not headers:
        return 0, {"error": "cannot sign (see the relay signing message above)"}
    return _http("POST", f"{base}/payout", body, headers)


def record_declaration(keydir: str, miner_id: str, address: str, *, source: str, setting: str | None = None,
                       locked: bool = False) -> bool:
    """Writes, in the miner's volume, the declaration the programme just ACCEPTED (and only then): the
    miner's self-test reads it to say whether the season still pays this machine's own key, and the daemon
    to declare DENDRA_PAYOUT_ADDRESS again only when that setting changes. `setting` is the
    DENDRA_PAYOUT_ADDRESS value behind the declaration; a declaration made by hand keeps the previous one,
    so a setting that did not change is not declared over it. False when the volume is not there."""
    from modea import crypto
    from modea import keyring as kring
    if not keydir or not os.path.isdir(keydir):
        return False
    prev = kring.payout_record(keydir) or {}
    if setting is None:
        setting = str(prev.get("setting") or "") if prev.get("miner_id") == miner_id else ""
    doc = {"miner_id": miner_id, "address": address, "source": source, "setting": setting,
           "at": int(time.time())}
    if locked:
        # Only what the programme ANSWERED: its "locked": true. A record never says locked on a guess.
        doc["locked"] = True
    try:
        crypto.write_private(os.path.join(keydir, kring.PAYOUT_RECORD), json.dumps(doc).encode("utf-8"))
    except OSError:
        return False
    return True


# A lock the programme REFUSED (409, "lock_refused": true): nothing was filed, so `record_declaration` writes
# nothing, and a watcher that reads only that record (docker/cloud-start.sh) would wait for a declaration that
# will never come. This record says what the programme answered, for this identity and this address, and
# nothing more: the declaration before it, if any, stands.
LOCK_REFUSED = "payout-lock-refused.json"


def record_lock_refusal(keydir: str, miner_id: str, address: str) -> bool:
    """Writes, in the miner's volume, that the programme refused to LOCK `address` for `miner_id`. False when
    the volume is not there or the write fails."""
    from modea import crypto
    if not keydir or not os.path.isdir(keydir):
        return False
    doc = {"miner_id": miner_id, "address": address, "refused": "lock_refused", "at": int(time.time())}
    try:
        crypto.write_private(os.path.join(keydir, LOCK_REFUSED), json.dumps(doc).encode("utf-8"))
    except OSError:
        return False
    return True


def prepare_payout(base: str, miner_id: str, address: str, owner: str, keydir: str, *, node: str | None = None,
                   numero=None) -> tuple:
    """(rc, document to sign | {"error": ...}, sign command argv). Signs NOTHING and sends NOTHING.

    The document is `relay_signature.document`, the one a deposit signs, built for the OWNER's address at
    the height the programme reports, with the owner account's number and sequence read from the chain
    (they enter the signature, so they are read, never guessed). What payout-submit needs is kept in the
    miner's volume (PENDING): the owner's machine only ever sees the document and the command."""
    from final_season_address import payable_address
    from modea import crypto
    from modea import relay_signature as rs
    from modea.miner_id import miner_id_for_account
    bad = payable_address(address)
    if bad:
        return 2, {"error": f"--address {address}: {bad}"}, []
    bad = payable_address(owner)
    if bad:
        return 2, {"error": f"--owner {owner}: {bad}"}, []
    owner, address = owner.lower(), address.lower()
    try:
        derived = miner_id_for_account(owner)
    except Exception as e:  # noqa: BLE001
        return 2, {"error": f"the identifier of {owner} cannot be derived ({type(e).__name__}: {e})"}, []
    if derived != miner_id:
        # The chain derives the identifier from the key that registered the miner: an owner whose derived
        # identifier is another one did not register THIS miner, and the programme would refuse its
        # signature. Said here, before anything is signed on the owner's machine.
        return 2, {"error": f"{miner_id} was not registered by {owner} (that key registers {derived}): the "
                            f"owner is the key that signed create-miner for {miner_id}"}, []
    if not keydir or not os.path.isdir(keydir):
        return 2, {"error": f"the miner's key directory {keydir!r} is not here: run this inside the miner's "
                            f"container (docker compose -p dendra-miner exec -T miner ...)"}, []
    code, st = status(base)
    h = st.get("height") if code == 200 else None
    if not isinstance(h, int) or h <= 0:
        return 1, {"error": f"the service's height is unknown (status {code}): nothing was prepared"}, []
    try:
        an, seq = (numero or rs.numero_et_sequence)(owner, node=node)
    except Exception as e:  # noqa: BLE001 -- an unread account is said, and nothing is prepared on it
        return 1, {"error": f"the owner account {owner} could not be read ({e}): nothing was prepared"}, []
    body = _payout_body(miner_id, address)
    kind, key = "fspay", f"payout__{miner_id}"
    pending = {"kind": kind, "key": key, "body": body.decode("utf-8"), "miner_id": miner_id, "height": h,
               "owner": owner, "address": address, "account_number": str(an), "sequence": str(seq),
               "programme": base}
    crypto.write_private(os.path.join(keydir, PENDING), json.dumps(pending, sort_keys=True).encode("utf-8"))
    argv = rs.sign_argv("payout.json", owner, an, seq) + ["--output-document", "payout.signed.json"]
    return 0, rs.document(kind, key, body, miner_id, h, owner), argv


def pending_declaration(keydir: str):
    """The declaration payout-prepare left in the volume, or None (absent or unreadable)."""
    try:
        with open(os.path.join(keydir or "", PENDING), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def submit_payout(signed_text: str, keydir: str, *, base: str = "") -> tuple:
    """(rc, answer). Sends the declaration prepared by prepare_payout, signed on the owner's machine.

    Checked HERE before anything is sent: the signer is the owner the declaration was prepared for, and the
    signature covers exactly the prepared declaration (same body, miner, height, account number and
    sequence). The programme checks the same things; refusing here says which one failed."""
    from modea import cosmos_addr, relay_canon, relay_carrier
    from modea import relay_signature as rs
    path = os.path.join(keydir or "", PENDING)
    try:
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
        kind, key, body = str(p["kind"]), str(p["key"]), str(p["body"]).encode("utf-8")
        mid, h, owner, address = str(p["miner_id"]), int(p["height"]), str(p["owner"]), str(p["address"])
        an, seq = str(p["account_number"]), str(p["sequence"])
        base = base or str(p.get("programme") or "")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return 2, {"error": f"no prepared declaration in {path} ({type(e).__name__}): run payout-prepare first"}
    try:
        signed = json.loads(signed_text)
        pub, sig, _ = relay_carrier.elements_signature(signed)
    except (ValueError, TypeError, relay_carrier.InvalidCarrier) as e:
        return 2, {"error": f"STDIN is not a transaction signed by dendrad tx sign --sign-mode amino-json ({e})"}
    who = cosmos_addr.address_from_pubkey(pub)
    if who.lower() != owner.lower():
        return 2, {"error": f"signed by {who}, not by the owner {owner}: nothing was sent"}
    message = relay_canon.canonical_message(kind, key, body, mid, h)
    if not relay_carrier.verifier(an, seq)(pub, message, sig):
        return 2, {"error": "the signature does not cover the prepared declaration (another file, another "
                            "account number or sequence, or a document changed after it was prepared): "
                            "prepare it again; nothing was sent"}
    if not base:
        return 2, {"error": "the programme's address is unknown (DENDRA_FINAL_SEASON_URL): nothing was sent"}
    headers = rs.headers_from_signed(signed, mid, h, an, seq)
    code, r = _http("POST", f"{base.rstrip('/')}/payout", body, headers)
    if code != 200:
        return 1, r
    setting = os.environ.get("DENDRA_PAYOUT_ADDRESS", "").strip().lower()
    record_declaration(keydir, mid, address, source="owner", setting=address if setting == address else None)
    try:
        os.remove(path)
    except OSError:
        pass
    return 0, r


def resolved_identity(path: str | None = None) -> str:
    """The `dm1...` identity the daemon resolved and renamed its key to, or '' (absent, unreadable, or not
    a resolved id — a memory, never a guess)."""
    try:
        with open(path or RESOLVED, encoding="utf-8") as f:
            ident = f.read().strip()
    except OSError:
        return ""
    return ident if ident.startswith("dm1") else ""


def _say(*lines) -> None:
    """Messages of payout-prepare go to stderr: its stdout is the document, redirected to a file."""
    for ln in lines:
        print(ln, file=sys.stderr, flush=True)


def main(argv=None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="Final Testnet Season declarations for a miner identity")
    ap.add_argument("action", choices=("payout", "status", "payout-prepare", "payout-submit"))
    ap.add_argument("--miner", default="", help=f"default: the identity resolved by the daemon ({RESOLVED})")
    ap.add_argument("--address", default="")
    ap.add_argument("--programme", default=PROGRAMME)
    ap.add_argument("--owner", default=os.environ.get("DENDRA_MINER_OWNER", "").strip(),
                    help="payout-prepare: the miner's owner, the key that registered it (default: DENDRA_MINER_OWNER)")
    ap.add_argument("--keydir", default="", help=f"payout-prepare/-submit: default {os.path.dirname(RESOLVED)}")
    ap.add_argument("--lock", action="store_true",
                    help="payout: LOCK the address -- the programme then refuses any other one for this identity, "
                         "for good; check the address before using it. Granted only on the identity's FIRST "
                         "declaration (or from its owner's key in owner mode)")
    a = ap.parse_args(argv)
    keydir = a.keydir or os.path.dirname(RESOLVED)
    if a.action == "payout-submit":
        rc, r = submit_payout((stdin or sys.stdin).read(), keydir, base=(a.programme or "").rstrip("/"))
        print(json.dumps(r))
        return rc
    if not a.programme:
        print("DENDRA_FINAL_SEASON_URL is not set (the programme's address, e.g. https://testnet-api.dendranetwork.com/final-season/v1)")
        return 2
    base = a.programme.rstrip("/")
    if a.action == "status":
        code, st = status(base)
        print(json.dumps(st, indent=1))
        return 0 if code == 200 else 1
    if a.action == "payout-prepare":
        mid = a.miner or resolved_identity()
        if not mid or not a.owner or not a.address:
            _say(f"payout-prepare needs --address, the owner (--owner or DENDRA_MINER_OWNER) and the miner "
                 f"(--miner, or the identity resolved in {RESOLVED})")
            return 2
        rc, doc, sign = prepare_payout(base, mid, a.address, a.owner, keydir, node=os.environ.get("DENDRA_NODE") or None)
        height = (pending_declaration(keydir) or {}).get("height", "?")
        if rc != 0:
            _say(f"[payout] NOT prepared: {doc.get('error')}")
            return rc
        print(json.dumps(doc))
        _say(f"[payout] {mid}: Final Testnet Season rewards to {a.address.lower()}, to be signed by the owner {a.owner.lower()}.",
             "  1. the document to sign is on STDOUT (redirect it: > payout.json). No chain accepts it as a",
             "     transaction: its chain id is the relay's signing domain, so the signature moves no funds.",
             "  2. on the machine that holds the owner key (add the --keyring-backend / --keyring-dir of its keyring):",
             "       " + " ".join(shlex.quote(x) for x in sign),
             "  3. back on the miner's host:",
             "       docker compose -p dendra-miner exec -T miner python3 final_season_miner.py payout-submit < payout.signed.json",
             f"  The signature is bound to height {height}: the programme refuses it once its chain has moved past",
             "  that height by more than its replay window -- then prepare it again.")
        return 0
    resolved = resolved_identity()
    if resolved:
        # The keyring entry now carries this name: signing for the start-up name would sign for nothing.
        import relay_client
        relay_client.set_sign_key(resolved)
    mid = a.miner or resolved
    if not mid:
        print(f"--miner is required: no resolved identity in {RESOLVED}")
        return 2
    # Without --lock the call keeps its earlier shape, word for word.
    code, r = declare_payout(base, mid, a.address, lock=True) if a.lock else declare_payout(base, mid, a.address)
    if code == 200:
        # The record sits next to the identity, in the volume (RESOLVED's directory); outside the container,
        # where that directory does not exist, nothing is written.
        record_declaration(os.path.dirname(RESOLVED), mid, a.address, source="command",
                           locked=(r or {}).get("locked") is True)
    print(json.dumps(r))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
