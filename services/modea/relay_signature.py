"""Writer side of a signed relay write: turn a deposit into the headers the relay can verify.

Why this file exists
`relay_write` (verification) and `relay_carrier` (the carrier) were both implemented and tested, and
nothing produced a signed write. This module is the producer, and it is deliberately the only place
that knows the wire format: the relay reads exactly what is written here, so the format lives in one
file rather than in an agreement between two.

What travels, and what does not
    X-Dendra-Miner    the identifier the write claims
    X-Dendra-Height   the block height the signed message carries
    X-Dendra-Pubkey   base64, 33 bytes, compressed secp256k1
    X-Dendra-Sig      base64, 64 bytes, compact r||s
    X-Dendra-Acct     account_number
    X-Dendra-Seq      sequence
The address is not sent: the relay recomputes it from the public key. A value that can be recomputed
is not a value to carry — carrying it would create a second place to lie.

None of these fields needs to be trusted on arrival. Every one of them enters the signed document, so
a wrong value rebuilds a different document and the signature simply fails. That is the property that
lets them come from the client at all.

The signature is produced by `dendrad`, not here
No private key is read by this process: the key stays in the keyring and `dendrad tx sign` is asked
for a signature over a transaction that cannot be broadcast — its chain-id is `dendra/relay-write/v1`,
which no chain will accept. That is what stops a captured write from becoming a denial of service
against the account's next real settlement (see `relay_carrier`, which measured it against a witness).

What is not proven here. The exact `dendrad tx sign` invocation below cannot be exercised without
the binary, a funded account and a live chain — none of which exist in the environment where this file
was written. What is proven is everything downstream of it: `test_relay_carrier.py` verifies a real
binary signature against a rebuilt document, and `tests/test_relay_carrier_verifier.py` verifies
that the relay's adapter rebuilds the right document. The unproven link is the invocation itself, and
`autotest()` below exists to close it in one command before anyone relies on it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import tempfile

from . import relay_canon, relay_carrier

HEADER_MINER = "X-Dendra-Miner"
HEADER_HEIGHT = "X-Dendra-Height"
HEADER_PUBKEY = "X-Dendra-Pubkey"
HEADER_SIG = "X-Dendra-Sig"
HEADER_ACCT = "X-Dendra-Acct"
HEADER_SEQ = "X-Dendra-Seq"

#: Every header the relay needs. Named once so the server can refuse a partial set by name rather
#: than discovering a missing field halfway through verification.
HEADERS = (HEADER_MINER, HEADER_HEIGHT, HEADER_PUBKEY, HEADER_SIG, HEADER_ACCT, HEADER_SEQ)


class SignatureUnavailable(RuntimeError):
    """Signing failed, and the caller must be told why rather than shown a silent False.

    A deposit that cannot be signed is a deposit that will be refused. Returning a bare failure here
    is how an operator ends up reading "0 jobs" and concluding the network is quiet.
    """
    #: What dendrad printed, whole (stderr then stdout): the message above is cut to one line, and a caller
    #: that classifies the failure reads this, never the summary.
    text = ""


class AccountNotFound(SignatureUnavailable):
    """The chain read the address and holds no account for it: a transient state, not a failure.

    A new miner's address has no account until its first credit (the faucet's drip) lands: that is the
    normal state of every first start, and it ends by itself. It is told apart from a failure because
    the two call for opposite reactions (wait, or fix something), and because the chain's own wording
    for it ("account ... not found: key not found") names a key while it is about an account: carried into
    a log, that wording reads as a keyring failure to anything that greps for one
    (deploy/join.sh::wait_healthy did, and declared a healthy first start not healthy). So the message
    of this class never repeats the chain's wording; `text` keeps it, whole, for whoever needs it."""


# The chain's answer for an address it has no account for, as `dendrad query auth account` prints it:
# "rpc error: code = NotFound desc = account dendra1... not found: key not found". Matched on the account
# and the gRPC status, never on the trailing "key not found" alone, which is also how a keyring says that
# a key name is unknown -- the opposite state, and a real failure.
_ACCOUNT_NOT_FOUND = re.compile(r"code = NotFound[^\n]*\baccount\s+\S+\s+not found")


def _run_output(cmd, stdin_text=None) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, input=stdin_text if stdin_text is not None else "", timeout=60)
    if p.returncode != 0:
        from . import keyring as _kr
        text = (p.stderr or "") + "\n" + (p.stdout or "")
        # A keyring refusal is named: dendrad prints it as "not a valid name or address", after its usage
        # text, and the head of that output says nothing about a passphrase.
        cause = _kr.explain(text) or _kr.last_line(text)
        e = SignatureUnavailable(f"{' '.join(cmd[:3])} -> code {p.returncode}: {cause[:300]}")
        e.text = text
        raise e
    return p.stdout


