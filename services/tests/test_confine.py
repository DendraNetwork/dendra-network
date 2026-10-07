"""Tests of the Mode A process confinement (confine.py). Best effort: they check the STRUCTURE of the
report and the idempotence of the cache, not OS-specific values, which vary by platform and CI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modea import confine


def test_report_has_all_keys():
    rep = confine.apply_process_confinement(mlockall=False)
    for k in ("platform", "non_dumpable", "no_new_privs", "core_dumps_disabled",
              "umask_0077", "mlockall", "mlockall_requested", "residual_root_risk"):
        assert k in rep, f"missing key: {k}"
    # honesty: the residual root risk is ALWAYS reported
    assert rep["residual_root_risk"] is True
    # mlockall=False requested -> nothing is locked
    assert rep["mlockall"] is False and rep["mlockall_requested"] is False


def test_cache_idempotent():
    a = confine.apply_process_confinement_report_cached()
    b = confine.apply_process_confinement_report_cached()
    assert a is b  # the cache returns the SAME object (apply runs only once)


def test_attestation_measures_code():
    att = confine.confinement_attestation()
    assert "code_manifest" in att and isinstance(att["code_manifest"], dict)
    assert "confine.py" in att["code_manifest"]      # the module measures itself
    assert "confinement" in att
