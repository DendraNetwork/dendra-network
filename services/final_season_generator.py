#!/usr/bin/env python3
"""final_season_generator.py — the Final Testnet Season's own requests: a fixed number a day.

What it does
It sends `requests_per_day` requests each programme day, spread over the day. The number is fixed, not
counted per identity: counted per identity, every identity a farm adds would raise the programme's total
of paid work. A fixed number is shared by the chain among its present miners, each identity's stake
weighing in the draw up to `assignment_stake_cap_multiple` x `min_stake` (`committee.go::capAssignmentWeights`).
Below that ceiling an identity's share follows its stake and splitting the
stake does not raise it; above it, splitting into identities that each still reach the ceiling does, while
a split into identities below it can lower it; at a multiple of 1 every identity bonded at `min_stake` sits
at the ceiling, so each identity added draws its own share. Governance moves the multiple: read
it and `min_stake` with `GET /dendra/jobs/v1/params` (0, or absent, selects the compiled multiple). Either
way, adding identities does not raise the day's number of requests.
It is an ordinary client of the chain: it opens the job, the chain draws the miner among the present
ones, the miner serves it, the generator settles it. Nothing here chooses who serves: the generator
cannot favour anyone, and the work reward counts only what the chain recorded.

The answers go to the grader, not to a key
The generator reads the miner's answer (it is the client: the response is sealed to it) and forwards it,
with the request, to the programme service on its internal route. The service keeps a bounded sample per
identity and per day in the evidence; the grader asks a model whether each is a coherent attempt to answer
the request, and an identity whose graded work answers of a day are all incoherent earns no work reward
that day (`final_season_facts`). Nothing here scores an answer, and nothing sets a test: the work is the
check.

The day's number is a number of answered requests
An identity can be present on the chain without a model behind it, and the chain can still draw it as the
primary of a programme request: no answer comes, and the job stays open until its expiry
(`job_expiry.go::jobExpiryBlocks`), when its escrow returns to the generator. Counted as one of the day's
`requests_per_day`, such a request took a share of the fixed volume away from the miners that answer. So the
day's number counts the requests whose answer reached the programme -- the only ones that can be paid as work
(`final_season_facts.answered_jobs`) -- and an unanswered one is replaced by the next request. A forward whose
reply is lost is sent again, and the programme answers a job it already holds as recorded (`report_work`); one
lost every time is counted in the day's number, since the programme may hold it. So the paid work of a day
does not exceed `requests_per_day`, save one request per stop of this process between an answer recorded and
the count written after it (`save_sent`). (A retry that crosses the day's boundary after the day was sealed files
the answer again under the new day: the job still counts once, on the day it settles.) What bounds the replacements: at most ATTEMPTS_PER_REQUEST x `requests_per_day` requests are sent a day, so a
day the chain refuses every request, or none is answered, sends a bounded number and says so.
What the bound does not bound, said: time. A request no one answers holds the generator for the answer's
wait, the commit wait and a settlement that fails (`client.quick_metered`), many times what a served
one takes, and requests go one at a time. Identities without a model drawn often enough leave fewer
answered requests than the day's number by its end; the day's log line says by how much.

The day's number is the rule set's, named by the service
Each day is ranked under one rule set (`final_season_rules.rules_for_day`), and its `requests_per_day` is that
day's number: decision 23 raised it from one day on (`final_season_rules.RULES_MORE_REQUESTS`). The service's
status names the set of the current day by its fingerprint; the number, and the bound of ATTEMPTS_PER_REQUEST x
that number, are read from this code's own copy of the set the fingerprint names (`day_quota`,
`final_season_rules.rule_set_of`), never from a number the network sends. A fingerprint of no set this code knows,
or none at all, sends nothing, and the log says so once while it lasts. The set is read again at every reading of
the status: a new day takes its own number, and a set the service fixed from the current day (a restart of the
service) takes effect within the day, said in the log. The set of decision 24 carries the kit floor, a number
this code learns from the status (`rules.kit_min`) and accepts only when the fingerprint the status names is the
one of the set built from it (`kit_set_named`): bound by the fingerprint, not taken on the network's word.

A restart resumes the day's count
How many requests of the day it has sent, and how many were answered, is written after each one (`STATE`,
keyed by the season's first block and the day): a restart in the middle of a day sends what is left of that
day's number, spread over what is left of the day, never the whole number again, which would put up to twice
the fixed number on that day. A count it cannot read stops it, said: "not known" is not "none sent".

What it cannot do, said
Requests are sent one at a time, because every request is two transactions from one account and
concurrent transactions from one account collide on its sequence. At about 30 s a request (an estimate,
not a measure), one generator keeps up with roughly 2 800 requests a day. If a day's requests are not
all sent, the log says by how much: a second generator account is then needed.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from final_season_grader import MAX_ANSWER_CHARS  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_rules import RULES, end_epoch, fingerprint, rule_set_of  # noqa: E402

# No request is opened in the last STOP_MARGIN_S seconds before the season's end (chain time): a request
# counts on the block it settles, and one opened just before the end would be served after it and paid by
# nothing. A request is bounded by the client (`client.quick_metered`), not by its 240 s wait for
# the answer alone: the open transaction (about 48 s), the committee (45 s), that wait (240 s), the forward
# (20 s), the commit wait (180 s) and the settle transaction (about 48 s), some 580 s at the worst. Requests
# go one at a time, so at most one can straddle the end; a typical one takes about 30 s.
STOP_MARGIN_S = 600
END = end_epoch()
# Chosen, not measured: an unanswered request is replaced, at most this many requests are sent per request of
# the day's number. It bounds what a day the chain refuses everything, or nobody answers, can send; each
# unanswered request holds its escrow until the job expires (job_expiry.go::jobExpiryBlocks).
ATTEMPTS_PER_REQUEST = 3

PROGRAMME = os.environ.get("DENDRA_FINAL_SEASON_URL", "http://final-season:8093/final-season/v1").rstrip("/")
INTERNAL = os.environ.get("DENDRA_FINAL_SEASON_INTERNAL", "http://final-season:8093/internal").rstrip("/")
INTERNAL_TOKEN = os.environ.get("DENDRA_FINAL_SEASON_INTERNAL_TOKEN", "")
RELAY = os.environ.get("DENDRA_RELAY", "http://relay:8645")
CLIENT = os.environ.get("DENDRA_FINAL_SEASON_GENERATOR_KEY", "generator")
BASE_FEE = int(os.environ.get("DENDRA_BASE_FEE", "500"))
PER_TOKEN = int(os.environ.get("DENDRA_PER_TOKEN", "10"))
OUT_ALLOW = int(os.environ.get("DENDRA_FINAL_SEASON_OUT_ALLOW", "768"))
STATE = os.environ.get("DENDRA_FINAL_SEASON_GENERATOR_STATE", "/data/final-season-generator/sent.json")

# The requests are composed, never picked from a short list: a published list of a few prompts can be
# answered from a table of answers written once, with no model at window time. A form, a subject, an
# audience and a length drawn at random give tens of thousands of requests, each needing its own answer.
FORMS = (
    "Explain {subject} to {audience} in {n} sentences.",
    "Give {n} practical tips about {subject} for {audience}.",
    "Write a {n}-sentence story in which {audience} learns about {subject}.",
    "List {n} common misconceptions about {subject}, each with a one-sentence correction, for {audience}.",
    "Write {n} quiz questions about {subject} for {audience}, each followed by its answer.",
    "Summarise how {subject} works in {n} sentences, for {audience}.",
    "Write a short dialogue of {n} lines between {audience} and an expert about {subject}.",
    "Compare {subject} with {other} in {n} sentences, for {audience}.",
)
SUBJECTS = (
    "lighthouses", "the water cycle", "bicycle gears", "prime numbers", "vegetable soup", "weather and climate",
    "train timetables", "libraries", "flat tyres", "the colour of the sky", "compasses", "tomato plants",
    "saving energy at home", "volcanoes", "honey bees", "tides", "solar panels", "bread baking",
    "recycling plastic", "paper maps", "rainbows", "wind turbines", "composting", "the night sky",
    "first aid for small cuts", "public libraries", "postal services", "electric kettles", "bridges",
    "ice skating", "river boats", "musical scales", "chess openings", "mountain hiking", "bird migration",
    "coffee brewing", "street markets", "seed saving", "cloud types", "home insulation", "fire safety",
    "letter writing", "earthquakes", "photosynthesis", "the moon's phases", "sailing knots", "clocks",
    "telescopes", "fresh water wells", "pottery", "orchards", "snowfall", "radio waves", "hand washing",
    "tree rings", "deserts", "lightning", "bus networks", "soap making", "ocean currents",
)
AUDIENCES = (
    "a ten-year-old", "a retired engineer", "a new gardener", "a tourist", "a nurse", "a farmer",
    "a first-year student", "a shop owner", "a librarian", "a bus driver", "a grandparent", "a sailor",
)


def make_prompt(rnd) -> str:
    form = rnd.choice(FORMS)
    subject = rnd.choice(SUBJECTS)
    other = rnd.choice([x for x in SUBJECTS if x != subject])
    return form.format(subject=subject, other=other, audience=rnd.choice(AUDIENCES), n=3 + rnd.randrange(5))


def _get(url: str):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


# What a forward of an answer to the programme came to (`report_work`): three answers, never two.
RECORDED = "recorded"      # the programme answered that it holds the answer (200, or 409 "already_filed")
UNKNOWN = "unknown"        # every try was lost in transit: the programme may hold it
FORWARD_TRIES = 3          # Chosen: the internal route is on the same host; three losses in a row is an outage
FORWARD_RETRY_S = 5.0


class ForwardRefused(RuntimeError):
    """The programme answered and did not record the answer (season not started or over, bad token, ...)."""


def report_work(job_id: str, miner_id: str, prompt: str, answer: str, tries: int = FORWARD_TRIES,
                sleep=None) -> str:
    """The miner's answer to one programme request, for the service to sample and the grader to read. Cut at
    the grader's own limit: what it would never read is not carried. RECORDED or UNKNOWN; raises
    ForwardRefused when the programme answered without recording it.

    A forward lost in transit is tried again, and the second try can find the first. The service may write the
    answer and its reply be lost (a timeout, a dropped connection): read as "not recorded", the request was
    replaced while its job was settled anyway, and the day paid one request more than its number. The same
    body is sent again; the service answers a job it already holds with 409 and `"already_filed": true`, which
    is read as recorded. A 503 (the service's handlers are all busy, nothing read) is tried again too. When
    every try is lost, the answer is UNKNOWN: the service may hold it, and the caller says so."""
    body = json.dumps({"job_id": job_id, "miner_id": miner_id, "prompt": prompt,
                       "answer": answer[:MAX_ANSWER_CHARS]}).encode()
    sleep = sleep or time.sleep
    last = ""
    for i in range(max(1, tries)):
        if i:
            sleep(FORWARD_RETRY_S)
        req = urllib.request.Request(f"{INTERNAL}/work_answer", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Final-Season-Internal", INTERNAL_TOKEN)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            return RECORDED
        except urllib.error.HTTPError as e:
            try:
                doc = json.loads(e.read() or b"{}")
            except ValueError:
                doc = {}
            if e.code == 409 and isinstance(doc, dict) and doc.get("already_filed") is True:
                return RECORDED
            if e.code == 503:
                last = "503"
                continue
            raise ForwardRefused(f"{e.code} {str((doc or {}).get('error') if isinstance(doc, dict) else '')[:160]}")
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            # lost in transit: refused or dropped connection, timeout, a reply cut short (IncompleteRead)
            last = f"{type(e).__name__}: {e}"
    print(f"[generator] the answer to {job_id} could not be confirmed after {max(1, tries)} tries ({last[:160]}): "
          f"the programme may hold it", flush=True)
    return UNKNOWN


def run_one(dc, day: int, n: int, rnd: random.Random) -> tuple:
    """(what happened, answered): `answered` is True when the miner's answer was forwarded to the programme,
    which recorded it -- the one condition under which the request can be paid as work; UNKNOWN when the
    forward was lost in transit every time (the programme may hold it); False otherwise."""
    prompt = make_prompt(rnd)
    forwarded = {"state": None}

    def forward(jid, committee, answer):
        # Before the settlement (`client.quick_metered`): the answer's record lands on the day the
        # job is settled, never the next. A request whose answer is not recorded is not paid as work
        # (`final_season_facts.answered_jobs`), so a job settled with nothing behind it earns nothing.
        miner = (committee or [""])[0]
        if not (miner and isinstance(answer, str) and answer):
            raise ValueError("no answer to forward")
        forwarded["state"] = report_work(jid, miner, prompt, answer)

    r = dc.quick_metered(prompt, BASE_FEE, PER_TOKEN, OUT_ALLOW, RELAY, client=CLIENT, on_answer=forward)
    if "error" in r:
        return f"refused: {r['error']}", False
    miner = (r.get("committee") or [""])[0] or "?"
    # What `forward` came to is read from its own record, never from the text of an exception's message.
    if r.get("reported") is True and forwarded["state"] == RECORDED:
        return f"served by {miner}, answer forwarded", True
    if r.get("reported") is True and forwarded["state"] == UNKNOWN:
        return (f"served by {miner}, answer forwarded but not confirmed: counted in the day's number, since the "
                f"programme may hold it and the job is settled"), UNKNOWN
    return (f"drawn {miner}, answer not forwarded ({r.get('reported')}): not counted as work, and not "
            f"counted in the day's number: another request takes its place"), False


def spacing_s(quota: int, sent: int, height: int, start: int, block_s: float = 5.0,
              time_left_s: float | None = None):
    """Seconds until the next request, or None when today's quota is reached. `sent` is what counts against
    the quota: the day's answered requests (see the module's header). The requests left are
    spread evenly over the blocks left in the day (about 5 s per block: an assumption, which only paces
    the sending — what counts is the block-day the chain records). On the season's last day the time left
    before the end (`time_left_s`, chain time) can be shorter: the day's requests are spread over it."""
    left = quota - sent
    if left <= 0:
        return None
    blocks_left = RULES["day_blocks"] - ((max(height, start) - start) % RULES["day_blocks"])
    span = blocks_left * block_s
    if time_left_s is not None:
        span = min(span, max(0.0, time_left_s))
    return max(1.0, span / left)


