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

# THE ROOT MUST BE POINTABLE, OR THIS BENCH IS IMMUTABLE: pinned to its own path, it reads the
# SHIPPED tree whatever copy it is shown, so mutating that copy still returns the SAME green.
# Enforced before publication by dendra_bancs_argumentables_garde.py, in the
# development repository -- so the rule binds even though the gate is not in this tree.
MODEA = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(MODEA))

from modea.dendrad_argv import cli_usage_error, dendrad_argv  # noqa: E402

# A stub that reproduces the ONLY behaviour under test: before the `--` terminator, a token starting
# with a dash is an option; after it, everything is an operand. Known long options consume a value.
STUB = r'''#!/usr/bin/env python3
import sys, os
KNOWN_WITH_VALUE = {"--from", "--keyring-backend", "--keyring-dir", "--chain-id", "--gas", "--gas-adjustment",
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

VECTOR = "-813148,-469320,1048576,-347390,631181"   # the real shape: first number NEGATIVE


def _sandbox():
    d = Path(tempfile.mkdtemp(prefix="dendrad-stub-"))
    exe = d / "dendrad"
    exe.write_text(STUB)
    exe.chmod(0o755)
    return d, d / "argv.txt"


def _play(argv, out_file, env_extra=None):
    """Runs a full argv through the stub. Returns (rc, stdout+stderr, positionals-seen)."""
    d, _ = out_file.parent, None
    env = dict(os.environ, PATH=str(d) + os.pathsep + os.environ["PATH"], STUB_OUT=str(out_file))
    env.update(env_extra or {})
    if out_file.exists():
        out_file.unlink()
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    seen = out_file.read_text().splitlines() if out_file.exists() else None
    return r.returncode, (r.stdout or "") + (r.stderr or ""), seen


V, K = 0, 0


def check(title, cond, detail=""):
    global V, K
    if cond:
        V += 1
        print("   ok  %s" % title)
    else:
        K += 1
        print("   XX  %s %s" % (title, detail))


def main():
    d, out = _sandbox()
    try:
        print("-- BENCH: dendrad argument terminator ------------------------------------------")

        # ① THE WITNESS THAT THE STUB CAN REFUSE. Without it every green below is worthless.
        old_argv = ["dendrad", "tx", "jobs", "create-commit", "k", VECTOR, VECTOR, "infer",
                 "--from", "me", "--yes"]
        rc, txt, _ = _play(old_argv, out)
        check("(1) old shape, no terminator -> the stub REFUSES",
            rc != 0 and "unknown shorthand flag" in txt, "(rc=%s)" % rc)

        # ② the builder's shape passes, and the dangerous value arrives as an OPERAND
        new_argv = dendrad_argv(("dendrad", "tx", "jobs"), "create-commit",
                            ["k", VECTOR, VECTOR, "infer"],
                            ["--model-id", "m", "--from", "me", "--yes"])
        rc, txt, seen = _play(new_argv, out)
        check("(2) terminator present -> accepted", rc == 0, "(rc=%s %s)" % (rc, txt[:80]))
        check("(3) the negative vector is an OPERAND, not an option",
            seen is not None and seen.count(VECTOR) == 2, "(seen=%s)" % (seen,))
        check("(4) the flag stays a FLAG (absent from the operands)",
            seen is not None and "--model-id" not in seen and "m" not in seen, "(seen=%s)" % (seen,))
        # ⛔ `.index()` RAISES WHEN THE TERMINATOR IS GONE, and an aborted bench reports a traceback
        # instead of a verdict — which reads as broken tooling, not as a red. Measured: the first
        # version of this file crashed here under the very mutation it exists to catch, and the four
        # cases below never ran. A bench must FAIL, never DIE.
        def before_terminator(token):
            return "--" in new_argv and token in new_argv and new_argv.index(token) < new_argv.index("--")

        check("(5) every flag precedes the terminator",
            all(before_terminator(f) for f in ("--model-id", "--from", "--yes")))

        # ⑥ the sub-command must NOT fall after the terminator, or cobra sees no command at all
        check("(6) the sub-command precedes the terminator", before_terminator("create-commit"))

        # ⑦ REGRESSION THIS PASS NEARLY INTRODUCED. Moving to an explicit `flags=` made every
        # `query(..., "--output", "json")` call site pass an option as an operand. Locked here.
        q = dendrad_argv(("dendrad", "query", "jobs"), "get-commit", ["k"], ["--output", "json"])
        rc, txt, seen = _play(q, out)
        # The stub reports every operand it sees, which includes the `query jobs get-commit` path
        # since those follow the executable. The property under test is narrower and exact: the
        # option and its value must NOT be among them, and the real operand must be last.
        check("(7) --output json stays a flag, not an operand",
            rc == 0 and seen is not None and "--output" not in seen and "json" not in seen
            and seen[-1] == "k", "(seen=%s)" % (seen,))

        # ⑧ THE ERROR MUST BE READABLE. This is why the defect survived: the extractor returned the
        # TAIL, and a usage error echoes the offending argument at the tail.
        usage_output = "unknown shorthand flag: '8' in %s\nUsage:\n  dendrad tx jobs create-commit\n" % VECTOR
        check("(8) the usage error is read from the HEAD",
            cli_usage_error(usage_output).startswith("unknown shorthand flag"),
            "(got=%r)" % cli_usage_error(usage_output))
        check("(9) a normal output carries no usage error",
            cli_usage_error('code: 0\ntxhash: "ABC"\nraw_log: ""') == "")

        # ⑩ the workers' REAL tx_from is exercised, not a local copy of it
        # DENDRA_CHAIN_ID is set the way the kit's network file sets it: since ADR-048 the workers read
        # the chain id and refuse to sign without one, and the stub answers `status` with no JSON.
        # The keyring is read from the state of a DIRECTORY (modea/keyring.py) and always named on the
        # command line: an empty one of the bench's own, never the home of the machine running it.
        kr_dir = d / "keyring"
        kr_dir.mkdir(exist_ok=True)
        env = {"DENDRA_NODE": "", "DENDRA_KEYRING_DIR": str(kr_dir), "DENDRA_CHAIN_ID": "dendra-testnet",
               "DENDRA_KEYRING_PASSPHRASE_FILE": str(d / "no-passphrase")}
        code = ("import sys; sys.path.insert(0, %r);"
                "import miner as m;"
                "print(m.tx_from('me', 'create-commit', 'k', %r, %r, 'infer',"
                "                flags=['--model-id','x']))" % (str(MODEA), VECTOR, VECTOR))
        rc, txt, seen = _play([sys.executable, "-c", code], out, env)
        check("(10) miner.tx_from really passes the vector as an operand",
            rc == 0 and seen is not None and VECTOR in seen, "(rc=%s seen=%s out=%s)" % (rc, seen, txt[:120]))

        print("DENDRAD_ARGV_BENCH cases=%d ok=%d failed=%d" % (V + K, V, K))
        return 1 if K else 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())


def test_dendrad_argv():
    assert main() == 0
