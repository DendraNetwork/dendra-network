"""Builds a `dendrad` command line whose positional values cannot be read as options.

Why this exists. The miner anchors its content commitment by passing an embedding
vector, serialised as a comma-separated list of signed integers, as a positional argument:

    dendrad tx jobs create-commit <key> -813148,-469320,1048576,... ...

About half of those vectors begin with a negative number. Cobra/pflag then reads the leading `-` as an
option prefix and fails with `unknown shorthand flag: '8'`. The job is then retried on every pass with
the response posted and the commitment never anchored: the miner serves the work, seals the answer and
publishes it, and its commitment never reaches the chain.

The same cause makes GNU grep read a pattern that starts with dashes as a run of options: it returns
rc=2 with empty output. A value that may begin with `-` is not safe as a positional unless `--`
precedes it.

The contract, and it is deliberately explicit rather than clever:
  · the caller states which arguments are flags and which are positionals. No heuristic infers it.
    A splitter that guessed ("a token starting with `--` is a flag, the next one is its value") would
    be wrong the first time a valueless flag is followed by a positional — and it would be wrong
    silently, which is how this class of bug survives.
  · every flag, the sub-command's own and the global ones, goes before the terminator; pflag stops
    parsing at `--`, so a flag placed after it is delivered to the program as literal text.
  · `--` is emitted unconditionally, not only when a value looks dangerous. A separator that appears
    only for inputs someone judged risky is a separator that is absent on the day the judgement is
    wrong, and its absence is invisible until then.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence

# The margin over a simulation's gas estimate, for every transaction the miner builds: the ones it sends
# (`miner.tx_from`) and the ones it prepares for its owner to sign (`modea.owner_tx`). One value,
# written once.
GAS_ADJUSTMENT = "1.6"


def dendrad_argv(base: Sequence[str], sub: str,
                 positionals: Iterable[str] = (),
                 flags: Iterable[str] = ()) -> List[str]:
    """Assembles `[*base, sub, *flags, '--', *positionals]`.

    `base` is the command prefix (e.g. `("dendrad", "tx", "jobs")`), `sub` the sub-command,
    `flags` every option with its values, `positionals` the operands — any of which may begin
    with `-` without being mistaken for an option.
    """
    return [*base, sub, *flags, "--", *[str(p) for p in positionals]]


def cli_usage_error(out: str) -> str:
    """Returns the pflag/cobra usage error carried by `out`, or "" when there is none.

    A usage error has no `raw_log` and no `code`. Error extractors on this codebase look for those
    two fields and fall back to the tail of the output. A usage error puts its message at the head and
    then echoes the offending argument, so the tail is the argument itself: the log printed the
    embedding vector where the reason belonged, and the defect stayed unreadable for as long as it
    stayed unread. This function exists so the reason is looked for where it actually is.
    """
    for line in (out or "").splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith(("unknown shorthand flag", "unknown flag", "flag needs an argument",
                           "unknown command", "bad flag syntax", "invalid argument")):
            return s[:200]
    return ""
