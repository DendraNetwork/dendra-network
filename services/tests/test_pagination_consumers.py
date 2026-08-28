"""NO CONSUMER OF `list-*` MAY READ A SINGLE PAGE.

════════════════════════════════════════════════════════════════════════════════════════════════
THE CLASS, NOT THE OCCURRENCE. The chain paginates correctly (`query.CollectionPaginate`); the risk
lives AT THE CALLER: one that queries once and ignores `pagination.next_key` sees only the first
page — and the SDK applies `DefaultLimit = 100` when the request carries no limit. This defect has
been fixed at least three times in this repository (`client.list_all`, `c3_gate.rpc_all`,
`reveal_helpers.query_all`), each time on the caller that had just been noticed — and one more,
`capacity_server`, surfaced weeks later.

WHAT MAKES THIS FAMILY SO EXPENSIVE: truncation produces only GOOD news. Fewer disputed jobs, fewer
pending audits, fewer registered miners. The failure mode is indistinguishable from a healthy
network, so there is no moment at which anyone thinks "that looks odd". Only a guard placed on the
SHAPE of the call can catch it.

The check is an ALLOW-LIST: a call is accepted if it goes through a KNOWN paginating reader, or if it
carries `--page-limit` itself. A NEW reader goes red until it is declared here — deliberately. A
deny-list protects only against what could be named the day it was written.

WHAT THIS CHECK DOES NOT SEE, stated plainly: it reads the SHAPE of the code, not its execution. A
dynamically built call (subcommand name in a variable, command assembled by concatenation) escapes
it. It closes the usual front door; it does not replace the benches that exercise the readers
themselves (`test_reveal_worker.py` and `test_capacity_pagination.py`).
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Declared PAGINATING readers. Adding a name here is a decision: it asserts that the function loops on
# `pagination.next_key` AND returns a failure (never a partial list) when it cannot finish. The four
# listed do so, each with its own bench.
PAGINATING_READERS = ("rpc_all", "list_all", "query_all", "_rest_all")

RE_LIST = re.compile(r"""["']list-(job|miner|commit|beacon)["']""")

# Test files are excluded: they quote the subcommands to FABRICATE responses, not to query a chain.
# Forbidding them there would turn the bench that exercises the guard red.
def _sources():
    files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "modea").glob("*.py"))
    return [f for f in files if not f.name.startswith("test_")]


def test_the_sweep_really_bites():
    """A check that finds no occurrence is indistinguishable from a check that does not look."""
    src = _sources()
    assert len(src) >= 20, f"only {len(src)} sources scanned: the path is wrong"
    n = sum(len(RE_LIST.findall(f.read_text("utf-8", errors="replace"))) for f in src)
    assert n >= 8, (
        f"only {n} `list-*` call(s) found in {len(src)} files. Either the repository changed "
        "radically, or the expression no longer bites - in both cases this guard guards nothing and "
        "must be re-read before being believed.")


def test_no_list_call_reads_a_single_page():
    offenders = []
    for f in _sources():
        lines = f.read_text("utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not RE_LIST.search(line):
                continue
            window = "\n".join(lines[i:i + 4])
            via_reader = any(h in line for h in PAGINATING_READERS)
            carries_limit = "page-limit" in window or "page_limit" in window
            if not (via_reader or carries_limit):
                offenders.append(f"{f.relative_to(ROOT)}:{i + 1}: {line.strip()[:90]}")
    assert not offenders, (
        "`list-*` call(s) that read only ONE page:\n  " + "\n  ".join(offenders) + "\n\n"
        "Without pagination the SDK returns 100 entries and stops - with no error. The response is a "
        "PREFIX of reality, and that prefix produces only good news: fewer disputed jobs, fewer open "
        "audits, fewer registered miners. Go through a paginating reader "
        f"({', '.join(PAGINATING_READERS)}), or carry `--page-limit` and loop on "
        "`pagination.next_key` - treating an INCOMPLETE read as a failure, never as a partial result.")


def test_witness_the_predicate_sees_a_bare_call():
    """WITNESS — the predicate above must be able to go red.

    The exact same decision is replayed on a fabricated line that violates the rule, and on a line
    that respects it. Without this witness, a weakened predicate (for instance an `any()` that
    accepted everything) would stay green and nobody would know.
    """
    bare = '        cmd = ["dendrad", "query", "jobs", "list-miner", "--output", "json"]'
    assert RE_LIST.search(bare), "the expression no longer recognizes a bare call"
    assert not any(h in bare for h in PAGINATING_READERS)
    assert "page-limit" not in bare

    correct = '    rows = rpc_all(node, "list-job", "job")'
    assert RE_LIST.search(correct)
    assert any(h in correct for h in PAGINATING_READERS), (
        "a call going through a declared paginating reader would be refused: the guard would produce "
        "a false red, and a false red disarms a guard as surely as a false green empties it")