def _keyring_flags(keyring_backend, keyring_dir):
    """(flags, stdin) of the keyring a call reads. A backend given explicitly by the caller is used as it
    is, with no passphrase; otherwise the backend is the one the disk holds (modea/keyring.py), and the
    passphrase goes on stdin. There is no default backend: a default of `test` is what made a passphrase
    absent fall back, in silence, to a keyring that holds no key."""
    if keyring_backend:
        return ["--keyring-backend", keyring_backend] + (["--keyring-dir", keyring_dir] if keyring_dir else []), None
    from . import keyring as _kr
    try:
        kr = _kr.resolve(keyring_dir)
    except _kr.KeyringError as e:
        raise SignatureUnavailable(f"{e} -- {e.hint}") from e
    return kr.flags(), kr.stdin()


def account_number_and_sequence(address: str, node: str | None = None, dendrad: str = "dendrad"):
    """Reads (account_number, sequence) for an address. The two are not treated the same way.

    The zero rule, and the side each field falls on. proto3 omits a field at its zero value, so
    `sequence` is absent from the JSON of an account that has never sent a transaction — which is the
    normal state of a fresh miner, not an anomaly. Reading that absence as an error refuses to sign
    for exactly the accounts that most need to.

      · `account_number` absent  -> error. The chain has never seen this account; there is no number
        to guess, and inventing 0 would build a document for a different account.
      · `sequence` absent        -> 0, the zero value of its type. It is legitimate and it is safe
        here for a reason this module already relies on: the sequence enters the signed document, so
        a wrong value produces a signature that simply does not verify. It authenticates itself.

    The asymmetry is the point. "Never default anything" and "always default to zero" are both wrong;
    what decides is whether the value can lie undetected — and a signed field cannot.

    Three answers, never two. The pair, read; `AccountNotFound` when the chain answered that it holds no
    account for this address (a fresh miner before its first credit: transient, it ends by itself);
    `SignatureUnavailable` for everything else (node down, wrong --node, an answer that is not an
    account). The second is not folded into the third: one waits, the other needs an operator.
    """
    cmd = [dendrad, "query", "auth", "account", address, "--output", "json"]
    if node:
        cmd += ["--node", node]
    try:
        output = _run_output(cmd)
    except SignatureUnavailable as e:
        if _ACCOUNT_NOT_FOUND.search(e.text or ""):
            absent = AccountNotFound(
                f"the chain holds no account for {address} yet: an address becomes an account with its "
                f"first credit (the faucet's drip, for a new miner)")
            absent.text = e.text
            raise absent from e
        raise
    raw = json.loads(output)
    account = raw.get("account", raw)
    for key in ("value", "base_account"):          # the shape depends on the account type
        if isinstance(account.get(key), dict):
            account = account[key]
    an = account.get("account_number")
    if an is None:
        raise SignatureUnavailable(
            f"account {address} carries no account_number (keys: {sorted(account)[:8]}). "
            f"The chain has never seen this account: fund it, then sign.")
    return str(an), str(account.get("sequence") or 0)


def headers(kind: str, key: str, body: bytes, miner_id: str, height: int, *,
            key_name: str, address: str, account_number, sequence,
            keyring_backend: str | None = None, keyring_dir: str | None = None,
            dendrad: str = "dendrad") -> dict:
    """Produces the headers for one signed deposit. `body` must be the exact bytes that will be sent.

    "Exact" is the contract: the signature covers a digest of the body, so a body
    re-serialised between signing and sending (a different key order, a space after a colon) is a
    different body, and the relay refuses it. The caller therefore signs the bytes it will transmit,
    never the object it will re-encode.
    """
    doc = document(kind, key, body, miner_id, height, address)
    flags, stdin_text = _keyring_flags(keyring_backend, keyring_dir)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(doc, f)
        path = f.name
    try:
        cmd = [*sign_argv(path, key_name, account_number, sequence, dendrad=dendrad), *flags,
               "--output", "json"]
        signed = json.loads(_run_output(cmd, stdin_text))
    finally:
        os.unlink(path)
    return headers_from_signed(signed, miner_id, height, account_number, sequence)


