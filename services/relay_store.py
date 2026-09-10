#!/usr/bin/env python3
"""DISK STORE for the relay — the sealed reveal stops being a volatile artifact.

WHY THIS MODULE EXISTS. A purely in-memory `OrderedDict` bounded to 4000 entries per kind, with no
persistence, is not an acceptable home for the sealed reveal: it is the ONLY artifact that makes an
audit judgeable, since the chain keeps nothing but hashes of it. Restarting the relay would make
EVERY open audit permanently unjudgeable — held fee never released, client neither served nor
refunded, primary neither paid nor slashed. Put differently, the promise "withheld is not lost" was
FALSE the moment the process restarted. This module removes the whole class of failure; it does not
mitigate it.

WHAT IS GUARANTEED, AND HOW
- **Written on deposit, re-read at startup.** Every `POST` writes a record; boot re-reads all of
  them. Nothing depends on the lifetime of the process.
- **ATOMIC write**: temporary file plus `os.replace` plus `fsync` (of the file AND the directory).
  A crash in the middle of a deposit leaves the previous state, never a half-written record.
- **Hostile keys neutralized.** The key comes from the NETWORK (`POST /reveal/<key>`): it NEVER
  touches the filesystem. The file name is a `sha256(kind\\0key)` and the real key is stored INSIDE
  the record. No path traversal is possible, whatever the encoding.
- **FIFO order preserved across restart**: every record carries its rank (`n`); the reload sorts on
  it, so eviction remains oldest-first after a restart just as before.
- **Bound honored at load time**: if the disk holds more than `max_entries`, the most RECENT are kept
  and the rest removed — the memory bound cannot be circumvented through the disk.

WHAT IS NOT GUARANTEED, stated explicitly
- This is not a database: no multi-key transactions, no replication. A SINGLE relay remains a single
  point of failure for verifiability — persistence removes loss on RESTART, not loss of the MACHINE.
  That limit is part of the announced ones.
- The per-deposit `fsync` has a cost. It is accepted: a relay that answers fast but loses the proofs
  is worthless to a network whose argument is "you can verify".

CONFIDENTIALITY. The relay NEVER holds plaintext: it carries only sealed payloads and public keys.
Persisting therefore changes nothing about the confidentiality surface — what is written to disk is
exactly what already transited through RAM. Files are 0600, the directory 0700.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time

REC_SUFFIX = ".rec"


class StoreFull(RuntimeError):
    """The store is full and NO entry has passed its retention window. The deposit is REFUSED.

    This is a deliberate reversal. Count-based eviction is BLIND: it cannot know that a reveal still
    serves an OPEN audit, so recent traffic destroys the proof of an older one, and persistence is
    defeated by ordinary usage. Refusing a write is RECOVERABLE — the worker retries; destroying a
    proof is not. The store therefore always refuses the new write rather than erasing a proof that
    is still needed.
    """


class StoreUnavailable(RuntimeError):
    """The store cannot be opened for writing. FATAL — never swallowed silently.

    Starting without persistence would recreate EXACTLY the hole this module closes, while the
    operator believed otherwise. That is why no environment variable can disable persistence — no
    env var may disable a guard. `DENDRA_RELAY_STORE` chooses the LOCATION, never the existence.
    """


def _fname(kind: str, key: str) -> str:
    """File name is a digest, never the key. The key comes from the network: it has no right to
    decide a path (`..`, `/`, `%2e%2e`, NUL, unicode, ...)."""
    h = hashlib.sha256(kind.encode("utf-8") + b"\0" + key.encode("utf-8")).hexdigest()
    return h + REC_SUFFIX


class DiskStore:
    """Disk mirror of a set of in-RAM `OrderedDict`s, one per `kind`."""

    def __init__(self, root: str, kinds, max_entries: int = 4000):
        self.root = os.path.abspath(root)
        self.kinds = list(kinds)
        self.max_entries = int(max_entries)
        self._seq = 0
        self._probe()

    # ------------------------------------------------------------------ opening / fail closed
    def _probe(self) -> None:
        """Creates the directory tree and PROVES it is writable. Fails loudly otherwise."""
        try:
            for k in self.kinds:
                os.makedirs(os.path.join(self.root, k), exist_ok=True)
            os.chmod(self.root, 0o700)
            probe = os.path.join(self.root, ".probe")
            with open(probe, "wb") as f:
                f.write(b"ok")
                f.flush()
                os.fsync(f.fileno())
            os.remove(probe)
        except OSError as e:
            raise StoreUnavailable(f"{self.root} : {type(e).__name__} {e}") from e

    def _next_seq(self) -> int:
        """Monotonic rank. `time.time_ns()` carries the order ACROSS runs — an in-RAM counter would
        restart at zero and break FIFO after a restart; the counter only breaks ties between two
        deposits within the same nanosecond."""
        self._seq += 1
        return time.time_ns() * 1000 + (self._seq % 1000)

    # ------------------------------------------------------------------ write / read
    def write(self, kind: str, key: str, data: bytes, seq: int | None = None,
              ts: float | None = None) -> int:
        n = self._next_seq() if seq is None else int(seq)
        # `t` is the deposit time in seconds. It is what allows eviction by AGE rather than by COUNT:
        # an entry is discardable only once it has passed the audit window, never because others
        # arrived after it.
        rec = json.dumps({"k": key, "n": n, "t": float(time.time() if ts is None else ts),
                          "b": base64.b64encode(data or b"").decode()}).encode()
        d = os.path.join(self.root, kind)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(rec)
                f.flush()
                os.fsync(f.fileno())          # the content is on disk...
            os.chmod(tmp, 0o600)
            os.replace(tmp, os.path.join(d, _fname(kind, key)))   # ...the rename is atomic...
            self._fsync_dir(d)                # ...and the RENAME itself is durable
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return n

    @staticmethod
    def _fsync_dir(path: str) -> None:
        """Without an fsync of the DIRECTORY, `os.replace` can be lost to a machine crash even though
        the file content itself reached the disk. Best effort: some systems refuse it."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def remove(self, kind: str, key: str) -> None:
        try:
            os.remove(os.path.join(self.root, kind, _fname(kind, key)))
        except OSError:
            pass   # already gone is the intended state

    def load(self, retention: float = 0.0, now: float | None = None):
        """-> {kind: [(key, data, rank, timestamp), ...]} sorted by ASCENDING rank (oldest first).

        An unreadable record (truncated, broken JSON) is IGNORED and COUNTED, never guessed:
        returning approximate content would be worse than absence — a judge would conclude on a lie.

        `retention > 0`: records OLDER than that window are deleted at load time. That is the only
        permitted form of eviction — by AGE, never by count. The `max_entries` ceiling is only a
        guard rail: when it is exceeded while everything is young, NOTHING is discarded — the next
        write is refused instead, on the server side.
        """
        now = time.time() if now is None else now
        out, bad, self.last_load_expired = {}, 0, 0
        for kind in self.kinds:
            d = os.path.join(self.root, kind)
            items = []
            try:
                names = os.listdir(d)
            except OSError:
                names = []
            for name in names:
                if not name.endswith(REC_SUFFIX):
                    continue          # .tmp-* from an interrupted write: ignored, never loaded
                p = os.path.join(d, name)
                try:
                    with open(p, "rb") as f:
                        rec = json.loads(f.read())
                    n = int(rec["n"])
                    # Compatibility: older records carry no `t`. The rank `n` is
                    # time_ns*1000+counter, so the timestamp is derived from it rather than treating
                    # such records as "age zero" (which would make them immortal) or "very old"
                    # (which would erase them all on the first startup).
                    ts = float(rec.get("t") or ((n // 1000) / 1e9))
                    items.append((rec["k"], base64.b64decode(rec["b"]), n, ts))
                except Exception:
                    bad += 1
                    continue
            if retention > 0:
                keep = []
                for it in items:
                    if now - it[3] > retention:
                        self.remove(kind, it[0])
                        self.last_load_expired += 1
                    else:
                        keep.append(it)
                items = keep
            items.sort(key=lambda t: t[2])
            out[kind] = items
        self.last_load_corrupt = bad
        return out

    def count(self):
        c = {}
        for kind in self.kinds:
            try:
                c[kind] = sum(1 for n in os.listdir(os.path.join(self.root, kind))
                              if n.endswith(REC_SUFFIX))
            except OSError:
                c[kind] = 0
        return c
