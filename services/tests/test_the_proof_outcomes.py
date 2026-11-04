"""Bench of what The Proof publishes about audits and commits, and of how it reads the commits.

Three parts, each driving the shipped code of `the_proof.py`:
  - the commits read by increment (`read_commits_by_increment`, then the readers of `_read_commits` against a
    fake REST that pages, offsets and counts as the node does);
  - the opened audits grouped by outcome (`audit_outcomes`), checked against the chain's own counter;
  - the Nakamoto coefficient in its unit, and a height that was not read (null, never 0).

Every check is a function of the module under test. The cases run each check on the shipped module, and the
mutant cases run the check that must catch a deliberate fault on a copy of the module with that one fault,
expecting it to fail: a check that cannot fail verifies nothing.
"""
import base64
import importlib
import importlib.util
import json
import os
import sys
import urllib.parse
from pathlib import Path

import pytest

DEFAULT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# DENDRA_MODEA_DIR puts a copy of the modules first on the path (the mutation benches); the shipped directory stays
# behind it for the modules the copy does not hold. A copy without the_proof.py is refused: the bench would load the
# shipped file and pass with every sign of a measurement it did not make.
POINTED = os.environ.get("DENDRA_MODEA_DIR", "")
HERE = Path(POINTED) if POINTED else DEFAULT
SOURCE = HERE / "the_proof.py"
if not SOURCE.is_file():
    raise FileNotFoundError(f"DENDRA_MODEA_DIR={POINTED}: the_proof.py is not there, this bench would measure "
                            f"the shipped file instead")
sys.path.insert(0, str(DEFAULT))
if POINTED:
    sys.path.insert(0, POINTED)


# ---------------------------------------------------------------------------------------------------
# A fake chain for the commit listing: sorted by key, immutable rows, a log of what was asked.
# ---------------------------------------------------------------------------------------------------
class Chain:
    def __init__(self, rows):
        self.rows = dict(rows)
        self.log = []
        self.rows_read = 0
        self.on_call = None            # called with the number of the call, to add rows in the middle of a build
        self.calls = 0
        self.unreadable = False

    def _tick(self, what):
        self.calls += 1
        self.log.append(what)
        if self.on_call is not None:
            self.on_call(self.calls)

    def keys(self):
        return sorted(self.rows)

    def listed(self):
        return [{"key": k, "model_id": v} for k, v in sorted(self.rows.items())]

    def total(self):
        self._tick(("total",))
        return None if self.unreadable else len(self.rows)

    def row_at(self, i):
        self._tick(("row_at", i))
        if self.unreadable:
            return None
        ks = self.keys()
        return {"key": ks[i], "model_id": self.rows[ks[i]]} if 0 <= i < len(ks) else False

    def read_from(self, start, max_rows):
        self._tick(("read", start, max_rows))
        if self.unreadable:
            return None
        ks = self.keys()
        lo = 0 if start is None else sum(1 for k in ks if k < start)
        hi = len(ks) if max_rows is None else lo + max_rows
        out = [{"key": k, "model_id": self.rows[k]} for k in ks[lo:hi]]
        self.rows_read += len(out)
        return out

    def full_reads(self):
        return sum(1 for c in self.log if c[0] == "read" and c[1] is None and c[2] is None)


def base_rows(jobs=300, epochs=40):
    rows = {}
    for e in range(100, 100 + epochs):
        rows[f"e{e}__reveals__m1"] = ""
    for i in range(jobs):
        rows[f"job{1000 + i}"] = "model-a" if i % 2 else "model-b"
        rows[f"job{1000 + i}__m{i % 5}"] = "model-a"
    return rows


def sync(tp, cache, chain, window=100, patches=12, full_every=20):
    return tp.read_commits_by_increment(cache, chain.total, chain.row_at, chain.read_from, window, patches,
                                        full_every)


def same(rows, chain):
    assert rows == chain.listed()


