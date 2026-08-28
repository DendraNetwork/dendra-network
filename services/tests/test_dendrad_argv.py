"""Bench for the `dendrad` argument terminator.

⛔ THIS BENCH EXECUTES THE COMMAND LINE. It does not re-apply a pattern with its own invocation, which
is the mistake that let a private-key guard die in July with 111/111 green: `_cas` replayed the guard's
REGEX and never its argv, so a missing `--` was structurally invisible. Here a stub `dendrad` is put on
PATH, it PARSES ITS OWN ARGV the way cobra/pflag does, and the workers' real `tx_from`/`query` are
called. Remove the `--` from any of them and cases below turn red.

⛔ AND THE STUB IS PROVEN ABLE TO FAIL. Case ② runs the OLD argv shape through the same stub and
REQUIRES an error. Without it, a stub that accepted everything would make every other case green while
measuring nothing — a bench that cannot go red is decoration.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MODEA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODEA))

from modea.dendrad_argv import cli_usage_error, dendrad_argv  # noqa: E402

# A stub that reproduces the ONLY behaviour under test: before the `--` terminator, a token starting
# with a dash is an option; after it, everything is an operand. Known long options consume a value.
STUB = r'''#!/usr/bin/env python3
import sys, os
KNOWN_WITH_VALUE = {"--from", "--keyring-backend", "--chain-id", "--gas", "--gas-adjustment",
                    "--node", "--home", "--output", "--model-id", "--weights-hash", "--vrf-pubkey",
                    "--job-id", "--fees", "--gas-prices"}
KNOWN_BOOL = {"--yes"}
argv = sys.argv[1:]
positionals, i, terminated = [], 0, False
while i < len(argv):
    tok = argv[i]
    if not terminated:
        if tok == "--":
            terminated = True; i += 1; continue
        if tok in KNOWN_WITH_VALUE:
            i += 2; continue
        if tok in KNOWN_BOOL:
            i += 1; continue
        if tok.startswith("--"):
            sys.stderr.write("unknown flag: %s\n" % tok); sys.exit(1)
        if tok.startswith("-") and len(tok) > 1:
            sys.stderr.write("unknown shorthand flag: %r in %s\n" % (tok[1], tok)); sys.exit(1)
    positionals.append(tok)
    i += 1
with open(os.environ["STUB_OUT"], "w") as f:
    f.write("\n".join(positionals))
print("code: 0")
'''

VECTEUR = "-813148,-469320,1048576,-347390,631181"   # the real shape: first number NEGATIVE


def _bac():
    d = Path(tempfile.mkdtemp(prefix="dendrad-stub-"))
    exe = d / "dendrad"
    exe.write_text(STUB)
    exe.chmod(0o755)
    return d, d / "argv.txt"


def _joue(argv, out_file, env_extra=None):
    """Runs a full argv through the stub. Returns (rc, stdout+stderr, positionals-seen)."""
    d, _ = out_file.parent, None
    env = dict(os.environ, PATH=str(d) + os.pathsep + os.environ["PATH"], STUB_OUT=str(out_file))
    env.update(env_extra or {})
    if out_file.exists():
        out_file.unlink()
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    vus = out_file.read_text().splitlines() if out_file.exists() else None
    return r.returncode, (r.stdout or "") + (r.stderr or ""), vus


V, K = 0, 0


def cas(titre, cond, detail=""):
    global V, K
    if cond:
        V += 1
        print("   ok  %s" % titre)
    else:
        K += 1
        print("   XX  %s %s" % (titre, detail))


def main():
    d, out = _bac()
    try:
        print("-- BENCH: dendrad argument terminator ------------------------------------------")

        # ① THE WITNESS THAT THE STUB CAN REFUSE. Without it every green below is worthless.
        vieux = ["dendrad", "tx", "jobs", "create-commit", "k", VECTEUR, VECTEUR, "infer",
                 "--from", "me", "--yes"]
        rc, txt, _ = _joue(vieux, out)
        cas("(1) old shape, no terminator -> the stub REFUSES",
            rc != 0 and "unknown shorthand flag" in txt, "(rc=%s)" % rc)

        # ② the builder's shape passes, and the dangerous value arrives as an OPERAND
        neuf = dendrad_argv(("dendrad", "tx", "jobs"), "create-commit",
                            ["k", VECTEUR, VECTEUR, "infer"],
                            ["--model-id", "m", "--from", "me", "--yes"])
        rc, txt, vus = _joue(neuf, out)
        cas("(2) terminator present -> accepted", rc == 0, "(rc=%s %s)" % (rc, txt[:80]))
        cas("(3) the negative vector is an OPERAND, not an option",
            vus is not None and vus.count(VECTEUR) == 2, "(seen=%s)" % (vus,))
        cas("(4) the flag stays a FLAG (absent from the operands)",
            vus is not None and "--model-id" not in vus and "m" not in vus, "(seen=%s)" % (vus,))
        # ⛔ `.index()` RAISES WHEN THE TERMINATOR IS GONE, and an aborted bench reports a traceback
        # instead of a verdict — which reads as broken tooling, not as a red. Measured: the first
        # version of this file crashed here under the very mutation it exists to catch, and the four
        # cases below never ran. A bench must FAIL, never DIE.
        def avant_terminateur(token):
            return "--" in neuf and token in neuf and neuf.index(token) < neuf.index("--")

        cas("(5) every flag precedes the terminator",
            all(avant_terminateur(f) for f in ("--model-id", "--from", "--yes")))

        # ⑥ the sub-command must NOT fall after the terminator, or cobra sees no command at all
        cas("(6) the sub-command precedes the terminator", avant_terminateur("create-commit"))

        # ⑦ REGRESSION THIS PASS NEARLY INTRODUCED. Moving to an explicit `flags=` made every
        # `query(..., "--output", "json")` call site pass an option as an operand. Locked here.
        q = dendrad_argv(("dendrad", "query", "jobs"), "get-commit", ["k"], ["--output", "json"])
        rc, txt, vus = _joue(q, out)
        # The stub reports every operand it sees, which includes the `query jobs get-commit` path
        # since those follow the executable. The property under test is narrower and exact: the
        # option and its value must NOT be among them, and the real operand must be last.
        cas("(7) --output json stays a flag, not an operand",
            rc == 0 and vus is not None and "--output" not in vus and "json" not in vus
            and vus[-1] == "k", "(seen=%s)" % (vus,))

        # ⑧ THE ERROR MUST BE READABLE. This is why the defect survived: the extractor returned the
        # TAIL, and a usage error echoes the offending argument at the tail.
        sortie = "unknown shorthand flag: '8' in %s\nUsage:\n  dendrad tx jobs create-commit\n" % VECTEUR
        cas("(8) the usage error is read from the HEAD",
            cli_usage_error(sortie).startswith("unknown shorthand flag"),
            "(got=%r)" % cli_usage_error(sortie))
        cas("(9) a normal output carries no usage error",
            cli_usage_error('code: 0\ntxhash: "ABC"\nraw_log: ""') == "")

        # ⑩ the workers' REAL tx_from is exercised, not a local copy of it
        env = {"DENDRA_NODE": "", "DENDRA_KEYRING_DIR": ""}
        code = ("import sys; sys.path.insert(0, %r);"
                "import miner as m;"
                "print(m.tx_from('me', 'create-commit', 'k', %r, %r, 'infer',"
                "                flags=['--model-id','x']))" % (str(MODEA), VECTEUR, VECTEUR))
        rc, txt, vus = _joue([sys.executable, "-c", code], out, env)
        cas("(10) miner.tx_from really passes the vector as an operand",
            rc == 0 and vus is not None and VECTEUR in vus, "(rc=%s vus=%s out=%s)" % (rc, vus, txt[:120]))

        print("DENDRAD_ARGV_BENCH cases=%d ok=%d failed=%d" % (V + K, V, K))
        return 1 if K else 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())


def test_dendrad_argv():
    assert main() == 0
