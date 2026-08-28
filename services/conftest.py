# pytest collection rules for this directory.
#
# The `test_*.py` files listed below are NOT pytest cases: they are standalone scripts whose body
# runs at import time and ends in `sys.exit()` / `raise SystemExit`. pytest imports them during
# collection, the SystemExit escapes into `wrap_session`, and the run dies with INTERNALERROR having
# executed nothing. A third party following the README's own sequence saw that instead of a test run.
# `dendra_tests_all.sh` launches these files as scripts and grades their exit code (phase B); pytest
# simply has to leave them alone.
#
# THE LIST IS NOMINAL, AND THAT IS THE POINT. The obvious fix — `collect_ignore_glob = ["test_*.py"]`
# — also swallows the six files in this same directory that DO contain real `def test_` functions
# (cosmos_addr, registry_cache, relay_antireplay, relay_canon, relay_carrier, relay_write). It would
# delete six live cases from every run in order to repair a collection error: the exact silent green
# it claims to fix, in the other direction. Measured both ways before landing — with this file the
# root collects those six and `pytest -q` reports 302 passed / 4 skipped, exit 0; without it, exit 3
# and no test at all. A new file dropped in this directory is therefore collected by default, which
# is the safe default: opting a script OUT is a deliberate line here, never an inherited wildcard.
collect_ignore = [
    "test_c3_gate.py",
    "test_c3_jury_forensics.py",
    "test_c3_jury_model.py",
    "test_c3_run_measure.py",
    "test_relay_exporter.py",
    "test_relay_store.py",
    "test_reveal_marker.py",
    "test_reveal_worker_marker.py",
]
