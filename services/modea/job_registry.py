"""Job cache — `job_id -> paying client`. Same state machine as the miner registry.

Why this module exists
----------------------
A deposit of kind `req` must be bound to the job's PAYING CLIENT, not to a miner operator. Two
different authorities, two different registries:

    kind = "req"      -> who PAYS for this job    -> `job.client`      (this module)
    other kinds       -> who OPERATES this miner  -> `miner.operator`  (`registry_cache`)

This module INHERITS from `RegistryCache` instead of copying it. The three-state rule
(`NEVER_READ` / `FEES` / `STALE`), the "never fail open" policy and the retention of cached content
on a failed read are already settled and tested once. Copying that shape is signing up for it to
diverge the first time a fix is applied on only one side. Only the EXTRACTION differs, so only the
extraction is rewritten.

Truncation is proven, not guessed
---------------------------------
The chain paginates correctly:

    proto/dendra/jobs/v1/query.proto  : QueryAllJobRequest{ pagination: PageRequest }
                                        QueryAllJobResponse{ job[], pagination: PageResponse }
    x/jobs/keeper/query_job.go        : ListJob -> query.CollectionPaginate(..., req.Pagination, ...)
                                        GetJob  -> reads ONE job by its id
    query.proto                       : REST route /dendra/jobs/v1/job/{job_id}

The truncation risk is therefore ON THE CONSUMER SIDE: a caller that queries once and ignores
`pagination.next_key` only ever sees the first page. That is an obligation of the caller, not a debt
of the chain, and the distinction changes the remedy.

Two consequences, both in the code below:
  - `liste_tronquee()` PROVES truncation from the payload itself (`pagination.next_key` non-empty).
    No heuristic on "suspicious" page sizes: a suspicion based on a round number is noise as soon as
    a real signal exists.
  - `job_reader` (optional) resolves ONE job by its id through `GetJob`. That path cannot be
    truncated, since it lists nothing. When it is wired, a cache miss is no longer a verdict.

An unknown job means the caller REFUSES (the policy lives in `relay_write`). Losing a legitimate
write is noisy and repairable; accepting an illegitimate one is silent and final.
"""
from __future__ import annotations

import json

try:                                   # as part of the `modea` package
    from .registry_cache import FEES, NEVER_READ, STALE, RegistryCache
except ImportError:                    # flat layout, modules side by side
    from registry_cache import FEES, NEVER_READ, STALE, RegistryCache  # type: ignore

__all__ = ["FEES", "NEVER_READ", "STALE", "JobRegistry", "job_from_key"]


def job_from_key(cle: str, miner_id: str) -> str:
    """`<jobId>__<minerId>` -> `<jobId>`, with VERIFICATION of the suffix.

    The split happens on the LAST `__`, not the first: the `minerId` is the tail, and a `jobId`
    containing `__` would break a left-hand split.

    The function does more than split: it CHECKS that the tail is the announced `miner_id`. Without
    that check, a key `otherjob__m1` signed by the client of `otherjob` would authorise a write
    filed under any miner, and the naming convention would become a guard one only believes in.
    It raises `ValueError` rather than returning a value it cannot justify.
    """
    if not isinstance(cle, str) or "__" not in cle:
        raise ValueError(f"key {cle!r:.40}: expected the form `<jobId>__<minerId>`")
    tete, queue = cle.rsplit("__", 1)
    if not tete:
        raise ValueError(f"key {cle!r:.40}: empty jobId")
    if queue != miner_id:
        raise ValueError(f"key {cle!r:.40}: tail {queue!r} does not match the announced miner_id {miner_id!r}")
    return tete


