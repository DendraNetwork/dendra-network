#!/usr/bin/env python3
"""final_season_server.py — the public service of the Final Testnet Season.

What it serves (behind Caddy, under /final-season/v1/)
    GET  status                 season, day, height, the rules of the day and their fingerprint
    POST payout                 the address an identity's rewards go to (signed)
    GET  evidence/day-NNN.jsonl the evidence log of a day              GET  ranking/day-NNN.json
    GET  grading/sample, POST grading/result   the model-graded sample of the answers to programme
                                requests (a grader token), of a day not ranked. Each grader token is bound
                                to a grader name, written on every grade it posts; the sample offers each
                                grader the answers it has not graded itself, so two graders grade the same
                                answers and the facts layer combines their grades
                                (final_season_facts.answer_incoherent)
And, never proxied by Caddy: POST /internal/work_answer, the miners' answers to the programme's own
requests, forwarded by the generator (internal token); a bounded sample of them enters the evidence.

What it does not do
It sets no test of its own. Every reward is paid on what the chain records — the programme's requests a
miner served and that were verified, its verdicts as a drawn juror, the availability windows it proved —
and on the grades of a sample of its real answers.

What it holds
    No key that can move funds. Its only secret is the 32-byte draw key (`secret.bin`): it sets, after
    each day, which answers to programme requests enter the graded sample, so nobody can know in advance.

A ranked day is closed
A published ranking must recompute identically from the published evidence, so nothing is appended to a
day once it is final or ranked: a grade arriving later is refused, and the sample offers no such day.

The signature is the relay's, not a new one
Declarations are signed exactly like a relay deposit (`modea.relay_signature` on the miner,
`modea.relay_write.verify_write` here): shape, signature, attribution, replay guard, in that order. The
deposit key names the miner (`payout__<id>`), and the key and the header must name the same miner, as on
the relay. Nothing a request carries is acted on before its signature is checked.

A payout declaration is the owner's, not the operator's
The chain records two addresses per miner (`msg_server_miner.go::CreateMiner`): the creator, which holds
the bond and alone may update or delete the miner, and the operator, which signs its work. A miner that
registered itself has one key in both places, and the declaration is attributed to it as before. In the
miner kit's owner mode (`deploy/join.sh --owner`) they differ: the operator key lives on the mining machine
and the creator's does not. Where the season pays is then the creator's decision -- a copy of the machine's
key must not be able to redirect it -- so only a declaration signed by the creator is accepted. A registry
read that names no creator for a miner attributes the declaration to nobody: it is refused, never handed
to the operator in its place.

A payout address can be locked, once, and then it no longer changes
A miner on a rented machine (docker/cloud-start.sh) keeps its signing key on a disk the host can read, so a
host could sign a new declaration and send the season's rewards to itself. The declaration therefore takes
an optional `"lock": true`, inside the signed body. Once an identity has filed a locked declaration, a
declaration of any other address is refused (409), with or without the flag; the same address is answered
200 and files nothing. Without the flag, and for an identity that never locked, nothing changes. The lock
is an evidence record like the declaration it rides on, so a restart rebuilds it from the log. What it
cannot do, and nothing here claims it does: protect an identity whose key was used by someone else before
the owner's locked declaration -- the first locked declaration wins. And it cannot be lifted: an owner
who locks a wrong address keeps it, which is why the pod checks the address before anything starts.

A lock is accepted only as an identity's first declaration, or from an owner who always declared as one
Accepted at any time, a lock would hand a thief of a self-registered miner's key -- the one key that signs
its work and its declarations -- the power to lock their address for good, where a plain declaration only
lets them race the owner. So a lock is accepted in two cases only: the identity has filed no declaration
before (the pod declares, with the lock, as soon as its miner is registered), or the declaration is
signed by the miner's on-chain creator while another key operates it (owner mode: the machine's key cannot
declare at all there) and every earlier declaration of the identity was filed in owner mode too. A lock
asked for otherwise is refused (409, `"lock_refused": true`) and files nothing.
Why "every earlier one" and not "this one": owner mode read at the time of the lock alone could be made by
the thief. On a self-registered miner the stolen key is the creator, and the creator alone may change the
operator (`msg_server_miner.go::UpdateMiner`): one `update-miner` to another key of theirs, at zero gas, and
the miner reads as owner mode; they sign the lock with the creator key, then set the operator back. So the
mode is written into each payout record at the time it is filed (`"owner": true`, only when it holds), and
a late lock asks the record of the past, which an `update-miner` today cannot rewrite. A record without
the field (filed from a key that also operated the miner, or before the field existed) is not owner
mode: unknown is never owner mode. Owner mode itself is decided on what the registry names (creator,
operator), never on a registry it could not read. The rule reads the records the service has filed, which
the evidence log keeps, so a restart rebuilds it.

Decision 18 takes effect from one day, fixed once, and never on a day already published
The work of an unwound audit is paid from the first day decision 18 applies to (`final_season_rules.rules_for_day`).
That day is fixed the first time this service starts with it: the parameter DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY
when given, else the first day not ranked yet -- and a day already ranked is refused either way, since applying the
rule to it would rewrite a published ranking. It is written to `decision-18.json` in the data directory once, then
only read back at every later start, never rewritten, so a restart days later does not move it (the first day not
ranked then would be a later one, and the days in between would be ranked under one rule and recomputed under
another), and a data directory that can no longer be written does not stop a restart. Every published ranking
is checked against it at start-up: a ranked day whose fingerprint is not the one of the rules in force that day
stops the service, said. The day is published in the status (`unwound_audit_work_from_day`), where
`final_season_rank.py` reads it to recompute a day.

The doubled rates take effect from one day too, fixed once, never on a day already published
`final_season_rules.RULES_DOUBLED` apply from the day the parameter DENDRA_FINAL_SEASON_DOUBLED_FROM_DAY names,
the first time this service starts with it, kept in `doubled-rates.json` and read back at every later start like
decision 18's day. Unlike decision 18, the parameter has no fallback, and it is required until the record
exists: a day number, or `none` for no doubled day. Absent or empty at a start with no record stops the service,
since read as "no day" it would rank the next day at the old rates, and a published day is never ranked again. A
day already ranked, a day before decision 18's, `none` against a record, or anything else stops the service too.
The day is published in the status (`doubled_rates_from_day`, null when there is none), and the published rankings
are checked against both days at start-up.

Decision 23's larger daily volume takes effect from one day as well, under the same contract
`final_season_rules.RULES_MORE_REQUESTS` apply from the day the parameter DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY
names, the first time this service starts with it, kept in `more-requests.json` and read back at every later start,
never rewritten (`State._first_day_of`, shared with the doubled rates). The parameter is required until the record
exists: a day number, or `none`, which is never recorded and is therefore required at every start, as it is for the
doubled rates. A day already ranked, a day before the first doubled day, a day with no doubled day
at all, a value that contradicts the record, or anything else stops the service. The day is published in the status
(`more_requests_from_day`, null when there is none), and every published ranking is checked against the three days
at start-up, before anything is written.

Decision 24's set (the judge presence reward, the kit floor, the probation) takes effect the same way
`final_season_rules.RULES_KIT` apply from the day DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY names, under the contract of
decision 23 (`State._first_day_of`), kept in `kit-rules.json` with the kit floor the set carries: DENDRA_KIT_MIN is
required at the start that fixes the day, is recorded with it, and at every later start the recorded floor wins (an
environment that names another stops the service, an absent one reads the record). One rule more than decision 23:
the day must not have begun at the start that fixes it (`State._kit_rules_from` reads the chain's height), since
the set reads the capacity registry during each day it applies to. With the set fixed for a day,
the service also needs the capacity registry's store (DENDRA_FINAL_SEASON_CAPACITY_DB, read-only) and the relay's
store (DENDRA_FINAL_SEASON_RELAY_STORE, read-only): the first start that fixes the day refuses a registry it cannot
read, since the set pays nobody without signed reports. From then on `watch_capacity` files in
the evidence, as `capacity` records, each identity's signed report whenever its kit number or judge role changes,
with the height it was read at; the ranking reads the latest record at or before a day's end
(`final_season_facts.kit_report`). The day is published in the status (`kit_rules_from_day`, null when there is none).

The jury set takes effect the same way, after decision 24
`final_season_rules.RULES_JURY` apply from the day DENDRA_FINAL_SEASON_JURY_RULES_FROM_DAY names, under the contract of
decision 24 (`State._jury_rules_from`), kept in `jury-rules.json` with the fault periods and the season payer the set
carries: DENDRA_FINAL_SEASON_FAULT_PERIODS and DENDRA_FINAL_SEASON_FAULT_PAYER are required at the start that fixes the
day, recorded with it, and at every later start the record wins. The day must not have begun at that start, and no
period may start before it. One reading differs: with no record, no day named and no fault commitment named, the set
cannot exist in this process, so no day is under it; once a commitment is named, an absent day stops the service as
for the sets before. With the set fixed, DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR names the directory the periods' reveals
are read from when a day of a period is ranked (`final_season_rank.checked_faults`); a day of a period waits for its
reveal, and the days after it wait in order. The day is published in the status (`jury_rules_from_day`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
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
import final_season_rules as R  # noqa: E402
from final_season_address import payable_address  # noqa: E402
from final_season_evidence import Evidence  # noqa: E402
from final_season_facts import CAPACITY, FIRST_GRADER, grader_of  # noqa: E402
from final_season_grader import MAX_ANSWER_CHARS  # noqa: E402
from final_season_rules import (RULES, RULES_17, RULES_DOUBLED, RULES_MORE_REQUESTS, day_bounds,  # noqa: E402
                                day_of_height, end_epoch, fingerprint, rules_for_day)

# The season's end, read against the header time of the blocks (RULES["end_time"]).
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
# The graders read a sample of answers and post their grades, each with its own token. A token is bound to a
# grader name, and every grade records the name of the grader that posted it, never the token: the first
# grader (final_season_facts.FIRST_GRADER, the owner's machine with a GPU, whose grades filed before the names
# existed carry no name) and the second one, on the chain host's CPU (owner's decision of
# 2026-10-15, question 6 of the work plan). An empty token closes its grader's access and nothing else.
GRADER_TOKEN = os.environ.get("DENDRA_FINAL_SEASON_GRADER_TOKEN", "")
GRADER_CPU_TOKEN = os.environ.get("DENDRA_FINAL_SEASON_GRADER_CPU_TOKEN", "")
CPU_GRADER = "cpu"


def grader_tokens() -> dict:
    """{grader name: token}, read at each call so a token set later in the process is seen (the benches set
    them on the module)."""
    return {FIRST_GRADER: GRADER_TOKEN, CPU_GRADER: GRADER_CPU_TOKEN}


def watch_interval_s() -> float:
    """The seconds between two chain-status reads: DENDRA_FINAL_SEASON_WATCH_S, WATCH_DEFAULT_S when it is absent.
    Read at each call. A value that is not a finite number from WATCH_MIN_S to WATCH_MAX_S raises ValueError
    (`main` refuses to start): reading it as the default would hide a typo, a value near zero would spin the
    watcher on the node, and a long poll widens the window in which answers are accepted after the last block."""
    raw = os.environ.get("DENDRA_FINAL_SEASON_WATCH_S")
    if raw is None:
        return WATCH_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not (math.isfinite(value) and WATCH_MIN_S <= value <= WATCH_MAX_S):
        raise ValueError(f"DENDRA_FINAL_SEASON_WATCH_S={raw!r} is not a number of seconds from {WATCH_MIN_S:g} "
                         f"to {WATCH_MAX_S:g}")
    return value


def grader_token_conflict() -> str:
    """Why the grader tokens cannot be served, or "" when they can: two graders with the same non-empty token.
    `Handler._grader` names the first grader whose token matches, so an operator who copied the first token into
    the second variable would file every grade of the second grader under the first name and have them refused
    as already graded. The service refuses to start instead (`main`)."""
    tokens = [t for t in grader_tokens().values() if t]
    if len(tokens) != len(set(tokens)):
        return ("DENDRA_FINAL_SEASON_GRADER_TOKEN and DENDRA_FINAL_SEASON_GRADER_CPU_TOKEN are equal: a token names "
                "one grader, and two graders with one token would file every grade under the first name. Give the "
                "second grader its own token, or leave its variable empty for a single grader.")
    return ""
# The first day decision 18 applies to, when the operator names it; empty: the first day not ranked when this
# service first starts with decision 18. Read once, then kept in DECISION_18_FILE (see the module's header).
UNWOUND_FROM_PARAM = os.environ.get("DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY", "")
DECISION_18_FILE = "decision-18.json"
# The first day of the doubled rates, or NO_DAY for none. Required until an earlier start fixed the day:
# empty is refused then, never read as either (see the module's header). Read once, then kept in DOUBLED_FILE.
DOUBLED_FROM_PARAM = os.environ.get("DENDRA_FINAL_SEASON_DOUBLED_FROM_DAY", "")
NO_DAY = "none"
DOUBLED_FILE = "doubled-rates.json"
# The first day of decision 23's larger daily volume, or NO_DAY for none: the doubled day's contract, kept in
# MORE_REQUESTS_FILE.
MORE_REQUESTS_FROM_PARAM = os.environ.get("DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY", "")
MORE_REQUESTS_FILE = "more-requests.json"
# The first day of decision 24's set (RULES_KIT), or NO_DAY for none: the same contract, kept in KIT_RULES_FILE with
# the kit floor (`final_season_rules.KIT_MIN_ENV`, read by final_season_rules at import).
KIT_RULES_FROM_PARAM = os.environ.get("DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY", "")
KIT_RULES_FILE = "kit-rules.json"
# The first day of the jury set (RULES_JURY), or NO_DAY for none, kept in JURY_RULES_FILE with the fault
# periods and the season payer (`final_season_rules.FAULT_PERIODS_ENV`, `FAULT_PAYER_ENV`, read at import).
JURY_RULES_FROM_PARAM = os.environ.get("DENDRA_FINAL_SEASON_JURY_RULES_FROM_DAY", "")
JURY_RULES_FILE = "jury-rules.json"
# The directory the reveals of the fault periods are read from (`final_season_facts.read_fault_reveal`), required once
# the jury set applies to a day; read-only, and read only when a day of a period is ranked.
FAULT_REVEAL_DIR = os.environ.get("DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR", "")
# The capacity registry's store (capacity_server.py, `DENDRA_CAPACITY_DB`) and the relay's reveal store
# (relay.py, `DENDRA_RELAY_STORE`), both mounted read-only in this service's container (docker-compose.yml).
# Each is required once the set of decision 24 applies to a day, and read by nothing before.
CAPACITY_DB = os.environ.get("DENDRA_FINAL_SEASON_CAPACITY_DB", "")
RELAY_STORE = os.environ.get("DENDRA_FINAL_SEASON_RELAY_STORE", "")
# How often `watch_chain` reads the node's status. A poll shorter than the block interval mostly rereads the same
# height, and that interval emerges from the network. Every consumer of the height tolerates a reading up to
# WATCH_MAX_S old: `seal_days` already polls every 5 s, and `watch_capacity` every CAPACITY_EVERY_S.
# After the season's last block, answers are still accepted until `watch_chain` reads that block, so the poll
# interval is the width of that window: WATCH_MAX_S caps it. WATCH_MIN_S keeps a typo from spinning the node.
WATCH_DEFAULT_S = 5.0
WATCH_MIN_S = 0.5
WATCH_MAX_S = 30.0
WATCH_S = WATCH_DEFAULT_S        # set by `main` from DENDRA_FINAL_SEASON_WATCH_S, after `watch_interval_s` accepted it
CAPACITY_EVERY_S = 300           # how often the registry is read for changed reports (`watch_capacity`)
CAPACITY_SIGNED = "signed::"     # capacity_server.KEY_PROVEN: the namespace of the signed reports
DECLARE_EVERY_BLOCKS = 720       # one payout declaration per identity per ~hour of blocks
MAX_BODY = 256 * 1024
# Answers to programme requests: at most WORK_SAMPLE per identity per day enter the evidence. They are
# kept in a private file while the day runs and drawn only once it is over, in an order set by the
# service's secret. Writing a drawn answer straight into the evidence told the miner, live (the day's
# evidence is public), when its sample was complete, after which nothing it served could be graded;
# a draw made after the day leaves it nothing to adapt to. WORK_PENDING_MAX bounds the private file.
WORK_SAMPLE = 3
WORK_PENDING_MAX = 50
POST_PER_MINUTE = 30             # per client address; the grader's route with its token is exempt (Handler._limit_exempt)
MAX_HANDLERS = 64                # concurrent requests beyond this are refused, not queued without end
MINER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
JOB_ID = re.compile(r"[A-Za-z0-9_]{1,64}\Z")     # the client's own jid shape (client._RE_JID), no "__"
# `\Z`, not `$`: `$` also matches before a trailing newline, and "m1" followed by one would be filed as
# another identity.


def _now() -> float:
    return time.time()


class State:
    """Everything the service believes, rebuilt from the evidence log at start-up."""

    def __init__(self, data: str, start_height: int, unwound_from_param: str | None = None,
                 doubled_from_param: str | None = None, more_requests_from_param: str | None = None,
                 kit_rules_from_param: str | None = None, jury_rules_from_param: str | None = None):
        os.makedirs(data, exist_ok=True)
        self.data = data
        self.start = start_height
        self.ev = Evidence(os.path.join(data, "evidence"))
        self.secret = self._secret(os.path.join(data, "secret.bin"))
        # The first day decision 18 applies to, the first day of the doubled rates, the first day of decision 23,
        # the first day of decision 24 and the first day of the jury set: each fixed once, then read back (see the
        # module's header).
        (self.unwound_from, self.doubled_from, self.more_requests_from, self.kit_rules_from,
         self.jury_rules_from) = self._unwound_from(
            UNWOUND_FROM_PARAM if unwound_from_param is None else unwound_from_param,
            DOUBLED_FROM_PARAM if doubled_from_param is None else doubled_from_param,
            MORE_REQUESTS_FROM_PARAM if more_requests_from_param is None else more_requests_from_param,
            KIT_RULES_FROM_PARAM if kit_rules_from_param is None else kit_rules_from_param,
            JURY_RULES_FROM_PARAM if jury_rules_from_param is None else jury_rules_from_param)
        self.lock = threading.Lock()
        self.capacity = {}                 # miner_id -> (kit_version, judge_role) last filed (`watch_capacity`)
        self.height = 0
        self.eb = None                     # avail_epoch_blocks read from the chain; None = not read yet
        self.declared = {}                 # (kind, miner_id) -> height of the last declaration
        self.locked = {}                   # miner_id -> the payout address its first locked declaration filed
        # miner_id of every identity with at least one payout record not filed in owner mode (no
        # `"owner": true`): such an identity is never granted a late lock (see the module's header).
        self.not_owner_declared = set()
        self.work = {}                     # job_id -> (day, miner_id) of a sampled work answer
        self.work_count = {}               # (day, miner_id) -> work answers sampled
        self.work_graded = {}              # job_id -> names of the graders that graded it
        self.work_grades = {}              # day -> grades filed under it (the alert of `rank_days`, `_status`)
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

    def ranked_count(self) -> int:
        """How many days are ranked: day-000 .. day-(N-1), the next day `rank_days` ranks. A gap stops the
        service, said: the days are ranked in order, so one cannot be missing below a ranked one."""
        rdir = os.path.join(self.data, "ranking")
        names = os.listdir(rdir) if os.path.isdir(rdir) else []
        days = sorted(int(n[4:-5]) for n in names if n.startswith("day-") and n.endswith(".json")
                      and re.fullmatch(r"[0-9]+", n[4:-5]))
        if len(days) != len([n for n in names if n.startswith("day-") and n.endswith(".json")]):
            raise SystemExit(f"[final-season] FATAL: {rdir} holds a ranking file whose name is not day-NNN.json")
        if days != list(range(len(days))):
            raise SystemExit(f"[final-season] FATAL: the ranked days in {rdir} are not 0..N in order ({days[:10]}): "
                             f"a published day is missing below a ranked one")
        return len(days)

    def _unwound_from(self, param: str, doubled_param: str, more_param: str, kit_param: str, jury_param: str) -> tuple:
        """(first day of decision 18, first day of the doubled rates or None, first day of decision 23 or None,
        first day of decision 24 or None, first day of the jury set or None).
        Decision 18's day is written once in DECISION_18_FILE -- the parameter when it is given, else the first day
        not ranked yet -- and read back at every later start; a parameter that names another day than the one
        written, a day already ranked, or anything but a day number stops the service (fatal, said). The doubled
        rates' day, decision 23's and decision 24's follow the same rules (`_first_day_of`), with no fallback.
        Then every ranked day is checked: its fingerprint must be the one of the rules in force that day
        (`final_season_rules.rules_for_day`), or the files do not describe what was published."""
        path = os.path.join(self.data, DECISION_18_FILE)
        if param and not re.fullmatch(r"[0-9]{1,6}", param):
            raise SystemExit(f"[final-season] FATAL: DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY={param!r} is not a day "
                             f"number")
        ranked = self.ranked_count()
        record = None                                    # written only when no record exists yet
        if os.path.exists(path):
            # Read, never rewritten: the record of an earlier start stands as it was written, and a data
            # directory that can no longer be written (a volume mounted read-only) does not stop a restart.
            try:
                with open(path, encoding="utf-8") as f:
                    kept = json.load(f)
                day = kept["from_day"]
                same = (kept["rules_fingerprint"] == fingerprint(RULES)
                        and kept["rules_fingerprint_before"] == fingerprint(RULES_17))
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise SystemExit(f"[final-season] FATAL: {path} is unreadable ({type(e).__name__}: {e}): the day "
                                 f"decision 18 took effect is not known, and is never guessed") from e
            if type(day) is not int or day < 0 or not same:
                raise SystemExit(f"[final-season] FATAL: {path} names day {day!r} under other rules than this "
                                 f"service's ({fingerprint(RULES)[:12]} after {fingerprint(RULES_17)[:12]})")
            if param and int(param) != day:
                raise SystemExit(f"[final-season] FATAL: decision 18 already applies from day {day} ({path}); "
                                 f"DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY={param} names another day. A day "
                                 f"once fixed is not moved: remove the parameter")
        else:
            day = int(param) if param else ranked
            if day < ranked:
                raise SystemExit(f"[final-season] FATAL: day {day} is already ranked (days 0..{ranked - 1} are "
                                 f"published): decision 18 cannot apply to it without rewriting a published "
                                 f"ranking. The first day it can apply to is {ranked}")
            record = {"from_day": day, "set_by": "parameter" if param else "first_unranked_day",
                      "first_unranked_day": ranked, "rule": RULES["unwound_audit_work"],
                      "rules_fingerprint": fingerprint(RULES), "rules_fingerprint_before": fingerprint(RULES_17)}
        doubled, doubled_record = self._doubled_from(doubled_param, ranked, day)
        more, more_record = self._more_requests_from(more_param, ranked, doubled)
        kit, kit_record = self._kit_rules_from(kit_param, ranked, more)
        jury, jury_record = self._jury_rules_from(jury_param, ranked, kit)
        # Checked before anything is written: a day fixed against the published rankings is never recorded.
        for d in range(ranked):
            p = os.path.join(self.data, "ranking", f"day-{d:03d}.json")
            try:
                with open(p, encoding="utf-8") as f:
                    fp = json.load(f).get("rules_fingerprint")
            except (OSError, ValueError, AttributeError) as e:
                raise SystemExit(f"[final-season] FATAL: {p} is unreadable ({type(e).__name__})") from e
            want = fingerprint(R.rules_for_day_with_jury(d, day, doubled, more, kit, jury))
            if fp != want:
                raise SystemExit(f"[final-season] FATAL: day {d} is published under rules {str(fp)[:12]}, but with "
                                 f"decision 18 from day {day}, the doubled rates from day {doubled}, decision 23 "
                                 f"from day {more}, decision 24 from day {kit} and the jury set from day {jury} it is "
                                 f"under {want[:12]}")
        if record is not None:
            self._write_record(path, record)
            print(f"[final-season] decision 18 (the work of an unwound audit, answered and not graded incoherent) "
                  f"applies from day {day} ({record['set_by']})", flush=True)
        if doubled_record is not None:
            self._write_record(os.path.join(self.data, DOUBLED_FILE), doubled_record)
            print(f"[final-season] the doubled rates apply from day {doubled} (rules "
                  f"{doubled_record['rules_fingerprint'][:12]})", flush=True)
        if more_record is not None:
            self._write_record(os.path.join(self.data, MORE_REQUESTS_FILE), more_record)
            print(f"[final-season] the rules of decision 23 ({RULES_MORE_REQUESTS['requests_per_day']} requests a day) "
                  f"apply from day {more} (rules {more_record['rules_fingerprint'][:12]})", flush=True)
        if kit_record is not None:
            self._write_record(os.path.join(self.data, KIT_RULES_FILE), kit_record)
            print(f"[final-season] the rules of decision 24 (judge presence reward, kit floor {kit_record['kit_min']}, "
                  f"probation) apply from day {kit} (rules {kit_record['rules_fingerprint'][:12]})", flush=True)
        if jury_record is not None:
            self._write_record(os.path.join(self.data, JURY_RULES_FILE), jury_record)
            periods = ", ".join(f"{p['from_day']}-{p['to_day']}" for p in jury_record["fault_periods"])
            print(f"[final-season] the jury set (verdicts paid after the reveal, fault periods {periods}) "
                  f"applies from day {jury} (rules {jury_record['rules_fingerprint'][:12]})", flush=True)
        return day, doubled, more, kit, jury

    def _jury_rules_from(self, param: str, ranked: int, kit_rules_from) -> tuple:
        """(first day of the jury set or None, the record to write or None), never before the first day of
        decision 24 and refused when decision 24 applies to no day. The set exists only with its fault periods and its
        season payer (`final_season_rules.jury_rules`): a record kept by an earlier start names them and wins -- the
        environment must name the same ones or nothing; the start that fixes the day takes them from
        `final_season_rules.FAULT_PERIODS` and `FAULT_PAYER`, required then, and refuses a day that has begun and a
        period that starts before it. With no record, no day named and no commitment named, the set cannot exist in
        this process and no day is under it; a commitment named without a day stops the service, as for the sets
        before. Once the set applies to a day, DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR is required at every start."""
        where = os.path.join(self.data, JURY_RULES_FILE)
        if os.path.exists(where):
            try:
                with open(where, encoding="utf-8") as f:
                    kept = json.load(f)
                periods, payer = R.check_fault_periods(kept["fault_periods"]), kept["fault_payer"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise SystemExit(f"[final-season] FATAL: {where} is unreadable ({type(e).__name__}: {e}): the fault "
                                 f"periods of the jury set are not known, and are never guessed") from e
            if (R.FAULT_PERIODS is not None and R.FAULT_PERIODS != periods) or (
                    R.FAULT_PAYER is not None and R.FAULT_PAYER != payer):
                raise SystemExit(f"[final-season] FATAL: the jury set was fixed with other fault periods or another "
                                 f"season payer ({where}) than {R.FAULT_PERIODS_ENV} and {R.FAULT_PAYER_ENV} name. "
                                 f"Values once fixed are not moved: set them to the record's, or remove them")
            if R.RULES_JURY is None:
                try:
                    R.set_fault_commitments(periods, payer)
                except ValueError as e:
                    raise SystemExit(f"[final-season] FATAL: the jury set of {where} cannot be built ({e})") from e
        elif not param and R.FAULT_PERIODS is None and R.FAULT_PAYER is None:
            return None, None
        elif param and param != NO_DAY and re.fullmatch(r"[0-9]{1,6}", param) and R.RULES_JURY is None:
            raise SystemExit(f"[final-season] FATAL: DENDRA_FINAL_SEASON_JURY_RULES_FROM_DAY={param} fixes the jury set, "
                             f"which carries the fault periods, the season payer and the kit floor: "
                             f"{R.FAULT_PERIODS_ENV}, {R.FAULT_PAYER_ENV} and decision 24's floor are required with it "
                             f"and one of them is not set")
        first, new = self._first_day_of(param, ranked, kit_rules_from,
                                        env="DENDRA_FINAL_SEASON_JURY_RULES_FROM_DAY", file=JURY_RULES_FILE,
                                        rules=R.RULES_JURY if R.RULES_JURY is not None else {},
                                        before=R.RULES_KIT if R.RULES_KIT is not None else {},
                                        what="the jury set (verdicts paid after the reveal, contradiction "
                                             "on controlled-fault requests only)",
                                        after_what="decision 24")
        if first is None:
            return None, None
        if new is not None:
            ps = R.RULES_JURY["fault_periods"]
            early = [p for p in ps if p["from_day"] < first]
            if early:
                raise SystemExit(f"[final-season] FATAL: the fault period of days {early[0]['from_day']} to "
                                 f"{early[0]['to_day']} starts before day {first}, the jury set's first day: its first "
                                 f"days would be ranked under decision 24, which pays the period's identities")
            # Every day of the set up to the last period's end is in a period: a day before the first one, or between
            # two, would pay the identities of the next period, secret until its reveal, and contradict nobody.
            if ps[0]["from_day"] != first:
                raise SystemExit(f"[final-season] FATAL: the first fault period starts on day {ps[0]['from_day']}, after "
                                 f"day {first}, the jury set's first day: days {first} to {ps[0]['from_day'] - 1} would be "
                                 f"under the set with no period. A new commitment names the days")
            gaps = [(p, q) for p, q in zip(ps, ps[1:]) if q["from_day"] != p["to_day"] + 1]
            if gaps:
                raise SystemExit(f"[final-season] FATAL: the fault periods of days {gaps[0][0]['from_day']} to "
                                 f"{gaps[0][0]['to_day']} and {gaps[0][1]['from_day']} to {gaps[0][1]['to_day']} leave "
                                 f"days between them under the set with no period. A new commitment names the days")
            # The set's first day comes after this start, as for decision 24: its controlled-fault identities read the
            # day from the status, and each commitment must sit below the first block of its period.
            try:
                now_height = C.height(RPC)
            except C.ChainUnreadable as e:
                raise SystemExit(f"[final-season] FATAL: the start that fixes the first day of the jury set reads the "
                                 f"chain's height to check that day {first} has not begun, and cannot ({e})") from e
            now_day = day_of_height(now_height, self.start)
            if now_day >= first:
                raise SystemExit(f"[final-season] FATAL: DENDRA_FINAL_SEASON_JURY_RULES_FROM_DAY={first} names a day "
                                 f"that has begun (the chain is at block {now_height}, day {now_day}). Name day "
                                 f"{now_day + 1} or later")
            new.update(kit_min=R.KIT_MIN, fault_payer=R.FAULT_PAYER, fixed_at_height=now_height,
                       fault_periods=[dict(p) for p in R.RULES_JURY["fault_periods"]])
        if not FAULT_REVEAL_DIR:
            raise SystemExit("[final-season] FATAL: the jury set reads the reveals of its fault periods from the "
                             "directory DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR names, and it is not set")
        return first, new

    def _kit_rules_from(self, param: str, ranked: int, more_requests_from) -> tuple:
        """(first day of decision 24 or None, the record to write or None), never before the first day of decision
        23 and refused when decision 23 applies to no day. The set exists only with its kit floor
        (`final_season_rules.kit_rules`): a record kept by an earlier start names the floor and wins -- the
        environment must name the same one or nothing; the start that fixes the day takes the floor from
        `final_season_rules.KIT_MIN` (DENDRA_KIT_MIN), required then. That same start refuses a capacity registry it
        cannot read and an unnamed relay store, since the set pays nobody without signed reports and no judge
        without the relay's records; a later start only says so, and the watcher retries."""
        where = os.path.join(self.data, KIT_RULES_FILE)
        if os.path.exists(where):
            try:
                with open(where, encoding="utf-8") as f:
                    kept = json.load(f)["kit_min"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise SystemExit(f"[final-season] FATAL: {where} is unreadable ({type(e).__name__}: {e}): the kit floor "
                                 f"of decision 24 is not known, and is never guessed") from e
            if type(kept) is not int or kept < 1:
                raise SystemExit(f"[final-season] FATAL: {where} names kit floor {kept!r}, which is not a kit number")
            if R.KIT_MIN is not None and R.KIT_MIN != kept:
                raise SystemExit(f"[final-season] FATAL: the rules of decision 24 were fixed with kit floor {kept} "
                                 f"({where}); {R.KIT_MIN_ENV}={R.KIT_MIN} names another one. A floor once fixed is not "
                                 f"moved: set it to {kept}, or remove it")
            if R.KIT_MIN is None:
                R.set_kit_min(kept)
        elif param and param != NO_DAY and re.fullmatch(r"[0-9]{1,6}", param) and R.KIT_MIN is None:
            raise SystemExit(f"[final-season] FATAL: DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY={param} fixes the rules of "
                             f"decision 24, which carry the kit floor: {R.KIT_MIN_ENV} is required with them (the kit "
                             f"number of the release the announcement names) and is not set")
        first, new = self._first_day_of(param, ranked, more_requests_from,
                                        env="DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY", file=KIT_RULES_FILE,
                                        rules=R.RULES_KIT if R.RULES_KIT is not None else {}, before=RULES_MORE_REQUESTS,
                                        what="the rules of decision 24 (judge presence reward, kit floor, probation)",
                                        after_what="decision 23")
        if first is None:
            return None, None
        if new is not None:
            new["kit_min"] = R.KIT_MIN
            # The set reads a registry as the days go by (`watch_capacity`), unlike decision 23: its first day must
            # come after this start, so that the registry is watched from before the day's first block. A day that
            # has begun, or ended unranked, would be ranked on the records filed from now on -- none at or before
            # its end for a day already over, and the ranking refuses such a day rather than publish it at zero
            # (`final_season_rank.rank_day`). The chain is read here for the current day: unreadable, the day is not
            # fixed, since "not read" is not "not begun".
            try:
                now_height = C.height(RPC)
            except C.ChainUnreadable as e:
                raise SystemExit(f"[final-season] FATAL: the start that fixes the first day of decision 24 reads the "
                                 f"chain's height to check that day {first} has not begun, and cannot ({e})") from e
            now_day = day_of_height(now_height, self.start)
            if first <= now_day:
                raise SystemExit(f"[final-season] FATAL: DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY={first} names a day that "
                                 f"has begun (the chain is at block {now_height}, day {now_day}): the rules of "
                                 f"decision 24 read the capacity registry during each day they apply to, so their first "
                                 f"day must come after this start. Name day {now_day + 1} or later")
            new["fixed_at_height"] = now_height
        if not CAPACITY_DB or not RELAY_STORE:
            raise SystemExit("[final-season] FATAL: the rules of decision 24 read the capacity registry's store and the "
                             "relay's store: DENDRA_FINAL_SEASON_CAPACITY_DB and DENDRA_FINAL_SEASON_RELAY_STORE name "
                             "them (docker-compose.yml mounts both read-only) and one of them is not set")
        try:
            read_capacity_registry(CAPACITY_DB)
        except (OSError, ValueError) as e:
            if new is not None:
                raise SystemExit(f"[final-season] FATAL: the capacity registry at {CAPACITY_DB} cannot be read "
                                 f"({type(e).__name__}: {e}): the rules of decision 24 pay nobody without its signed "
                                 f"reports, so their first day is not fixed before it reads") from e
            print(f"[final-season] WARNING: the capacity registry at {CAPACITY_DB} cannot be read "
                  f"({type(e).__name__}): no new report is filed until it does", flush=True)
        return first, new

    @staticmethod
    def _write_record(path: str, record: dict) -> None:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            raise SystemExit(f"[final-season] FATAL: {path} cannot be written ({e}): a day not kept could move "
                             f"at the next start") from e

    def _doubled_from(self, param: str, ranked: int, unwound_from: int) -> tuple:
        """(first day of the doubled rates or None, the record to write or None), never before decision 18's day."""
        return self._first_day_of(param, ranked, unwound_from, env="DENDRA_FINAL_SEASON_DOUBLED_FROM_DAY",
                                  file=DOUBLED_FILE, rules=RULES_DOUBLED, before=RULES, what="the doubled rates",
                                  after_what="decision 18")

    def _more_requests_from(self, param: str, ranked: int, doubled_from) -> tuple:
        """(first day of decision 23 or None, the record to write or None), never before the first doubled day,
        and refused when no day is doubled: the sets take effect in the order of RULE_HISTORY."""
        return self._first_day_of(param, ranked, doubled_from, env="DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY",
                                  file=MORE_REQUESTS_FILE, rules=RULES_MORE_REQUESTS, before=RULES_DOUBLED,
                                  what="the rules of decision 23 (more requests a day)", after_what="the doubled rates")

    def _first_day_of(self, param: str, ranked: int, after, *, env: str, file: str, rules: dict, before: dict,
                      what: str, after_what: str) -> tuple:
        """(first day of the set `rules` or None, the record to write or None). Read from `file` when an earlier
        start wrote it, never rewritten; else the parameter `env`, which is then required: a day number, or NO_DAY
        for none. Absent or empty with no record stops the service: it is never read as "no day". The day is never
        before `after`, the first day of the set published before (`before`), and a set whose predecessor applies
        to no day (`after` None) applies to no day either. Nothing is written here: the caller writes the record
        once every published day has been checked against it."""
        where = os.path.join(self.data, file)
        if param and param != NO_DAY and not re.fullmatch(r"[0-9]{1,6}", param):
            raise SystemExit(f"[final-season] FATAL: {env}={param!r} is neither a day number nor {NO_DAY!r}")
        if os.path.exists(where):
            try:
                with open(where, encoding="utf-8") as f:
                    doc = json.load(f)
                first = doc["from_day"]
                ours = (doc["rules_fingerprint"] == fingerprint(rules)
                        and doc["rules_fingerprint_before"] == fingerprint(before))
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise SystemExit(f"[final-season] FATAL: {where} is unreadable ({type(e).__name__}: {e}): the day "
                                 f"{what} took effect is not known, and is never guessed") from e
            if type(first) is not int or first < 0 or not ours:
                raise SystemExit(f"[final-season] FATAL: {where} names day {first!r} under other rules than this "
                                 f"service's ({fingerprint(rules)[:12]} after {fingerprint(before)[:12]})")
            if param == NO_DAY or (param and int(param) != first):
                raise SystemExit(f"[final-season] FATAL: {what} already apply from day {first} ({where}); "
                                 f"{env}={param} names another day. A day once fixed is not moved: set the "
                                 f"parameter to {first}")
            new = None
        elif not param:
            raise SystemExit(f"[final-season] FATAL: {env} is not set and no earlier start fixed the first day of "
                             f"{what} ({where} does not exist): name the day, or {NO_DAY!r} for none. An absent "
                             f"value is never read as either")
        elif param == NO_DAY:
            return None, None
        else:
            first = int(param)
            if first < ranked:
                raise SystemExit(f"[final-season] FATAL: day {first} is already ranked (days 0..{ranked - 1} are "
                                 f"published): {what} cannot apply to it without rewriting a published ranking. "
                                 f"The first day they can apply to is {ranked}")
            new = {"from_day": first, "set_by": "parameter", "first_unranked_day": ranked,
                   "rules_fingerprint": fingerprint(rules), "rules_fingerprint_before": fingerprint(before)}
        if after is None or first < after:
            since = "fixed for no day" if after is None else f"from day {after}"
            raise SystemExit(f"[final-season] FATAL: {what} from day {first} would take effect before {after_what} "
                             f"({since}): the sets take effect in their published order")
        return first, new

    def _replay(self, rec: dict) -> None:
        t, mid = rec.get("type"), rec.get("miner_id", "")
        if t == "payout":
            self.declared[(t, mid)] = int(rec.get("height", 0) or 0)
            # Only the service's own `true` is owner mode: an absent field, or anything else, is not.
            if rec.get("owner") is not True:
                self.not_owner_declared.add(mid)
            # The first locked record holds: once it is filed the service files no other address for this
            # identity, so a log written by this service never carries a second lock that disagrees.
            if rec.get("lock") is True:
                self.locked.setdefault(mid, str(rec.get("address", "")).lower())
        elif t == "work_answer":
            d = int(rec["day"])
            self.work[rec["job_id"]] = (d, mid)
            self.work_count[(d, mid)] = self.work_count.get((d, mid), 0) + 1
        elif t == "work_grade":
            self.work_graded.setdefault(rec["job_id"], set()).add(grader_of(rec))
            d = int(rec["day"])
            self.work_grades[d] = self.work_grades.get(d, 0) + 1
        elif t == "work_sealed":
            self.work_sealed.add(int(rec["day"]))
        elif t == CAPACITY:
            self.capacity[mid] = (rec.get("kit_version"), rec.get("judge_role"))

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

    def ranked_days(self) -> list:
        root = os.path.join(self.data, "ranking")
        names = os.listdir(root) if os.path.isdir(root) else []
        return sorted(int(n[4:-5]) for n in names if n.startswith("day-") and n.endswith(".json") and n[4:-5].isdigit())

    def ungraded_ranked_days(self) -> list:
        """The ranked days whose evidence holds sampled work answers and not one grade of them. Such a day is
        final: its work of an unwound audit was not paid (decision 18 pays a graded answer only,
        final_season_facts.unwound_work) and no incoherent answer took a day's work away. Read from the
        evidence replayed at start-up and filed since, never from a count written down."""
        answers = {}
        for (d, _mid), n in self.work_count.items():
            answers[d] = answers.get(d, 0) + n
        return [d for d in self.ranked_days() if answers.get(d, 0) > 0 and self.work_grades.get(d, 0) == 0]

    def day_closed(self, day: int) -> str:
        """'' while records may still be filed under `day`, else why not. Called under `rank_lock`.
        Final means past the day's last counted block: its full end, or the season's last block on the
        day that holds it — the same window the ranking uses (`final_season_rank.season_window`)."""
        if os.path.exists(os.path.join(self.data, "ranking", f"day-{int(day):03d}.json")):
            return f"day {day} is already ranked"
        if self.end_height is not None and day > self.last_day():
            return f"day {day} is after the season's end"
        last = day_bounds(day, self.start, end_height=self.end_height)[1]
        if self.finality is not None and self.height >= last + self.finality:
            return f"day {day} is final: its ranking is computed from the evidence as it stands"
        return ""

    # -- work answers kept privately until their day is over -------------------------------------
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

    def _fresh(self):
        if self.cache.state() != registry_cache.FEES and time.monotonic() - self._last > 10:
            self._last = time.monotonic()
            self.cache.refresh()

    def operator(self, miner_id):
        self._fresh()
        return self.cache.operator(miner_id)

    def creator(self, miner_id):
        self._fresh()
        return self.cache.creator(miner_id)


class _Owners:
    """The registry as a payout declaration reads it: `operator(miner_id)` -- the interface
    `relay_write.verify_write` asks -- answers the miner's owner, its creator. For a miner registered by
    its own key the creator is the operator, so nothing changes for it. An unknown creator answers None:
    refused, never replaced by the operator (see the module's header)."""

    def __init__(self, registry):
        self.registry = registry

    def operator(self, miner_id):
        op, state = self.registry.operator(miner_id)
        if op is None:
            return None, state
        creator, _ = self.registry.creator(miner_id)
        return (creator or None), state


# Who may sign each kind of declaration. A table rather than a test scattered in the handlers, as in
# `relay_write.ROLES_BY_KIND`: a kind absent from it is attributed to the miner's operator.
OWNER_KINDS = frozenset({"fspay"})


REGISTRY = _Registry()
ANTIREPLAY = relay_antireplay.Antireplay(time.time, retention=7 * 86400)
_RATE = {}
_RATE_LOCK = threading.Lock()
_HANDLERS = threading.BoundedSemaphore(MAX_HANDLERS)


def _owner_refusal(miner_id: str, r) -> str:
    """The reason a declaration that only the owner may sign was refused, in the owner's terms, or '' to
    keep the verifier's own. Read after the signature was verified: it names addresses the chain
    publishes anyway, never anything the request carried."""
    op, _ = REGISTRY.operator(miner_id)
    creator, _ = REGISTRY.creator(miner_id)
    if r.reason == relay_write.FOREIGN_KEY and creator and op and creator != op:
        return (f"{miner_id} is owned by {creator} and operated by {op}: where its rewards go is declared "
                f"with the owner's key only (final_season_miner.py payout-prepare, signed on the owner's "
                f"machine, then payout-submit)")
    if r.reason == relay_write.UNKNOWN_MINER and op and not creator:
        return (f"the registry read names no owner (creator) for {miner_id}: the declaration cannot be "
                f"attributed, so it is refused")
    return ""


def _owner_signed(miner_id: str, who) -> bool:
    """True only when the registry names a creator and an operator for `miner_id`, they differ, and `who` --
    the address the declaration was attributed to, after its signature was verified -- is the creator: a
    declaration made by the owner of a miner another key operates. Anything not read, or not named, is
    False (security criterion: no default, and unknown is never owner mode)."""
    op, _ = REGISTRY.operator(miner_id)
    creator, _ = REGISTRY.creator(miner_id)
    if not (isinstance(op, str) and op and isinstance(creator, str) and creator and isinstance(who, str) and who):
        return False
    return creator.lower() != op.lower() and who.lower() == creator.lower()


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
        owner_kind = kind in OWNER_KINDS
        r = relay_write.verify_write(kind, key, body, miner, h, pub, sig,
                                     _Owners(REGISTRY) if owner_kind else REGISTRY, ANTIREPLAY,
                                     ST.height or None, canon=_canon, verify_signature=verif,
                                     address_from_key=cosmos_addr.address_from_pubkey,
                                     read_height=relay_canon.message_height)
        if not r.accepted or not r.attributed_to():
            return None, (_owner_refusal(miner, r) if owner_kind else "") or f"{r.reason} ({(r.detail or '')[:120]})"
        return miner, r.attributed_to()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n)

    def _bearer(self, token: str) -> bool:
        got = self.headers.get("Authorization", "")
        return bool(token) and hmac.compare_digest(got, "Bearer " + token)

    def _grader(self):
        """The name of the grader whose token the request carries, or None: no token, an empty token, or
        a token of nobody. The name is what the grade records; the token never is."""
        got = self.headers.get("Authorization", "")
        for name, token in grader_tokens().items():
            if token and hmac.compare_digest(got, "Bearer " + token):
                return name
        return None

    def _limit_exempt(self, route) -> bool:
        """True only for the graders' route with a grader's token. A grading run posts one grade per
        sampled answer, more than POST_PER_MINUTE from a single address, and the route is already closed
        to anyone without a token. The token is checked here, before the limit: a request without it is
        counted like any other. Every other route, known or not, stays under the limit."""
        return route == "grading/result" and self._grader() is not None

    # -- GET -------------------------------------------------------------------------------
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
        # `window_blocks` is the chain's availability window (avail_epoch_blocks), on which presence is
        # proven: the launcher refuses a chain where it is 0. None while it has not been read.
        # The end is the rule (`rules.end_time`); `ended` is whether the latest block is past it (None while
        # the chain's time is unread), and `end_height` / `last_day` exist only once the chain has passed
        # it — never estimated from a block interval. `day` is not capped: after the end it keeps counting,
        # and `ended` says the season is over.
        # `rules` are the rules of the current day (of day 0 before the season starts), null while the day is
        # not known. `unwound_audit_work_from_day` is the first day decision 18 applies to: a day before it was
        # ranked under `final_season_rules.RULES_17`. `doubled_rates_from_day` is the first day of
        # `RULES_DOUBLED`, and `more_requests_from_day` the first day of `RULES_MORE_REQUESTS` (decision 23), each
        # null when there is none. Each ranking carries the fingerprint of its own set, and the generator takes the
        # day's number of requests from the set `rules_fingerprint` names (`final_season_generator.day_quota`).
        # `ungraded_ranked_days`: the ranked days with sampled answers and no grade (`State.ungraded_ranked_days`),
        # for the graders' cron to relay in its log: a day in it is final and was ranked with no grading at all.
        # `kit_rules_from_day` is the first day of `RULES_KIT` (decision 24), null when there is none; the floor the
        # set carries is in `rules` (`kit_min`) on a day under it. `jury_rules_from_day` is the first day of
        # `RULES_JURY`, null when there is none; the fault periods and the season payer are in `rules`.
        # `generator` is the address whose jobs are the programme's work (DENDRA_FINAL_SEASON_GENERATOR), for a
        # reader that partitions the chain's jobs by who asked for them (the_proof.py::origin_partition reads this
        # key); null while it is not set, never "", so that an unset address cannot match a job with no client.
        day = ST.day_now() if h else None
        rules = None if day is None else R.rules_for_day_with_jury(max(day, 0), ST.unwound_from, ST.doubled_from,
                                                                   ST.more_requests_from, ST.kit_rules_from,
                                                                   ST.jury_rules_from)
        return {"season": RULES["season"], "start_height": ST.start, "height": h,
                "day": day, "rules": rules, "rules_fingerprint": None if rules is None else fingerprint(rules),
                "window_blocks": ST.eb, "latest_block_time": ST.latest_time, "ended": ST.ended(),
                "end_height": ST.end_height, "last_day": ST.last_day(),
                "doubled_rates_from_day": ST.doubled_from,
                "more_requests_from_day": ST.more_requests_from,
                "kit_rules_from_day": ST.kit_rules_from,
                "jury_rules_from_day": ST.jury_rules_from,
                "unwound_audit_work_from_day": ST.unwound_from,
                "ungraded_ranked_days": ST.ungraded_ranked_days(),
                "generator": GENERATOR or None}

    def _grading_sample(self, day_s: str):
        """The sampled answers to programme requests of a day this grader has not graded yet, with their
        request. A grader token only; each grader is offered what it has not graded itself, whatever the
        other graded. A day already final or ranked is not offered: its grades would be refused."""
        grader = self._grader()
        if grader is None:
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
               if rec.get("type") == "work_answer" and grader not in ST.work_graded.get(rec["job_id"], ())]
        return self._send(200, {"day": day, "grader": grader, "sample": out})

    # -- POST ------------------------------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/internal/work_answer":
            return self._work_answer()
        route, _q = self._path()
        if not self._limit_exempt(route) and not _rate_ok(self._ip()):
            return self._send(429, {"error": "too many requests"})
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
        """A miner's answer to one programme request, from the generator. Kept privately until the day is
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
            # The end is read again under the lock the seal reads under: the last day is sealed once the
            # chain passes the season's last block, and an answer that passed the check above just before
            # the end, then waited here, would otherwise be written into a file already sealed and removed.
            if ST.ended() is not False:
                return self._send(410, {"error": "the season is over"})
            # The day is read under the lock the seal reads under: an answer read as day d before a
            # boundary and written after the seal of d would land in a file nobody seals again.
            day = ST.day_now()
            if jid in ST.work or jid in ST.work_pending:
                # In a field the generator reads (`final_season_generator.report_work`): a forward retried after
                # its answer was lost in transit finds its own first copy here, and for it that is a success --
                # the answer is recorded. Read as a failure, the request was replaced and the day paid one more.
                return self._send(409, {"error": "this job's answer is already filed", "already_filed": True})
            # Every answer is recorded as received (its job id): a programme request counts as work only
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
        # `_signed` answers (miner_id, the address the declaration is attributed to) once the signature holds.
        signer = why
        bad = payable_address(addr)
        if bad:
            return self._send(400, {"error": bad})
        # The lock is read from the signed body, and only a real boolean counts: a string "true" or a 1 is a
        # malformed request, never a lock and never a silent "no lock".
        lock = doc.get("lock", False)
        if not isinstance(lock, bool):
            return self._send(400, {"error": "lock must be true or false"})
        ended = ST.ended()
        if ended is None:
            return self._send(425, {"error": "the chain's time is not read yet"})
        if ended:
            # The addresses the season pays are those declared before its end: a declaration after it is
            # refused, never filed, so a miner is never told a change took effect when it did not.
            return self._send(410, {"error": "the season is over: payout addresses no longer change"})
        # Whether this declaration is made in owner mode, read once, before ST.lock (the registry may be
        # refreshed over the network): it is filed with the record, which is what a later lock reads.
        owner = _owner_signed(mid, signer)
        # Read and filed under ST.lock: two locked declarations of different addresses arriving together
        # must not both see "not locked yet".
        with ST.lock:
            held = ST.locked.get(mid)
            if held is not None:
                if held != addr.lower():
                    return self._send(409, {"error": f"the payout address of {mid} is locked: another address "
                                                     f"is refused, and the lock cannot be lifted", "locked": True})
                # The address it already holds: nothing to file, and the hourly allowance is not spent.
                return self._send(200, {"ok": True, "locked": True, "unchanged": True})
            if lock and ("payout", mid) in ST.declared and (mid in ST.not_owner_declared or not owner):
                # A lock after a declaration without it: from a self-registered miner's key, which a thief may
                # hold as well as its owner, it would hold the thief's address for good -- and owner mode read
                # now can be made by that thief with one `update-miner`. So a late lock needs owner mode now
                # and in every record filed before. Refused for good, whatever the address, and nothing is
                # filed (see the module's header).
                return self._send(409, {"error": f"the payout address of {mid} cannot be locked: a lock is accepted "
                                                 f"only as an identity's first declaration, or from its owner's key "
                                                 f"(owner mode) when every earlier declaration was made in owner mode "
                                                 f"too, and {mid} has declared before without that. Nothing was "
                                                 f"filed.", "lock_refused": True})
            if not self._declaration_allowed("payout", mid):
                return self._send(429, {"error": f"one payout declaration per {DECLARE_EVERY_BLOCKS} blocks"})
            ST.declared[("payout", mid)] = ST.height
            # Stored in lowercase: bech32 is case-insensitive, and one account must be one key of the week's
            # totals and one form for the checks that read it back.
            rec = {"type": "payout", "miner_id": mid, "address": addr.lower(), "height": ST.height}
            if lock:
                rec["lock"] = True
            # Written only when it holds: a record without it keeps its earlier shape, and reads as not owner
            # mode, the side that refuses a late lock.
            if owner:
                rec["owner"] = True
            ST.ev.append(max(0, ST.day_now()), rec)
            if not owner:
                ST.not_owner_declared.add(mid)
            if lock:
                ST.locked[mid] = addr.lower()
        return self._send(200, {"ok": True, "locked": True} if lock else {"ok": True})

    def _grading_result(self, doc):
        """One grade of one sampled answer, by the grader the token names. A grader grades an answer once;
        another grader may grade the same answer, and the facts layer combines them
        (final_season_facts.answer_incoherent). The record names the grader, never its token."""
        grader = self._grader()
        if grader is None:
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
        day = filed[0]
        rec = {"type": "work_grade", "job_id": jid, "miner_id": mid, "coherent": coherent, "model": model,
               "grader": grader}
        with ST.rank_lock:
            closed = ST.day_closed(day)
            if closed:
                return self._send(409, {"error": closed})
            with ST.lock:
                graded = ST.work_graded.get(jid, set())
                if grader in graded:
                    return self._send(409, {"error": "already graded"})
                ST.work_graded[jid] = graded | {grader}
                ST.work_grades[day] = ST.work_grades.get(day, 0) + 1
            ST.ev.append(day, rec)
        return self._send(200, {"ok": True, "grader": grader})


# -- background work --------------------------------------------------------------------------
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
        time.sleep(WATCH_S)


def read_capacity_registry(path: str) -> dict:
    """{miner_id: (kit_version, judge_role, reported_ts)} of the signed reports in the capacity registry's store
    (capacity_server.py: one JSON object, the signed reports under KEY_PROVEN with `_proven` naming the identity the
    signature proved). The number is read as the registry stored it, an integer, and None when it is anything
    else: on the refusal of a stale kit, absent is not a current kit. The role is the registry's word
    (capacity_server.JUDGE_ROLES), "unknown" when it is not a string. A file that cannot be read or is not an
    object raises: the caller decides what an unreadable registry means."""
    with open(path, encoding="utf-8") as f:
        store = json.load(f)
    if not isinstance(store, dict):
        raise ValueError("the capacity registry's store is not an object")
    out = {}
    for key, rep in store.items():
        if not (isinstance(key, str) and key.startswith(CAPACITY_SIGNED) and isinstance(rep, dict)):
            continue
        mid = rep.get("_proven")
        if not isinstance(mid, str) or mid != key[len(CAPACITY_SIGNED):] or not MINER_ID.match(mid):
            continue
        kv = rep.get("kit_version")
        kv = kv if (type(kv) is int and kv >= 0) else None
        role = rep.get("judge_role")
        role = role if isinstance(role, str) else "unknown"
        ts = rep.get("ts")
        out[mid] = (kv, role, ts if (type(ts) is int and ts >= 0) else None)
    return out


def file_capacity_changes(registry: dict, height: int, day: int) -> int:
    """File in the evidence, under `day`, one `capacity` record per identity whose signed report differs from the
    last one filed (kit number or judge role), at `height`; returns how many were filed. An identity that left the
    registry keeps its last record: the ranking reads the latest report at or before a day's end, and a report
    that stopped coming is still the latest. Pure on the registry it is given, so a bench drives it."""
    filed = 0
    for mid in sorted(registry):
        kv, role, ts = registry[mid]
        if ST.capacity.get(mid) == (kv, role):
            continue
        rec = {"type": CAPACITY, "miner_id": mid, "kit_version": kv, "judge_role": role, "height": height}
        if ts is not None:
            rec["reported_ts"] = ts
        ST.ev.append(day, rec)
        ST.capacity[mid] = (kv, role)
        filed += 1
    return filed


def watch_capacity() -> None:
    """Every CAPACITY_EVERY_S, once the rules of decision 24 apply to a day and the chain's height is known, read
    the capacity registry's store and file what changed (`file_capacity_changes`). An unreadable store is
    reported and tried again: the last records filed stand, and a day whose end passes without a newer one is
    ranked on them."""
    while True:
        time.sleep(CAPACITY_EVERY_S)
        if ST.kit_rules_from is None or not ST.height or not CAPACITY_DB:
            continue
        try:
            registry = read_capacity_registry(CAPACITY_DB)
        except (OSError, ValueError) as e:
            print(f"[final-season] capacity registry unreadable ({type(e).__name__}: {e}); retrying", flush=True)
            continue
        # The height is read once: `watch_chain` advances it between two reads, and a record stamped with the
        # height of day D under the day D+1 is not read by the ranking of day D (`ranking_inputs` reads the files
        # of the days up to D).
        h = ST.height
        try:
            n = file_capacity_changes(registry, h, max(0, day_of_height(h, ST.start)))
        except OSError as e:
            print(f"[final-season] capacity record not filed ({type(e).__name__}: {e}); retrying", flush=True)
            continue
        if n:
            print(f"[final-season] {n} signed capacity report(s) filed at height {h}", flush=True)


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
                # A day is over when the next one starts, or, for the last one, once the chain is past
                # the season's last block: waiting for its full 17 280 blocks would seal it late, or never
                # if the chain stops after the season.
                try:
                    seal_work_day(day)
                except OSError as e:
                    # Said, and tried again: a thread that died here would leave every later day
                    # unsealed, ungraded, and its incoherent work paid, without a word.
                    print(f"[final-season] day {day}: sample not sealed ({type(e).__name__}: {e}); retrying",
                          flush=True)


def seal_work_day(day: int) -> None:
    """Once `day` is over, draw at most WORK_SAMPLE answers per identity from its private file, in the
    order set by the secret, into the evidence; record the draw with the job ids whose answer was received
    that day (the only programme requests that count as work, `final_season_facts.answered_jobs`); then delete
    the private file. A day already ranked takes nothing more: its ranking stands as published. A day only
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
            # The finality rule of the day's own set: a day under the rules before decision 18 is final when
            # they said it was (`final_season_rules.finality_rule`), so a recompute finds the same window.
            day_rules = R.rules_for_day_with_jury(day, ST.unwound_from, ST.doubled_from, ST.more_requests_from,
                                                  ST.kit_rules_from, ST.jury_rules_from)
            finality = RK.finality_blocks(params, day_rules)
            height, records = ranking_inputs(day, finality)
            out = RK.rank_day(day, records, NODE, GENERATOR, ST.start, paid_before, finality, height,
                              RK.epoch_blocks(params), unwound_from=ST.unwound_from, doubled_from=ST.doubled_from,
                              more_requests_from=ST.more_requests_from, kit_rules_from=ST.kit_rules_from,
                              relay_store=RELAY_STORE, jury_rules_from=ST.jury_rules_from,
                              fault_reveal_dir=FAULT_REVEAL_DIR)
        except (C.ChainUnreadable, ValueError) as e:
            # "not final yet" is the normal answer for most of a day; anything else is logged.
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
        # The cheapest place for the alert: the day's records are in hand, nothing more is read. The status
        # repeats it (`ungraded_ranked_days`) for the graders' cron, which relays it in its own log.
        sampled = sum(1 for r in records if r.get("type") == "work_answer" and r.get("day") == day)
        graded = sum(1 for r in records if r.get("type") == "work_grade" and r.get("day") == day)
        if sampled and not graded:
            print(f"[final-season] alert: day {day} ranked with no grade of its {sampled} sampled answers: no "
                  f"work of an unwound audit is paid that day and no incoherent answer took a day's work away; "
                  f"the ranking is final", flush=True)


