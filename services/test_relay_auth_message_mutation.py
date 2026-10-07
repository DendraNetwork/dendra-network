#!/usr/bin/env python3
"""Mutation harness for `test_relay_auth_message.py` -- does that bench know how to say NO?

A bench reporting green attests to nothing until each property it claims to hold is BROKEN on
purpose and the bench turns red for it. This one damages `relay_client.py` in seven ways, each
re-creating a way a refusal message can claim more than it read, and requires a KO every time.

⚠️ IT NEVER TOUCHES THE WORKING TREE. Every mutation is applied to a COPY under a temp directory,
which also carries a copy of the bench -- the bench imports `relay_client` from its OWN directory,
so the copy is what gets exercised. Nothing here restores by `git checkout`: the fix under test is
typically not committed yet, and a restore would erase it.

⚠️ A NEEDLE THAT NO LONGER MATCHES EXACTLY ONCE IS NOT A KILL. It is a mutation that never happened,
and counting it green would be the reassuring default this repository forbids. Such a case is
reported as NOT APPLIED, and the harness fails.

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
     'suite = f" -> {cout}." if cout else " -> what this costs is not established here."',
     'suite = " -> no job will be received."'),

    ("the flood bound goes back to ONE key for the whole service",
     "the least informative refusal mutes the most informative one",
     '    cle = (methode, route)',
     '    cle = ("ALL", "ALL")'),

    ("a refused re-deposit again ASSERTS an absence",
     "a message describing a relay state it never read",
     '    ("POST", "pub"): "this deposit did not land -- if the relay holds no earlier copy, the "',
     '    ("POST", "pub"): "the relay copy of the encryption key is missing, and "'),

    ("the 401 body is discarded again, so the cause is inferred",
     "blaming the token while the relay stated an anti-replay hit",
     '        brut = exc.read(512)',
     '        brut = b""'),

    ("the remote reason is adopted verbatim, unbounded and unattributed",
     "text from a remote service printed as this client's own verdict",
     '    return " ".join(str(dit).split())[:160]',
     '    return str(dit)'),

    ("an unlisted route is given a DEFAULT consequence",
     "an unknown route inheriting the cost of another one",
     '    cout = _COST.get(cle)',
     '    cout = _COST.get(cle, "this miner will NOT see the jobs waiting for it")'),

    # ⚠️ THIS ONE MUST DESTROY BOTH MENTIONS. Erasing the route from the opening clause alone leaves
    # the second clause naming it, so "the route is named" still holds and a green result is CORRECT
    # -- the mutation would be measuring itself, not the bench. Weakening the bench to force a red
    # would be the opposite mistake.
    ("the route disappears from the printed line (BOTH mentions)",
     "an anonymous refusal: you know you were refused, never on what",
     'print(f"[relay] relay refused {methode} {route} with {code}{cause}.{suite} "\n'
     '          f"THIS REFUSAL CONCERNS {methode} {route} AND NOTHING ELSE: the relay decides access per "',
     'print(f"[relay] relay refused a request with {code}{cause}.{suite} "\n'
     '          f"THIS REFUSAL CONCERNS ONE ROUTE AND NOTHING ELSE: the relay decides access per "'),
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
        summary = [l for l in out.splitlines() if "RESUME" in l]
        return p.returncode, (summary[-1] if summary else (out.strip().splitlines() or [""])[-1])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    with open(CLIENT, encoding="utf-8") as f:
        src = f.read()

    # ① THE WITNESS FIRST. An unmutated copy must be GREEN, or every red below proves only that the
    #    copy is broken -- a harness whose baseline is not measured cannot attribute its own results.
    rc0, r0 = run(src)
    print(f"  witness  UNMUTATED copy -> rc={rc0}  {r0}")
    if rc0 != 0:
        print("HARNESS UNUSABLE: the unmutated copy is already red, so no mutation is attributable. "
              "Nothing is concluded.")
        return 2

    killed = applied = 0
    for name, recreates, needle, replacement in MUTATIONS:
        if src.count(needle) != 1:
            print(f"  ⛔ NOT APPLIED  {name}  (needle found {src.count(needle)} times, expected 1)")
            continue
        applied += 1
        rc, r = run(src.replace(needle, replacement))
        if rc != 0:
            killed += 1
            print(f"  ✓ KILLED  {name}\n            (re-creates: {recreates})\n            rc={rc}  {r}")
        else:
            print(f"  ⛔ SURVIVES  {name}\n            (re-creates: {recreates}) -> the bench does NOT "
                  f"judge this property\n            rc={rc}  {r}")

    print()
    print(f"MUTATION_RELAY_AUTH_MESSAGE_RESUME killed={killed}/{len(MUTATIONS)} applied={applied}")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
