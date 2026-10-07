"""Final Testnet Season chain reads: the only place the programme talks to the node.

Every read here either returns what it read or RAISES. None of them returns an empty list for a query
that failed: "no job settled" and "the node did not answer" must never look alike, because the first
pays nobody for a reason and the second would pay nobody by accident (rule of zero: a failed read is
unknown, not zero).

AN INDEX THAT DOES NOT REACH BACK IS A FAILED READ TOO. A node bootstrapped by state sync, or one that
prunes, has no transaction or block index before some height, and a search over that range answers
"nothing found" with every sign of success. `require_index_from` refuses such a node for the heights a
caller is about to search; without it, a ranking would credit no work and a payment check would find no
earlier payment.
"""
from __future__ import annotations

import calendar
import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


class ChainUnreadable(RuntimeError):
    pass


def rpc_url(node: str) -> str:
    """`tcp://chain:26657` (the CLI's form) -> `http://chain:26657`."""
    return "http://" + node[len("tcp://"):] if node.startswith("tcp://") else node.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could point the reader at another host than the node it was given. Refused, not followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)
_MAX_ANSWER = 4 * 1024 * 1024


def _read_bounded(r, deadline: float) -> bytes:
    """Read at most _MAX_ANSWER bytes, and give up at `deadline` whatever the pace. A socket timeout bounds
    each read, not the whole answer: a server sending one byte every few seconds would otherwise hold the
    reader for hours."""
    chunks, total = [], 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("answer not complete before the deadline")
        b = r.read(65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)
        total += len(b)
        if total > _MAX_ANSWER:
            raise ValueError("answer larger than 4 MiB")


