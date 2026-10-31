#!/usr/bin/env python3
"""Mutation harness for `test_relay_auth_message.py` -- does that bench know how to say no?

A bench reporting green attests to nothing until each property it claims to hold is broken on
purpose and the bench turns red for it. This one damages `relay_client.py` in several ways, each
re-creating a way a refusal message can claim more than it read, and requires a failure every time.

It never touches the working tree. Every mutation is applied to a copy under a temp directory,
which also carries a copy of the bench -- the bench imports `relay_client` from its own directory,
so the copy is what gets exercised. Nothing here restores by `git checkout`: the fix under test is
typically not committed yet, and a restore would erase it.

A needle that no longer matches exactly once is not a kill. It is a mutation that never happened,
and counting it green would be the reassuring default this repository forbids. Such a case is
reported as not applied, and the harness fails.

Usage: python3 test_relay_auth_message_mutation.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "relay_client.py")
BENCH = os.path.join(HERE, "test_relay_auth_message.py")

# (name, the defect it re-creates, needle, replacement)
MUTATIONS = [
    ("one sentence again serves every route",
     "a consequence copied across call sites instead of derived from the route",
     'tail = f" -> {cost}." if cost else " -> what this costs is not established here."',
     'tail = " -> no job will be received."'),

    ("the flood bound goes back to one key for the whole service",
     "the least informative refusal mutes the most informative one",
     '    key = (method, route)',
     '    key = ("ALL", "ALL")'),

    ("a refused re-deposit again asserts an absence",
     "a message describing a relay state it never read",
     '    ("POST", "pub"): "this deposit did not land -- if the relay holds no earlier copy, the "',
     '    ("POST", "pub"): "the relay copy of the encryption key is missing, and "'),

    ("the 401 body is discarded again, so the cause is inferred",
     "blaming the token while the relay stated an anti-replay hit",
     '        raw = exc.read(2048)',
     '        raw = b""'),

    ("the remote reason is adopted verbatim, unbounded and unattributed",
     "text from a remote service printed as this client's own verdict",
     '    why = " ".join(str(said).split())[:160]',
     '    why = str(said)'),

    ("the relay's help is dropped, so the operator is never told where to read",
     "a refusal that carries a pointer and prints without it",
     '    return why, help_text',
     '    return why, ""'),

    ("the help is adopted verbatim, unbounded and on several lines",
     "text from a remote service printed without a bound",
     '    help_text = " ".join(pointer.split())[:240]',
     '    help_text = pointer'),

    ("a default pointer takes the help's place when the relay sends none",
     "a message that points somewhere the relay never named",
     '        pointer = doc.get("help") or ""',
     '        pointer = doc.get("help") or "https://dendranetwork.com/docs/#relay-refusals"'),

    ("a help that is not a string is quoted anyway",
     "a structure printed as if it were a sentence",
     '    if not isinstance(pointer, str):\n        pointer = ""',
     '    pointer = str(pointer) if pointer else ""'),

    ("the help is shown only next to a reason",
     "a pointer withheld whenever the relay gives no reason",
     'if pointer else ""',
     'if pointer and said else ""'),

    ("the help is read and never printed",
     "a pointer that is computed and never printed",
     'and restart.{more}",',
     'and restart.",'),

    ("an unlisted route is given a default consequence",
     "an unknown route inheriting the cost of another one",
     '    cost = _COST.get(key)',
     '    cost = _COST.get(key, "this miner will not see the jobs waiting for it")'),

    # This one must destroy both mentions. Erasing the route from the opening clause alone leaves
    # the second clause naming it, so "the route is named" still holds and a green result is correct
    # -- the mutation would be measuring itself, not the bench. Weakening the bench to force a red
    # would be the opposite mistake.
    ("the route disappears from the printed line (both mentions)",
     "an anonymous refusal: you know you were refused, never on what",
     'print(f"[relay] relay refused {method} {route} with {code}{cause}.{tail} "\n'
     '          f"This refusal concerns {method} {route} and nothing else: the relay decides access per "',
     'print(f"[relay] relay refused a request with {code}{cause}.{tail} "\n'
     '          f"This refusal concerns one route and nothing else: the relay decides access per "'),
]


def run(mutated_src):
    """Run the bench against a mutated copy and return (exit code, its summary line)."""
    d = tempfile.mkdtemp(prefix="dendra-mut-relay-")
    try:
        with open(os.path.join(d, "relay_client.py"), "w", encoding="utf-8") as f:
            f.write(mutated_src)
        shutil.copy(BENCH, os.path.join(d, "test_relay_auth_message.py"))
        p = subprocess.run([sys.executable, "test_relay_auth_message.py"],
                           cwd=d, capture_output=True, text=True, timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        summary = [l for l in out.splitlines() if "SUMMARY" in l]
        return p.returncode, (summary[-1] if summary else (out.strip().splitlines() or [""])[-1])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    with open(CLIENT, encoding="utf-8") as f:
        src = f.read()

    # The witness first. An unmutated copy must be green, or every red below proves only that the
    #    copy is broken -- a harness whose baseline is not measured cannot attribute its own results.
    rc0, r0 = run(src)
    print(f"  witness  unmutated copy -> rc={rc0}  {r0}")
    if rc0 != 0:
        print("Harness unusable: the unmutated copy is already red, so no mutation is attributable. "
              "Nothing is concluded.")
        return 2

    killed = applied = 0
    for name, recreates, needle, replacement in MUTATIONS:
        if src.count(needle) != 1:
            print(f"  not applied  {name}  (needle found {src.count(needle)} times, expected 1)")
            continue
        applied += 1
        rc, r = run(src.replace(needle, replacement))
        if rc != 0:
            killed += 1
            print(f"  killed  {name}\n            (re-creates: {recreates})\n            rc={rc}  {r}")
        else:
            print(f"  survives  {name}\n            (re-creates: {recreates}) -> the bench does not "
                  f"judge this property\n            rc={rc}  {r}")

    print()
    print(f"MUTATION_RELAY_AUTH_MESSAGE_SUMMARY killed={killed}/{len(MUTATIONS)} applied={applied}")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
