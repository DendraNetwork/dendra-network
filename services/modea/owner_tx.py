"""Transactions a miner's OWNER signs, prepared on the mining machine (owner mode: deploy/join.sh --owner).

WHY THEY ARE PREPARED HERE AND SIGNED ELSEWHERE
In owner mode the miner is registered by a key that is NOT on the mining machine. The chain derives the
miner's identifier from the key that signs `create-miner`, takes the bond from it, lets it alone update or
delete the miner, and gives the bond back to it (chain/x/jobs/keeper/msg_server_miner.go). The
machine's own key only OPERATES the miner. So the transactions that touch the bond -- create-miner,
update-miner, delete-miner -- are signed by the owner, and the machine can only PREPARE them: every step that
needs no key happens here, the one that does happens on the owner's machine, offline.

  1. a SIMULATION from the owner's ADDRESS (`--dry-run`): the chain says whether it would accept the
     transaction as prepared, and how much gas it uses. Nothing is signed, nothing is sent;
  2. the UNSIGNED transaction (`--generate-only`, at that estimate times the kit's margin);
  3. the owner account's NUMBER and SEQUENCE, read from the chain: they enter the signature, so they are
     read, never guessed;
  4. the SIGN command for the owner's machine (`--offline`), printed complete;
  5. the BROADCAST, from any machine that reaches the chain: `dendrad tx broadcast -` reads the signed
     transaction on stdin.

MEASURED with the real dendrad on a throwaway single-validator chain, running these very functions: the
simulation and the unsigned transaction need no key, the owner being named by its address; `--gas auto` together with `--generate-only` does NOT work from an address (dendrad looks the key
up to simulate), which is why the gas comes from step 1; and the signed transaction, broadcast from stdin,
registers the miner with the owner as creator and this machine's key as operator.

THE COMMANDS ARE BUILT HERE AND NOWHERE ELSE. The daemon prints them, exit-miner.sh prints them, the bench runs
them against a chain: a printed command is delivered code, and two copies of it drift apart.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess

from . import dendrad_argv as da

# What each sub-command puts in the transaction. A file that claims to be one of them is checked against it
# before it is broadcast (check_tx): a signed transaction is otherwise an opaque blob, and broadcasting
# whatever was handed back would send a transfer as readily as an exit.
TYPES = {"create-miner": "/dendra.jobs.v1.MsgCreateMiner",
         "update-miner": "/dendra.jobs.v1.MsgUpdateMiner",
         "delete-miner": "/dendra.jobs.v1.MsgDeleteMiner"}
_SIM = re.compile(r"gas estimate:\s*(\d+)")


class OwnerTxError(RuntimeError):
    """A step that needs the chain could not be completed; the message says which and why."""


def _run(argv, timeout=120):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input="")
    except Exception as e:  # noqa: BLE001 -- a dendrad that does not run is an answer, never a traceback
        return 127, "", f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout or "", p.stderr or ""


def first_line(*texts) -> str:
    """The first non-empty line, skipping the build warnings some dendrad binaries print on every call."""
    for t in texts:
        for ln in (t or "").splitlines():
            s = ln.strip()
            if s and not s.startswith("WARNING: sonic"):
                return s[:300]
    return ""


def _doc(text: str):
    text = text or ""
    try:
        d = json.loads(text[text.find("{"):text.rfind("}") + 1]) if "{" in text else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def tx_argv(sub, positionals, owner, chain_id, *, node_flags=(), dry_run=False, gas=0, extra=()) -> list:
    """`dendrad tx jobs <sub>` from the owner's ADDRESS: a simulation (`dry_run`), else the unsigned
    transaction at `gas`. Positionals after `--` (modea/dendrad_argv.py). The unsigned transaction needs no
    node: only the simulation does."""
    flags = [*extra, "--from", owner, "--chain-id", chain_id]
    flags += ["--dry-run", *node_flags] if dry_run else ["--generate-only", "--gas", str(int(gas))]
    return da.dendrad_argv(("dendrad", "tx", "jobs"), sub, positionals, flags)


def gas_from_simulation(text):
    """The gas to sign for: the simulation's estimate times the kit's margin, or None when the text carries
    no estimate -- a simulation that printed none proved nothing, whatever its exit code."""
    m = _SIM.search(text or "")
    if not m:
        return None
    return int(int(m.group(1)) * float(da.GAS_ADJUSTMENT)) + 1


def account(address, node_flags=(), run=_run):
    """(account_number, sequence) of an account the chain KNOWS, as strings; OwnerTxError otherwise.
    THE ZERO RULE: inside an answer that was read and names THIS address, an absent number is 0 (proto3
    omits zeros -- a fresh account has no `sequence`, the first account of a genesis no `account_number`).
    An account the chain does not know answers NotFound: it must receive funds before it can sign."""
    rc, out, err = run(["dendrad", "query", "auth", "account", address, "--output", "json", *node_flags])
    if rc != 0:
        if "NotFound" in (out + err):
            raise OwnerTxError(f"the chain knows no account {address} yet: it has to receive funds before it can sign")
        raise OwnerTxError(f"the account {address} could not be read: {first_line(err, out) or f'exit {rc}'}")
    d = _doc(out)
    acct = d.get("account", d) if d is not None else None
    for k in ("value", "base_account"):
        if isinstance(acct, dict) and isinstance(acct.get(k), dict):
            acct = acct[k]
    if not isinstance(acct, dict) or str(acct.get("address", "")).lower() != address.lower():
        raise OwnerTxError(f"the answer for {address} is not that account's record")
    try:
        return str(int(acct.get("account_number", 0) or 0)), str(int(acct.get("sequence", 0) or 0))
    except (TypeError, ValueError) as e:
        raise OwnerTxError(f"the account {address} carries an unreadable number or sequence ({e})") from e


def check_tx(text, sub, creator, *, miner_id=None, signed=False) -> str:
    """'' when `text` is exactly ONE `sub` message from `creator` (about `miner_id` when given), signed or
    unsigned as asked; otherwise why not."""
    d = _doc(text)
    if d is None:
        return "not the JSON of a transaction"
    msgs = (d.get("body") or {}).get("messages") if isinstance(d.get("body"), dict) else None
    if not isinstance(msgs, list) or len(msgs) != 1 or not isinstance(msgs[0], dict):
        return f"{len(msgs) if isinstance(msgs, list) else 0} message(s), exactly one expected"
    m = msgs[0]
    if m.get("@type") != TYPES[sub]:
        return f"a {m.get('@type')!r} message, not {TYPES[sub]}"
    if str(m.get("creator", "")).lower() != str(creator).lower():
        return f"its signer is {m.get('creator')!r}, not the owner {creator}"
    if miner_id is not None and m.get("miner_id") != miner_id:
        return f"it is about the miner {m.get('miner_id')!r}, not {miner_id}"
    sigs = d.get("signatures")
    if signed and not sigs:
        return "it carries no signature"
    if not signed and sigs:
        return "it is already signed"
    return ""


def prepare(sub, positionals, owner, chain_id, *, node_flags=(), extra=(), miner_id=None, run=_run) -> dict:
    """Steps 1 to 3. {"ok": True, "gas", "unsigned" (JSON text), "account_number", "sequence", "generate"
    (the argv that produced it)} or {"ok": False, "why", "chain_said"}. Signs nothing, sends nothing."""
    rc, out, err = run(tx_argv(sub, positionals, owner, chain_id, node_flags=node_flags, dry_run=True, extra=extra))
    gas = gas_from_simulation(out + "\n" + err) if rc == 0 else None
    if gas is None:
        return {"ok": False, "why": f"the chain does not accept this {sub} as prepared",
                "chain_said": first_line(err, out) or f"exit {rc}, no gas estimate"}
    gen = tx_argv(sub, positionals, owner, chain_id, gas=gas, extra=extra)
    rc, out, err = run(gen)
    why = (check_tx(out, sub, owner, miner_id=miner_id) if rc == 0 else (first_line(err, out) or f"exit {rc}"))
    if why:
        return {"ok": False, "why": f"the unsigned {sub} could not be produced: {why}", "chain_said": ""}
    try:
        an, seq = account(owner, node_flags, run)
    except OwnerTxError as e:
        return {"ok": False, "why": str(e), "chain_said": ""}
    return {"ok": True, "why": "", "gas": gas, "unsigned": out.strip(), "account_number": an, "sequence": seq,
            "generate": gen}


def sign_argv(src, owner, chain_id, account_number, sequence, dst) -> list:
    """Step 4, on the owner's machine: `--from` takes the owner's ADDRESS, which dendrad resolves in that
    machine's keyring (whose --keyring-backend / --keyring-dir the owner adds)."""
    return ["dendrad", "tx", "sign", src, "--from", owner, "--chain-id", chain_id, "--offline",
            "--account-number", str(account_number), "--sequence", str(sequence), "--output-document", dst]


def broadcast_argv(node_flags=()) -> list:
    """Step 5: the signed transaction on stdin."""
    return ["dendrad", "tx", "broadcast", "-", *node_flags]


def shell(argv) -> str:
    return " ".join(shlex.quote(str(x)) for x in argv)
