#!/usr/bin/env python3
"""final_season_generator.py — the Final Testnet Season's own requests (ADR-047): a fixed number a day.

WHAT IT DOES
It sends `requests_per_day` requests each programme day, spread over the day. The number is FIXED, not
counted per identity: counted per identity, every identity a farm adds would raise the programme's total
of paid work. A fixed number is shared by the chain among its present miners, drawn by stake up to
`assignment_stake_cap_multiple` x `min_stake` per identity, a ceiling the launch genesis sets as high as
the chain accepts (ADR-047, decision 10): below it, splitting a stake buys no more of it.
It is an ordinary client of the chain: it opens the job, the chain draws the miner among the present
ones, the miner serves it, the generator settles it. Nothing here chooses who serves: the generator
cannot favour anyone, and the work reward counts only what the chain recorded.

THE ANSWERS GO TO THE GRADER, NOT TO A KEY
The generator reads the miner's answer (it is the client: the response is sealed to it) and forwards it,
with the request, to the programme service on its internal route. The service keeps a bounded sample per
identity and per day in the evidence; the grader asks a model whether each is a coherent attempt to answer
the request, and an identity whose graded work answers of a day are all incoherent earns no work reward
that day (`final_season_facts`). Nothing here scores an answer, and nothing sets a test: the work IS the
check.

A RESTART RESUMES THE DAY'S COUNT
How many requests of the day it has sent is written after each one (`STATE`, keyed by the season's first
block and the day): a restart in the middle of a day sends what is left, never the whole number again,
which would put up to twice the fixed number on that day. A count it cannot read stops it, said: "not
known" is not "none sent".

WHAT IT CANNOT DO, SAID
Requests are sent one at a time, because every request is two transactions from one account and
concurrent transactions from one account collide on its sequence. At about 30 s a request (an estimate,
not a measure), one generator keeps up with roughly 2 800 requests a day. If a day's requests are NOT
all sent, the log says by how much: a second generator account is then needed.
"""
from __future__ import annotations

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
from final_season_rules import RULES, end_epoch  # noqa: E402

# No request is opened in the last STOP_MARGIN_S seconds before the season's end (chain time): a request
# counts on the block it SETTLES, and one opened just before the end would be served after it and paid by
# nothing. A request is bounded by the client (`client.quick_metered`), not by its 240 s wait for
# the answer alone: the open transaction (about 48 s), the committee (45 s), that wait (240 s), the forward
# (20 s), the commit wait (180 s) and the settle transaction (about 48 s), some 580 s at the worst. Requests
# go one at a time, so at most one can straddle the end; a typical one takes about 30 s.
STOP_MARGIN_S = 600
END = end_epoch()

PROGRAMME = os.environ.get("DENDRA_FINAL_SEASON_URL", "http://final-season:8093/final-season/v1").rstrip("/")
INTERNAL = os.environ.get("DENDRA_FINAL_SEASON_INTERNAL", "http://final-season:8093/internal").rstrip("/")
INTERNAL_TOKEN = os.environ.get("DENDRA_FINAL_SEASON_INTERNAL_TOKEN", "")
RELAY = os.environ.get("DENDRA_RELAY", "http://relay:8645")
CLIENT = os.environ.get("DENDRA_FINAL_SEASON_GENERATOR_KEY", "generator")
BASE_FEE = int(os.environ.get("DENDRA_BASE_FEE", "500"))
PER_TOKEN = int(os.environ.get("DENDRA_PER_TOKEN", "10"))
OUT_ALLOW = int(os.environ.get("DENDRA_FINAL_SEASON_OUT_ALLOW", "768"))
STATE = os.environ.get("DENDRA_FINAL_SEASON_GENERATOR_STATE", "/data/final-season-generator/sent.json")

# The requests are COMPOSED, never picked from a short list: a published list of a few prompts can be
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


