"""Tests des canaries durcis (B0.5) + de la zéroisation best-effort (hardening).

Canaries : mode 'needle' (sans délimiteur public), canary de SORTIE, détection robuste au retrait du
wrapper. HONNÊTE : ces canaries restent contournables par un mineur qui voit le clair (Mode A) ; les
tests valident la TRAÇABILITÉ et le strip, pas une preuve anti-fuite cryptographique."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modea import canary as canary_mod
from modea import hardening


def test_needle_mode_has_no_public_delimiter():
    can = canary_mod.make_canary()
    txt = canary_mod.embed("mon prompt", can, mode="needle")
    assert can.token in txt
    assert "[[ref:" not in txt          # pas de balise regex publique en mode needle
    assert canary_mod.strip(txt).strip() == "mon prompt"  # nettoyé pour le client


def test_output_canary_instruction_and_strip():
    can = canary_mod.make_canary()
    instr = canary_mod.output_canary_instruction(can)
    assert can.token in instr
    # simule une réponse modèle qui a bien émis la sentinelle
    answer = f"voici la reponse.\n[[sentinel:{can.token}]]"
    assert can.token not in canary_mod.strip(answer)


def test_detect_robust_to_stripped_wrapper():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "miner-XYZ")
    # le mineur a RETIRÉ le wrapper [[ref:]] mais laissé le token nu dans le transcript revendu
    leaked = f"prompt revendu en clair {can.token} reponse..."
    assert reg.detect(leaked) == ["miner-XYZ"]


def test_detect_output_sentinel():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "miner-OUT")
    leaked = f"une reponse fuite\n[[sentinel:{can.token}]]"
    assert reg.detect(leaked) == ["miner-OUT"]


def test_detect_no_false_positive():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "m1")
    # un hex16 quelconque NON enregistré ne doit incriminer personne
    assert reg.detect("0123456789abcdef autre texte") == []


def test_detect_dedup_multiple_occurrences():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "m1")
    leaked = f"{can.token} ... {can.token} ... [[sentinel:{can.token}]]"
    assert reg.detect(leaked) == ["m1"]  # dédupliqué


# --------------------------- zéroisation best-effort --------------------------
def test_best_effort_wipe_bytearray():
    ba = bytearray(b"\xaa" * 48)
    assert hardening.best_effort_wipe(ba) is True
    assert all(b == 0 for b in ba)


def test_best_effort_wipe_empty_and_none():
    assert hardening.best_effort_wipe(b"") is True
    assert hardening.best_effort_wipe(None) is False


def test_best_effort_wipe_bytes_does_not_raise():
    # bytes immuable : best-effort (peut échouer selon CPython) mais ne doit JAMAIS lever
    data = bytes(bytearray(b"secret-prompt-xyz"))
    res = hardening.best_effort_wipe(data)
    assert res in (True, False)


def test_securebytes_view_into_zerocopy():
    with hardening.SecureBytes.copy_from(b"abc123") as sb:
        mv = sb.view_into()
        assert bytes(mv) == b"abc123"
        del mv  # libère la vue avant la sortie du with (sinon BufferError sur resize)
    assert all(b == 0 for b in sb.buf)
