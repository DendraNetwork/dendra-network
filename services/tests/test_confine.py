"""Tests du confinement processus Mode A (confine.py). Best-effort : on vérifie la STRUCTURE du
rapport et l'idempotence du cache, pas des valeurs OS spécifiques (varient selon la plateforme/CI)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modea import confine


def test_report_has_all_keys():
    rep = confine.apply_process_confinement(mlockall=False)
    for k in ("platform", "non_dumpable", "no_new_privs", "core_dumps_disabled",
              "umask_0077", "mlockall", "mlockall_requested", "residual_root_risk"):
        assert k in rep, f"clé manquante: {k}"
    # honnêteté : le risque résiduel root est TOUJOURS signalé
    assert rep["residual_root_risk"] is True
    # mlockall=False demandé -> pas verrouillé
    assert rep["mlockall"] is False and rep["mlockall_requested"] is False


def test_cache_idempotent():
    a = confine.apply_process_confinement_report_cached()
    b = confine.apply_process_confinement_report_cached()
    assert a is b  # le cache renvoie le MÊME objet (apply ne s'exécute qu'une fois)


def test_attestation_measures_code():
    att = confine.confinement_attestation()
    assert "code_manifest" in att and isinstance(att["code_manifest"], dict)
    assert "confine.py" in att["code_manifest"]      # le module se mesure lui-même
    assert "confinement" in att