def report_work(job_id: str, miner_id: str, prompt: str, answer: str) -> None:
    """The miner's answer to one programme request, for the service to sample and the grader to read.
    Cut at the grader's own limit: what it would never read is not carried."""
    body = json.dumps({"job_id": job_id, "miner_id": miner_id, "prompt": prompt,
                       "answer": answer[:MAX_ANSWER_CHARS]}).encode()
    req = urllib.request.Request(f"{INTERNAL}/work_answer", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Final-Season-Internal", INTERNAL_TOKEN)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def run_one(dc, day: int, n: int, rnd: random.Random) -> str:
    prompt = make_prompt(rnd)

    def forward(jid, committee, answer):
        # Before the settlement (`client.quick_metered`): the answer's record lands on the day the
        # job is settled, never the next. A request whose answer is not recorded is not paid as work
        # (`final_season_facts.answered_jobs`), so a job settled with nothing behind it earns nothing.
        miner = (committee or [""])[0]
        if not (miner and isinstance(answer, str) and answer):
            raise ValueError("no answer to forward")
        report_work(jid, miner, prompt, answer)

    r = dc.quick_metered(prompt, BASE_FEE, PER_TOKEN, OUT_ALLOW, RELAY, client=CLIENT, on_answer=forward)
    if "error" in r:
        return f"refused: {r['error']}"
    miner = (r.get("committee") or [""])[0] or "?"
    if r.get("reported") is True:
        return f"served by {miner}, answer forwarded"
    return f"served by {miner}, answer NOT forwarded ({r.get('reported')}): not counted as work"


def spacing_s(quota: int, sent: int, height: int, start: int, block_s: float = 5.0,
              time_left_s: float | None = None):
    """Seconds until the next request, or None when today's quota is reached. The requests left are
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


def load_sent(path: str, start: int, day: int) -> tuple[int, int]:
    """(sent, answered) of `day`, in the season that starts at block `start`, as this generator last wrote
    them. No file: none (a fresh volume). A file of another season or another day: none that day. A file
    that cannot be read raises: a count NOT KNOWN is not a count of 0, and reading it as 0 sends the whole
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
    quota = RULES["requests_per_day"]
    next_at = time.time()
    stopped, said_unknown = False, False
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
            # "ended" unread or malformed is NOT "still running": an older service, or a chain time not read
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
                    print(f"[generator] day {day_seen} (the last day it sent requests on): {sent} of {quota} sent, "
                          f"{served} answered", flush=True)
                print(f"[generator] the season ends at {RULES['end_time']}: no more requests", flush=True)
                stopped = True
            time.sleep(600)
            continue
        if day != day_seen:
            if day_seen >= 0:
                # Sent is attempted, answered is an answer received and recorded: a day the chain refused
                # every request shows a full count of the first and none of the second.
                print(f"[generator] day {day_seen}: {sent} of {quota} sent, {served} answered"
                      + ("" if sent >= quota else " -- quota NOT reached"), flush=True)
            try:
                sent, served = load_sent(STATE, start, day)
            except (OSError, ValueError) as e:
                print(f"[generator] FATAL: the count of day {day} already sent is unreadable ({e}). Sending "
                      f"from zero could put twice the day's fixed number on it: fix or remove {STATE} by "
                      f"hand once the day's requests are known.", flush=True)
                return 2
            day_seen, next_at = day, time.time()
            print(f"[generator] day {day}: {quota} requests"
                  + (f", {sent} already sent before a restart" if sent else ""), flush=True)
        spacing = spacing_s(quota, sent, height, start, time_left_s=END - now - STOP_MARGIN_S)
        if spacing is None or time.time() < next_at:
            time.sleep(5)
            continue
        next_at = time.time() + spacing
        n += 1
        try:
            out = run_one(dc, day, n, rnd)
        except Exception as e:  # noqa: BLE001
            out = f"error {type(e).__name__}: {e}"
        sent += 1
        served += out.endswith("answer forwarded")
        print(f"[generator] day {day} request {sent}/{quota}: {out}", flush=True)
        try:
            save_sent(STATE, start, day, sent, served)
        except OSError as e:
            print(f"[generator] WARNING: the day's count is NOT saved ({type(e).__name__}: {e}): a restart "
                  f"today would send the requests already sent again", flush=True)


if __name__ == "__main__":
    sys.exit(main())
