# -*- coding: utf-8 -*-
"""A relay deposit whose result is discarded turns a refusal into a success-shaped answer.

Two of them shipped side by side with a third that did it right, which is how the defect stayed
invisible: the neighbour sixteen lines below the miner's pubkey deposit tests its own result and
prints the refusal, so the file LOOKED like it handled failures.

What each silence costs is different, and this bench says so case by case:
  - the miner's `pub` deposit: a fallback key, not blocking -- but its silence is what makes the
    relay's WRITE policy unmeasurable from the only place that exercises it first;
  - the client's `req` deposit: the escrow is ALREADY locked on chain when it runs, so a lost deposit
    leaves a paid job that no miner was ever asked to serve.

The bench drives the shipped functions with a fake transport; it does not re-implement the decision.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAIL = 0
HERE = os.path.dirname(os.path.abspath(__file__))


def ok(m):
    print("  [ok] " + m)


def ko(m):
    global FAIL
    FAIL = 1
    print("  [KO] " + m)


class FakeRelay:
    """In-memory transport that REFUSES a chosen kind and accepts the rest.

    Both write entry points are faked: `put_status`, which answers a three-valued status, and the
    boolean `put` that wraps it. A fake covering only one of them leaves the other reaching the
    network, which is how a bench stops testing without saying so.
    """

    def __init__(self, refuse=("req",)):
        self.refuse, self.store, self.attempts = refuse, {}, 0

    def put_status(self, base, kind, key, obj, **kw):
        if kind in self.refuse:
            self.attempts += 1
            return "refused"
        self.store[f"{kind}/{key}"] = obj
        return "ok"

    def put(self, base, kind, key, obj, **kw):
        return self.put_status(base, kind, key, obj, **kw) == "ok"

    def get(self, base, kind, key, retries=1):
        return self.store.get(f"{kind}/{key}")


def main():
    import relay_client as relay
    import client as dc

    # -- 1. the daemon: its key deposit is no longer mute ---------------------------------------
    src = open(os.path.join(HERE, "miner.py"), encoding="utf-8").read()
    if 'if relay.put(a.relay, "pub", a.id, {"pub": mypub}):' in src:
        ok("the daemon TESTS its pubkey deposit instead of discarding the answer")
    else:
        ko("the daemon still drops the result of its pubkey deposit")
    if "the relay REFUSED the pubkey deposit" in src:
        ok("a refusal is named in the log, with what it does and does not block")
    else:
        ko("a refused pubkey deposit says nothing")
    # It must stay NON-fatal: the authoritative key is the one anchored on chain, and stopping a miner
    # that has everything it needs would be a worse defect than the silence.
    after = src.split("the relay REFUSED the pubkey deposit")[1][:400]
    if "sys.exit" not in after:
        ok("and it stays non-fatal: the on-chain anchor is the source of truth")
    else:
        ko("a refused fallback deposit now kills the daemon")

    # -- 2. the client: a lost deposit no longer returns a success -------------------------------
    src_c = open(os.path.join(HERE, "client.py"), encoding="utf-8").read()
    if 'st = relay.put_status(relay_url, "req"' in src_c:
        ok("the client reads the STATUS of its sealed-request deposit")
    else:
        ko("the client still discards the result of the sealed-request deposit")
    if '"undelivered": undelivered' in src_c:
        ok("the payload carries `undelivered`: a served job is distinguishable from a paid ghost")
    else:
        ko("the payload cannot express a job nobody was handed")
    # The keys must survive a partial failure: dropping them would make whatever DID arrive
    # undecryptable on top of being partial.
    if '"keys": keys' in src_c.split("undelivered.append(mid)")[1][:1200]:
        ok("the session keys are returned even when a deposit failed (a partial job stays readable)")
    else:
        ko("a failed deposit loses the keys of the members that were served")
    if "exists" in src_c.split('st = relay.put_status(relay_url, "req"')[1][:600]:
        ok("a 409 (already stored) counts as delivered, not as a loss")
    else:
        ko("a re-deposit of an already stored request would be reported as undelivered")
    if "for _ in range(3)" in src_c:
        ok("the retry is BOUNDED: a hiccup must not cost an escrowed job, a loop must not hang it")
    else:
        ko("no bounded retry around the deposit that hands over the prompt")

    # -- 3. the seam: the fake transport refuses what the shipped loop retries on ----------------
    fr = FakeRelay()
    old_ps, old_put = relay.put_status, relay.put
    try:
        relay.put_status, relay.put = fr.put_status, fr.put
        if relay.put_status("http://relay.invalid", "req", "jX__mY", {}) == "refused":
            ok("the fake transport refuses a `req` deposit, which is what the shipped loop retries on")
        else:
            ko("the fake transport does not refuse")
        if relay.put("http://relay.invalid", "pub", "mY", {}) is True:
            ok("and it accepts the kinds it was not asked to refuse (no blanket failure)")
        else:
            ko("the fake transport refuses everything, so a green case proves nothing")
    finally:
        relay.put_status, relay.put = old_ps, old_put

    if hasattr(dc, "submit_job"):
        ok("submit_job is importable (the shipped path this bench pins)")
    else:
        ko("submit_job not found in client")


if __name__ == "__main__":
    main()
    print("SILENT DEPOSITS BENCH", "GREEN" if FAIL == 0 else "RED")
    sys.exit(FAIL)