def main() -> int:
    global ST, WATCH_S
    try:
        WATCH_S = watch_interval_s()
    except ValueError as e:
        print("[final-season] FATAL: " + str(e), flush=True)
        return 2
    if not START.isdigit():
        print("[final-season] FATAL: DENDRA_FINAL_SEASON_START_HEIGHT is not set. The season's first block is a published "
              "fact of the programme; it is never guessed.", flush=True)
        return 2
    conflict = grader_token_conflict()
    if conflict:
        print("[final-season] FATAL: " + conflict, flush=True)
        return 2
    ST = State(DATA, int(START))
    workers = [watch_chain, seal_days, watch_capacity]
    if GENERATOR:
        workers.append(rank_days)
    else:
        print("[final-season] DENDRA_FINAL_SEASON_GENERATOR is not set: no ranking will be published. The programme's "
              "work reward counts the generator's jobs, so without its address no day can be ranked.",
              flush=True)
    graders = sorted(name for name, token in grader_tokens().items() if token)
    if not graders:
        print("[final-season] neither DENDRA_FINAL_SEASON_GRADER_TOKEN nor DENDRA_FINAL_SEASON_GRADER_CPU_TOKEN is "
              "set: no answer to a programme request can be graded, and no day's work can be taken away for "
              "incoherent answers.", flush=True)
    else:
        print(f"[final-season] graders with a token: {', '.join(graders)}", flush=True)
    for fn in workers:
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[final-season] listening on {HOST}:{PORT} start_height={ST.start} decision 18 from day {ST.unwound_from}, "
          f"doubled rates from day {ST.doubled_from}, decision 23 (more requests a day) from day "
          f"{ST.more_requests_from}, decision 24 (kit rule set, floor {R.KIT_MIN}) from day {ST.kit_rules_from}, "
          f"the jury set from day {ST.jury_rules_from}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
