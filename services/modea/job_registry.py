"""Job cache — `job_id -> paying client`. Same state machine as the miner registry.

Why this module exists
----------------------
A deposit of kind `req` must be bound to the job's paying client, not to a miner operator. Two
different authorities, two different registries:

    kind = "req"      -> who pays for this job    -> `job.client`      (this module)
    other kinds       -> who operates this miner  -> `miner.operator`  (`registry_cache`)

This module inherits from `RegistryCache` instead of copying it. The three-state rule
(`NEVER_READ` / `FEES` / `STALE`), the "never fail open" policy and the retention of cached content
on a failed read are already settled and tested once. Copying that shape is signing up for it to
diverge the first time a fix is applied on only one side. Only the extraction differs, so only the
extraction is rewritten.

Truncation is proven, not guessed
---------------------------------
The chain paginates correctly:

    proto/dendra/jobs/v1/query.proto  : QueryAllJobRequest{ pagination: PageRequest }
                                        QueryAllJobResponse{ job[], pagination: PageResponse }
    x/jobs/keeper/query_job.go        : ListJob -> query.CollectionPaginate(..., req.Pagination, ...)
                                        GetJob  -> reads one job by its id
    query.proto                       : REST route /dendra/jobs/v1/job/{job_id}

The truncation risk is therefore on the consumer side: a caller that queries once and ignores
`pagination.next_key` only ever sees the first page. That is an obligation of the caller, not a debt
of the chain, and the distinction changes the remedy.

Two consequences, both in the code below:
  - `list_truncated()` proves truncation from the payload itself (`pagination.next_key` non-empty).
    No heuristic on "suspicious" page sizes: a suspicion based on a round number is noise as soon as
    a real signal exists. It is part of this object's contract with `relay_write`, which calls it
    directly: an object without it is an error there, never a list read "complete".
  - `job_reader` (optional) resolves one job by its id through `GetJob`. That path cannot be
    truncated, since it lists nothing. When it is wired, a cache miss is no longer a verdict.

An unknown job means the caller refuses (the policy lives in `relay_write`). Losing a legitimate
write is noisy and repairable; accepting an illegitimate one is silent and final.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict

try:                                   # as part of the `modea` package
    from .registry_cache import FEES, NEVER_READ, STALE, RegistryCache
except ImportError:                    # flat layout, modules side by side
    from registry_cache import FEES, NEVER_READ, STALE, RegistryCache  # type: ignore

__all__ = ["FEES", "NEVER_READ", "STALE", "JobRegistry", "job_from_key"]

# Jobs resolved by id that are kept between two list reads. The chain holds more jobs than any bound,
# so the map evicts its least recently used entry rather than grow.
BY_ID_MAX = 4096
_LOOKUP_FAILED = object()      # a lookup by id that read nothing, distinct from "the chain names nobody"


def job_from_key(key: str, miner_id: str) -> str:
    """`<jobId>__<minerId>` -> `<jobId>`, with verification of the suffix.

    The split happens on the last `__`, not the first: the `minerId` is the tail, and a `jobId`
    containing `__` would break a left-hand split.

    The function does more than split: it checks that the tail is the announced `miner_id`. Without
    that check, a key `otherjob__m1` signed by the client of `otherjob` would authorise a write
    filed under any miner, and the naming convention would become a guard one only believes in.
    It raises `ValueError` rather than returning a value it cannot justify.
    """
    if not isinstance(key, str) or "__" not in key:
        raise ValueError(f"key {key!r:.40}: expected the form `<jobId>__<minerId>`")
    head, tail = key.rsplit("__", 1)
    if not head:
        raise ValueError(f"key {key!r:.40}: empty jobId")
    if tail != miner_id:
        raise ValueError(f"key {key!r:.40}: tail {tail!r} does not match the announced miner_id {miner_id!r}")
    return head


def _pair(j):
    """`(job_id, client)` from a Job object, or None when it cannot be used.

    A present field is not a present value. Measured against a live chain: `creator` is present on
    every job and empty on every job. A job without a non-empty `client` is therefore discarded, not
    stored under an empty string; an empty entry would eventually compare equal to a missing address
    and attribute the job to everyone. Only `client` is read, never `creator`: the latter exists in
    the proto but carries nothing.
    """
    if not isinstance(j, dict):
        return None
    jid = j.get("job_id") or j.get("jobId") or j.get("id")
    cli = j.get("client")
    if isinstance(jid, str) and jid and isinstance(cli, str) and cli:
        return jid, cli
    return None


def _load(raw):
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    d = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(d, dict):
        raise ValueError(f"root is {type(d).__name__}, expected an object")
    return d


class JobRegistry(RegistryCache):
    """`job_id -> client`.

    `reader()` -> the JSON of `/dendra/jobs/v1/job` (the list).
    `job_reader(job_id)` -> the JSON of `/dendra/jobs/v1/job/{job_id}`. Optional but recommended:
        it is the only path pagination cannot truncate.
    """

    def __init__(self, reader, clock, expiry: float = 300.0, job_reader=None,
                 by_id_max: int = BY_ID_MAX):
        super().__init__(reader, clock, expiry)
        if job_reader is not None and not callable(job_reader):
            raise ValueError("job_reader must be callable or None")
        # bool is an int subclass: `True` would pass as a bound of 1.
        if isinstance(by_id_max, bool) or not isinstance(by_id_max, int) or by_id_max <= 0:
            raise ValueError(f"by_id_max must be a positive integer (received {by_id_max!r})")
        self._job_reader = job_reader
        self._next_key = ""       # `pagination.next_key` of the last success; non-empty means truncated
        self.lookups_ok = 0
        self.lookups_failed = 0
        # Jobs resolved by id, kept apart from the list snapshot so that `refresh()`, which replaces the
        # snapshot, does not drop them: a job the list does not hold would otherwise be read by id again
        # after every refresh. `job_id -> (client, resolved_at)`, least recently used first.
        self._by_id: OrderedDict = OrderedDict()
        self._by_id_max = by_id_max
        # Request threads share `_by_id`: an eviction landing between a read and its `move_to_end`
        # would raise KeyError out of `client()`. Never held across a read of the chain.
        self._by_id_lock = threading.Lock()

    def _extract_operators(self, raw) -> dict:
        """`job_id -> client` from the list JSON, and record `pagination.next_key`.

        An instance method where the parent declared a `staticmethod`: the unread continuation must
        be remembered, and the parent calls `self._extract_operators(raw)`, so the substitution is legitimate.
        `self._next_key` is assigned only after every possible raise: a failed extraction must not leave
        behind a belief about pagination.
        """
        d = _load(raw)
        entries = None
        for name in ("job", "Job", "jobs"):
            if isinstance(d.get(name), list):
                entries = d[name]
                break
        if entries is None:
            raise ValueError(f"no job list found (keys seen: {sorted(d)[:6]})")
        out = {}
        for j in entries:
            p = _pair(j)
            if p:
                out[p[0]] = p[1]
        if not out:
            raise ValueError(f"{len(entries)} entry/entries but no usable job_id/client pair")
        # proto3 omits an empty `next_key`, and an absent `pagination` carries none either: both are
        # the zero of the field, i.e. no continuation announced -- a reading, not a guess. A
        # `pagination` that is present but is not an object is neither: nothing was read, so the
        # extraction fails (and the previous content is kept) instead of announcing a complete list.
        pag = d.get("pagination")
        if pag is not None and not isinstance(pag, dict):
            raise ValueError(f"`pagination` is a {type(pag).__name__}, an object was expected")
        self._next_key = (pag or {}).get("next_key") or ""
        return out

    # -- lookup by id: the path no pagination can truncate ----------------------------------------
    def _resolve_by_id(self, job_id: str):
        """Query `GetJob` for one job. Never raises.

        Returns the client when the chain names one, None when the read succeeded and named nobody (the
        job is absent or its `client` is empty), and `_LOOKUP_FAILED` when nothing was read: an absence
        is a fact about the chain, a failed read is not."""
        if self._job_reader is None:
            return _LOOKUP_FAILED
        try:
            p = _pair(_load(self._job_reader(job_id)).get("job"))
        except Exception as e:  # noqa: BLE001
            self.lookups_failed += 1
            self.last_error = f"lookup {job_id}: {type(e).__name__}: {str(e)[:80]}"
            return _LOOKUP_FAILED
        self.lookups_ok += 1
        if p is None or p[0] != job_id:
            # Read but unusable, or naming another job: an absence, not a failure. A binding the chain
            # no longer confirms is dropped rather than kept for its age.
            with self._by_id_lock:
                self._by_id.pop(job_id, None)
            return None
        now = float(self._clock())
        with self._by_id_lock:
            self._by_id[job_id] = (p[1], now)
            self._by_id.move_to_end(job_id)
            while len(self._by_id) > self._by_id_max:
                self._by_id.popitem(last=False)
        return p[1]

    def client(self, job_id: str):
        """`(client, state)`. `client` is None when unknown; the caller must read the state as well.

        Returning `None` alone would conflate "this job does not exist" with "the list could never be
        read", two situations that call for opposite decisions.

        `_operators` is the parent's store — an inherited name whose content here is
        `job_id -> client`. It is deliberately not duplicated under a second name: `refresh()`
        reassigns it, so an alias bound at construction time would point at the stale dictionary from
        the first refresh onwards. Two names for one moving state is a state that diverges.

        A job the list does not hold is answered from `_by_id` while its resolution is younger than
        `expiry`, the same age bound the list snapshot has, and read by id again past it. When that
        re-read fails, the binding it already had is kept, as `refresh()` keeps the list on a failed read.
        """
        v = self._operators.get(job_id)
        if v is None and isinstance(job_id, str) and job_id:
            # Cache miss: ask for the job by its ID rather than concluding "unknown". Without this
            # fallback, a truncated list would cause perfectly valid writes to be refused.
            with self._by_id_lock:
                held = self._by_id.get(job_id)
                fresh = held is not None and float(self._clock()) - held[1] <= self.expiry
                if fresh:
                    self._by_id.move_to_end(job_id)
            if fresh:
                return held[0], self.state()
            got = self._resolve_by_id(job_id)
            if got is _LOOKUP_FAILED:
                v = held[0] if held is not None else None
            else:
                v = got
        return v, self.state()

    def list_truncated(self) -> bool:
        """Did the last list read announce a continuation? Proof, not suspicion.

        A non-empty `QueryAllJobResponse.pagination.next_key` means jobs remain unread, whatever the
        page size. No heuristic is needed, and a heuristic superseded by a real signal is not caution
        but noise operators learn to ignore.
        """
        return bool(self._next_key)

    def summary(self) -> str:
        a = self.age()
        return (f"JOB_REGISTRY state={self.state()} jobs={self.size()} "
                f"age={'-' if a is None else f'{a:.0f}'} ok={self.reads_ok} "
                f"failed={self.reads_failed} truncated={'yes' if self.list_truncated() else 'no'} "
                f"lookups={self.lookups_ok}/{self.lookups_ok + self.lookups_failed} "
                f"by_id={'yes' if self._job_reader else 'no'} held={self.held()}")

    def held(self) -> int:
        """Number of jobs resolved by id and kept."""
        with self._by_id_lock:
            return len(self._by_id)
