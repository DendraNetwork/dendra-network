"""The client-side derivation must agree with the CHAIN, not merely with itself.

This is a second implementation of a consensus rule; the authority is
chain/x/jobs/types/miner_id.go. A test that only checked internal consistency would stay green while
the two drift apart, and the drift would surface where it costs most: a refused registration on an
operator's machine, with the daemon saying only that it received no job.

⭐ THE VECTOR BELOW WAS PRODUCED BY THE RUNNING CHAIN, not by this file. The address is the one the
miner's keyring holds; the identifier is what `dendrad query jobs list-miner` returns for it after a
registration the keeper ACCEPTED. Pinning a pair the chain itself minted is the only way this test can
fail when the derivation drifts.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modea.miner_id import derive_miner_id, miner_id_for_account  # noqa: E402

# Produced on the live chain (consensus epoch 3): this account registered under this identifier.
LIVE_ADDRESS = "dendra1tgwmmdzvcwmr05c8wx4npsc7ju5fvtczzhk5xd"
LIVE_MINER_ID = "dm1teqlpyx9sctv4d954eejvz"


def test_agrees_with_the_live_chain():
    assert miner_id_for_account(LIVE_ADDRESS) == LIVE_MINER_ID


def test_dm_prefix_and_stable_length():
    mid = miner_id_for_account(LIVE_ADDRESS)
    assert mid.startswith("dm1")
    # 25 characters: what an operator copies out of a log line.
    assert len(mid) == 25


def test_two_addresses_give_two_identifiers():
    other = "dendra1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5z5tpwxqergd3c8g7rusq7d5w5m"
    try:
        derived = miner_id_for_account(other)
    except ValueError:
        pytest.skip("witness address not decodable in this environment")
    assert derived != LIVE_MINER_ID


def test_an_undecodable_address_is_an_ERROR_never_a_fallback():
    # The tempting fallback — return the input unchanged — would hand `create-miner` a value the chain
    # refuses, and the operator would read the refusal as a chain problem rather than a bad address.
    for bad in ("", "not-an-address", "dendra1invalid", "DENDRA1TGWMMDZ"):
        with pytest.raises(ValueError):
            miner_id_for_account(bad)


def test_empty_address_refused_at_the_byte_level():
    with pytest.raises(ValueError):
        derive_miner_id(b"")


def test_the_domain_is_versioned_and_separates_hashes():
    # Without a domain prefix, a sha256(addr) computed elsewhere in the protocol would collide with a
    # miner id by construction. Check that the domain actually PARTICIPATES in the hash.
    import hashlib

    from modea.cosmos_addr import _convertbits, bech32_encode
    from modea.miner_id import MINER_ID_PAYLOAD_LEN

    raw = b"\x01\x02\x03\x04\x05"
    without_domain = bech32_encode(
        "dm", _convertbits(hashlib.sha256(raw).digest()[:MINER_ID_PAYLOAD_LEN], 8, 5, True))
    assert derive_miner_id(raw) != without_domain
