#!/usr/bin/env python3
"""final_season_server.py — the public service of the Final Testnet Season (ADR-047).

WHAT IT SERVES (behind Caddy, under /final-season/v1/)
    GET  status                 season, day, height, the rules and their fingerprint
    POST payout                 the address an identity's rewards go to (signed)
    GET  evidence/day-NNN.jsonl the evidence log of a day              GET  ranking/day-NNN.json
    GET  grading/sample, POST grading/result   the model-graded sample of the answers to programme
                                requests (grader token), of a day not ranked
And, never proxied by Caddy: POST /internal/work_answer, the miners' answers to the programme's own
requests, forwarded by the generator (internal token); a bounded sample of them enters the evidence.

WHAT IT DOES NOT DO
It sets no test of its own. Every reward is paid on what the CHAIN records — the programme's requests a
miner served and that were verified, its verdicts as a drawn juror, the availability windows it proved —
and on the grades of a sample of its real answers (owner's decisions of 2026-10-05).

WHAT IT HOLDS
    No key that can move funds. Its only secret is the 32-byte draw key (`secret.bin`): it sets, after
    each day, which answers to programme requests enter the graded sample, so nobody can know in advance.

A RANKED DAY IS CLOSED
A published ranking must recompute identically from the published evidence, so nothing is appended to a
day once it is final or ranked: a grade arriving later is refused, and the sample offers no such day.

THE SIGNATURE IS THE RELAY'S, NOT A NEW ONE
Declarations are signed exactly like a relay deposit (`modea.relay_signature` on the miner,
`modea.relay_write.verify_write` here): shape, signature, attribution to the miner's on-chain operator,
replay guard, in that order. The deposit key names the miner (`payout__<id>`), and the key and the header
must name the same miner, as on the relay. Nothing a request carries is acted on before its signature is
checked.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from modea import (cosmos_addr, registry_cache, relay_antireplay, relay_canon,  # noqa: E402
                   relay_carrier, relay_write)
from modea import relay_signature as _rs  # noqa: E402

import final_season_chain as C  # noqa: E402
import final_season_rank as RK  # noqa: E402
from final_season_address import payable_address  # noqa: E402
from final_season_evidence import Evidence  # noqa: E402
from final_season_grader import MAX_ANSWER_CHARS  # noqa: E402
from final_season_rules import RULES, day_bounds, day_of_height, end_epoch, fingerprint  # noqa: E402

# The season's end, read against the header time of the blocks (RULES["end_time"], ADR-047 decision 17).
END = end_epoch()
DATA = os.environ.get("DENDRA_FINAL_SEASON_DATA", "/data/final-season")
NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
RPC = C.rpc_url(NODE)
REST = os.environ.get("DENDRA_FINAL_SEASON_REST", "http://chain:1317").rstrip("/")
HOST = os.environ.get("DENDRA_FINAL_SEASON_HOST", "0.0.0.0")
PORT = int(os.environ.get("DENDRA_FINAL_SEASON_PORT", "8093"))
TRUST_PROXY = os.environ.get("DENDRA_TRUST_PROXY", "") == "1"
START = os.environ.get("DENDRA_FINAL_SEASON_START_HEIGHT", "")
GENERATOR = os.environ.get("DENDRA_FINAL_SEASON_GENERATOR", "")    # the generator's address: whose jobs are work
# The generator forwards the miners' answers on /internal/work_answer, a route Caddy does not proxy. The
# token keeps a container on the same Docker network from writing evidence it did not earn.
INTERNAL_TOKEN = os.environ.get("DENDRA_FINAL_SEASON_INTERNAL_TOKEN", "")
# The grader (a model run where a GPU is) reads a sample of answers and posts its grades with this token.
GRADER_TOKEN = os.environ.get("DENDRA_FINAL_SEASON_GRADER_TOKEN", "")
DECLARE_EVERY_BLOCKS = 720       # one payout declaration per identity per ~hour of blocks
MAX_BODY = 256 * 1024
# Answers to programme requests: at most WORK_SAMPLE per identity per day enter the evidence. They are
# kept in a PRIVATE file while the day runs and drawn only once it is over, in an order set by the
# service's secret. Writing a drawn answer straight into the evidence told the miner, live (the day's
# evidence is public), when its sample was complete, after which nothing it served could be graded;
# a draw made after the day leaves it nothing to adapt to. WORK_PENDING_MAX bounds the private file.
WORK_SAMPLE = 3
WORK_PENDING_MAX = 50
POST_PER_MINUTE = 30             # per client address
MAX_HANDLERS = 64                # concurrent requests beyond this are refused, not queued without end
MINER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
JOB_ID = re.compile(r"[A-Za-z0-9_]{1,64}\Z")     # the client's own jid shape (client._RE_JID), no "__"
# `\Z`, not `$`: `$` also matches before a trailing newline, and "m1" followed by one would be filed as
# another identity.


def _now() -> float:
    return time.time()


class State:
    """Everything the service believes, rebuilt from the evidence log at start-up."""

    def __init__(self, data: str, start_height: int):
        os.makedirs(data, exist_ok=True)
        self.data = data
        self.start = start_height
        self.ev = Evidence(os.path.join(data, "evidence"))
        self.secret = self._secret(os.path.join(data, "secret.bin"))
        self.lock = threading.Lock()
        self.height = 0
        self.eb = None                     # avail_epoch_blocks read from the chain; None = not read yet
        self.declared = {}                 # (kind, miner_id) -> height of the last declaration
        self.work = {}                     # job_id -> (day, miner_id) of a sampled work answer
        self.work_count = {}               # (day, miner_id) -> work answers sampled
        self.work_graded = set()           # job_id already graded
        self.work_sealed = set()           # days whose work sample has been drawn into the evidence
        self.work_pending = {}             # job_id -> (day, miner_id) kept privately until the day is over
        self.finality = None               # blocks past a day's end before it is final; None = not read yet
        self.latest_time = None            # header time of the latest block (UTC s); None = not read yet
        self.end_height = None             # the season's last block; None = the chain has not passed the end
        # Held while a ranking reads its evidence and while a grade is filed: a grade lands either before
        # the read (and is ranked) or after the day is closed (and is refused), never in between.
        self.rank_lock = threading.Lock()
        for rec in self.ev.all():
            self._replay(rec)
        for day in self.pending_days():
            if day not in self.work_sealed:
                for rec in self.read_pending(day):
                    self.work_pending[rec["job_id"]] = (day, rec["miner_id"])

    @staticmethod
    def _secret(path: str) -> bytes:
        if os.path.exists(path):
            with open(path, "rb") as f:
                s = f.read()
            if len(s) != 32:
                raise SystemExit(f"[final-season] FATAL: {path} holds {len(s)} bytes, 32 expected")
            return s
        s = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(s)
        return s

    def _replay(self, rec: dict) -> None:
        t, mid = rec.get("type"), rec.get("miner_id", "")
        if t == "payout":
            self.declared[(t, mid)] = int(rec.get("height", 0) or 0)
        elif t == "work_answer":
            d = int(rec["day"])
            self.work[rec["job_id"]] = (d, mid)
            self.work_count[(d, mid)] = self.work_count.get((d, mid), 0) + 1
        elif t == "work_grade":
            self.work_graded.add(rec["job_id"])
        elif t == "work_sealed":
            self.work_sealed.add(int(rec["day"]))

    def day_now(self) -> int:
        return day_of_height(self.height, self.start)

    def ended(self):
        """True once the latest block is at or after the season's end, False before it, None while the
        chain's time has not been read: three answers, never two — an unread time is not "still running"."""
        if self.latest_time is None:
            return None
        return self.latest_time >= END

    def last_day(self):
        """The season's last day, once its last block is known; None before."""
        return None if self.end_height is None else day_of_height(self.end_height, self.start)

    def day_ranked(self, day: int) -> bool:
        return os.path.exists(os.path.join(self.data, "ranking", f"day-{int(day):03d}.json"))

    def day_closed(self, day: int) -> str:
        """'' while records may still be filed under `day`, else why not. Called under `rank_lock`.
        Final means past the day's last COUNTED block: its full end, or the season's last block on the
        day that holds it — the same window the ranking uses (`final_season_rank.season_window`)."""
        if os.path.exists(os.path.join(self.data, "ranking", f"day-{int(day):03d}.json")):
            return f"day {day} is already ranked"
        if self.end_height is not None and day > self.last_day():
            return f"day {day} is after the season's end"
        last = day_bounds(day, self.start, end_height=self.end_height)[1]
        if self.finality is not None and self.height >= last + self.finality:
            return f"day {day} is final: its ranking is computed from the evidence as it stands"
        return ""

    # ── work answers kept privately until their day is over ─────────────────────────────────────
    def pending_path(self, day: int) -> str:
        return os.path.join(self.data, "work-pending", f"day-{int(day):03d}.jsonl")

    def pending_days(self) -> list:
        root = os.path.join(self.data, "work-pending")
        names = os.listdir(root) if os.path.isdir(root) else []
        return sorted(int(n[4:-6]) for n in names if n.startswith("day-") and n.endswith(".jsonl") and n[4:-6].isdigit())

    def read_pending(self, day: int) -> list:
        p, out = self.pending_path(day), []
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue   # a torn last line (crash mid-write) is one answer less, not a crash
        return out

    def work_order(self, job_id: str) -> bytes:
        return hmac.new(self.secret, f"work|{job_id}".encode(), hashlib.sha256).digest()


