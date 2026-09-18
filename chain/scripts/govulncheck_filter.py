#!/usr/bin/env python3
"""govulncheck_filter.py -- decide on a `govulncheck -format json` stream with an ACCEPTED list.

    go run golang.org/x/vuln/cmd/govulncheck@<version> -format json ./... > stream.json \
        && python3 scripts/govulncheck_filter.py .govulncheck-accepted < stream.json

WHY THIS EXISTS. govulncheck exits non-zero for every advisory it reports as CALLED, including one
with "Fixed in: N/A" -- an advisory no dependency bump can close (a deprecated package the framework
still imports). With such an advisory in the graph, `make test` stopped at the scanner BEFORE running
a single test, for good. The reflex seen elsewhere is `|| true`, which also silences the advisories
that ARE fixable and network-facing. This filter keeps the scanner's verdict and subtracts a list of
advisories the chain ASSUMES, each with its reason written next to its ID.

WHAT IT DECIDES.
  * A finding is CALLED when its first trace entry names a function (govulncheck's own definition of
    a symbol-level finding). Package- and module-level findings are reported, never refused.
  * exit 0 -- no called advisory outside the accepted list.
  * exit 1 -- a called advisory that is not accepted (listed with its fixed version and first trace),
    OR an accepted entry whose advisory is no longer reported AT ALL (not called, not imported, not
    required): an exemption does not outlive its object, and the line that keeps it must be removed.
    "Reported at package level only" keeps an exemption alive on purpose: whether a symbol counts as
    called depends on the scan mode (source call graph vs linked binary), and an accepted line must
    mean the same thing under both.
  * exit 2 -- no usable stream on stdin (the scanner did not run, or its output is not the JSON
    stream), or the accepted file is malformed (an ID without a reason, a reason too short to name a
    path, a duplicate).
No default is supplied on any of these: an empty stream is a non-measurement, never a pass. The
caller must also read the SCANNER's exit status (the Makefile does): a scanner that died mid-run
leaves a partial stream that has the shape of a measurement.
For every assumed advisory the filter PRINTS the entry points the scanner traced, so a path that
changes is visible at the next run; it does not judge them, the accepted line names the path in words.
"""
import json
import re
import sys

ID = re.compile(r"^GO-\d{4}-\d+$")
MIN_REASON = 40


def refuse(why):
    print("  [X] govulncheck filter UNUSABLE: %s" % why)
    print("GOVULNCHECK_FILTER_SUMMARY called=? accepted=? unaccepted=? stale=? state=unmeasured")
    sys.exit(2)


def read_accepted(path):
    accepted = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        refuse("accepted file %s unreadable (%s)" % (path, e))
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        if not ID.match(parts[0]):
            refuse("%s:%d does not start with an advisory ID (GO-YYYY-NNNN): %r" % (path, n, s[:60]))
        if len(parts) < 2 or not parts[1].strip():
            refuse("%s:%d accepts %s WITHOUT a reason -- an exemption is declared, not ticked" % (path, n, parts[0]))
        if len(parts[1].strip()) < MIN_REASON:
            # A one-word reason is a tick with a word next to it. Forty characters is not proof of a
            # reason either, but it is long enough to have to write the path and why.
            refuse("%s:%d accepts %s with a %d-character reason -- write the executable path and why it is "
                   "acceptable (at least %d characters)" % (path, n, parts[0], len(parts[1].strip()), MIN_REASON))
        if parts[0] in accepted:
            refuse("%s:%d repeats %s" % (path, n, parts[0]))
        accepted[parts[0]] = parts[1].strip()
    return accepted


def read_stream(text):
    """The stream is a sequence of JSON objects, not an array; decode them one after the other."""
    dec = json.JSONDecoder()
    i, n, objects = 0, len(text), []
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            refuse("stdin is not a govulncheck JSON stream (undecodable at byte %d of %d)" % (i, n))
        objects.append(obj)
        i = end
    return objects


path = sys.argv[1] if len(sys.argv) > 1 else ".govulncheck-accepted"
accepted = read_accepted(path)
objects = read_stream(sys.stdin.read())
if not objects:
    refuse("empty stream on stdin -- the scanner did not run, or printed nothing")
if not any(isinstance(o, dict) and "config" in o for o in objects):
    refuse("the stream carries no `config` object -- not a govulncheck stream")

osv = {}
called = {}         # id -> (fixed_version, first trace description)
entry_points = {}   # id -> set of entry points (the last frame of each trace): where the path starts
reported = set()
for o in objects:
    if not isinstance(o, dict):
        continue
    if "osv" in o and isinstance(o["osv"], dict) and o["osv"].get("id"):
        osv[o["osv"]["id"]] = o["osv"].get("summary", "")
    f = o.get("finding")
    if not isinstance(f, dict) or not f.get("osv"):
        continue
    reported.add(f["osv"])
    trace = f.get("trace")
    t0 = trace[0] if isinstance(trace, list) and trace and isinstance(trace[0], dict) else {}
    is_called = bool(t0.get("function"))
    if is_called and f["osv"] not in called:
        called[f["osv"]] = (f.get("fixed_version") or "N/A",
                            "%s %s%s" % (t0.get("package", "?"), t0.get("receiver", ""), t0.get("function", "?")))
    if is_called:
        last = trace[-1] if isinstance(trace[-1], dict) else {}
        entry_points.setdefault(f["osv"], set()).add(
            "%s.%s%s" % (last.get("package", "?"), last.get("receiver", ""), last.get("function", "?")))

unaccepted = sorted(i for i in called if i not in accepted)
assumed = sorted(i for i in called if i in accepted)
stale = sorted(i for i in accepted if i not in reported)

print("--> govulncheck: %d advisor%s called, %d reported at package/module level only"
      % (len(called), "y" if len(called) == 1 else "ies", len(reported - set(called))))
for i in assumed:
    print("    ASSUMED  %-14s fixed in %-10s %s" % (i, called[i][0], accepted[i]))
    print("             entry points traced by the scanner: %s" % ", ".join(sorted(entry_points.get(i, ()))))
for i in unaccepted:
    print("    CALLED   %-14s fixed in %-10s via %s%s" % (i, called[i][0], called[i][1],
                                                         (" -- " + osv[i]) if osv.get(i) else ""))
for i in stale:
    print("    STALE    %-14s is accepted in %s but no longer reported by the scanner: remove the line" % (i, path))
for i in sorted(i for i in accepted if i in reported and i not in called):
    print("    DORMANT  %-14s accepted, reported at package/module level only in this mode (kept: the other mode may call it)" % i)
print("GOVULNCHECK_FILTER_SUMMARY called=%d accepted=%d unaccepted=%d stale=%d state=%s"
      % (len(called), len(assumed), len(unaccepted), len(stale), "red" if (unaccepted or stale) else "green"))
if unaccepted:
    print("  [X] %d called advisor%s not in the accepted list: bump the dependency, or accept it WITH a reason."
          % (len(unaccepted), "y is" if len(unaccepted) == 1 else "ies are"))
if stale:
    print("  [X] an exemption does not outlive its object: %s" % ", ".join(stale))
if unaccepted or stale:
    sys.exit(1)
print("  OK: every called advisory is either fixed or assumed with a written reason.")
sys.exit(0)
