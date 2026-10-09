# The miner's keyring module (modea/keyring.py) decides from the STATE OF A DIRECTORY on disk, and reads a
# passphrase from a FILE. Without this fixture every case that reaches it -- a relay deposit, a signature,
# the self-test -- would read the keyring of the machine running the bench (~/.dendra) and its passphrase
# file: a case that passes on one machine and refuses on another (a developer's home holding both a test
# and a file keyring is refused as "two keyrings") measures the machine, not the code.
# Each case gets an EMPTY keyring directory and a passphrase path that does not exist; a case that needs
# another state builds it itself, under its own tmp_path.
import pytest


@pytest.fixture(autouse=True)
def _hermetic_keyring(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("keyring-hermetic")
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(base / "keyring"))
    monkeypatch.setenv("DENDRA_KEYRING_PASSPHRASE_FILE", str(base / "no-passphrase-file"))
    monkeypatch.delenv("DENDRA_MINER_PASSPHRASE", raising=False)
    monkeypatch.delenv("DENDRA_HOME", raising=False)