ST: State | None = None


def _read_registry() -> bytes:
    """Every page of the miner registry, merged: a reader that stops at the first page would turn every
    miner past it into an unknown identity, refused on every route."""
    merged, key = [], ""
    for _ in range(200):
        url = REST + "/dendra/jobs/v1/miner?pagination.limit=1000"
        if key:
            url += "&pagination.key=" + urllib.parse.quote(key)
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read())
        merged += d.get("miner") or []
        key = (d.get("pagination") or {}).get("next_key") or ""
        if not key:
            return json.dumps({"miner": merged}).encode()
    raise RuntimeError("miner registry longer than 200 pages")


class _Registry:
    """The miner registry read from the chain's REST gateway, cached (same cache as the relay)."""

    def __init__(self):
        self.cache = registry_cache.RegistryCache(_read_registry, time.time, expiry=300)
        self._last = 0.0

    def operator(self, miner_id):
        if self.cache.state() != registry_cache.FEES and time.monotonic() - self._last > 10:
            self._last = time.monotonic()
            self.cache.refresh()
        return self.cache.operator(miner_id)


REGISTRY = _Registry()
ANTIREPLAY = relay_antireplay.Antireplay(time.time, retention=7 * 86400)
_RATE = {}
_RATE_LOCK = threading.Lock()
_HANDLERS = threading.BoundedSemaphore(MAX_HANDLERS)