def day_quota(status: dict):
    """The number of answered requests of the status's day: `requests_per_day` of the published rule set the
    status's `rules_fingerprint` names (`final_season_rules.rule_set_of`, RULE_HISTORY), read from this code's own
    copy of the set. None when the fingerprint is missing, malformed, or names no set this code knows: the number
    is then unknown, and never taken from the `rules` the status carries, a number the network sends."""
    fp = status.get("rules_fingerprint") if isinstance(status, dict) else None
    if not isinstance(fp, str):
        return None
    rules = rule_set_of(fp)[1]
    if rules is None:
        rules = kit_set_named(status, fp)
    return None if rules is None else rules["requests_per_day"]


def kit_set_named(status: dict, fp: str):
    """The set of decision 24 (`final_season_rules.RULES_KIT`) when the status's fingerprint is its: the set
    carries the kit floor, which this code does not know until the service names it, and the only number the
    status is trusted for is one the fingerprint binds -- `rules.kit_min` is taken, the set is built from it, and
    it stands only when its fingerprint is the one the status names (`final_season_rules.set_kit_min`). A floor
    already fixed in this process is not moved. None otherwise: the set stays unknown, and nothing is sent."""
    if R.KIT_MIN is not None:
        return None
    rules = status.get("rules")
    kit_min = rules.get("kit_min") if isinstance(rules, dict) else None
    if type(kit_min) is not int or isinstance(kit_min, bool) or kit_min < 1:
        return None
    if fingerprint(R.kit_rules(kit_min)) != fp:
        return None
    return R.set_kit_min(kit_min)