def _get(url: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + 3 * timeout
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            raw = _read_bounded(r, deadline)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise ChainUnreadable(f"{url}: {type(e).__name__}: {e}") from e


def status(rpc: str) -> dict:
    d = _get(rpc + "/status")
    r = d.get("result")
    if not isinstance(r, dict):
        raise ChainUnreadable("status without a result")
    return r


def height(rpc: str) -> int:
    try:
        return int(status(rpc)["sync_info"]["latest_block_height"])
    except (KeyError, TypeError, ValueError) as e:
        raise ChainUnreadable(f"status without a height: {e}") from e


def require_index_from(rpc: str, first_height: int) -> None:
    """Refuse a node whose block and transaction history does not reach back to `first_height`."""
    try:
        earliest = int(status(rpc)["sync_info"].get("earliest_block_height", 0) or 0)
    except (KeyError, TypeError, ValueError) as e:
        raise ChainUnreadable(f"status without earliest_block_height: {e}") from e
    if earliest == 0 or earliest > max(1, int(first_height)):
        raise ChainUnreadable(f"this node's history starts at block {earliest}, after block {first_height}: "
                              f"a search there would find nothing and look like a success. Use a node "
                              f"that keeps the whole history (no state sync, no pruning of the index).")


_HEADER_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?Z$")


def time_epoch(s) -> int:
    """Whole UTC seconds of a CometBFT header time (RFC 3339, nanoseconds, trailing zeros trimmed), the
    fraction dropped. NEVER compared as text: '…T00:00:00.5Z' sorts before '…T00:00:00Z' ('.' < 'Z'), so a
    block after the end would read as inside the season. Unreadable raises: a time not read is not early."""
    m = _HEADER_TIME.match(s) if isinstance(s, str) else None
    if not m:
        raise ChainUnreadable(f"unreadable block time {s!r}")
    try:
        # datetime refuses month 13, 31 November, hour 24 or second 60, which timegm would roll over.
        t = datetime.datetime(*(int(x) for x in m.groups()))
    except ValueError as e:
        raise ChainUnreadable(f"unreadable block time {s!r}: {e}") from e
    return calendar.timegm(t.timetuple())


def block_time(rpc: str, h: int, timeout: float = 10.0) -> int:
    """Header time of block `h`, in whole UTC seconds. BFT time: it never decreases from one block to the
    next, which is what makes the season's last block a binary search."""
    d = _get(f"{rpc}/block?height={int(h)}", timeout)
    try:
        return time_epoch(d["result"]["block"]["header"]["time"])
    except (KeyError, TypeError) as e:
        raise ChainUnreadable(f"block {h} without a header time: {e}") from e


def latest(rpc: str) -> tuple[int, int]:
    """(height, header time in whole UTC seconds) of the latest block, from ONE status read."""
    try:
        si = status(rpc)["sync_info"]
        return int(si["latest_block_height"]), time_epoch(si["latest_block_time"])
    except (KeyError, TypeError, ValueError) as e:
        raise ChainUnreadable(f"status without a height or a time: {e}") from e


def season_end_height(rpc: str, end: int, lo: int, hi: int) -> int:
    """The season's last block: the greatest height in [lo, hi] whose header time is before `end`.

    Called once the chain has passed the end, so block `hi` must be at or after it: if it is not, the
    end is NOT KNOWN yet, and that is raised — never answered with `hi`, which would close the season on
    the block the caller happened to read. Returns lo - 1 when block `lo` itself is at or after the end
    (the season holds no block from `lo` on). Every node finds the same height: header times are signed."""
    if block_time(rpc, hi) < end:
        raise ChainUnreadable(f"block {hi} is before the season's end: the last block is not known yet")
    a, b = lo - 1, hi        # time(a) < end (a == lo - 1 is never read), time(b) >= end
    while b - a > 1:
        m = (a + b) // 2
        if block_time(rpc, m) < end:
            a = m
        else:
            b = m
    return a


def app_hash_at(rpc: str, h: int, timeout: float = 10.0) -> str:
    d = _get(f"{rpc}/block?height={int(h)}", timeout)
    try:
        return d["result"]["block"]["header"]["app_hash"]
    except (KeyError, TypeError) as e:
        raise ChainUnreadable(f"block {h} without an app_hash: {e}") from e


def jobs_params(rest: str) -> dict:
    d = _get(rest.rstrip("/") + "/dendra/jobs/v1/params")
    p = d.get("params")
    if not isinstance(p, dict):
        raise ChainUnreadable("params answer without `params`")
    return p


def param_int(p: dict, key: str) -> int:
    """proto3 omits a zero field: absent is 0 (the query itself succeeded)."""
    v = p.get(key, 0)
    try:
        return int(v)
    except (TypeError, ValueError) as e:
        raise ChainUnreadable(f"param {key}={v!r} is not an integer") from e


def _cli(args: list, node: str, timeout: int = 60) -> dict:
    cmd = ["dendrad", *args, "--node", node, "--output", "json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ChainUnreadable(f"{' '.join(args[:3])}: {type(e).__name__}") from e
    if out.returncode != 0:
        raise ChainUnreadable(f"{' '.join(args[:3])}: rc={out.returncode} {out.stderr.strip()[:200]}")
    try:
        return json.loads(out.stdout)
    except ValueError as e:
        raise ChainUnreadable(f"{' '.join(args[:3])}: answer is not JSON") from e


SETTLE_ACTION = "/dendra.jobs.v1.MsgSettleSemantic"


def settle_heights(node: str, sender: str | None = None, max_pages: int = 2000) -> dict:
    """{job_id: height} of every semantic settlement, page by page; only those `sender` signed when given.
    Settling is permissionless: the work reward is limited to the programme's requests by the JOB's client,
    never by who signed the settlement, so the ranking reads every settlement. Only settlement messages are
    read: a transaction can hold others naming a job_id (a verdict, an opening), which settled nothing and
    would date that job's work to the wrong day."""
    q = f"message.action='{SETTLE_ACTION}'"
    if sender:
        q += f" AND message.sender='{sender}'"
    out, page = {}, 1
    while page <= max_pages:
        d = _cli(["query", "txs", "--query", q, "--page", str(page), "--limit", "100"], node)
        txs = d.get("txs") or []
        for t in txs:
            h = int(t.get("height", 0) or 0)
            if int(t.get("code", 0) or 0) != 0:
                continue                      # a refused settlement settled nothing
            for m in ((t.get("tx") or {}).get("body") or {}).get("messages") or []:
                if m.get("@type") != SETTLE_ACTION:
                    continue
                jid = m.get("job_id") or m.get("jobId")
                if jid and jid not in out:
                    out[jid] = h
        total = _total_count(d)
        if page * 100 >= total or not txs:
            return out
        page += 1
    raise ChainUnreadable(f"settlement search did not end after {max_pages} pages")


def _total_count(d: dict) -> int:
    """The search's own count of matches. Absent, the pages cannot be known to be complete: refused, not
    read as a total of 0, which would stop after the first page with every sign of success."""
    try:
        return int(d["total_count"])
    except (KeyError, TypeError, ValueError) as e:
        raise ChainUnreadable(f"search answer without a total_count ({type(e).__name__})") from e


AVAIL_ACTION = "/dendra.jobs.v1.MsgProveAvailability"


def presence_windows(node: str, first: int, last: int, epoch_blocks: int, max_pages: int = 5000) -> dict:
    """{miner_id: distinct availability windows} of the day (`presence_proofs`, windows only)."""
    return presence_proofs(node, first, last, epoch_blocks, max_pages)[0]


def presence_proofs(node: str, first: int, last: int, epoch_blocks: int, max_pages: int = 5000) -> tuple:
    """({miner_id: distinct availability windows}, {miner_id: operator}) proven by transactions the chain
    ACCEPTED at a height between `first` and `last` (a programme day); the window of a proof is its height
    // `epoch_blocks`, the chain's own arithmetic. The operator is the `creator` of the identity's latest
    proof that day, which the chain requires to be the miner's operator at that height: a fact of the day,
    still readable after the miner has left the registry.

    The transaction index is the only record that lasts: the chain purges its `Available` set at each
    window boundary and keeps only the LAST window per miner, and no query serves either. Only messages of
    the availability type are read: other messages carry a `miner_id` too, and a transaction can hold
    several, so a miner named in another message of the same transaction is not made present."""
    if int(epoch_blocks) <= 0:
        raise ChainUnreadable("avail_epoch_blocks is 0 on this chain: it refuses every availability proof, "
                              "so no presence can be read (none is not zero here: nobody could have any)")
    q = f"message.action='{AVAIL_ACTION}' AND tx.height>={int(first)} AND tx.height<={int(last)}"
    seen, operator, page = {}, {}, 1
    while page <= max_pages:
        d = _cli(["query", "txs", "--query", q, "--page", str(page), "--limit", "100"], node)
        txs = d.get("txs") or []
        for t in txs:
            if int(t.get("code", 0) or 0) != 0:
                continue                      # a refused proof proved nothing
            h = int(t.get("height", 0) or 0)
            if not first <= h <= last:
                continue
            for m in ((t.get("tx") or {}).get("body") or {}).get("messages") or []:
                if m.get("@type") != AVAIL_ACTION:
                    continue
                mid = m.get("miner_id") or m.get("minerId")
                if mid:
                    seen.setdefault(mid, set()).add(h // int(epoch_blocks))
                    who = m.get("creator")
                    if who and h >= operator.get(mid, (-1, ""))[0]:
                        operator[mid] = (h, who)
        total = _total_count(d)
        if page * 100 >= total or not txs:
            return {m: len(w) for m, w in seen.items()}, {m: w for m, (_h, w) in operator.items()}
        page += 1
    raise ChainUnreadable(f"presence search did not end after {max_pages} pages")


def default_node() -> str:
    return os.environ.get("DENDRA_NODE", "tcp://chain:26657")


def _g(d, *keys, default=""):
    for k in keys:
        if k in d:
            return d[k]
    return default


def _list_all(node: str, subcmd: str, field: str, max_pages: int = 400) -> list:
    """Every page of `query jobs <subcmd>`. One call stops at the SDK's default of 100 entries, and a
    truncated list only ever produces good news (fewer jobs to pay, fewer audits): read to the end."""
    out, key = [], None
    for _ in range(max_pages):
        args = ["query", "jobs", subcmd, "--page-limit", "500"]
        if key:
            args += ["--page-key", key]
        d = _cli(args, node, timeout=120)
        out.extend(d.get(field) or d.get(field.capitalize()) or [])
        pg = d.get("pagination") or {}
        key = pg.get("next_key") or pg.get("nextKey")
        if not key:
            return out
    raise ChainUnreadable(f"{subcmd}: more than {max_pages} pages")


def jobs(node: str) -> list:
    out = []
    for j in _list_all(node, "list-job", "job"):
        if not isinstance(j, dict):
            continue
        mid = _g(j, "minerId", "miner_id")
        slashes = _g(j, "slashRecords", "slash_records", default=[]) or []
        out.append({"id": _g(j, "jobId", "job_id"), "state": _g(j, "state"), "miner_id": mid,
                    "client": _g(j, "client"),
                    # A slash record against the job's own miner is how an adjudication that convicts is
                    # told apart from one that clears: both end in `+resolved+adjudicated`.
                    "slashed_primary": any(isinstance(s, dict) and _g(s, "minerId", "miner_id") == mid
                                           for s in slashes)})
    return out


def miners(node: str) -> list:
    return [{"id": _g(m, "minerId", "miner_id"), "operator": _g(m, "operator")}
            for m in _list_all(node, "list-miner", "miner") if isinstance(m, dict)]


def audit_committee(rpc: str, job_id: str) -> list:
    """The jurors DRAWN for a job's audit, read from the `audit_requested` event of the block that drew
    them. The chain accepts a verdict commit from any registered miner and keeps its anchored committee
    only until the audit resolves; the event is the record that survives, and the only one that says who
    was actually summoned."""
    q = urllib.parse.quote(f"\"audit_requested.job_id='{job_id}'\"")
    d = _get(f"{rpc}/block_search?query={q}&per_page=20&page=1")
    blocks = (d.get("result") or {}).get("blocks")
    if not isinstance(blocks, list):
        raise ChainUnreadable(f"block_search for {job_id} answered without a block list")
    members = set()
    for b in blocks:
        try:
            h = int(b["block"]["header"]["height"])
        except (KeyError, TypeError, ValueError) as e:
            raise ChainUnreadable(f"block_search entry without a height: {e}") from e
        r = (_get(f"{rpc}/block_results?height={h}").get("result") or {})
        for ev in r.get("finalize_block_events") or []:
            if ev.get("type") != "audit_requested":
                continue
            attrs = {a.get("key"): a.get("value") for a in ev.get("attributes") or [] if isinstance(a, dict)}
            if attrs.get("job_id") == job_id:
                members |= {m.strip() for m in (attrs.get("committee") or "").split(",") if m.strip()}
    return sorted(members)


def verdicts(node: str, audited: list, candidates: dict) -> dict:
    """{job_id: {juror: "0"|"1"}} for resolved audits, read for the SUMMONED jurors only (`candidates`:
    {job_id: [miner ids]}). A juror who did not vote has no commit, and the query answers NotFound — an
    answer. Any other failure is not, and raises."""
    out = {}
    for j in audited:
        votes = {}
        for m in candidates.get(j["id"], []):
            if m == j.get("miner_id"):
                continue
            cmd = ["dendrad", "query", "jobs", "get-commit", f"{j['id']}__verdict__{m}", "--node", node,
                   "--output", "json"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise ChainUnreadable(f"get-commit {j['id']}/{m}: {type(e).__name__}") from e
            if r.returncode != 0:
                if "code = NotFound" in (r.stderr + r.stdout):
                    continue
                raise ChainUnreadable(f"get-commit {j['id']}/{m}: rc={r.returncode} {r.stderr.strip()[:160]}")
            try:
                d = json.loads(r.stdout)
            except ValueError as e:
                raise ChainUnreadable(f"get-commit {j['id']}/{m}: not JSON") from e
            c = d.get("commit") or d.get("Commit") or {}
            rc = str(c.get("result_commit") or c.get("resultCommit") or "").strip()
            if rc in ("0", "1"):
                votes[m] = rc
        if votes:
            out[j["id"]] = votes
    return out
