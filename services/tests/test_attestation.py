"""Software attestation, MEASURED and SIGNED (modea/confine.py).

The full chain is exercised: deterministic measured hash -> Ed25519 signature -> relay verification
(signature, recomputed measure, allow-list, public-key binding). What these tests prove is the
cryptographic mechanism of the attestation, NOT that a root operator cannot lie. That limit is
inherent to software attestation without a hardware root of trust."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modea import confine


def test_measured_hash_deterministic():
    h1, m1 = confine.measured_hash(model_id="llama", weights_hash="abcd")
    h2, m2 = confine.measured_hash(model_id="llama", weights_hash="abcd")
    assert h1 == h2 and len(h1) == 64
    # The measure does carry the bound elements.
    assert m1["model_id"] == "llama" and m1["weights_hash"] == "abcd"
    assert "code_manifest" in m1 and "confinement" in m1


def test_measured_hash_changes_with_model():
    h1, _ = confine.measured_hash(model_id="A")
    h2, _ = confine.measured_hash(model_id="B")
    assert h1 != h2  # the model_id enters the measured hash


def test_sign_then_verify_ok():
    with tempfile.TemporaryDirectory() as d:
        sk, pub = confine.load_or_create_attest_key(d, "m1")
        att = confine.signed_attestation(sk, miner_id="m1", model_id="llama")
        ok, why = confine.verify_attestation(att, allowed_hashes=[att["measured_hash"]],
                                             expected_pubkey=pub)
        assert ok, why


def test_verify_rejects_bad_signature():
    with tempfile.TemporaryDirectory() as d:
        sk, _ = confine.load_or_create_attest_key(d, "m1")
        att = confine.signed_attestation(sk, miner_id="m1")
        att["signature"] = "00" * 64  # forged signature
        ok, why = confine.verify_attestation(att, allowed_hashes=[att["measured_hash"]])
        assert not ok and "signature" in why


def test_verify_rejects_tampered_measure():
    with tempfile.TemporaryDirectory() as d:
        sk, _ = confine.load_or_create_attest_key(d, "m1")
        att = confine.signed_attestation(sk, miner_id="m1", model_id="llama")
        # The miner announces a clean hash together with a TAMPERED measure (a swapped model).
        att["measure"]["model_id"] = "evil-model"
        ok, why = confine.verify_attestation(att, allowed_hashes=[att["measured_hash"]])
        assert not ok and "measure" in why


def test_verify_rejects_unknown_hash():
    with tempfile.TemporaryDirectory() as d:
        sk, _ = confine.load_or_create_attest_key(d, "m1")
        att = confine.signed_attestation(sk, miner_id="m1")
        # The allow-list does NOT contain this hash, so an unrecognised build is refused.
        ok, why = confine.verify_attestation(att, allowed_hashes=["deadbeef"])
        assert not ok and "allow-list" in why


def test_verify_rejects_wrong_expected_pubkey():
    with tempfile.TemporaryDirectory() as d:
        sk, _ = confine.load_or_create_attest_key(d, "m1")
        att = confine.signed_attestation(sk, miner_id="m1")
        ok, why = confine.verify_attestation(att, allowed_hashes=[att["measured_hash"]],
                                             expected_pubkey="ff" * 32)
        assert not ok and "identity" in why


def test_attest_key_persistent():
    with tempfile.TemporaryDirectory() as d:
        _, p1 = confine.load_or_create_attest_key(d, "m1")
        _, p2 = confine.load_or_create_attest_key(d, "m1")  # reloaded, not regenerated
        assert p1 == p2
        # Private permissions (POSIX).
        kf = Path(d) / "m1.attestkey"
        if sys.platform != "win32":
            assert (kf.stat().st_mode & 0o077) == 0
