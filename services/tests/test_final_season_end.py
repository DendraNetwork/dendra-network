"""Bench of the season's END (owner's decision of 2026-10-07, ADR-047 decision 17): the season holds every
block whose HEADER time is before `RULES["end_time"]`, and no other.

The chain is replaced by a fake CometBFT RPC (`final_season_chain._get`) that serves header times the way a
node does (RFC 3339, nanoseconds, trailing zeros trimmed, no fraction when it is zero) and answers a read
outside the chain with the node's JSON-RPC error. Every time below is written as TEXT the way the node
writes it, and the bench checks the reader never compares that text: half a second after the end sorts
BEFORE the end as a string.

Covered: `time_epoch`, `block_time`, `latest`, `season_end_height` (exact boundary, sub-second blocks, all
before raises, lo already after, agreement with the definition by brute force, O(log n) reads),
`end_epoch`, `day_bounds` with `end_height`, and `final_season_rank.season_window` (whole day, whole day that
is the last, cut day, after the season, not final both ways)."""
import calendar
import datetime
import math
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import final_season_chain as C  # noqa: E402
import final_season_rank as RK  # noqa: E402
from final_season_rules import RULES, day_bounds, end_epoch  # noqa: E402

RPC = "http://n:26657"
NS = 1_000_000_000
E = end_epoch()
DAY = RULES["day_blocks"]
START = 1
F = 100   # finality blocks used by this bench


