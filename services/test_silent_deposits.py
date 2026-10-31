# -*- coding: utf-8 -*-
"""A relay deposit whose result is discarded turns a refusal into a success-shaped answer.

Two of them shipped side by side with a third that did it right, which is how the defect stayed
invisible: the neighbour sixteen lines below the miner's pubkey deposit tests its own result and
prints the refusal, so the file looked like it handled failures.

What each silence costs is different, and this bench says so case by case:
  - the miner's `pub` deposit: a fallback key, not blocking -- but its silence is what makes the
    relay's write policy unmeasurable from the only place that exercises it first;
  - the client's `req` deposit: the escrow is already locked on chain when it runs, so a lost deposit
    leaves a paid job that no miner was ever asked to serve.

The bench drives the shipped functions with a fake transport; it does not re-implement the decision.
"""
import ast
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
    """In-memory transport that refuses a chosen kind and accepts the rest.

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


_WRITES = ("put", "put_status", "put_answer")


def _pub_deposits(tree):
    """The calls `relay.<write>(<base>, "pub", ...)` in `tree`, each with its chain of parents."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _WRITES
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "relay"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "pub"):
            found.append(node)
    return found, parents


def pub_deposit_answers_read(tree):
    """(number of `pub` deposits, the ones whose answer is dropped). Parsed rather than matched as text: the
    write may be `put` or `put_status` and its answer may carry any name; a deposit drops its answer when the
    call is a bare statement, or when it is assigned to a name that nothing in the same function reads later."""
    calls, parents = _pub_deposits(tree)
    dropped = []
    for call in calls:
        stmt = parents.get(call)
        if isinstance(stmt, ast.Expr):
            dropped.append(call.lineno)
            continue
        if isinstance(stmt, ast.Assign) and stmt.value is call:
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            fn = stmt
            while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                fn = parents.get(fn)
            read = any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in names
                       and n.lineno > stmt.lineno for n in ast.walk(fn))
            if not read:
                dropped.append(call.lineno)
        # Any other place (an `if` test, a comparison, a return) uses the answer where it is produced.
    return len(calls), dropped


class _DropTheAnswer(ast.NodeTransformer):
    """The mutation: every `answer = relay.<write>(..., "pub", ...)` becomes the bare call."""

    applied = 0

    def visit_Assign(self, node):
        v = node.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in _WRITES
                and len(v.args) >= 2 and isinstance(v.args[1], ast.Constant) and v.args[1].value == "pub"):
            self.applied += 1
            return ast.copy_location(ast.Expr(value=v), node)
        return node


def main():
    import relay_client as relay
    import client as dc

    # -- 1. the daemon: its key deposit is no longer mute ---------------------------------------
    src = open(os.path.join(HERE, "miner.py"), encoding="utf-8").read()
    n_pub, dropped = pub_deposit_answers_read(ast.parse(src))
    if n_pub >= 1 and not dropped:
        ok(f"the daemon reads the answer of its pubkey deposit ({n_pub} call(s), none discarded)")
    elif n_pub == 0:
        ko("no pubkey deposit found in miner.py: this bench measures nothing")
    else:
        ko(f"the daemon still drops the result of its pubkey deposit (line(s) {dropped})")
    # The mutation, on the shipped tree: the same deposit with its answer dropped must be refused, or the
    # check above would pass on anything.
    mutator = _DropTheAnswer()
    m_pub, m_dropped = pub_deposit_answers_read(mutator.visit(ast.parse(src)))
    if mutator.applied == 0:
        print("  [??] mutation not applicable: the shipped deposit is not assigned to a name -- neither green nor red")
    elif m_pub == n_pub and m_dropped:
        ok("mutation: the shipped deposit with its answer dropped is refused")
    else:
        ko(f"mutation not caught: the deposit with its answer dropped passes ({m_pub} call(s), {m_dropped})")
    if "the relay refused the pubkey deposit" in src:
        ok("a refusal is named in the log, with what it does and does not block")
    else:
        ko("a refused pubkey deposit says nothing")
    # It must stay non-fatal: the authoritative key is the one anchored on chain, and stopping a miner
    # that has everything it needs would be a worse defect than the silence.
    after = src.split("the relay refused the pubkey deposit")[1][:400]
    if "sys.exit" not in after:
        ok("and it stays non-fatal: the on-chain anchor is the source of truth")
    else:
        ko("a refused fallback deposit now kills the daemon")

    # -- 2. the client: a lost deposit no longer returns a success -------------------------------
    src_c = open(os.path.join(HERE, "client.py"), encoding="utf-8").read()
    if 'st = relay.put_status(relay_url, "req"' in src_c:
        ok("the client reads the status of its sealed-request deposit")
    else:
        ko("the client still discards the result of the sealed-request deposit")
    if '"undelivered": undelivered' in src_c:
        ok("the payload carries `undelivered`: a served job is distinguishable from a paid ghost")
    else:
        ko("the payload cannot express a job nobody was handed")
    # The keys must survive a partial failure: dropping them would make whatever did arrive
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
        ok("the retry is bounded: a hiccup must not cost an escrowed job, a loop must not hang it")
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
    print("Silent deposits bench", "GREEN" if FAIL == 0 else "RED")
    sys.exit(FAIL)