def _canon(kind, key, body, miner_id, h):
    m = relay_canon.canonical_message(kind, key, body, miner_id, h)
    return m, hashlib.sha256(m).digest()


def _rate_ok(ip: str) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(ip, []) if now - t < 60]
        if len(hits) >= POST_PER_MINUTE:
            _RATE[ip] = hits
            return False
        hits.append(now)
        _RATE[ip] = hits
        if len(_RATE) > 10000:
            for k in [k for k, v in _RATE.items() if not v or now - v[-1] > 60]:
                _RATE.pop(k, None)
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "dendra-final-season"
    timeout = 30                         # a client that sends its request one byte a minute is dropped

    def log_message(self, fmt, *args):  # one line per request is noise at hundreds of miners
        return

    def handle(self):
        if not _HANDLERS.acquire(blocking=False):
            try:
                self.wfile.write(b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return
        try:
            super().handle()
        finally:
            _HANDLERS.release()

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, sort_keys=True).encode() if not isinstance(obj, bytes) else obj
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if not isinstance(obj, bytes) else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _ip(self) -> str:
        if TRUST_PROXY:
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if xff:
                return xff
        return self.client_address[0]

    def _path(self):
        u = urllib.parse.urlsplit(self.path)
        p = u.path
        for pre in ("/final-season/v1/", "/v1/"):
            if p.startswith(pre):
                return p[len(pre):], urllib.parse.parse_qs(u.query)
        return None, {}

    def _signed(self, kind: str, key: str, body: bytes):
        """(miner_id, operator) or (None, reason)."""
        missing = [h for h in _rs.HEADERS if not self.headers.get(h)]
        if missing:
            return None, "unsigned (missing: " + ", ".join(missing) + ")"
        try:
            pub = base64.b64decode(self.headers.get(_rs.HEADER_PUBKEY), validate=True)
            sig = base64.b64decode(self.headers.get(_rs.HEADER_SIG), validate=True)
            miner = self.headers.get(_rs.HEADER_MINER)
            h = int(self.headers.get(_rs.HEADER_HEIGHT))
            verif = relay_carrier.verifier(self.headers.get(_rs.HEADER_ACCT), self.headers.get(_rs.HEADER_SEQ))
        except Exception as e:  # noqa: BLE001
            return None, f"malformed ({type(e).__name__})"
        named = key.rsplit("__", 1)[1] if "__" in key else None
        if named is None or named != miner:
            return None, f"the key names {named!r}, the header names {miner!r}"
        r = relay_write.verify_write(kind, key, body, miner, h, pub, sig, REGISTRY, ANTIREPLAY,
                                     ST.height or None, canon=_canon, verify_signature=verif,
                                     address_from_key=cosmos_addr.address_from_pubkey,
                                     read_height=relay_canon.message_height)
        if not r.accepte or not r.attribue_a():
            return None, f"{r.motif} ({(r.detail or '')[:120]})"
        return miner, r.attribue_a()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n)

    def _bearer(self, token: str) -> bool:
        got = self.headers.get("Authorization", "")
        return bool(token) and hmac.compare_digest(got, "Bearer " + token)

    # ── GET ───────────────────────────────────────────────────────────────────────────────
    def do_GET(self):
        route, q = self._path()
        if route is None:
            return self._send(404, {"error": "route"})
        if route in ("", "status"):
            return self._send(200, self._status())
        if route == "grading/sample":
            return self._grading_sample((q.get("day") or [""])[0])
        if route.startswith("evidence/day-") and route.endswith(".jsonl"):
            return self._file(os.path.join(ST.data, "evidence"), route[len("evidence/"):])
        if route.startswith("ranking/day-") and route.endswith(".json"):
            return self._file(os.path.join(ST.data, "ranking"), route[len("ranking/"):])
        return self._send(404, {"error": "route"})

    def _file(self, directory: str, name: str):
        if "/" in name or ".." in name:
            return self._send(404, {"error": "route"})
        p = os.path.join(directory, name)
        if not os.path.exists(p):
            return self._send(404, {"error": "not published"})
        with open(p, "rb") as f:
            return self._send(200, f.read())

    def _status(self) -> dict:
        h = ST.height
        # `window_blocks` is the CHAIN's availability window (avail_epoch_blocks), on which presence is
        # proven: the launcher refuses a chain where it is 0. None while it has not been read.
        # The end is the rule (`rules.end_time`); `ended` is whether the latest block is past it (None while
        # the chain's time is unread), and `end_height` / `last_day` exist only once the chain has passed
        # it — never estimated from a block interval. `day` is not capped: after the end it keeps counting,
        # and `ended` says the season is over.
        return {"season": RULES["season"], "start_height": ST.start, "height": h,
                "day": ST.day_now() if h else None, "rules": RULES, "rules_fingerprint": fingerprint(),
                "window_blocks": ST.eb, "latest_block_time": ST.latest_time, "ended": ST.ended(),
                "end_height": ST.end_height, "last_day": ST.last_day()}

    def _grading_sample(self, day_s: str):
        """The sampled answers to programme requests of a day not graded yet, with their request. Grader
        token only. A day already final or ranked is not offered: its grades would be refused."""
        if not self._bearer(GRADER_TOKEN):
            return self._send(403, {"error": "grader route"})
        try:
            day = int(day_s)
        except ValueError:
            return self._send(400, {"error": "day is required"})
        with ST.rank_lock:
            closed = ST.day_closed(day)
        if closed:
            return self._send(409, {"error": closed})
        out = [{"kind": "work", "job_id": rec["job_id"], "miner_id": rec["miner_id"],
                "prompt": rec["prompt"], "answer": rec["answer"]}
               for rec in ST.ev.read(day)
               if rec.get("type") == "work_answer" and rec["job_id"] not in ST.work_graded]
        return self._send(200, {"day": day, "sample": out})

    # ── POST ──────────────────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/internal/work_answer":
            return self._work_answer()
        if not _rate_ok(self._ip()):
            return self._send(429, {"error": "too many requests"})
        route, _q = self._path()
        body = self._body()
        if route is None:
            return self._send(404, {"error": "route"})
        if body is None:
            return self._send(400, {"error": "empty or oversized body"})
        try:
            doc = json.loads(body)
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        if not isinstance(doc, dict):
            return self._send(400, {"error": "body is not an object"})
        if route == "grading/result":
            return self._grading_result(doc)
        # The miner id ends up in the evidence and in deposit keys: its shape is checked before anything
        # else reads it.
        if not MINER_ID.match(str(doc.get("miner_id", ""))):
            return self._send(400, {"error": "malformed miner_id"})
        if not ST.height:
            # Nothing is written while the chain height is unknown: a record filed under day 0 would sort
            # before older records and silently lose to them.
            return self._send(503, {"error": "chain height unknown"})
        if route == "payout":
            return self._payout(doc, body)
        return self._send(404, {"error": "route"})

    def _work_answer(self):
        """A miner's answer to one programme request, from the generator. Kept PRIVATELY until the day is
        over (`seal_work_day` draws the sample then); the answer to this call says nothing about the draw."""
        if not INTERNAL_TOKEN or not hmac.compare_digest(self.headers.get("X-Final-Season-Internal", ""), INTERNAL_TOKEN):
            return self._send(403, {"error": "internal route"})
        body = self._body()
        try:
            doc = json.loads(body or b"")
            jid, mid, prompt, answer = doc["job_id"], doc["miner_id"], doc["prompt"], doc["answer"]
        except (KeyError, TypeError, ValueError):
            return self._send(400, {"error": "job_id, miner_id, prompt, answer are required"})
        if not all(isinstance(x, str) for x in (jid, mid, prompt, answer)) or not JOB_ID.match(jid) \
                or "__" in jid or not MINER_ID.match(mid):
            return self._send(400, {"error": "malformed"})
        if not ST.height or ST.day_now() < 0:
            return self._send(425, {"error": "season not started or height unknown"})
        ended = ST.ended()
        if ended is None:
            return self._send(425, {"error": "the chain's time is not read yet"})
        if ended:
            # After the end no request counts (a job counts on the block it settles), and an answer filed
            # now would join the last day's sample and could void work that does count.
            return self._send(410, {"error": "the season is over"})
        with ST.lock:
            # The end is read again UNDER the lock the seal reads under: the last day is sealed once the
            # chain passes the season's last block, and an answer that passed the check above just before
            # the end, then waited here, would otherwise be written into a file already sealed and removed.
            if ST.ended() is not False:
                return self._send(410, {"error": "the season is over"})
            # The day is read UNDER the lock the seal reads under: an answer read as day d before a
            # boundary and written after the seal of d would land in a file nobody seals again.
            day = ST.day_now()
            if jid in ST.work or jid in ST.work_pending:
                return self._send(409, {"error": "this job's answer is already filed"})
            # EVERY answer is recorded as received (its job id): a programme request counts as work only
            # if its answer reached the programme (`final_season_facts.answered_jobs`), since a commit and a
            # settlement can exist without any answer. Only the first WORK_PENDING_MAX of an identity's
            # day keep their text for the sample.
            kept = sum(1 for (d, m) in ST.work_pending.values() if d == day and m == mid) < WORK_PENDING_MAX
            ST.work_pending[jid] = (day, mid)
            line = {"job_id": jid, "miner_id": mid, "height": ST.height}
            if kept:
                line.update(prompt=prompt[:MAX_ANSWER_CHARS], answer=answer[:MAX_ANSWER_CHARS])
            os.makedirs(os.path.dirname(ST.pending_path(day)), mode=0o700, exist_ok=True)
            fd = os.open(ST.pending_path(day), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            # A line torn by a crash must not swallow the next answer as well.
            if os.fstat(fd).st_size and os.pread(fd, 1, os.fstat(fd).st_size - 1) != b"\n":
                os.write(fd, b"\n")
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + chr(10))
        return self._send(200, {"ok": True})

    def _declaration_allowed(self, kind: str, mid: str) -> bool:
        last = ST.declared.get((kind, mid))
        return last is None or ST.height - last >= DECLARE_EVERY_BLOCKS

    def _payout(self, doc, body):
        mid, addr = str(doc.get("miner_id", "")), str(doc.get("address", ""))
        who, why = self._signed("fspay", f"payout__{mid}", body)
        if who is None:
            return self._send(401, {"error": why})
        bad = payable_address(addr)
        if bad:
            return self._send(400, {"error": bad})
        ended = ST.ended()
        if ended is None:
            return self._send(425, {"error": "the chain's time is not read yet"})
        if ended:
            # The addresses the season pays are those declared before its end: a declaration after it is
            # refused, never filed, so a miner is never told a change took effect when it did not.
            return self._send(410, {"error": "the season is over: payout addresses no longer change"})
        if not self._declaration_allowed("payout", mid):
            return self._send(429, {"error": f"one payout declaration per {DECLARE_EVERY_BLOCKS} blocks"})
        ST.declared[("payout", mid)] = ST.height
        # Stored in lowercase: bech32 is case-insensitive, and one account must be one key of the week's
        # totals and one form for the checks that read it back.
        ST.ev.append(max(0, ST.day_now()), {"type": "payout", "miner_id": mid, "address": addr.lower(),
                                            "height": ST.height})
        return self._send(200, {"ok": True})

    def _grading_result(self, doc):
        if not self._bearer(GRADER_TOKEN):
            return self._send(403, {"error": "grader route"})
        kind, mid, coherent = doc.get("kind"), doc.get("miner_id"), doc.get("coherent")
        if not isinstance(mid, str) or not MINER_ID.match(mid) or not isinstance(coherent, bool):
            return self._send(400, {"error": "kind, miner_id and a true/false coherent are required"})
        model = str(doc.get("model", ""))[:80]
        if kind != "work":
            return self._send(400, {"error": "kind is 'work'"})
        jid = doc.get("job_id")
        filed = ST.work.get(jid) if isinstance(jid, str) else None
        if filed is None or filed[1] != mid:
            return self._send(404, {"error": "no sampled answer for this job and identity"})
        key, graded, day = jid, ST.work_graded, filed[0]
        rec = {"type": "work_grade", "job_id": jid, "miner_id": mid, "coherent": coherent, "model": model}
        with ST.rank_lock:
            closed = ST.day_closed(day)
            if closed:
                return self._send(409, {"error": closed})
            with ST.lock:
                if key in graded:
                    return self._send(409, {"error": "already graded"})
                graded.add(key)
            ST.ev.append(day, rec)
        return self._send(200, {"ok": True})