def load_sent(path: str, start: int, day: int) -> tuple[int, int]:
    """(sent, answered) of `day`, in the season that starts at block `start`, as this generator last wrote
    them. No file: none (a fresh volume). A file of another season or another day: none that day. A file
    that cannot be read raises: a count not known is not a count of 0, and reading it as 0 sends the whole
    quota a second time."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return 0, 0
    if not isinstance(d, dict):
        raise ValueError(f"{path} is not a count")
    if d.get("start_height") != start or d.get("day") != day:
        return 0, 0
    sent, answered = d.get("sent"), d.get("answered")
    if type(sent) is not int or type(answered) is not int or not 0 <= answered <= sent:
        raise ValueError(f"{path} carries no valid count for day {day}")
    return sent, answered


def save_sent(path: str, start: int, day: int, sent: int, answered: int) -> None:
    """Written whole or not at all (a temporary file, then a rename): a crash never leaves half a count."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"start_height": start, "day": day, "sent": sent, "answered": answered}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main() -> int:
    import client as dc
    if not INTERNAL_TOKEN:
        print("[generator] FATAL: DENDRA_FINAL_SEASON_INTERNAL_TOKEN is not set: the miners' answers could not be "
              "forwarded, and an answer nobody grades is work nobody checks.", flush=True)
        return 2
    rnd = random.SystemRandom()
    day_seen, sent, served, n = -1, 0, 0, 0
    # The number of answered requests of `day_seen` and its bound on what is sent: those of the rule set the
    # status names for that day (`day_quota`), set when the day is first seen and whenever the set changes.
    quota, max_sent = None, 0
    next_at = time.time()
    stopped, said_unknown, said_bound, said_set = False, False, False, False
    while True:
        try:
            st = json.loads(_get(f"{PROGRAMME}/status"))
        except Exception as e:  # noqa: BLE001
            print(f"[generator] programme unreachable ({type(e).__name__}); retrying", flush=True)
            time.sleep(30)
            continue
        day, start, height = st.get("day"), st.get("start_height"), st.get("height")
        ended, now = st.get("ended"), st.get("latest_block_time")
        if type(day) is not int or day < 0 or type(start) is not int or type(height) is not int:
            # before the season, or while the service has not read the chain yet ("day": null)
            time.sleep(60)
            continue
        if (ended is not True and ended is not False) or type(now) is not int:
            # "ended" unread or malformed is not "still running": an older service, or a chain time not read
            # yet. Nothing is sent on a season whose end the service cannot say it has not reached.
            if not said_unknown:
                print("[generator] the programme's status says neither whether the season is over nor the "
                      "chain's time (an older service, or a chain not read yet): no request is sent until it "
                      "does", flush=True)
                said_unknown = True
            time.sleep(60)
            continue
        if ended or END - now <= STOP_MARGIN_S:
            # The season is over, or too close to its end for a request to settle inside it. The process
            # stays up and idle (the service restarts it if it exits): nothing is sent from here on.
            if not stopped:
                if day_seen >= 0:
                    print(f"[generator] day {day_seen} (the last day it sent requests on): {served} of {quota} "
                          f"answered, {sent} sent", flush=True)
                print(f"[generator] the season ends at {RULES['end_time']}: no more requests", flush=True)
                stopped = True
            time.sleep(600)
            continue
        today = day_quota(st)
        if today is None:
            # The day's number is a rule of the day's set: a set this code does not know has no number here.
            if not said_set:
                print(f"[generator] day {day}: the programme's status names no rule set this generator knows "
                      f"(rules_fingerprint {str(st.get('rules_fingerprint'))[:12]}): the day's number of requests "
                      f"is not known, and no request is sent until it is", flush=True)
                said_set = True
            time.sleep(60)
            continue
        said_set = False
        if day != day_seen:
            if day_seen >= 0:
                # Answered is an answer received and recorded, the day's number; sent is every attempt: a
                # day the chain refused every request shows the bound of the second and none of the first.
                print(f"[generator] day {day_seen}: {served} of {quota} answered, {sent} sent"
                      + ("" if served >= quota else " -- quota not reached"), flush=True)
            try:
                sent, served = load_sent(STATE, start, day)
            except (OSError, ValueError) as e:
                print(f"[generator] FATAL: the count of day {day} already sent is unreadable ({e}). Sending "
                      f"from zero could put twice the day's fixed number on it: fix or remove {STATE} by "
                      f"hand once the day's requests are known.", flush=True)
                return 2
            day_seen, next_at, said_bound = day, time.time(), False
            quota, max_sent = today, ATTEMPTS_PER_REQUEST * today
            print(f"[generator] day {day}: {quota} answered requests"
                  + (f", {served} answered and {sent} sent before a restart" if sent else "")
                  + f" (requests_per_day of rules {st['rules_fingerprint'][:12]})", flush=True)
        elif today != quota:
            # The service was restarted with a set fixed from the current day: the day is ranked under it, so its
            # number is the day's from now on, the requests already answered counted against it.
            print(f"[generator] day {day}: the programme now names rules {st['rules_fingerprint'][:12]} for the "
                  f"day: {today} answered requests instead of {quota}, with {served} answered and {sent} sent so "
                  f"far", flush=True)
            quota, max_sent, said_bound = today, ATTEMPTS_PER_REQUEST * today, False
        if sent >= max_sent:
            # The bound on replacements: the day sends no more, whatever is left of its number.
            if not said_bound:
                print(f"[generator] day {day}: {sent} requests sent, the day's bound ({ATTEMPTS_PER_REQUEST} x "
                      f"{quota}), with {served} answered: no more requests today", flush=True)
                said_bound = True
            time.sleep(60)
            continue
        spacing = spacing_s(quota, served, height, start, time_left_s=END - now - STOP_MARGIN_S)
        if spacing is None or time.time() < next_at:
            time.sleep(5)
            continue
        next_at = time.time() + spacing
        n += 1
        try:
            out, answered = run_one(dc, day, n, rnd)
        except Exception as e:  # noqa: BLE001
            out, answered = f"error {type(e).__name__}: {e}", False
        sent += 1
        # An answer whose forward could not be confirmed counts too: its job is settled and the programme may
        # hold it, so counted the other way the day could pay more requests than its number.
        served += 1 if (answered is True or answered == UNKNOWN) else 0
        print(f"[generator] day {day} request {sent} ({served}/{quota} answered): {out}", flush=True)
        try:
            save_sent(STATE, start, day, sent, served)
        except OSError as e:
            print(f"[generator] WARNING: the day's count is not saved ({type(e).__name__}: {e}): a restart "
                  f"today would send the requests already sent again", flush=True)


if __name__ == "__main__":
    sys.exit(main())
