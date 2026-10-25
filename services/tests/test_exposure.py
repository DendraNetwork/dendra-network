"""The shared reading of the public-exposure flags (`modea.exposure`).

Every service reads `DENDRA_PUBLIC` through this one function, so a value such as `true` cannot arm some
services and leave others unarmed without a message. The vocabulary is closed and a value outside it is
refused: a security flag does not guess what an unknown value means.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modea.exposure import (InvalidFlag, security_flag,  # noqa: E402
                            listens_locally, public_enabled, public_from_env)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " True ", "yes", "Yes", "on", "ON"])
def test_the_forms_that_ARM(value):
    assert public_enabled(value) is True


@pytest.mark.parametrize("value", [None, "", "   ", "0", "false", "FALSE", "no", "off"])
def test_the_forms_that_DO_NOT_ARM(value):
    """An absent variable (`None`) and an empty one are the two ways of not setting it: they must read
    identically, otherwise `DENDRA_PUBLIC=` becomes a third state."""
    assert public_enabled(value) is False


# "oui" is the French yes, kept as a case: the vocabulary is closed, a yes in another language is refused.
@pytest.mark.parametrize("value", ["2", "-1", "oui", "ture", "yes please", "True!", "enabled"])
def test_an_UNEXPECTED_value_refuses_instead_of_falling_back_to_permissive(value):
    """The point of the module. Folding the unknown onto the permissive default would turn a typo into a
    silent exposure: the service starts with its guards disarmed while believing itself public. On a
    security criterion, a value that is not understood never counts as "compliant"."""
    with pytest.raises(InvalidFlag):
        public_enabled(value)


def test_the_refusal_reason_NAMES_the_variable_and_the_accepted_forms():
    """A refusal that does not say what to set gets worked around by disarming the guard."""
    with pytest.raises(InvalidFlag) as e:
        security_flag("ture", "DENDRA_ATTEST_REQUIRE")
    reason = str(e.value)
    assert "DENDRA_ATTEST_REQUIRE" in reason and "true" in reason and "'ture'" in reason


def test_reading_from_an_injectable_environment():
    assert public_from_env({"DENDRA_PUBLIC": "yes"}) is True
    assert public_from_env({}) is False, "absent variable = inert"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "::1", " 127.0.0.1 "])
def test_addresses_confined_to_the_machine(host):
    assert listens_locally(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "myhost.local"])
def test_network_addresses(host):
    assert listens_locally(host) is False


@pytest.mark.parametrize("host", ["", None, "   "])
def test_the_EMPTY_address_is_a_network_listen(host):
    """`bind("")` means INADDR_ANY: an empty string listens on all interfaces. Classing it as loopback
    silenced every guard that depends on the address — exactly where those guards are needed, since a
    variable set to empty is the most common shape of a sloppy environment file."""
    assert listens_locally(host) is False


# The judge model's raw echo: a diagnostic channel that copies user-derived text.

@pytest.fixture
def env_judge(monkeypatch):
    monkeypatch.delenv("DENDRA_PUBLIC", raising=False)
    monkeypatch.delenv("DENDRA_JUDGE_DEBUG", raising=False)
    return monkeypatch


def test_the_raw_echo_is_SILENT_by_default(env_judge):
    from modea.judge import raw_dump_allowed
    assert raw_dump_allowed() is False


def test_the_raw_echo_turns_on_upon_explicit_request(env_judge):
    """Witness against a decorative guard: without it, an unconditional `return False` would pass this
    whole block."""
    from modea.judge import raw_dump_allowed
    env_judge.setenv("DENDRA_JUDGE_DEBUG", "1")
    assert raw_dump_allowed() is True


def test_the_value_that_TURNS_IT_OFF_can_no_longer_TURN_IT_ON(env_judge):
    """A bare `os.environ.get("DENDRA_JUDGE_DEBUG")` made "0" truthy: the value an operator writes to
    close the channel opened it. The polarity of a leak channel cannot be inverted."""
    from modea.judge import raw_dump_allowed
    for off_value in ("0", "false", "off", ""):
        env_judge.setenv("DENDRA_JUDGE_DEBUG", off_value)
        assert raw_dump_allowed() is False, off_value


def test_public_exposure_overrides_the_diagnostic_request(env_judge):
    """stderr is a container log: under public exposure the echo stays silent even when requested. An
    unreadable `DENDRA_PUBLIC` value also closes the channel — ambiguity never opens it."""
    from modea.judge import raw_dump_allowed
    env_judge.setenv("DENDRA_JUDGE_DEBUG", "1")
    for public in ("1", "true", "ture"):
        env_judge.setenv("DENDRA_PUBLIC", public)
        assert raw_dump_allowed() is False, public


# Output screening: armed by exposure, no longer by a variable nobody sets.

@pytest.fixture
def env_filter(monkeypatch):
    monkeypatch.delenv("DENDRA_PUBLIC", raising=False)
    monkeypatch.delenv("DENDRA_SCREEN_OUTPUT", raising=False)
    return monkeypatch


def test_output_is_screened_as_soon_as_exposure_is_declared(env_filter):
    """`DENDRA_SCREEN_OUTPUT` defaulted to 0 and was set by no file in the kit: the anti-jailbreak
    screening of output was announced and never applied. A guard that is not applied cannot be
    announced, so this one arms on a fact the stack already declares."""
    from content_filter import _screen_output_armed
    for public in ("1", "true", "yes"):
        env_filter.setenv("DENDRA_PUBLIC", public)
        assert _screen_output_armed() is True, public


def test_a_closed_stack_keeps_the_default_without_latency(env_filter):
    """Witness against a decorative guard: without it, an unconditional `return True` would pass the
    test above. A development machine changes neither behaviour nor latency."""
    from content_filter import _screen_output_armed
    assert _screen_output_armed() is False


def test_the_dedicated_variable_HARDENS_a_closed_stack_further(env_filter):
    from content_filter import _screen_output_armed
    env_filter.setenv("DENDRA_SCREEN_OUTPUT", "1")
    assert _screen_output_armed() is True


@pytest.mark.parametrize("var", ["DENDRA_PUBLIC", "DENDRA_SCREEN_OUTPUT"])
def test_an_unreadable_flag_ARMS_the_filter(env_filter, var):
    """The opposite direction from the diagnostic channel, and for the same reason: the consequence of
    failure decides. Doubt arms a filter; it silences a leak."""
    from content_filter import _screen_output_armed
    env_filter.setenv(var, "ture")
    assert _screen_output_armed() is True