# ── background work ──────────────────────────────────────────────────────────────────────────
def watch_chain() -> None:
    last_params = 0.0
    while True:
        try:
            h, t = C.latest(RPC)
            ST.height, ST.latest_time = h, t
            if t >= END and ST.end_height is None:
                # Once, when the chain first shows a block at or after the end: the season's last block,
                # found by a search over the signed header times that any node repeats identically.
                ST.end_height = C.season_end_height(RPC, END, ST.start, h)
                print(f"[final-season] the season is over: its last block is {ST.end_height} "
                      f"(day {ST.last_day()})", flush=True)
            if ST.eb is None or time.monotonic() - last_params > 600:
                ST.eb = C.param_int(C.jobs_params(REST), "avail_epoch_blocks")
                last_params = time.monotonic()
        except C.ChainUnreadable as e:
            print(f"[final-season] chain unreadable: {e}", flush=True)
        time.sleep(1.0)


def seal_days() -> None:
    """Once a day is over, draw its sample of answers to programme requests into the evidence."""
    while True:
        time.sleep(5)
        if not ST.height:
            continue
        for day in ST.pending_days():
            if day in ST.work_sealed:
                # sealed by a run that stopped before removing its private file
                try:
                    os.remove(ST.pending_path(day))
                except FileNotFoundError:
                    pass
            elif day < ST.day_now() or (ST.end_height is not None and ST.height > ST.end_height):
                # A day is over when the next one starts, or — for the last one — once the chain is past
                # the season's last block: waiting for its full 17 280 blocks would seal it late, or never
                # if the chain stops after the season.
                try:
                    seal_work_day(day)
                except OSError as e:
                    # Said, and tried again: a thread that died here would leave every later day
                    # unsealed, ungraded, and its incoherent work paid, without a word.
                    print(f"[final-season] day {day}: sample NOT sealed ({type(e).__name__}: {e}); retrying",
                          flush=True)


