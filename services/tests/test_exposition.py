"""The SHARED reading of the public-exposure flags (`modea.exposition`).

WHAT THIS BENCH EXISTS TO PREVENT. `DENDRA_PUBLIC` used to be parsed by each service in its own way. An
operator setting `DENDRA_PUBLIC=true` therefore armed the refusals of the services that accepted "true"
and not those that recognised only "1" — with no message saying so, on the one flag that arms the whole
exposure guard set. A half-armed stack reports itself as armed, and that is the most expensive kind of
failure, because it is indistinguishable from a success.

The vocabulary is therefore CLOSED, and a value outside it REFUSES. A security flag does not guess the
intent behind an unknown value.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modea.exposition import (DrapeauInvalide, drapeau_securite,  # noqa: E402
                              ecoute_locale, public_actif, public_from_env)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " True ", "yes", "Yes", "on", "ON"])
def test_the_forms_that_ARM(value):
    assert public_actif(value) is True


@pytest.mark.parametrize("value", [None, "", "   ", "0", "false", "FALSE", "no", "off"])
def test_the_forms_that_DO_NOT_ARM(value):
    """An ABSENT variable (`None`) and an EMPTY one are the two ways of not setting it: they must read
    identically, otherwise `DENDRA_PUBLIC=` becomes a third state."""
    assert public_actif(value) is False


@pytest.mark.parametrize("value", ["2", "-1", "oui", "ture", "yes please", "True!", "enabled"])
def test_an_UNEXPECTED_value_refuses_instead_of_falling_back_to_permissive(value):
    """THE POINT OF THE MODULE. Folding the unknown onto the permissive default would turn a typo into a
    silent exposure: the service starts with its guards disarmed while believing itself public. On a
    security criterion, a value that is not understood never counts as "compliant"."""
    with pytest.raises(DrapeauInvalide):
        public_actif(value)


def test_the_refusal_reason_NAMES_the_variable_and_the_accepted_forms():
    """A refusal that does not say what to set gets worked around by disarming the guard."""
    with pytest.raises(DrapeauInvalide) as e:
        drapeau_securite("ture", "DENDRA_ATTEST_REQUIRE")
    reason = str(e.value)
    assert "DENDRA_ATTEST_REQUIRE" in reason and "true" in reason and "'ture'" in reason


def test_reading_from_an_injectable_environment():
    assert public_from_env({"DENDRA_PUBLIC": "yes"}) is True
    assert public_from_env({}) is False, "absent variable = inert"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "::1", " 127.0.0.1 "])
def test_addresses_confined_to_the_machine(host):
    assert ecoute_locale(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "monhote.local"])
def test_network_addresses(host):
    assert ecoute_locale(host) is False


@pytest.mark.parametrize("host", ["", None, "   "])
def test_the_EMPTY_address_is_a_network_listen(host):
    """`bind("")` means INADDR_ANY: an empty string listens on ALL interfaces. Classing it as loopback
    silenced every guard that depends on the address — exactly where those guards are needed, since a
    variable set to empty is the most common shape of a sloppy environment file."""
    assert ecoute_locale(host) is False


# -- THE JUDGE MODEL'S RAW ECHO: a diagnostic channel that copies user-derived text ----------------

@pytest.fixture
def env_judge(monkeypatch):
    monkeypatch.delenv("DENDRA_PUBLIC", raising=False)
    monkeypatch.delenv("DENDRA_JUDGE_DEBUG", raising=False)
    return monkeypatch


def test_the_raw_echo_is_SILENT_by_default(env_judge):
    from modea.judge import dump_brut_autorise
    assert dump_brut_autorise() is False


def test_the_raw_echo_turns_on_upon_explicit_request(env_judge):
    """WITNESS against a decorative guard: without it, an unconditional `return False` would pass this
    whole block."""
    from modea.judge import dump_brut_autorise
    env_judge.setenv("DENDRA_JUDGE_DEBUG", "1")
    assert dump_brut_autorise() is True


def test_the_value_that_TURNS_IT_OFF_can_no_longer_TURN_IT_ON(env_judge):
    """A bare `os.environ.get("DENDRA_JUDGE_DEBUG")` made "0" truthy: the value an operator writes to
    close the channel opened it. The polarity of a leak channel cannot be inverted."""
    from modea.judge import dump_brut_autorise
    for off_value in ("0", "false", "off", ""):
        env_judge.setenv("DENDRA_JUDGE_DEBUG", off_value)
        assert dump_brut_autorise() is False, off_value


def test_public_exposure_overrides_the_diagnostic_request(env_judge):
    """stderr is a container log: under public exposure the echo stays silent even when requested. An
    unreadable `DENDRA_PUBLIC` value also closes the channel — ambiguity never opens it."""
    from modea.judge import dump_brut_autorise
    env_judge.setenv("DENDRA_JUDGE_DEBUG", "1")
    for public in ("1", "true", "ture"):
        env_judge.setenv("DENDRA_PUBLIC", public)
        assert dump_brut_autorise() is False, public


# -- OUTPUT SCREENING: armed by exposure, no longer by a variable nobody sets ----------------------

@pytest.fixture
def env_filter(monkeypatch):
    monkeypatch.delenv("DENDRA_PUBLIC", raising=False)
    monkeypatch.delenv("DENDRA_SCREEN_OUTPUT", raising=False)
    return monkeypatch


def test_output_is_screened_as_soon_as_exposure_is_declared(env_filter):
    """`DENDRA_SCREEN_OUTPUT` defaulted to 0 and was set by no file in the kit: the anti-jailbreak
    screening of OUTPUT was announced and never applied. A guard that is not applied cannot be
    announced, so this one arms on a fact the stack already declares."""
    from content_filter import _screen_output_arme
    for public in ("1", "true", "yes"):
        env_filter.setenv("DENDRA_PUBLIC", public)
        assert _screen_output_arme() is True, public


def test_a_closed_stack_keeps_the_default_without_latency(env_filter):
    """WITNESS against a decorative guard: without it, an unconditional `return True` would pass the
    test above. A development machine changes neither behaviour nor latency."""
    from content_filter import _screen_output_arme
    assert _screen_output_arme() is False


def test_the_dedicated_variable_HARDENS_a_closed_stack_further(env_filter):
    from content_filter import _screen_output_arme
    env_filter.setenv("DENDRA_SCREEN_OUTPUT", "1")
    assert _screen_output_arme() is True


@pytest.mark.parametrize("var", ["DENDRA_PUBLIC", "DENDRA_SCREEN_OUTPUT"])
def test_an_unreadable_flag_ARMS_the_filter(env_filter, var):
    """The opposite direction from the diagnostic channel, and for the same reason: the consequence of
    failure decides. Doubt arms a filter; it silences a leak."""
    from content_filter import _screen_output_arme
    env_filter.setenv(var, "ture")
    assert _screen_output_arme() is True
