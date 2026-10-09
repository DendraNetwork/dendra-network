#!/usr/bin/env python3
"""Bench for the miner's identity persistence (miner.py).

Why it exists: `align_identity` renamed the keyring entry to the `dm1…` identifier derived from the
account address, and nothing wrote that answer down. On the next start the daemon received the OLD
name again from `deploy/testnet-miner/.env`, found no key under it (it had been renamed), and
`keys_addr` CREATES one when the name is unknown: a new address, a new identifier, an empty account —
while the registration, the stake and the held fees stayed on-chain under an identity the machine no
longer held. Silent, and repeated at every restart.

PURE logic: no keyring, no chain, no container. It exercises the resume rule, the one place where a
mistake costs an identity.

Usage: python3 test_persistent_identity.py
"""
import sys
import tempfile
from pathlib import Path

N = KO = 0


def resolve(passed_id, memory_path, derive_from):
    """FAITHFUL COPY of the rule inserted into miner.main().

    `derive_from` stands in for keys_addr()+align_identity(): it returns the identifier the chain
    accepts for the key held under that name, or None when the derivation fails."""
    m = Path(memory_path)
    if not passed_id.startswith("dm1") and m.exists():
        kept = m.read_text(encoding="utf-8").strip()
        if kept.startswith("dm1"):
            passed_id = kept
    resolved = derive_from(passed_id)
    if resolved is None:
        resolved = passed_id                     # derivation impossible -> keep the current name
    if resolved.startswith("dm1"):
        m.write_text(resolved + "\n", encoding="utf-8")
    return resolved


def case(name, expected, got):
    global N, KO
    N += 1
    if expected == got:
        print(f"  OK  {name}")
    else:
        KO += 1
        print(f"  KO  {name}  (expected {expected!r}, got {got!r})")


tmp = tempfile.mkdtemp()
MEM = str(Path(tmp) / "identite-resolue")

# The simulated world: the key 'm-abc' exists only on the first call; after that it has been renamed
# to dm1AAA, so asking for the old name again would mint a brand-new one.
state = {"renamed": False}


def derive(name):
    if name == "dm1AAA":
        return "dm1AAA"                          # the key exists under this name: nothing to do
    if name == "m-abc" and not state["renamed"]:
        state["renamed"] = True
        return "dm1AAA"                          # first pass: key created, address A, renamed
    # THE ORIGINAL DEFECT: unknown name -> keys add -> NEW address -> NEW identifier
    return "dm1ZZZ-IDENTITY-LOST"


print("== THE SCENARIO THAT LOST THE STAKE ==")
case("first start: m-abc -> dm1AAA", "dm1AAA", resolve("m-abc", MEM, derive))
case("restart: m-abc -> dm1AAA (RESUMED)", "dm1AAA", resolve("m-abc", MEM, derive))
case("third start: still the same identity", "dm1AAA", resolve("m-abc", MEM, derive))
case("the memory holds the accepted identity", "dm1AAA", Path(MEM).read_text().strip())

print()
print("== AN EXPLICIT dm1 WINS: this file is a memory, never an override ==")
state["renamed"] = True
case("dm1AAA passed explicitly", "dm1AAA", resolve("dm1AAA", MEM, derive))

print()
print("== A FAILED DERIVATION MUST NOT POISON THE MEMORY ==")
tmp2 = tempfile.mkdtemp()
MEM2 = str(Path(tmp2) / "identite-resolue")
case("derivation impossible -> name kept", "m-xyz", resolve("m-xyz", MEM2, lambda n: None))
case("nothing is written (no memory file)", False, Path(MEM2).exists())

print()
print("== A CORRUPT MEMORY IS IGNORED, NEVER ADOPTED ==")
tmp3 = tempfile.mkdtemp()
MEM3 = str(Path(tmp3) / "identite-resolue")
Path(MEM3).write_text("not-an-identifier\n", encoding="utf-8")
state["renamed"] = False
case("non-dm1 memory ignored, start from the passed name", "dm1AAA", resolve("m-abc", MEM3, derive))

print()
print(f"BANC_PERSISTENT_IDENTITY_RESUME cas={N} ko={KO}")
sys.exit(1 if KO else 0)