def seal_work_day(day: int) -> None:
    """Once `day` is over, draw at most WORK_SAMPLE answers per identity from its private file, in the
    order set by the secret, into the evidence; record the draw with the job ids whose answer was received
    that day (the only programme requests that count as work, `final_season_facts.answered_jobs`); then delete
    the private file. A day already RANKED takes nothing more: its ranking stands as published. A day only
    final still takes its seal, which the ranking waits for: the seal is the service's own record of what
    it received during the day, not a late input."""
    with ST.lock:
        recs = ST.read_pending(day)
    per, answered = {}, {}
    for r in recs:
        answered.setdefault(r["miner_id"], set()).add(r["job_id"])
        if isinstance(r.get("answer"), str):
            per.setdefault(r["miner_id"], []).append(r)
    drawn = []
    for mid in sorted(per):
        drawn += sorted(per[mid], key=lambda r: ST.work_order(r["job_id"]))[:WORK_SAMPLE]
    with ST.rank_lock:
        if not ST.day_ranked(day):
            for r in drawn:
                if r["job_id"] in ST.work:
                    continue   # filed by a seal that stopped before recording itself: not twice
                ST.ev.append(day, {"type": "work_answer", "job_id": r["job_id"], "miner_id": r["miner_id"],
                                   "height": r.get("height", 0), "prompt": r["prompt"], "answer": r["answer"]})
                ST.work[r["job_id"]] = (day, r["miner_id"])
                ST.work_count[(day, r["miner_id"])] = ST.work_count.get((day, r["miner_id"]), 0) + 1
            ST.ev.append(day, {"type": "work_sealed", "answers": len(recs), "drawn": len(drawn),
                               "answered": {m: sorted(j) for m, j in sorted(answered.items())}})
        ST.work_sealed.add(day)
    with ST.lock:
        for r in recs:
            ST.work_pending.pop(r["job_id"], None)
    try:
        os.remove(ST.pending_path(day))
    except FileNotFoundError:
        pass