def iso(t_ns: int) -> str:
    """A header time as CometBFT writes it: RFC 3339 with nanoseconds, trailing zeros trimmed, and no
    fraction at all when it is zero."""
    s, ns = divmod(t_ns, NS)
    out = datetime.datetime.fromtimestamp(s, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    frac = f"{ns:09d}".rstrip("0")
    return out + ("." + frac if frac else "") + "Z"


def steady(boundary: int, *, step_ns: int = 5 * NS, offset_ns: int = 0):
    """Header times (epoch ns) one block every `step_ns`, block `boundary` at the end plus `offset_ns`.
    Offset 0: `boundary` is the FIRST block at the end, `boundary - 1` the season's last block."""
    return lambda h: E * NS + (h - boundary) * step_ns + offset_ns


class Node:
    """A fake node. Block h (1 <= h <= latest) has header time iso(ns_of(h)), unless `raw` serves another
    string for it; any other height answers the JSON-RPC error a real node gives. `budget` caps the block
    reads, so a search that scans fails at once instead of reading a million blocks."""

    def __init__(self, ns_of, latest: int, raw: dict | None = None, budget: int = 10_000):
        self.ns_of, self.latest, self.raw, self.budget = ns_of, latest, raw or {}, budget
        self.reads: list = []
        self.status_reads = 0

    def text(self, h: int) -> str:
        return self.raw[h] if h in self.raw else iso(self.ns_of(h))

    def get(self, url: str, timeout: float = 10.0) -> dict:
        u = urllib.parse.urlsplit(url)
        assert f"{u.scheme}://{u.netloc}" == RPC, url
        if u.path == "/status":
            self.status_reads += 1
            return {"jsonrpc": "2.0", "id": -1, "result": {"sync_info": {
                "latest_block_height": str(self.latest), "latest_block_time": self.text(self.latest)}}}
        if u.path == "/block":
            h = int(urllib.parse.parse_qs(u.query)["height"][0])
            self.reads.append(h)
            if len(self.reads) > self.budget:
                raise AssertionError(f"more than {self.budget} block reads: the search scans")
            if not 1 <= h <= self.latest:
                return {"jsonrpc": "2.0", "id": -1, "error": {
                    "code": -32603, "message": "Internal error",
                    "data": f"height {h} must be less than or equal to the current blockchain height {self.latest}"}}
            return {"jsonrpc": "2.0", "id": -1,
                    "result": {"block": {"header": {"height": str(h), "time": self.text(h)}}}}
        raise AssertionError(f"unexpected read {url}")

    def last_before(self, lo: int, hi: int, end: int = E) -> int:
        """The DEFINITION, by brute force and in nanoseconds (never through `time_epoch`): the greatest
        height in [lo, hi] whose header time is before `end`, or lo - 1 when there is none."""
        inside = [h for h in range(lo, hi + 1) if self.ns_of(h) < end * NS]
        return max(inside) if inside else lo - 1


@pytest.fixture()
def node(monkeypatch):
    def make(ns_of, latest: int, raw: dict | None = None, budget: int = 10_000) -> Node:
        n = Node(ns_of, latest, raw, budget)
        monkeypatch.setattr(C, "_get", n.get)
        return n
    return make


# ---------------------------------------------------------------- the fake writes what the node writes

def test_the_fake_node_writes_header_times_as_cometbft_does():
    assert iso(E * NS) == "2026-11-08T00:00:00Z"
    assert iso(E * NS + NS // 2) == "2026-11-08T00:00:00.5Z"
    assert iso(E * NS - 1) == "2026-11-07T23:59:59.999999999Z"
    assert iso(E * NS + 120_000_000) == "2026-11-08T00:00:00.12Z"


# ---------------------------------------------------------------- end_epoch

def test_end_epoch_of_the_rules_is_the_8th_of_november_2026_at_midnight_utc():
    assert RULES["end_time"] == "2026-11-08T00:00:00Z"
    assert end_epoch(RULES) == end_epoch() == 1794096000
    assert end_epoch(RULES) == int(datetime.datetime(2026, 11, 8, tzinfo=datetime.timezone.utc).timestamp())
    # "ends 7 November 2026, 23:59 UTC": the last nanosecond of the 7th is in, midnight is out.
    assert C.time_epoch("2026-11-07T23:59:59.999999999Z") < end_epoch()
    assert not C.time_epoch("2026-11-08T00:00:00Z") < end_epoch()


def test_end_epoch_reads_the_rules_it_is_given():
    assert end_epoch(dict(RULES, end_time="2027-01-01T00:00:00Z")) == 1798761600


def test_the_rules_hold_no_count_of_days():
    assert "days" not in RULES


# ---------------------------------------------------------------- time_epoch

@pytest.mark.parametrize("k", range(1, 10))
def test_a_fraction_of_any_length_is_dropped_never_rounded_up(k):
    assert C.time_epoch("2026-11-07T23:59:59." + "9" * k + "Z") == E - 1
    assert C.time_epoch("2026-11-08T00:00:00." + "1" * k + "Z") == E
    assert C.time_epoch("2026-11-08T00:00:00." + "0" * (k - 1) + "5Z") == E


@pytest.mark.parametrize("s, want", [
    ("2026-11-08T00:00:00Z", E),                      # a zero fraction is not written at all
    ("2026-11-07T23:59:59Z", E - 1),
    ("2026-11-07T23:59:59.1Z", E - 1),                # 100 000 000 ns, trailing zeros trimmed
    ("2026-11-07T23:59:59.000000001Z", E - 1),
    ("2026-11-08T00:00:00.000000001Z", E),
    ("1970-01-01T00:00:00Z", 0),
    ("2024-02-29T12:34:56.789Z",
     int(datetime.datetime(2024, 2, 29, 12, 34, 56, tzinfo=datetime.timezone.utc).timestamp())),
])
def test_time_epoch_reads_whole_utc_seconds(s, want):
    assert C.time_epoch(s) == want


@pytest.mark.parametrize("s", [
    "", "garbage", None, 1794096000, 1794096000.5, b"2026-11-08T00:00:00Z", ["2026-11-08T00:00:00Z"],
    "2026-11-08T00:00:00",                 # no zone
    "2026-11-08T00:00:00+01:00",           # an offset, read as UTC it would be an hour off
    "2026-11-08 00:00:00Z",
    "2026-11-08T00:00:00.Z",               # an empty fraction
    "2026-11-08T00:00:00.1234567890Z",     # ten digits: finer than the nanosecond a header carries
    " 2026-11-08T00:00:00Z",
    "2026-11-08T00:00:00Z ",
    "2026-11-8T00:00:00Z",
    "26-11-08T00:00:00Z",
    # The right shape, an impossible instant: `timegm` alone rolls these over (31 November becomes
    # 1 December) or lets a bare ValueError escape, which kills the service's chain watcher.
    "2026-13-01T00:00:00Z", "2026-00-10T00:00:00Z", "2026-11-31T00:00:00Z",
    "2026-11-07T24:00:00Z", "2026-11-07T23:59:60Z",
])
def test_an_unreadable_time_raises_never_reads_as_early(s):
    with pytest.raises(C.ChainUnreadable):
        C.time_epoch(s)


def test_time_is_never_compared_as_text():
    after, at = "2026-11-08T00:00:00.5Z", "2026-11-08T00:00:00Z"
    assert after < at                      # the trap: as TEXT, half a second after the end sorts before it
    assert C.time_epoch(after) >= C.time_epoch(at) == E
    assert not C.time_epoch(after) < E
    assert C.time_epoch("2026-11-07T23:59:59.9Z") < C.time_epoch("2026-11-08T00:00:00Z")


# ---------------------------------------------------------------- block_time

def test_block_time_reads_the_header_of_the_block_asked(node):
    n = node(steady(600, offset_ns=NS // 2), latest=1000)
    assert n.text(600) == "2026-11-08T00:00:00.5Z"
    assert C.block_time(RPC, 600) == E
    assert C.block_time(RPC, 599) == E - 5         # 23:59:55.5
    assert n.reads == [600, 599]


def test_block_time_of_a_block_the_node_does_not_have_raises(node):
    node(steady(600), latest=1000)
    with pytest.raises(C.ChainUnreadable):
        C.block_time(RPC, 1001)
    with pytest.raises(C.ChainUnreadable):
        C.block_time(RPC, 0)


def test_block_time_unreadable_or_missing_raises(node, monkeypatch):
    node(steady(600), latest=1000, raw={7: "yesterday", 8: ""})
    for h in (7, 8):
        with pytest.raises(C.ChainUnreadable):
            C.block_time(RPC, h)
    for answer in ({"result": {"block": {"header": {"height": "9"}}}},       # no time
                   {"result": {"block": {"header": {"time": None}}}},
                   {"result": {"block": None}},
                   {"result": None},
                   {}):
        monkeypatch.setattr(C, "_get", lambda url, timeout=10.0, a=answer: a)
        with pytest.raises(C.ChainUnreadable):
            C.block_time(RPC, 9)


# ---------------------------------------------------------------- latest

def test_latest_reads_height_and_time_from_one_status_read(node):
    n = node(steady(600, offset_ns=NS // 2), latest=600)
    assert C.latest(RPC) == (600, E)
    assert n.status_reads == 1 and n.reads == []
    n2 = node(steady(600), latest=599)
    assert C.latest(RPC) == (599, E - 5)
    assert n2.status_reads == 1


@pytest.mark.parametrize("sync_info", [
    {"latest_block_height": "600"},                                            # no time
    {"latest_block_time": "2026-11-08T00:00:00Z"},                             # no height
    {"latest_block_height": "600", "latest_block_time": "soon"},
    {"latest_block_height": "600", "latest_block_time": None},
    {"latest_block_height": "six hundred", "latest_block_time": "2026-11-08T00:00:00Z"},
    {"latest_block_height": None, "latest_block_time": "2026-11-08T00:00:00Z"},
    None,
])
def test_latest_without_a_readable_height_or_time_raises(monkeypatch, sync_info):
    monkeypatch.setattr(C, "_get", lambda url, timeout=10.0: {"result": {"sync_info": sync_info}})
    with pytest.raises(C.ChainUnreadable):
        C.latest(RPC)


def test_latest_without_a_result_raises(monkeypatch):
    monkeypatch.setattr(C, "_get", lambda url, timeout=10.0: {"error": {"code": -32603}})
    with pytest.raises(C.ChainUnreadable):
        C.latest(RPC)


# ---------------------------------------------------------------- season_end_height

def test_the_block_at_the_end_exactly_is_not_in_the_season(node):
    n = node(steady(600), latest=1000)
    assert n.text(600) == "2026-11-08T00:00:00Z" and n.text(599) == "2026-11-07T23:59:55Z"
    assert C.season_end_height(RPC, E, 1, 1000) == 599 == n.last_before(1, 1000)


def test_half_a_second_after_the_end_is_after_the_end(node):
    n = node(steady(600, offset_ns=NS // 2), latest=1000)
    assert n.text(600) == "2026-11-08T00:00:00.5Z"
    assert C.season_end_height(RPC, E, 1, 1000) == 599


def test_the_last_nanosecond_before_the_end_is_in_the_season(node):
    n = node(steady(600, offset_ns=-1), latest=1000)
    assert n.text(600) == "2026-11-07T23:59:59.999999999Z"
    assert C.season_end_height(RPC, E, 1, 1000) == 600


def test_sub_second_blocks_that_floor_to_the_end_are_all_after_it(node):
    # Five blocks a second: 600..604 all read as second E once floored, and none is in the season.
    n = node(steady(600, step_ns=NS // 5), latest=1000)
    assert [C.time_epoch(n.text(h)) for h in range(599, 606)] == [E - 1] + [E] * 5 + [E + 1]
    assert C.season_end_height(RPC, E, 1, 1000) == 599 == n.last_before(1, 1000)


def test_hi_still_before_the_end_raises_never_answers_hi(node):
    node(steady(2000), latest=1500)
    with pytest.raises(C.ChainUnreadable, match="not known yet"):
        C.season_end_height(RPC, E, 1, 1000)
    with pytest.raises(C.ChainUnreadable):
        C.season_end_height(RPC, E, 1, 1500)


def test_hi_that_the_node_cannot_read_raises(node):
    node(steady(600), latest=1000)
    with pytest.raises(C.ChainUnreadable):
        C.season_end_height(RPC, E, 1, 1200)


def test_lo_already_after_the_end_answers_lo_minus_one_without_reading_it(node):
    n = node(steady(600), latest=1000)
    assert C.season_end_height(RPC, E, 700, 1000) == 699
    assert 699 not in n.reads
    n.reads.clear()
    assert C.season_end_height(RPC, E, 600, 1000) == 599       # lo is the first block at the end
    assert 599 not in n.reads
    # lo = 1 at the end: the answer is 0, and block 0 (which no node has) is never read.
    n = node(steady(1), latest=50)
    assert C.season_end_height(RPC, E, 1, 50) == 0
    assert 0 not in n.reads


def test_season_end_height_matches_the_definition_everywhere(node):
    for offset in (0, NS // 2, -1, 1):
        for lo in (1, 5):
            for hi in range(lo, lo + 24):
                for boundary in range(lo - 2, hi + 2):
                    if boundary < 1:
                        continue
                    n = node(steady(boundary, offset_ns=offset), latest=hi + 5)
                    if not n.ns_of(hi) >= E * NS:
                        with pytest.raises(C.ChainUnreadable):
                            C.season_end_height(RPC, E, lo, hi)
                        continue
                    want = n.last_before(lo, hi)
                    assert C.season_end_height(RPC, E, lo, hi) == want, (offset, lo, hi, boundary)
                    assert lo - 1 not in n.reads                  # lo - 1 is never read


def test_season_end_height_reads_logarithmically_many_blocks(node):
    lo, hi = 1, 2 ** 20
    bound = 1 + math.ceil(math.log2(hi - lo + 2))       # block hi, then one read per halving
    for boundary in (1, 2, 3, 12_345, 2 ** 19, 2 ** 20 - 1, 2 ** 20):
        n = node(steady(boundary), latest=hi, budget=bound)
        assert C.season_end_height(RPC, E, lo, hi) == boundary - 1
        assert len(n.reads) <= bound, (boundary, len(n.reads))
        assert len(set(n.reads)) == len(n.reads)        # no block read twice


# ---------------------------------------------------------------- day_bounds with end_height

def test_day_bounds_without_an_end_is_the_full_day():
    assert day_bounds(0, START) == (1, DAY)
    assert day_bounds(1, START) == (DAY + 1, 2 * DAY)
    assert day_bounds(1, START, end_height=None) == (DAY + 1, 2 * DAY)


def test_the_day_that_holds_the_end_is_cut_there():
    assert day_bounds(1, START, end_height=DAY + 101) == (DAY + 1, DAY + 101)
    assert day_bounds(1, START, end_height=DAY + 1) == (DAY + 1, DAY + 1)        # a one-block last day
    assert day_bounds(1, START, end_height=2 * DAY) == (DAY + 1, 2 * DAY)        # the end on its last block


def test_a_day_before_the_end_is_never_lengthened_by_it():
    assert day_bounds(0, START, end_height=5 * DAY) == (1, DAY)
    assert day_bounds(1, START, end_height=2 * DAY + 1) == (DAY + 1, 2 * DAY)


@pytest.mark.parametrize("end_height", [DAY, DAY - 1, 1, 0])
def test_a_day_after_the_end_raises_never_an_empty_or_inverted_window(end_height):
    with pytest.raises(ValueError, match="after the season"):
        day_bounds(1, START, end_height=end_height)


def test_day_bounds_reads_the_rules_it_is_given():
    rules = dict(RULES, day_blocks=10)
    assert day_bounds(2, 1, rules, end_height=25) == (21, 25)
    assert day_bounds(2, 1, rules, end_height=99) == (21, 30)
    with pytest.raises(ValueError):
        day_bounds(3, 1, rules, end_height=30)


# ---------------------------------------------------------------- season_window

def window(day, current, end=None):
    return RK.season_window(RPC, day, START, current, F, end)


def test_a_whole_day_before_the_end_is_not_the_last(node):
    b = 3 * DAY + 500                                   # the end falls in day 3
    node(steady(b), latest=2 * DAY + 5000)
    assert window(0, 2 * DAY + 5000) == (1, DAY, None)
    assert window(1, 2 * DAY + 5000) == (DAY + 1, 2 * DAY, None)
    # Final exactly `finality` blocks past its last block, and not one block before.
    node(steady(b), latest=DAY + F)
    assert window(0, DAY + F) == (1, DAY, None)
    with pytest.raises(ValueError, match="not final"):
        window(0, DAY + F - 1)


def test_a_whole_day_whose_next_block_is_at_the_end_is_the_last(node):
    n = node(steady(DAY + 1), latest=DAY + F + 10)      # block DAY + 1, first of day 1, is at the end
    assert n.text(DAY + 1) == "2026-11-08T00:00:00Z"
    assert window(0, DAY + F + 10) == (1, DAY, DAY)
    node(steady(DAY + 1, offset_ns=NS // 2), latest=DAY + F + 10)
    assert window(0, DAY + F + 10, end=E) == (1, DAY, DAY)
    # One block later, the next block is still in the season: day 0 is whole and NOT the last.
    node(steady(DAY + 2), latest=DAY + F + 10)
    assert window(0, DAY + F + 10) == (1, DAY, None)


def test_the_day_that_holds_the_end_is_cut_at_the_seasons_last_block(node):
    b = DAY + 1 + 4000
    n = node(steady(b), latest=b + F)
    first, last, end_h = window(1, b + F)
    assert (first, last, end_h) == (DAY + 1, b - 1, b - 1)
    assert (first, last) == day_bounds(1, START, end_height=end_h)
    assert len(n.reads) <= 3 + math.ceil(math.log2(DAY + 1))     # never a scan of the day
    # Far past the day: the same window, read from the same signed header times.
    node(steady(b), latest=3 * DAY)
    assert window(1, 3 * DAY) == (DAY + 1, b - 1, b - 1)


def test_the_cut_day_is_final_exactly_finality_blocks_past_the_seasons_last_block(node):
    # The service closes the day's records at end_height + finality (`State.day_closed`): the ranking
    # must be able to rank it at that same height, not one block later, and not one block earlier.
    b = DAY + 1 + 4000                                  # first block at the end; the last is b - 1
    node(steady(b), latest=b - 1 + F)
    assert window(1, b - 1 + F) == (DAY + 1, b - 1, b - 1)
    node(steady(b), latest=b - 2 + F)
    with pytest.raises(ValueError, match="not final"):
        window(1, b - 2 + F)


def test_season_window_honours_the_end_it_is_given(node):
    b = DAY + 1 + 4000
    node(steady(b), latest=3 * DAY)
    # An hour later is 720 blocks of 5 s later.
    assert window(1, 3 * DAY, end=E + 3600) == (DAY + 1, b + 719, b + 719)


@pytest.mark.parametrize("b, day", [(DAY + 1 + 4000, 2), (DAY + 1, 1), (DAY + 1, 2), (2, 1)])
def test_a_day_after_the_season_is_refused(node, b, day):
    node(steady(b), latest=4 * DAY)
    with pytest.raises(ValueError, match="after the season"):
        window(day, 4 * DAY)


def test_a_day_whose_finality_probe_is_before_its_first_block_is_not_final(node):
    n = node(steady(5 * DAY), latest=DAY + F)
    with pytest.raises(ValueError, match="not final"):
        window(1, DAY + F)                                # probe = DAY, before the day's first block
    assert n.reads == []                                  # refused before any read


def test_a_day_still_before_the_end_at_its_probe_is_not_final(node):
    # The end is far: the chain is inside day 1 and has not reached it.
    node(steady(5 * DAY), latest=DAY + 1 + F + 50)
    with pytest.raises(ValueError, match="not final"):
        window(1, DAY + 1 + F + 50)
    # The end is inside day 1, but the chain is not yet `finality` past the season's last block (b - 1):
    # one block short of it.
    b = DAY + 1 + 4000
    node(steady(b), latest=b - 2 + F)
    with pytest.raises(ValueError, match="not final"):
        window(1, b - 2 + F)


def test_an_unreadable_header_time_is_not_a_refusal_for_lateness(node):
    b = DAY + 1 + 4000
    node(steady(b), latest=b + F, raw={b: "tomorrow"})
    with pytest.raises(C.ChainUnreadable):
        window(1, b + F)


def test_season_window_reads_the_default_end_from_the_rules(node):
    b = DAY + 1 + 4000
    node(steady(b), latest=3 * DAY)
    assert window(1, 3 * DAY) == window(1, 3 * DAY, end=end_epoch(RULES)) == (DAY + 1, b - 1, b - 1)