def _pair(j):
    """`(job_id, client)` from a Job object, or None when it cannot be used.

    A PRESENT FIELD IS NOT A PRESENT VALUE. Measured against a live chain: `creator` is present on
    every job and empty on every job. A job without a non-empty `client` is therefore DISCARDED, not
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


def _load(brut):
    if isinstance(brut, (bytes, bytearray)):
        brut = brut.decode("utf-8")
    d = json.loads(brut) if isinstance(brut, str) else brut
    if not isinstance(d, dict):
        raise ValueError(f"root is {type(d).__name__}, expected an object")
    return d


class JobRegistry(RegistryCache):
    """`job_id -> client`.

    `reader()` -> the JSON of `/dendra/jobs/v1/job` (the list).
    `job_reader(job_id)` -> the JSON of `/dendra/jobs/v1/job/{job_id}`. Optional but recommended:
        it is the only path pagination cannot truncate.
    """

    def __init__(self, reader, clock, expiry: float = 300.0, job_reader=None):
        super().__init__(reader, clock, expiry)
        if job_reader is not None and not callable(job_reader):
            raise ValueError("job_reader must be callable or None")
        self._job_reader = job_reader
        self._suite = ""          # `pagination.next_key` of the last success; non-empty means truncated
        self.lookups_ok = 0
        self.lookups_ko = 0

    def _extraire(self, brut) -> dict:
        """`job_id -> client` from the list JSON, and record `pagination.next_key`.

        An INSTANCE method where the parent declared a `staticmethod`: the unread continuation must
        be remembered, and the parent calls `self._extraire(brut)`, so the substitution is legitimate.
        `self._suite` is assigned only AFTER every possible raise: a failed extraction must not leave
        behind a belief about pagination.
        """
        d = _load(brut)
        liste = None
        for cle in ("job", "Job", "jobs"):
            if isinstance(d.get(cle), list):
                liste = d[cle]
                break
        if liste is None:
            raise ValueError(f"no job list found (keys seen: {sorted(d)[:6]})")
        out = {}
        for j in liste:
            p = _pair(j)
            if p:
                out[p[0]] = p[1]
        if not out:
            raise ValueError(f"{len(liste)} entry/entries but no usable job_id/client pair")
        pag = d.get("pagination")
        self._suite = (pag or {}).get("next_key") or "" if isinstance(pag, dict) else ""
        return out

    # ── lookup by id: the path no pagination can truncate ────────────────────────────────────────
    def _resoudre(self, job_id: str) -> bool:
        """Query `GetJob` for ONE job. Never raises; returns True if the cache gained the entry."""
        if self._job_reader is None:
            return False
        try:
            p = _pair(_load(self._job_reader(job_id)).get("job"))
        except Exception as e:  # noqa: BLE001
            self.lookups_ko += 1
            self.derniere_erreur = f"lookup {job_id}: {type(e).__name__}: {str(e)[:80]}"
            return False
        if p is None:
            # The response was READ but is unusable (job absent, `client` empty). That is an absence,
            # not a failure, and it counts as a successful lookup that found nothing.
            self.lookups_ok += 1
            return False
        self._operators[p[0]] = p[1]
        self.lookups_ok += 1
        return True

    def client(self, job_id: str):
        """`(client, state)`. `client` is None when unknown; the caller MUST read the state as well.

        Returning `None` alone would conflate "this job does not exist" with "the list could never be
        read", two situations that call for opposite decisions.

        `_operators` is the PARENT's store — an inherited name whose content here is
        `job_id -> client`. It is deliberately not duplicated under a second name: `refresh()`
        REASSIGNS it, so an alias bound at construction time would point at the stale dictionary from
        the first refresh onwards. Two names for one moving state is a state that diverges.
        """
        v = self._operators.get(job_id)
        if v is None and isinstance(job_id, str) and job_id:
            # Cache miss: ask for the job BY ITS ID rather than concluding "unknown". Without this
            # fallback, a truncated list would cause perfectly valid writes to be refused.
            if self._resoudre(job_id):
                v = self._operators.get(job_id)
        return v, self.state()

    def liste_tronquee(self) -> bool:
        """Did the last list read announce a CONTINUATION? Proof, not suspicion.

        A non-empty `QueryAllJobResponse.pagination.next_key` means jobs remain unread, whatever the
        page size. No heuristic is needed, and a heuristic superseded by a real signal is not caution
        but noise operators learn to ignore.
        """
        return bool(self._suite)

    def resume(self) -> str:
        a = self.age()
        return (f"JOB_REGISTRY state={self.state()} jobs={self.size()} "
                f"age={'-' if a is None else f'{a:.0f}'} ok={self.lectures_ok} "
                f"ko={self.lectures_ko} tronquee={'yes' if self.liste_tronquee() else 'no'} "
                f"lookups={self.lookups_ok}/{self.lookups_ok + self.lookups_ko} "
                f"par_id={'yes' if self._job_reader else 'no'}")