def ranking_inputs(day: int, finality: int) -> tuple:
    """(height, records) a ranking of `day` is computed from. From this read on the day takes no more
    records (`day_closed`): what is ranked is what is published, and a reader recomputing it from the
    evidence gets the same bytes."""
    with ST.rank_lock:
        ST.finality = finality
        return ST.height, [r for d in range(day + 1) for r in ST.ev.read(d)]


def rank_days() -> None:
    """Publish each day's ranking once the day is final, in order: a day's budget depends on what the
    days before it paid, so day N is never ranked before day N-1."""
    rdir = os.path.join(ST.data, "ranking")
    os.makedirs(rdir, exist_ok=True)
    said_over = False
    while True:
        time.sleep(600)
        try:
            day = len([n for n in os.listdir(rdir) if n.startswith("day-") and n.endswith(".json")])
            # The season stops at the day that holds its last block, read from the chain once it has
            # passed the end (`ST.end_height`) — never at a day count written down in advance.
            last = ST.last_day()
            if last is not None and day > last:
                if not said_over:
                    print(f"[final-season] season over: every day up to its last ({last}) is ranked", flush=True)
                    said_over = True
                continue
            paid_before = 0
            for d in range(day):
                with open(os.path.join(rdir, f"day-{d:03d}.json"), encoding="utf-8") as f:
                    paid_before += int(json.load(f)["total_paid_udndr"])
            if os.path.exists(ST.pending_path(day)) and day not in ST.work_sealed:
                # The seal records which answers arrived: ranked before it, the day would pay no work.
                print(f"[final-season] ranking deferred: day {day}'s answers are not sealed yet", flush=True)
                continue
            params = C.jobs_params(REST)
            finality = RK.finality_blocks(params)
            height, records = ranking_inputs(day, finality)
            out = RK.rank_day(day, records, NODE, GENERATOR, ST.start, paid_before, finality, height,
                              RK.epoch_blocks(params))
        except (C.ChainUnreadable, ValueError) as e:
            # "not final yet" is the normal answer for most of a day; anything else is said.
            if "not final" not in str(e):
                print(f"[final-season] ranking deferred: {e}", flush=True)
            continue
        except Exception as e:  # noqa: BLE001
            # Said, and tried again: a thread that died here would publish no ranking again, silently.
            print(f"[final-season] ranking failed ({type(e).__name__}: {e}); retrying", flush=True)
            continue
        tmp = os.path.join(rdir, f".day-{day:03d}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, sort_keys=True, indent=1)
            f.write(chr(10))
        os.replace(tmp, os.path.join(rdir, f"day-{day:03d}.json"))
        print(f"[final-season] day {day} ranked: {len(out['ranking'])} identities, "
              f"{out['total_paid_udndr']} udndr", flush=True)


def main() -> int:
    global ST
    if not START.isdigit():
        print("[final-season] FATAL: DENDRA_FINAL_SEASON_START_HEIGHT is not set. The season's first block is a published "
              "fact of the programme; it is never guessed.", flush=True)
        return 2
    ST = State(DATA, int(START))
    workers = [watch_chain, seal_days]
    if GENERATOR:
        workers.append(rank_days)
    else:
        print("[final-season] DENDRA_FINAL_SEASON_GENERATOR is not set: no ranking will be published. The programme's "
              "work reward counts the generator's jobs, so without its address no day can be ranked.",
              flush=True)
    if not GRADER_TOKEN:
        print("[final-season] DENDRA_FINAL_SEASON_GRADER_TOKEN is not set: no answer to a programme request can be graded, "
              "and no day's work can be taken away for incoherent answers.", flush=True)
    for fn in workers:
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[final-season] listening on {HOST}:{PORT} start_height={ST.start} rules={fingerprint()[:12]}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
