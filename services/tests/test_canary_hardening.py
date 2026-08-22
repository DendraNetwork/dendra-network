"""Hardened canaries and best-effort zeroisation.

Canaries: 'needle' mode (no public delimiter), OUTPUT canary, and detection that survives removal of
the wrapper. These canaries remain circumventable by a miner that sees the plaintext, which Mode A
allows by construction; the tests validate TRACEABILITY and stripping, not a cryptographic proof
against leakage."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modea import canary as canary_mod
from modea import hardening


def test_needle_mode_has_no_public_delimiter():
    can = canary_mod.make_canary()
    txt = canary_mod.embed("my prompt", can, mode="needle")
    assert can.token in txt
    assert "[[ref:" not in txt          # no publicly recognisable marker in needle mode
    assert canary_mod.strip(txt).strip() == "my prompt"  # cleaned before the client sees it


def test_output_canary_instruction_and_strip():
    can = canary_mod.make_canary()
    instr = canary_mod.output_canary_instruction(can)
    assert can.token in instr
    # Simulates a model answer that did emit the sentinel.
    answer = f"here is the answer.\n[[sentinel:{can.token}]]"
    assert can.token not in canary_mod.strip(answer)


def test_detect_robust_to_stripped_wrapper():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "miner-XYZ")
    # The miner REMOVED the [[ref:]] wrapper but left the bare token in the resold transcript.
    leaked = f"prompt resold in the clear {can.token} answer..."
    assert reg.detect(leaked) == ["miner-XYZ"]


def test_detect_output_sentinel():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "miner-OUT")
    leaked = f"a leaked answer\n[[sentinel:{can.token}]]"
    assert reg.detect(leaked) == ["miner-OUT"]


def test_detect_no_false_positive():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "m1")
    # An arbitrary 16-hex string that was never registered must incriminate nobody.
    assert reg.detect("0123456789abcdef other text") == []


def test_detect_dedup_multiple_occurrences():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "m1")
    leaked = f"{can.token} ... {can.token} ... [[sentinel:{can.token}]]"
    assert reg.detect(leaked) == ["m1"]  # deduplicated


# --------------------------- best-effort zeroisation --------------------------
def test_best_effort_wipe_bytearray():
    ba = bytearray(b"\xaa" * 48)
    assert hardening.best_effort_wipe(ba) is True
    assert all(b == 0 for b in ba)


def test_best_effort_wipe_empty_and_none():
    assert hardening.best_effort_wipe(b"") is True
    assert hardening.best_effort_wipe(None) is False


def test_best_effort_wipe_bytes_does_not_raise():
    # Immutable bytes: best effort, so it may fail depending on the interpreter, but it must NEVER raise.
    data = bytes(bytearray(b"secret-prompt-xyz"))
    res = hardening.best_effort_wipe(data)
    assert res in (True, False)


def test_securebytes_view_into_zerocopy():
    with hardening.SecureBytes.copy_from(b"abc123") as sb:
        mv = sb.view_into()
        assert bytes(mv) == b"abc123"
        del mv  # release the view before leaving the with block, or resize raises BufferError
    assert all(b == 0 for b in sb.buf)
