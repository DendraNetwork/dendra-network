# The miner's keyring module (modea/keyring.py) decides from the state of a directory on disk, and reads a
# passphrase from a file. Without this fixture every case that reaches it -- a relay deposit, a signature,
# the self-test -- would read the keyring of the machine running the bench (~/.dendra) and its passphrase
# file: a case that passes on one machine and refuses on another (a developer's home holding both a test
# and a file keyring is refused as "two keyrings") measures the machine, not the code.
# Each case gets an empty keyring directory and a passphrase path that does not exist; a case that needs
# another state builds it itself, under its own tmp_path.
import importlib.abc
import importlib.util
import os
import sys

import pytest

# The mutation bench (dendra/onchain-staging/dendra_saison_vague1_test.sh) plays a case against a copy of one module,
# named by DENDRA_MODEA_DIR, and expects red. The test files put that directory first on sys.path, but a delivered
# module that inserts its own directory at the front (`sys.path.insert(0, ...)` in final_season_payout.py,
# final_season_server.py, final_season_generator.py, final_season_miner.py) pushes the copy behind the original as
# soon as it is imported: every module imported after it comes from the original, the mutation is invisible, and
# the case passes with every sign of a red that did not happen. Measured: a faucet mutation red under `-k faucet`
# and green on the whole file. This finder answers first for any module whose file is in the copy, whatever the
# order of sys.path, and the fixture below refuses a session in which such a module was loaded from elsewhere.
MODEA_DIR = os.environ.get("DENDRA_MODEA_DIR") or ""


class _CopyFirst(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if path is not None or "." in name:
            return None                              # top-level modules only, as the copy holds them
        p = os.path.join(MODEA_DIR, name + ".py")
        return importlib.util.spec_from_file_location(name, p) if os.path.isfile(p) else None


if MODEA_DIR:
    sys.meta_path.insert(0, _CopyFirst())


@pytest.fixture(autouse=True, scope="session")
def _modules_come_from_the_copy():
    yield
    if not MODEA_DIR:
        return
    root = os.path.abspath(MODEA_DIR)
    copied = {n[:-3] for n in os.listdir(root) if n.endswith(".py")}
    elsewhere = sorted(n for n in copied if n in sys.modules
                       and not os.path.abspath(getattr(sys.modules[n], "__file__", "") or "").startswith(root))
    assert not elsewhere, f"DENDRA_MODEA_DIR={MODEA_DIR}: {elsewhere} loaded from outside the copy, the case measured the original"


# The season service requires the first day of the doubled rates, and the first day of decision 23, until a start
# has fixed each (`final_season_server.State._first_day_of`), and reads both parameters once, when the module is
# imported. These cases run a service with neither day unless they pass one; set here, before any test module
# imports the service, so a value left in the shell of the machine running the bench is never what a case measures.
os.environ["DENDRA_FINAL_SEASON_DOUBLED_FROM_DAY"] = "none"
os.environ["DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY"] = "none"
# Decision 24 (final_season_rules.RULES_KIT): no day either, and no kit floor from the shell: a case that needs
# the set fixes the bench floor itself (final_season_rules.set_kit_min), the same one in every module.
os.environ["DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY"] = "none"
os.environ.pop("DENDRA_KIT_MIN", None)


@pytest.fixture(autouse=True)
def _hermetic_keyring(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("keyring-hermetic")
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(base / "keyring"))
    monkeypatch.setenv("DENDRA_KEYRING_PASSPHRASE_FILE", str(base / "no-passphrase-file"))
    monkeypatch.delenv("DENDRA_MINER_PASSPHRASE", raising=False)
    monkeypatch.delenv("DENDRA_HOME", raising=False)
