"""Which addresses can receive a Final Testnet Season payment. Shared by the programme service (it refuses an
unpayable payout declaration) and the weekly payment (it never puts one in a batch).

An address can be well formed and still unpayable: the chain refuses to credit its own module accounts
(`chain/app/app_config.go`, `blockAccAddrs`), whose addresses anyone can compute. One such address in
a batch fails the whole transaction in the block, and every other transfer of that batch with it.
"""
from __future__ import annotations

import hashlib

from modea import cosmos_addr

BLOCKED_MODULES = ("fee_collector", "distribution", "bonded_tokens_pool", "not_bonded_tokens_pool", "nft")


def module_address(name: str) -> str:
    return cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(hashlib.sha256(name.encode()).digest()[:20], 8, 5))


BLOCKED_ADDRESSES = frozenset(module_address(n) for n in BLOCKED_MODULES)


def payable_address(addr: str) -> str:
    """'' when `addr` can receive a transfer, else why not.

    Bech32 is case-insensitive (mixed case is invalid, and `bech32_decode` refuses it), so DENDRA1... in
    capitals names the same account as dendra1...: the blocked set is compared on the lowercase form,
    or a module account written in capitals would pass."""
    if not isinstance(addr, str):
        return "not a dendra1... address"
    dec = cosmos_addr.bech32_decode(addr)
    if dec is None or dec[0] != "dendra":
        return "not a dendra1... address"
    raw = cosmos_addr._convertbits(dec[1], 5, 8, False)
    if raw is None or len(raw) not in (20, 32):
        return "not an account address (wrong length)"
    if addr.lower() in BLOCKED_ADDRESSES:
        return "a module account the chain refuses to credit"
    return ""