def document(kind: str, key: str, body: bytes, miner_id: str, height: int, address: str) -> dict:
    """The transaction `dendrad tx sign` is asked to sign for one write: a self-transfer whose memo is the
    digest of the canonical message, under a chain id no chain accepts (`relay_carrier.DOMAIN_CHAIN_ID`),
    so the signature can never be broadcast as a transaction. One builder, used by `headers()` on the
    miner and by `final_season_miner.py payout-prepare` for a declaration signed on another machine: the
    document an owner signs offline is byte for byte the one this module would have signed."""
    digest = relay_canon.digest(kind, key, body, miner_id, height)
    return {
        "body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend",
                               "from_address": address, "to_address": address,
                               "amount": [{"denom": "udndr", "amount": "1"}]}],
                 "memo": binascii.hexlify(digest).decode(),
                 "timeout_height": "0", "extension_options": [],
                 "non_critical_extension_options": []},
        "auth_info": {"signer_infos": [],
                      "fee": {"amount": [], "gas_limit": "200000", "payer": "", "granter": ""}},
        "signatures": [],
    }


def sign_argv(path: str, signer: str, account_number, sequence, *, dendrad: str = "dendrad") -> list:
    """The `dendrad tx sign` command for a write's document, without keyring flags. The relay verifies only
    an amino-JSON signature under the relay's own chain id, so these flags are not a choice: the miner runs
    exactly this, and the command printed for an owner's machine is built here too."""
    return [dendrad, "tx", "sign", path, "--from", signer,
            "--chain-id", relay_carrier.DOMAIN_CHAIN_ID,
            "--sign-mode", "amino-json", "--offline",
            "--account-number", str(account_number), "--sequence", str(sequence)]


def headers_from_signed(signed: dict, miner_id: str, height: int, account_number, sequence) -> dict:
    """The headers of a write, from the JSON `dendrad tx sign` produced over `document(...)`."""
    pub, sig, _ = relay_carrier.signature_elements(signed)
    return {
        HEADER_MINER: miner_id,
        HEADER_HEIGHT: str(height),
        HEADER_PUBKEY: base64.b64encode(pub).decode(),
        HEADER_SIG: base64.b64encode(sig).decode(),
        HEADER_ACCT: str(account_number),
        HEADER_SEQ: str(sequence),
    }


def address_from_key(key_name: str, *, keyring_backend: str | None = None,
                   keyring_dir: str | None = None, dendrad: str = "dendrad") -> str:
    """The address of a keyring key. Derived, never asked for.

    A caller that has to supply both a key name and its address can supply a mismatched pair, and the
    failure surfaces as a signature error pointing at neither. One input cannot disagree with itself.
    """
    flags, stdin_text = _keyring_flags(keyring_backend, keyring_dir)
    return _run_output([dendrad, "keys", "show", key_name, "-a", *flags], stdin_text).strip()


def autotest(key_name: str, address: str | None = None, *, node: str | None = None,
             keyring_backend: str | None = None, keyring_dir: str | None = None,
             dendrad: str = "dendrad") -> bool:
    """Closes the one link no unit test can reach: does `dendrad` really sign what the relay rebuilds?

    Run this once, on a machine that has the binary and the key, before arming enforcement on the
    relay. It signs a throwaway write and verifies it with the relay's own adapter — the same code
    path, not an imitation. If it prints OK, a real deposit will verify; if it does not, arming
    enforcement would refuse every write and the symptom would be an empty network.

    One argument, and that is deliberate. The address is derived from the key name; asking for both
    would let a caller paste a mismatched pair — or, as happened, paste the placeholders themselves.
    A command with a hole to fill is a command that gets run with the hole still in it.
    """
    kind, key, body, height = "res", "autotest__" + key_name, b'{"autotest":true}', 1
    if address is None:
        address = address_from_key(key_name, keyring_backend=keyring_backend,
                                 keyring_dir=keyring_dir, dendrad=dendrad)
        print(f"  key={key_name} -> {address}")
    an, seq = account_number_and_sequence(address, node=node, dendrad=dendrad)
    h = headers(kind, key, body, key_name, height, key_name=key_name, address=address,
                account_number=an, sequence=seq, keyring_backend=keyring_backend,
                keyring_dir=keyring_dir, dendrad=dendrad)
    message = relay_canon.canonical_message(kind, key, body, key_name, height)
    verif = relay_carrier.verifier(h[HEADER_ACCT], h[HEADER_SEQ])
    ok = verif(base64.b64decode(h[HEADER_PUBKEY]), message, base64.b64decode(h[HEADER_SIG]))
    print(f"  account_number={an} sequence={seq}")
    print(f"  digest         ={hashlib.sha256(message).hexdigest()}")
    print("  " + ("OK — dendrad signs exactly what the relay rebuilds; enforcement can be armed."
                  if ok else
                  "failed — do not arm enforcement: every write would be refused. "
                  "Check the chain-id, the sign mode, and the account/sequence pair."))
    return ok