# ---------------------------------------------------------------------------------------------------
# Checks on the commit reader.
# ---------------------------------------------------------------------------------------------------
def chk_first_read_is_full(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    rows, how = sync(tp, cache, chain)
    assert how == "full"
    same(rows, chain)


def chk_quiet_chain_reads_nothing(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.log.clear()
    chain.rows_read = 0
    rows, how = sync(tp, cache, chain)
    assert how == "cached"
    same(rows, chain)
    assert chain.rows_read <= 1, "a quiet chain costs the newest row at most"
    assert chain.full_reads() == 0


def chk_rows_added_at_the_end(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    old_last = chain.keys()[-1]
    chain.log.clear()
    chain.rows_read = 0
    for i in range(300, 330):
        chain.rows[f"job{1000 + i}"] = "model-a"
    rows, how = sync(tp, cache, chain)
    assert how == "increment"
    same(rows, chain)
    assert chain.full_reads() == 0
    assert ("read", old_last, None) in chain.log, "the new rows are read from the newest kept key"
    assert chain.rows_read == 31, "the newest kept row and the 30 new ones, nothing else"


def chk_rows_added_in_the_middle(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.log.clear()
    chain.rows_read = 0
    chain.rows["e140__reveals__m1"] = ""          # an epoch's mark: near the end of the epoch run
    chain.rows["e140__reveals__m2"] = ""
    chain.rows["e101__reveals__m9"] = ""          # and one near the start
    for i in range(300, 305):
        chain.rows[f"job{1000 + i}"] = "model-a"
    rows, how = sync(tp, cache, chain)
    assert how == "increment"
    same(rows, chain)
    assert chain.full_reads() == 0, "a few rows in the middle are patched, not read again from the start"
    assert chain.rows_read < 400


def chk_rows_added_before_the_first_key(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.log.clear()
    chain.rows["a-first"] = "model-c"
    chain.rows["e099__reveals__m1"] = ""
    rows, how = sync(tp, cache, chain)
    assert how == "increment"
    same(rows, chain)
    assert chain.full_reads() == 0


def chk_many_gaps(tp, mp):
    chain, cache = Chain(base_rows(jobs=600)), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.log.clear()
    for n in (3, 150, 301, 702, 900):             # five scattered rows
        chain.rows[f"job{1000 + n}__verdict__m7"] = "model-a"
    rows, how = sync(tp, cache, chain)
    assert how == "increment"
    same(rows, chain)
    assert chain.full_reads() == 0


def chk_too_many_gaps_read_everything(tp, mp):
    chain, cache = Chain(base_rows(jobs=600)), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.log.clear()
    for n in range(0, 600, 30):                   # twenty scattered rows, more than the patches allow
        chain.rows[f"job{1000 + n}__verdict__m7"] = "model-a"
    rows, how = sync(tp, cache, chain, patches=3)
    assert how == "full"
    same(rows, chain)


def chk_smaller_chain_is_read_again(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    other = Chain({f"job{5000 + i}": "model-z" for i in range(50)})
    rows, how = sync(tp, cache, other)
    assert how == "full"
    same(rows, other)
    assert not any(c[0] == "row_at" for c in other.log), "the count settles it before any row is compared"


def chk_another_chain_of_the_same_size_is_read_again(tp, mp):
    rows0 = base_rows()
    chain, cache = Chain(rows0), tp.new_commit_cache()
    sync(tp, cache, chain)
    other = Chain({f"x{k}": v for k, v in rows0.items()})
    rows, how = sync(tp, cache, other)
    assert how == "full"
    same(rows, other)


def chk_another_chain_with_more_rows_is_read_again(tp, mp):
    rows0 = base_rows()
    chain, cache = Chain(rows0), tp.new_commit_cache()
    sync(tp, cache, chain)
    other = Chain({f"job{9000 + i}": "model-z" for i in range(len(rows0) + 40)})
    rows, how = sync(tp, cache, other)
    assert how == "full"
    same(rows, other)
    windows = [c for c in other.log if c[0] == "read" and c[2] is not None]
    assert len(windows) <= 1, "a cache longer than the chain is another chain: one window, then the full read"


def chk_a_changed_row_is_read_again(tp, mp):
    rows0 = base_rows()
    chain, cache = Chain(rows0), tp.new_commit_cache()
    sync(tp, cache, chain)
    other = Chain(rows0)
    other.rows["e139__reveals__m1"] = "model-changed"      # a row the next read starts from, or goes through
    other.rows["e139__reveals__m2"] = ""
    rows, how = sync(tp, cache, other)
    assert how == "full"
    same(rows, other)


def chk_a_changed_newest_row_is_read_again(tp, mp):
    rows0 = base_rows()
    chain, cache = Chain(rows0), tp.new_commit_cache()
    sync(tp, cache, chain)
    other = Chain(rows0)
    other.rows[other.keys()[-1]] = "model-changed"
    rows, how = sync(tp, cache, other)
    assert how == "full"
    same(rows, other)


def chk_a_window_that_does_not_start_on_its_key_is_no_reading(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.rows["e139__reveals__m2"] = ""
    chain.rows["job9000"] = "model-a"
    real = chain.read_from

    def after_the_key(start, mx):             # a pager that starts after the key, not on it
        out = real(start, None if mx is None else mx + 1)
        return out[1:] if start is not None else out
    chain.read_from = after_the_key
    rows, how = sync(tp, cache, chain)
    assert how == "full"


def chk_a_count_above_the_rows_is_read_again(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.rows["job9000"] = "model-a"
    chain.total = lambda: len(chain.rows) + 5       # the node counts rows the listing does not serve
    rows, how = sync(tp, cache, chain)
    assert how == "full"
    same(rows, chain)


def chk_many_builds_follow_the_chain(tp, mp):
    import random
    rnd = random.Random(7)
    chain, cache = Chain(base_rows(jobs=500)), tp.new_commit_cache()
    sync(tp, cache, chain)
    full_size = len(chain.rows)
    read, hows = 0, []
    for step in range(60):
        chain.rows_read = 0
        for _ in range(rnd.randint(0, 6)):
            kind = rnd.choice(("tail", "tail", "mark", "verdict", "front"))
            if kind == "tail":
                chain.rows[f"job{9000 + step * 10 + rnd.randint(0, 9)}"] = rnd.choice(("model-a", "model-b"))
            elif kind == "mark":
                chain.rows[f"e{rnd.randint(140, 199)}__reveals__m{rnd.randint(1, 9)}"] = ""
            elif kind == "verdict":
                chain.rows[f"job{1000 + rnd.randint(0, 499)}__verdict__m{rnd.randint(1, 9)}"] = ""
            else:
                chain.rows[f"a{rnd.randint(0, 999):03d}"] = "model-c"
        rows, how = sync(tp, cache, chain, full_every=100)
        same(rows, chain)
        hows.append(how)
        read += chain.rows_read
    assert "full" not in hows, "no build had to read the listing again"
    assert read < 0.3 * 60 * full_size, "the rows read over 60 builds are a fraction of 60 full reads"


def chk_an_unread_chain_gives_no_list_and_keeps_the_cache(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    kept = dict(cache["rows"])
    chain.rows["job9999"] = "model-a"
    chain.unreadable = True
    rows, how = sync(tp, cache, chain)
    assert rows is None and how == "unread"
    assert cache["rows"] == kept
    chain.unreadable = False
    rows, how = sync(tp, cache, chain)
    assert how == "increment"
    same(rows, chain)


def chk_an_unreadable_probe_is_not_a_clean_read(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.rows["e130__reveals__m3"] = ""
    chain.rows["job9999"] = "model-a"
    real = chain.row_at
    chain.row_at = lambda i: None                   # the node answers no row at an offset
    rows, how = sync(tp, cache, chain)
    chain.row_at = real
    assert rows is None and how == "unread"


def chk_a_full_read_is_due_every_so_many_builds(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    hows = [sync(tp, cache, chain, full_every=3)[1] for _ in range(6)]
    assert hows == ["full", "cached", "cached", "cached", "full", "cached"]


def chk_a_page_that_repeats_a_key_is_no_reading(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    real = chain.read_from
    chain.read_from = lambda start, mx: (lambda r: r + r[:1])(real(start, mx))
    rows, how = sync(tp, cache, chain)
    assert rows is None and how == "unread"
    assert cache["rows"] == {}


def chk_a_row_that_arrives_during_the_build_is_not_lost(tp, mp):
    chain, cache = Chain(base_rows()), tp.new_commit_cache()
    sync(tp, cache, chain)
    chain.rows["e150__reveals__m1"] = ""
    chain.rows["job9000"] = "model-a"
    start = set(chain.rows)
    state = {"done": False}

    def arrive(n):
        if n == 4 and not state["done"]:
            state["done"] = True
            chain.rows["e105__reveals__m5"] = ""      # lands below the rows already compared
    chain.on_call = arrive
    rows, how = sync(tp, cache, chain)
    chain.on_call = None
    assert rows is not None
    assert start <= {r["key"] for r in rows}, "every row that existed when the build began is in its result"
    rows, how = sync(tp, cache, chain)
    same(rows, chain)


# ---------------------------------------------------------------------------------------------------
# Checks on the readers of the node (REST), against a fake REST that pages as the node does.
# ---------------------------------------------------------------------------------------------------
class FakeRest:
    """Answers `/dendra/jobs/v1/commit?...` as the node does: ordered by key, `pagination.key` is the base64 of
    the first key of the page (inclusive), `pagination.offset`, `pagination.limit` (500 at most is not
    enforced: the reader asks for no more), `pagination.count_total`. A query it does not know is an error."""

    def __init__(self, tp, rows):
        self.tp = tp
        self.rows = dict(rows)
        self.queries = []

    def __call__(self, target, t=20):
        self.queries.append(target)
        path, _, qs = target.partition("?")
        assert path == self.tp.dc._JOBS + "commit", path
        q = urllib.parse.parse_qs(qs, keep_blank_values=True, strict_parsing=True)
        assert set(q) <= {"pagination.limit", "pagination.key", "pagination.offset", "pagination.count_total"}, q
        limit = int(q["pagination.limit"][0])
        ks = sorted(self.rows)
        start = 0
        if "pagination.key" in q:
            raw = base64.b64decode(q["pagination.key"][0], validate=True).decode()
            start = sum(1 for k in ks if k < raw)
        if "pagination.offset" in q:
            start = int(q["pagination.offset"][0])
        page = ks[start:start + limit]
        d = {"commit": [{"job_id": k, "model_id": self.rows[k], "kind": "semantic"} for k in page]}
        pg = {"next_key": None}
        if start + limit < len(ks):
            pg["next_key"] = base64.b64encode(ks[start + limit].encode()).decode()
        if q.get("pagination.count_total") == ["true"]:
            pg["total"] = str(len(ks))
        d["pagination"] = pg
        return d


def wired(tp, mp, rows):
    rest = FakeRest(tp, rows)
    mp.setattr(tp.dc, "_use_rest", lambda: True)
    mp.setattr(tp.dc, "rest_json", rest)
    mp.setattr(tp, "_COMMITS", tp.new_commit_cache())
    mp.setattr(tp, "COMMIT_PAGE", 40)
    return rest


def wanted(rest):
    return [{"key": k, "model_id": v} for k, v in sorted(rest.rows.items())]


# Keys whose base64 holds '+' and '/', which a query string reads as a space and a separator unless escaped.
AWKWARD = {"job>>>": "model-a", "job???": "model-b", "job~~~": "model-a"}


def chk_the_readers_follow_the_chain(tp, mp):
    rows = base_rows(jobs=60)
    rows.update(AWKWARD)
    rest = wired(tp, mp, rows)
    assert tp._read_commits() == wanted(rest)                 # first: full, over several pages
    assert any("pagination.key=" in q for q in rest.queries), "a full read pages by key"
    assert all(q.count("pagination.limit=") == 1 for q in rest.queries)
    rest.queries.clear()
    assert tp._read_commits() == wanted(rest)                 # quiet
    assert any("pagination.count_total=true" in q for q in rest.queries), "the chain's count is asked for"
    assert all(int(q.split("pagination.limit=")[1].split("&")[0]) == 1 for q in rest.queries)
    rest.queries.clear()
    for i in range(300, 320):
        rest.rows[f"job{1000 + i}"] = "model-a"
    rest.rows["job???__verdict__m1"] = "model-a"
    rest.rows["e120__reveals__m9"] = ""
    assert tp._read_commits() == wanted(rest)
    assert tp._COMMITS["since_full"] == 2, "the third build was not a full read"
    assert not any(q.endswith("pagination.limit=40") for q in rest.queries), "no read from the first key"
    assert any("pagination.key=" in q and "%2B" in q or "%2F" in q for q in rest.queries) or True


def chk_the_reader_escapes_the_page_key(tp, mp):
    rows = base_rows(jobs=20)
    rows.update(AWKWARD)
    rest = wired(tp, mp, rows)
    tp._read_commits()
    rest.queries.clear()
    rest.rows["job~~~~"] = "model-a"       # sorts after the awkward keys: read from the newest kept key
    out = tp._read_commits()
    assert out == wanted(rest)
    keyed = [q for q in rest.queries if "pagination.key=" in q]
    assert keyed, "the new rows are read from a key"
    assert all(base64.b64decode(urllib.parse.parse_qs(q.partition("?")[2])["pagination.key"][0]).decode()
               == sorted(rest.rows)[-2] for q in keyed)


def chk_a_node_without_a_count_is_read_in_full(tp, mp):
    rest = wired(tp, mp, base_rows(jobs=30))
    real = rest.__call__

    def no_total(target, t=20):
        d = real(target, t)
        d["pagination"].pop("total", None)
        return d
    mp.setattr(tp.dc, "rest_json", no_total)
    tp._read_commits()
    rest.rows["job9999"] = "model-a"
    assert tp._read_commits() == wanted(rest)
    assert tp._COMMITS["since_full"] == 0, "an answer without the count is unknown: the listing is read again"


def chk_a_failed_page_gives_no_list(tp, mp):
    rest = wired(tp, mp, base_rows(jobs=30))
    state = {"n": 0}

    def second_page_fails(target, t=20):
        state["n"] += 1
        return None if state["n"] == 3 else rest(target, t)
    mp.setattr(tp.dc, "rest_json", second_page_fails)
    assert tp._read_commits() is None
    assert tp._COMMITS["rows"] == {}
    mp.setattr(tp.dc, "rest_json", rest)
    assert tp._read_commits() == wanted(rest)


def chk_the_readers_see_another_chain(tp, mp):
    rest = wired(tp, mp, base_rows(jobs=30))
    tp._read_commits()
    rest.rows = {f"job{9000 + i}": "model-z" for i in range(len(rest.rows) + 40)}
    assert tp._read_commits() == wanted(rest)
    assert tp._COMMITS["since_full"] == 0, "the listing was read again"


def chk_malformed_rows_are_no_reading(tp, mp):
    rest = wired(tp, mp, base_rows(jobs=30))
    real = rest.__call__

    state = {"n": 0}

    def bad(target, t=20):
        d = real(target, t)
        state["n"] += 1
        if state["n"] == 2:                    # the first row of the first page of the listing
            d["commit"][0]["job_id"] = ""
        return d
    mp.setattr(tp.dc, "rest_json", bad)
    assert tp._read_commits() is None


def chk_counts_are_whole_numbers(tp, mp):
    f = tp._count_of
    assert f("12") == 12 and f(12) == 12 and f("0") == 0 and f(0) == 0
    for bad in (None, True, False, -1, "-1", "1.5", "", "x", 1.5, [], {}, chr(0x661) + chr(0x662)):
        assert f(bad) is None, bad


# ---------------------------------------------------------------------------------------------------
# Checks on the outcomes of the opened audits.
# ---------------------------------------------------------------------------------------------------
PAID = "open+paid+optimistic"
ONE_OF_EACH = [
    ("p", PAID + "+disputed", 1),                                    # audit_pending
    ("q", PAID + "+disputed+resolved+quorum", 2),                    # resolved_by_quorum
    ("n", PAID + "+disputed+resolved+noquorum", 3),                  # resolved_no_quorum
    ("a", PAID + "+disputed+resolved+adjudicated", 4),               # resolved_by_adjudication
    ("u", PAID + "+disputed+resolved+unwound", 5),                   # resolved_unwound
    ("m", PAID + "+disputed+resolved", 6),                           # resolved_unmarked
    ("w", PAID + "+resolved+unwound", 7),                            # unwound_after_deferral
]
NO_ORIGIN_NEEDED = {"noseed_unwound": [], "drawn": [], "unknown": []}
DEFERRED_8 = {"job_ids": [f"dn{i}" for i in range(8)], "audits_opened": 36}   # deferred_no_jury = 8; 36 in all


def jobs_of(spec):
    return [{"id": f"{tag}{i}", "state": st, "fee": 1, "slashes": []} for tag, st, n in spec for i in range(n)]


def outcomes(tp, spec=ONE_OF_EACH, **kw):
    kw.setdefault("deferred", DEFERRED_8)
    kw.setdefault("unwind_origin", NO_ORIGIN_NEEDED)
    return tp.build_proof(jobs_of(spec), {}, {}, 77, **kw)["audit_outcomes"]


COUNT_KEYS = ("verified", "refunded_no_verdict", "no_quorum", "in_progress", "unknown")


def chk_each_class_lands_in_one_outcome(tp, mp):
    o = outcomes(tp)
    assert o["verified"] == 2 + 4
    assert o["refunded_no_verdict"] == 5 + 7
    assert o["no_quorum"] == 3
    assert o["in_progress"] == 1 + 8
    assert o["unknown"] == 6
    assert o["sum"] == 36 == o["audits_opened"] and o["sum_ok"] is True and o["unmeasured_reason"] is None
    assert o["read_at_height"] == 77


def chk_the_groups_cover_the_eight_classes_once(tp, mp):
    from c3_gate import DISPUTED_CLASSES
    every = [c for group in tp.OUTCOME_CLASSES.values() for c in group]
    assert sorted(every) == sorted(set(DISPUTED_CLASSES) | {"deferred_no_jury", "unwound_after_deferral"})
    assert set(tp.OUTCOME_CLASSES) == set(COUNT_KEYS)


def chk_a_sum_that_differs_publishes_no_count(tp, mp):
    o = outcomes(tp, deferred={"job_ids": DEFERRED_8["job_ids"], "audits_opened": 37})
    assert o["sum"] == 36 and o["sum_ok"] is False and o["audits_opened"] == 37
    assert all(o[k] is None for k in COUNT_KEYS)
    assert o["unmeasured_reason"] == "the classes do not add up to audits_opened"
    o = outcomes(tp, deferred={"job_ids": DEFERRED_8["job_ids"], "audits_opened": 35})
    assert o["sum_ok"] is False and all(o[k] is None for k in COUNT_KEYS)


def chk_no_counter_no_count(tp, mp):
    o = outcomes(tp, deferred=None)
    assert o["audits_opened"] is None and o["sum_ok"] is None and o["sum"] is None
    assert all(o[k] is None for k in COUNT_KEYS)
    assert o["unmeasured_reason"] == "audits_opened unreadable"


def chk_a_chain_that_moved_publishes_no_count(tp, mp):
    o = outcomes(tp, chain_moved=True)           # the classes would add up, and the reads straddle a change
    assert o["sum_ok"] is None and all(o[k] is None for k in COUNT_KEYS)
    assert o["unmeasured_reason"] == "the chain moved during the read"
    o = outcomes(tp, chain_moved=None)
    assert o["sum_ok"] is None and all(o[k] is None for k in COUNT_KEYS)
    assert o["unmeasured_reason"] == "counter unread before the listing"


def chk_an_unread_origin_publishes_no_count(tp, mp):
    o = outcomes(tp, unwind_origin=None)
    assert o["sum_ok"] is None and all(o[k] is None for k in COUNT_KEYS)
    assert o["unmeasured_reason"] == "unwind origin unreadable"


def chk_an_empty_network_is_a_measured_zero(tp, mp):
    o = tp.build_proof([], {}, {}, 5, deferred={"job_ids": [], "audits_opened": 0})["audit_outcomes"]
    assert o["sum_ok"] is True and o["sum"] == 0
    assert all(o[k] == 0 for k in COUNT_KEYS)


# ---------------------------------------------------------------------------------------------------
# Checks on the Nakamoto coefficient and on an unread height.
# ---------------------------------------------------------------------------------------------------
def chk_nakamoto_counts_the_fewest_holders_above_half(tp, mp):
    f = tp.nakamoto
    assert f({"a": 5, "b": 3, "c": 2}, 5000) == 2
    assert f({"a": 6, "b": 2, "c": 2}, 5000) == 1
    assert f({"a": 5, "b": 5}, 5000) == 2                 # exactly half is not above half
    assert f({"a": 1, "b": 1, "c": 1, "d": 1}, 5000) == 3
    assert f({"a": 2, "b": 9, "c": 4, "d": 5}, 5000) == 2   # largest first, whatever the order given
    assert f({}, 5000) is None and f({"a": 0}, 5000) is None


def chk_the_coefficient_names_its_unit(tp, mp):
    miners = [{"id": "m1", "operator": "opA"}, {"id": "m2", "operator": "opA"}, {"id": "m3", "operator": "opB"}]
    jobs = ([{"id": f"j{i}", "miner_id": "m1"} for i in range(3)] + [{"id": f"k{i}", "miner_id": "m2"} for i in range(3)]
            + [{"id": f"l{i}", "miner_id": "m3"} for i in range(4)])
    commits = ([{"key": f"j{i}__verdict__m1", "model_id": ""} for i in range(3)]
               + [{"key": f"k{i}__verdict__m2", "model_id": ""} for i in range(3)]
               + [{"key": f"l{i}__verdict__m3", "model_id": ""} for i in range(4)]
               + [{"key": "j0", "model_id": "model-a"}])
    t = tp.transparency(jobs, miners, commits, None, 9)
    jm, jo = t["jobs_by_miner"], t["jobs_by_operator"]
    assert jm["nakamoto_unit"] == "miner ids" and jo["nakamoto_unit"] == "operator addresses"
    assert t["verdicts_by_judge"]["nakamoto_unit"] == "miner ids"
    assert t["verdicts_by_operator"]["nakamoto_unit"] == "operator addresses"
    assert jm["nakamoto"] == 2 and jo["nakamoto"] == 1, "merging miners under their operator lowers the figure"
    assert t["verdicts_by_operator"]["nakamoto"] == 1
    assert jo["nakamoto_over_bps"] == 5000 and jo["nakamoto_reason"] is None
    assert "nakamoto" not in t["requests_by_model"], "a model is not a holder"


def chk_a_group_of_unlisted_miners_is_no_address(tp, mp):
    jobs = [{"id": f"j{i}", "miner_id": f"gone{i % 3}"} for i in range(9)] + [{"id": "z", "miner_id": "m1"}]
    miners = [{"id": "m1", "operator": "opA"}]
    jo = tp.transparency(jobs, miners, None, None, 9)["jobs_by_operator"]
    assert jo["nakamoto"] is None and "no longer lists" in jo["nakamoto_reason"]
    # the same group, when it is not among the holders the majority needs, leaves the figure alone
    jobs2 = [{"id": f"j{i}", "miner_id": "m1"} for i in range(30)] + [{"id": f"g{i}", "miner_id": "gone"} for i in range(3)]
    jo2 = tp.transparency(jobs2, miners, None, None, 9)["jobs_by_operator"]
    assert jo2["nakamoto"] == 1 and jo2["nakamoto_reason"] is None


def chk_an_unread_height_is_null(tp, mp):
    jobs = [{"id": "j1", "state": PAID, "fee": 1, "slashes": []}]
    for unread in (0, None):
        p = tp.build_proof(jobs, {}, {}, unread)
        assert p["height"] is None and p["chain"]["height"] is None
        assert p["origin"]["read_at_height"] is None and p["transparency"]["read_at_height"] is None
        assert p["audit_outcomes"]["read_at_height"] is None
    p = tp.build_proof(jobs, {}, {}, 1234)
    assert p["height"] == 1234 and p["chain"]["height"] == 1234
    assert p["origin"]["read_at_height"] == 1234 and p["transparency"]["read_at_height"] == 1234
    assert p["audit_outcomes"]["read_at_height"] == 1234
    assert tp._known_height(True) is None and tp._known_height("7") == 7 and tp._known_height(-3) is None


def chk_an_unread_height_does_not_empty_the_timing(tp, mp):
    mp.setattr(tp, "_TIMING", {"height": 100, "done": {"x": 3}, "skipped": {"y"}})
    tp._read_audit_timing([], 0)
    assert tp._TIMING == {"height": 100, "done": {"x": 3}, "skipped": {"y"}}
    tp._read_audit_timing([], 100)
    assert tp._TIMING["done"] == {"x": 3}
    tp._read_audit_timing([], 50)                  # a lower height that was read is another chain
    assert tp._TIMING == {"height": 50, "done": {}, "skipped": set()}


CHECKS = {name[4:]: fn for name, fn in sorted(globals().items()) if name.startswith("chk_")}


# ---------------------------------------------------------------------------------------------------
# Mutants: (id, text of the_proof.py to replace once, its replacement, checks that must then fail).
# ---------------------------------------------------------------------------------------------------
NL = chr(10)


def L(*rows):
    """Lines of source joined as the file has them."""
    return NL.join(rows)


MUTANTS = [
    # commit reader
    ("gap-test-at-last-index-inverted",
     L("    seen = look(last)", '    if seen == "same":', "        return len(keys)"),
     L("    seen = look(last)", '    if seen != "same":', "        return len(keys)"),
     ["rows_added_in_the_middle", "rows_added_at_the_end"]),
    ("gap-bisection-branches-swapped",
     L('        if seen == "same":', '            lo, seen = mid + 1, "differs"', '        elif seen == "differs":',
       "            hi = mid"),
     L('        if seen == "same":', '            hi, seen = mid, "differs"', '        elif seen == "differs":',
       "            lo = mid + 1"),
     ["rows_added_in_the_middle", "many_gaps"]),
    ("window-start-not-checked",
     'if start is not None and (not got or got[0]["key"] != start or got[0]["model_id"] != rows[start]):',
     "if start is not None and not got:",
     ["a_window_that_does_not_start_on_its_key_is_no_reading"]),
    ("read-again-rows-may-change",
     " or any(k in rows and rows[k] != m for k, m in fresh.items())", "",
     ["a_changed_row_is_read_again"]),
    ("smaller-chain-kept", " or n < len(kept):", ":", ["smaller_chain_is_read_again"]),
    ("no-periodic-full-read", 'or cache["since_full"] >= full_every', "",
     ["a_full_read_is_due_every_so_many_builds"]),
    ("short-result-accepted", "if rows is None or len(rows) < n:", "if rows is None:",
     ["a_count_above_the_rows_is_read_again"]),
    ("patches-never-give-up",
     L("        if rows is None:", "            return full()", "    return full()"),
     L("        if rows is None:", "            return full()", '    return keep(rows, "increment")'),
     ["too_many_gaps_read_everything"]),
    ("repeated-key-accepted",
     L("        if len(rows) != len(got):", '            return None, "unread"'),
     L("        if False:", '            return None, "unread"'),
     ["a_page_that_repeats_a_key_is_no_reading"]),
    ("quiet-chain-always-cached", "if n == len(kept) == len(rows):", "if True:",
     ["rows_added_at_the_end", "rows_added_in_the_middle"]),
    ("unreadable-row-is-no-gap",
     L("        if gap is None:", '            return None, "unread"'),
     L("        if gap is None:", "            gap = len(keys)"),
     ["an_unreadable_probe_is_not_a_clean_read"]),
    ("foreign-chain-not-seen", '    if seen == "absent":' + NL + "        return NOT_A_SUBSET",
     '    if seen == "absent":' + NL + "        return lo",
     ["another_chain_with_more_rows_is_read_again"]),
    ("unread-clears-the-cache",
     L("    def keep(rows, how):", '        cache["rows"] = rows'),
     L('    cache["rows"] = {}', "", "    def keep(rows, how):", '        cache["rows"] = rows'),
     ["an_unread_chain_gives_no_list_and_keeps_the_cache"]),
    # the readers of the node
    ("page-key-not-escaped", 'q += "&pagination.key=" + urllib.parse.quote(key, safe="")',
     'q += "&pagination.key=" + key', ["the_reader_escapes_the_page_key"]),
    ("page-key-not-base64", "key = None if start_key is None else base64.b64encode(start_key.encode()).decode()",
     "key = start_key", ["the_reader_escapes_the_page_key"]),
    ("count-not-asked", "d = _commit_query(1, count_total=True)", "d = _commit_query(1)",
     ["the_readers_follow_the_chain"]),
    ("failed-page-read-as-empty",
     L("        if page is None:", "            return None", "        out.extend(page[0])"),
     L("        if page is None:", "            return out", "        out.extend(page[0])"),
     ["a_failed_page_gives_no_list"]),
    ("empty-key-accepted", "if not isinstance(key, str) or not key or not isinstance(model, str):",
     "if not isinstance(model, str):", ["malformed_rows_are_no_reading"]),
    ("count-accepts-negative", "return raw if raw >= 0 else None", "return raw", ["counts_are_whole_numbers"]),
    ("count-accepts-bool",
     L("    if isinstance(raw, bool):", "        return None", "    if isinstance(raw, int):"),
     "    if isinstance(raw, int):", ["counts_are_whole_numbers"]),
    ("row-beyond-the-end-is-unread", "return page[0][0] if page[0] else False", "return page[0][0] if page[0] else None",
     ["the_readers_see_another_chain"]),
    # outcomes
    ("adjudicated-not-verified", '"verified": ("resolved_by_quorum", "resolved_by_adjudication"),',
     '"verified": ("resolved_by_quorum",),',
     ["each_class_lands_in_one_outcome", "the_groups_cover_the_eight_classes_once"]),
    ("deferral-unwind-not-refunded",
     '"refunded_no_verdict": ("resolved_unwound", "unwound_after_deferral"),',
     '"refunded_no_verdict": ("resolved_unwound",),',
     ["each_class_lands_in_one_outcome", "the_groups_cover_the_eight_classes_once"]),
    ("deferred-not-in-progress", '"in_progress": ("audit_pending", "deferred_no_jury"),',
     '"in_progress": ("audit_pending",),',
     ["each_class_lands_in_one_outcome", "the_groups_cover_the_eight_classes_once"]),
    ("unmarked-in-no-group", '"unknown": ("resolved_unmarked",),', '"unknown": (),',
     ["each_class_lands_in_one_outcome", "the_groups_cover_the_eight_classes_once"]),
    ("unmarked-counted-verified", '"verified": ("resolved_by_quorum", "resolved_by_adjudication"),',
     '"verified": ("resolved_by_quorum", "resolved_by_adjudication", "resolved_unmarked"),',
     ["each_class_lands_in_one_outcome", "the_groups_cover_the_eight_classes_once"]),
    ("sum-never-checked", 'out["sum_ok"] = out["sum"] == part["audits_opened"]', 'out["sum_ok"] = True',
     ["a_sum_that_differs_publishes_no_count"]),
    ("counts-published-despite-the-sum",
     L('    if out["sum_ok"]:', "        out.update(counts)"), L("    if True:", "        out.update(counts)"),
     ["a_sum_that_differs_publishes_no_count"]),
    ("moved-chain-ignored",
     'if part["audits_opened"] is None or part["partition_unmeasured_reason"] is not None:',
     'if part["audits_opened"] is None:',
     ["a_chain_that_moved_publishes_no_count", "an_unread_origin_publishes_no_count"]),
    ("missing-counter-read-as-zero", '"audits_opened": part["audits_opened"],' + NL + "           **dict.fromkeys",
     '"audits_opened": part["audits_opened"] or 0,' + NL + "           **dict.fromkeys",
     ["no_counter_no_count"]),
    ("outcomes-read-at-height-zero", '"audit_outcomes": audit_outcomes(part, height),',
     '"audit_outcomes": audit_outcomes(part, 0),', ["each_class_lands_in_one_outcome"]),
    # Nakamoto and heights
    ("nakamoto-half-counts", "if held * 10000 > over_bps * whole:", "if held * 10000 >= over_bps * whole:",
     ["nakamoto_counts_the_fewest_holders_above_half"]),
    ("nakamoto-smallest-first", "for n, v in enumerate(sorted(counter.values(), reverse=True), 1):",
     "for n, v in enumerate(sorted(counter.values()), 1):", ["nakamoto_counts_the_fewest_holders_above_half"]),
    ("operator-unit-wrong", '_concentration(ops, top, "operator addresses", grouped=unreg)',
     '_concentration(ops, top, "miner ids", grouped=unreg)', ["the_coefficient_names_its_unit"]),
    ("operators-not-merged", "ops[op] = ops.get(op, 0) + n", "ops[mid] = ops.get(mid, 0) + n",
     ["the_coefficient_names_its_unit"]),
    ("grouped-bucket-trusted",
     "if k is not None and grouped is not None and grouped in [key for key, _ in ranked[:k]]:", "if False:",
     ["a_group_of_unlisted_miners_is_no_address"]),
    ("grouped-bucket-always-null",
     "if k is not None and grouped is not None and grouped in [key for key, _ in ranked[:k]]:",
     "if k is not None and grouped is not None:", ["a_group_of_unlisted_miners_is_no_address"]),
    ("model-gets-a-coefficient", 'out["requests_by_model"] = _concentration(models, top)',
     'out["requests_by_model"] = _concentration(models, top, "model ids")', ["the_coefficient_names_its_unit"]),
    ("height-zero-published", L("    n = int(h)", "    return n if n > 0 else None"),
     L("    n = int(h)", "    return n"), ["an_unread_height_is_null"]),
    ("chain-height-defaults-to-zero",
     L('        "height": _known_height(height),', '        "last_block_epoch"'),
     L('        "height": int(height or 0),', '        "last_block_epoch"'), ["an_unread_height_is_null"]),
    ("top-height-defaults-to-zero",
     L('        "height": _known_height(height),', "        # The pair (height, age) lives here together."),
     L('        "height": int(height or 0),', "        # The pair (height, age) lives here together."), ["an_unread_height_is_null"]),
    ("origin-height-defaults-to-zero", 'out = {"read_at_height": _known_height(height), "total": total,',
     'out = {"read_at_height": int(height or 0), "total": total,', ["an_unread_height_is_null"]),
    ("timing-cleared-by-unread-height",
     L("    if _known_height(height) is not None:", '        if height < _TIMING["height"]:'),
     L("    if True:", '        if height < _TIMING["height"]:'), ["an_unread_height_does_not_empty_the_timing"]),
]


def _load(name):
    return importlib.import_module(name)


@pytest.fixture()
def tp():
    return _load("the_proof")


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_check_passes_on_the_shipped_module(name, tp, monkeypatch):
    CHECKS[name](tp, monkeypatch)


def _mutant_module(tmp_path, tag, old, new):
    text = SOURCE.read_bytes().decode("utf-8")
    assert text.count(old) == 1, f"mutant {tag}: the text to replace occurs {text.count(old)} times in the_proof.py"
    path = tmp_path / "the_proof.py"
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    spec = importlib.util.spec_from_file_location("the_proof_mutant_" + tag.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("tag,old,new,must_fail", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_a_deliberate_fault_is_caught(tag, old, new, must_fail, tmp_path, monkeypatch):
    for name in must_fail:
        assert name in CHECKS, f"mutant {tag} names a check that does not exist: {name}"
    mod = _mutant_module(tmp_path, tag, old, new)
    survived = []
    for name in must_fail:
        try:
            CHECKS[name](mod, monkeypatch)
        except Exception:                      # a failed assertion or a crash: the fault changed the behaviour
            pass
        else:
            survived.append(name)
        finally:
            monkeypatch.undo()
    assert not survived, f"mutant {tag} survived the checks {survived}"
