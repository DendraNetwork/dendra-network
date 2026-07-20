"""Tests du durcissement : mémoire sécurisée, anti core-dump, attestation logicielle."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modea import hardening
from modea.hardening import SecureBytes, build_manifest, self_attest


def test_securebytes_roundtrip_and_zeroize():
    sb = SecureBytes.copy_from(b"secret-4471-A")
    assert sb.view() == b"secret-4471-A"
    sb.close()
    assert all(b == 0 for b in sb.buf)          # zeroise apres fermeture


def test_securebytes_context_manager():
    with SecureBytes.copy_from(b"abc") as sb:
        assert sb.view() == b"abc"
    assert all(b == 0 for b in sb.buf)


def test_self_attest_ok():
    manifest = build_manifest()
    ok, mismatches = self_attest(manifest)
    assert ok and mismatches == []


def test_self_attest_detects_tamper():
    manifest = build_manifest()
    # simule un binaire altere : on falsifie le hash attendu d'un fichier
    name = next(iter(manifest))
    manifest[name] = "0" * 64
    ok, mismatches = self_attest(manifest)
    assert not ok and name in mismatches


def test_harden_process_runs():
    rep = hardening.harden_process()
    assert "platform" in rep and "memory_locking" in rep and "core_dumps_disabled" in rep
