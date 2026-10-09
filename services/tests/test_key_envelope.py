"""Bench of the key envelope (modea/crypto.py): the miner's key files sealed under the keyring's passphrase.

Pinned: a key file found IN CLEAR while a passphrase is there to seal it is re-encrypted IN PLACE -- the
false green this replaces: setting a passphrase after the fact left the key in clear while the warning,
which tested a variable and not the file, went quiet --; the re-encryption is ATOMIC (a failure leaves the
clear file exactly as it was, and no temporary file behind); every file is 0600 FROM ITS CREATION, under a
umask of 022; a sealed file without its passphrase is a NAMED error, never a new key; and each file is bound
to its kind, so a VRF secret cannot be opened as an encryption key.

The subject can be pointed at a mutated copy with DENDRA_CRYPTO_MODULE.
"""
import importlib.util
import os
import stat
import sys

import pytest

MODEA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODEA)
SUBJECT = os.environ.get("DENDRA_CRYPTO_MODULE") or os.path.join(MODEA, "modea", "crypto.py")


def _load():
    spec = importlib.util.spec_from_file_location("modea._crypto_under_bench", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses look their module up while the class is built
    spec.loader.exec_module(mod)
    return mod


C = _load()
PASS = "z" * 64
POSIX = os.name == "posix"


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_a_clear_sk_is_re_encrypted_in_place_when_a_passphrase_is_given(tmp_path):
    sk, _ = C.gen_keypair()
    p = tmp_path / "dm1abc.sk"
    C.save_sk(sk, str(p), "")
    raw = p.read_bytes()
    assert len(raw) == 32 and not C.file_is_sealed(str(p))
    again = C.load_sk(str(p), "", seal_with=PASS)
    assert C.pub_bytes(again) == C.pub_bytes(sk)
    assert p.read_bytes().startswith(C.MAGIC), "the clear key was left in clear: the false green"
    assert raw not in p.read_bytes()
    if POSIX:
        assert _mode(p) == 0o600
    assert C.pub_bytes(C.load_sk(str(p), PASS)) == C.pub_bytes(sk)


@pytest.mark.parametrize("name,aad,raw", [("dm1abc.vrf", "AAD_VRF", ("ab" * 64).encode()),
                                          ("dm1abc.attestkey", "AAD_ATTEST", ("cd" * 32).encode()),
                                          ("recovery-phrase.json", "AAD_RECOVERY", b'{"address": "dendra1x"}')])
def test_the_vrf_attestation_and_phrase_files_are_re_encrypted_the_same_way(tmp_path, name, aad, raw):
    p = tmp_path / name
    C.store_secret(str(p), raw, "", getattr(C, aad))
    assert p.read_bytes() == raw
    assert C.load_secret(str(p), "", getattr(C, aad), seal_with=PASS) == raw
    assert p.read_bytes().startswith(C.MAGIC) and raw not in p.read_bytes()
    assert C.load_secret(str(p), PASS, getattr(C, aad)) == raw
    if POSIX:
        assert _mode(p) == 0o600


def test_the_re_encryption_is_ATOMIC(tmp_path, monkeypatch):
    # The swap fails: the clear file is EXACTLY as it was (a reader sees the old file or the new one,
    # never half), and no temporary file is left next to it.
    p = tmp_path / "dm1abc.vrf"
    raw = ("ef" * 64).encode()
    C.store_secret(str(p), raw, "", C.AAD_VRF)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(C.os, "replace", boom)
    with pytest.raises(OSError):
        C.load_secret(str(p), "", C.AAD_VRF, seal_with=PASS)
    assert p.read_bytes() == raw
    assert sorted(x.name for x in tmp_path.iterdir()) == ["dm1abc.vrf"]


@pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
def test_a_key_file_is_never_readable_by_others_even_for_an_instant(tmp_path, monkeypatch):
    # Under umask 022 a file created by open() is 0644 until a chmod: the window this replaces. The mode
    # is read on the TEMPORARY file, at the moment it is swapped in.
    seen = []
    real = C.os.replace

    def watch(src, dst):
        seen.append(_mode(src))
        return real(src, dst)
    monkeypatch.setattr(C.os, "replace", watch)
    old = os.umask(0o022)
    try:
        C.store_secret(str(tmp_path / "a.sk"), b"0" * 32, "", C.AAD_SK)
        C.store_secret(str(tmp_path / "b.sk"), b"0" * 32, PASS, C.AAD_SK)
        C.load_secret(str(tmp_path / "a.sk"), "", C.AAD_SK, seal_with=PASS)
    finally:
        os.umask(old)
    assert seen and all(m == 0o600 for m in seen), seen


def test_a_sealed_file_without_its_passphrase_is_a_named_error(tmp_path):
    p = tmp_path / "dm1abc.sk"
    sk, _ = C.gen_keypair()
    C.save_sk(sk, str(p), PASS)
    with pytest.raises(C.EnvelopeLocked) as e:
        C.load_sk(str(p), "")
    assert str(p) in str(e.value) and "keyring-passphrase" in str(e.value)
    with pytest.raises(C.EnvelopeWrongPassphrase):
        C.load_sk(str(p), "y" * 64)
    assert issubclass(C.EnvelopeLocked, C.KeyEnvelopeError) and issubclass(C.EnvelopeWrongPassphrase, C.KeyEnvelopeError)


def test_each_file_is_bound_to_its_kind(tmp_path):
    p = tmp_path / "dm1abc.vrf"
    C.store_secret(str(p), ("ab" * 64).encode(), PASS, C.AAD_VRF)
    with pytest.raises(C.EnvelopeWrongPassphrase):
        C.load_secret(str(p), PASS, C.AAD_SK)


def test_the_legacy_sk_format_is_still_read_and_the_header_has_not_changed(tmp_path):
    # Keys sealed before this envelope was generalised carry the same header and the same binding.
    assert C.MAGIC == b"DENDRA-SKENC1\n" and C.AAD_SK == b"dendra-sk"
    p = tmp_path / "old.sk"
    p.write_bytes(b"\x01" * 32)
    assert len(C.sk_to_bytes(C.load_sk(str(p), ""))) == 32


def test_the_attestation_key_is_sealed_by_confine_with_the_keyring_passphrase(tmp_path):
    from modea import confine, crypto
    _, pub1 = confine.load_or_create_attest_key(str(tmp_path), "m1", seal_with=PASS)
    f = tmp_path / "m1.attestkey"
    assert crypto.file_is_sealed(str(f))
    _, pub2 = confine.load_or_create_attest_key(str(tmp_path), "m1", passphrase=PASS)
    assert pub1 == pub2
    # a clear attestation key from before is sealed in place at the next start, same key
    _, pub3 = confine.load_or_create_attest_key(str(tmp_path), "m2")
    assert not crypto.file_is_sealed(str(tmp_path / "m2.attestkey"))
    _, pub4 = confine.load_or_create_attest_key(str(tmp_path), "m2", seal_with=PASS)
    assert pub3 == pub4 and crypto.file_is_sealed(str(tmp_path / "m2.attestkey"))
