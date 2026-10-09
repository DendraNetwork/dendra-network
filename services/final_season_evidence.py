"""Final Testnet Season evidence log: the programme's record of what the chain does not hold.

ONE APPEND-ONLY FILE PER DAY, AND IT IS THE SOURCE OF TRUTH
Every fact the chain does not hold — a payout address chosen, a sampled answer to a programme request and
its grade, the draw of a day's sample — is appended here as one JSON line, then published. The service's
memory is REBUILT from these files at start-up, so what the service believes and what it publishes cannot
differ: there is no second store to drift from the first.

WHAT IS NEVER WRITTEN HERE
An IP address: the programme pays nothing on one.
"""
from __future__ import annotations

import json
import os
import threading

TYPES = ("payout", "work_answer", "work_grade", "work_sealed")


class Evidence:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, day: int) -> str:
        return os.path.join(self.dir, f"day-{int(day):03d}.jsonl")

    def append(self, day: int, record: dict) -> None:
        if record.get("type") not in TYPES:
            raise ValueError(f"unknown evidence type {record.get('type')!r}")
        line = json.dumps(dict(record, day=int(day)), sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            with open(self.path(day), "ab+") as f:
                # A last line torn by a crash must not swallow this record too: it is closed first, and
                # `read` skips it, said.
                f.seek(0, os.SEEK_END)
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write(line.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())

    def days(self) -> list:
        out = []
        for name in os.listdir(self.dir):
            if name.startswith("day-") and name.endswith(".jsonl"):
                try:
                    out.append(int(name[4:-6]))
                except ValueError:
                    continue
        return sorted(out)

    def read(self, day: int) -> list:
        """Records of one day. A torn last line (crash mid-write) is skipped and SAID, never guessed."""
        p = self.path(day)
        if not os.path.exists(p):
            return []
        out, bad = [], 0
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
        if bad:
            out.append({"type": "_unreadable_lines", "count": bad, "day": day})
        return out

    def all(self):
        for d in self.days():
            yield from self.read(d)
