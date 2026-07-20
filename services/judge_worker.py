#!/usr/bin/env python3
"""COMMITTEE WORKER: judges the audited sample and commits its verdict on-chain.

Run BY an already-registered miner (reuses its chain key + its X25519 key from `keydir`), IN ADDITION
to `miner.py`. Loop:
  1. discovers the `+disputed` jobs (audit) via `query jobs list-job` -- EXCEPT those where I am
     the primary, EXCEPT already resolved/judged;
  2. fetches the primary's sealed REVEAL (`reveal_helpers.open_reveal`) -> (prompt, answer);
  3. computes MY own answer (Ollama) on the prompt;
  4. judges "primary's answer == same fact as mine?" (`modea.judge.llm_judge`);
  5. commits the verdict on-chain: `create-commit "<jobId>__verdict__<minerId>" <0|1> <0|1> verdict`
     (REUSES the existing message; `AdjudicateDispute` tallies these verdicts weighted by stake);
  6. best-effort: tries `adjudicate-dispute <jobId>` after the window (permissionless) to close it.

No new chain message. A verdict from a miner of the ORIGINAL committee is filtered on-chain (no effect).

Usage: python3 judge_worker.py --id m2 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea.miner import Miner
from modea.judge import (llm_judge, llm_coherent, llm_relevant, llm_multi_ok, verdict_commit,
                         DEFAULT_JUDGE_MODEL)
import reveal_helpers as rv

CHAIN = "dendra"
NODE = os.environ.get("DENDRA_NODE", "")
JUDGE_MODEL_ID = os.environ.get("DENDRA_JUDGE_MODEL_ID", "")  # requis si enforce_model_registry=ON
# 2-STAGE: COHERENCE stage (anti-word-salad) BEFORE same-fact. Measured (mistral-nemo, 294 cases):
# salad 0/98, false 0/98, honest-FN 1/98; per-judge gate AND committee GREEN. Default ON; DENDRA_TWOSTAGE=0 to disable.
TWOSTAGE = os.environ.get("DENDRA_TWOSTAGE", "1") != "0"
# PRO-HONEST guard: off-topic-vs-PROMPT => ABSTENTION (no "invalid" vote
# on a confused question). Default ON; DENDRA_JUDGE_RELEVANCE=0 to roll back to the soft-launch behavior.
RELEVANCE = os.environ.get("DENDRA_JUDGE_RELEVANCE", "1") != "0"
# Layer A: judge SELF-CONSISTENCY -- the reference is generated K times
# at temperature>0; a judge that disagrees with ITSELF = ambiguous question => ABSTENTION, never "invalid".
# Closes the hole the relevance guard does NOT cover (ambiguous-but-on-topic => correlated DIVERGENT same-fact).
# Default ON; DENDRA_JUDGE_SELFCONSIST=0 = 1-reference rollback (the old behavior = the documented bug).
SELFCONSIST = os.environ.get("DENDRA_JUDGE_SELFCONSIST", "1") != "0"
SC_TEMPERATURE = float(os.environ.get("DENDRA_JUDGE_SC_TEMP", "0.7"))  # temp of the REFS (the same-fact stays temp=0)
# A MISSING REVEAL IS NOT PROOF OF CHEATING: it is produced just as easily by an infrastructure fault
# (the relay keeps its pub cache in MEMORY, so a relay restart leaves honest miners structurally unable
# to seal a reveal). Voting "invalid" there charges the full cheat penalty for a fault the miner did not
# commit. The protocol already
# answers silence PROPORTIONATELY (abstention -> no-quorum -> payment reclaimed + calibrated
# silence_slash), which stays -EV for a real cheater. Default ON; =0 restores the old vote-0 behavior.
NOREVEAL_ABSTAIN = os.environ.get("DENDRA_JUDGE_NOREVEAL_ABSTAIN", "1") != "0"
# Layer A′: MULTIPLICITY stage at the slash threshold.
# Layer A misses prompts with multiple SHORT correct answers (judge's strong mode: r1=r2 stable -> "reliable" ref
# -> a valid-but-different answer slashed, correlated on a mono-model committee). A′ = the judge must affirm the
# question does NOT admit both answers before voting "invalid". Default ON; DENDRA_JUDGE_MULTIOK=0 = rollback
# (A/B measurement on the bench). Pro-honest mitigation, NOT the gate closure (heterogeneous Layer C = structural fix).
MULTIOK = os.environ.get("DENDRA_JUDGE_MULTIOK", "1") != "0"
# α-(a) -- ARMED BY DEFAULT: when
# AMBIGUITY is POSITIVELY detected (the judge disagrees with itself [sc-diverge] or multiple correct
# answers [multiok]), vote "1" (benefit of the doubt) INSTEAD of abstaining -> vindication -> the honest party keeps
# fee+bond+points (ends the no-quorum triple penalty: clawback + compounded silence_slash + points ×0 -- measured
# at ~52-59k lost PER JUDGE despite 0 hard slash). Rationale: on an intrinsically
# ambiguous prompt, "cheating" is undetectable by construction (no ground truth) -> vindicating is the only
# honest option, and the cheater is already paid on the ~90% not audited. NEVER on TECHNICAL undecidability
# (relevance/gen-fail/timeout: not a proven ambiguity, abstention stays correct).
# Rollback: DENDRA_JUDGE_ABSTAIN_VOTE=0.
ABSTAIN_VOTE = os.environ.get("DENDRA_JUDGE_ABSTAIN_VOTE", "1") == "1"
# TRACE per judged job (diagnostic not identifiable per job in the artifact).
# DENDRA_JUDGE_TRACE=<path|1> writes 1 JSONL line per job: verdict, exit stage, number of generations,
# sha256 of the prompt/answer/refs. INVARIANT #1 (no cleartext in the logs): the CONTENT is written ONLY if
# DENDRA_JUDGE_TRACE_PLAIN=1 (BENCH ONLY, never in prod). Default: OFF.
TRACE_DEST = os.environ.get("DENDRA_JUDGE_TRACE", "")
TRACE_PLAIN = os.environ.get("DENDRA_JUDGE_TRACE_PLAIN", "0") == "1"
# Per-job reliability under GPU load: BOUNDED retry of MY reference inference
# (a transient timeout must not make a verdict missing -> quorum below the floor), explicit abstention
# if undecidable (NEVER post a false VALID that would let a cheater escape).
GEN_RETRIES = max(1, int(os.environ.get("DENDRA_JUDGE_GEN_RETRIES", "3")))
GEN_DELAY = float(os.environ.get("DENDRA_JUDGE_GEN_DELAY", "2.0"))


def _node():
    return ["--node", NODE] if NODE else []


def run(c, t=600):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t)
    return (r.stdout or "") + (r.stderr or "")


def resolve_judge_model(explicit: str = ""):
    """Chooses THE SAME judge model for the whole committee by reading `audit_judge_model`
    PINNED on-chain (`dendrad query modelregistry params`), instead of a divergent local config.

    Returns (model, source). FALLBACK chain (decreasing priority):
      1. `explicit`        -> manual override (--model-id), source "cli"
      2. on-chain          -> `audit_judge_model` from the modelregistry, source "chain"
      3. env               -> DENDRA_JUDGE_MODEL_ID, source "env"
      4. default           -> DEFAULT_JUDGE_MODEL (pinned MoE "qwen3:30b-a3b-instruct-2507-q4_K_M",
                              cf. modea/judge.py; fallback box <26 GB RAM = mistral-nemo -- NEVER
                              qwen3:4b as a judge), source "default"

    ROBUST by design: never a hard-fail. dendrad absent, failed query, unreadable JSON
    or empty field -> we simply fall back to the next tier (the worker must run even
    when the chain is unreachable, as today)."""
    if explicit:
        return explicit, "cli"
    try:
        out = run(["dendrad", "query", "modelregistry", "params", "--output", "json", *_node()], t=20)
        d = json.loads(out)
        # QueryParamsResponse = {"params": {...}}; we also tolerate a top-level Params,
        # and both naming conventions (snake_case proto3 JSON / camelCase gogoproto).
        p = d.get("params", d) if isinstance(d, dict) else {}
        model = (p.get("audit_judge_model") or p.get("auditJudgeModel") or "").strip()
        if model:
            return model, "chain"
    except Exception:
        pass  # chain unreachable / dendrad absent / unreadable JSON -> fallback below
    env_model = os.environ.get("DENDRA_JUDGE_MODEL_ID", "").strip()
    if env_model:
        return env_model, "env"
    return DEFAULT_JUDGE_MODEL, "default"


def tx_from(frm, *a):
    # robust NONCE: account shared across processes -> retry on "account sequence mismatch"
    # (dendrad re-fetches the sequence each attempt -> passes once the in-flight tx is included). Bounded backoff.
    cmd = ["dendrad", "tx", "jobs", *a, "--from", frm, "--keyring-backend", "test",
           "--chain-id", CHAIN, "--gas", "auto", "--gas-adjustment", "1.6", "--yes", *_node()]
    o = ""
    for attempt in range(6):
        o = run(cmd)
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    return o


def query(*a):
    return run(["dendrad", "query", "jobs", *a, *_node()])


def _ok(t):
    m = re.search(r'(^|\n)code: (\d+)', t)
    return bool(m) and m.group(2) == "0"


def verdict_already_posted(vkey: str, want: str) -> bool:
    """Has THIS judge already committed `want` for `vkey`?

    The previous test was a BARE SUBSTRING match on the CLI output (`if "0" in query(...)`), and `run()`
    MERGES stdout+stderr — so any transport noise containing a '0' or a '1' ("connection refused", a gas
    figure, a bech32 address) read as "already posted". The judge then SKIPPED its vote: quorum missed
    -> no-quorum -> silence_slash on an HONEST primary: an idempotence guard must never be able to
    destroy a verdict.

    The fail direction is therefore deliberate: when we cannot parse a DEFINITE answer we return False
    and re-emit. Re-emitting is harmless (the chain refuses a duplicate commit); skipping silently
    destroys a verdict and punishes an honest miner.
    """
    out = query("get-commit", vkey, "--output", "json") or ""
    try:
        d = json.loads(out)
    except Exception:
        return False                      # unparsable / RPC error -> assume NOT posted, re-emit
    node = d.get("commit") if isinstance(d, dict) else None
    if not isinstance(node, dict):
        node = d if isinstance(d, dict) else {}
    for k in ("result", "resultCommit", "result_commit", "value"):
        v = node.get(k)
        if v is not None:
            return str(v).strip() == str(want)
    return False


def wait_tx(o, timeout=24):
    if not _ok(o):
        return False
    h = re.search(r'txhash:\s*([A-Fa-f0-9]{64})', o)
    if not h:
        return False
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return _ok(q)
        time.sleep(2)
    return False


def list_jobs():
    """[(job_id, state, miner_id)] via `list-job --output json` (the query EXISTS: query_job.go::ListJob)."""
    out = run(["dendrad", "query", "jobs", "list-job", "--output", "json", *_node()])
    try:
        d = json.loads(out)
    except Exception:
        return []
    res = []
    for j in d.get("job", []):
        res.append((j.get("job_id", ""), j.get("state", ""), j.get("miner_id", "")))
    return res


def list_miner_ids():
    out = run(["dendrad", "query", "jobs", "list-miner", "--output", "json", *_node()])
    try:
        d = json.loads(out)
    except Exception:
        return []
    return [m.get("miner_id", "") for m in d.get("miner", []) if m.get("miner_id")]


def is_disputed(state: str) -> bool:
    return "+disputed" in state and "+resolved" not in state


# A non-OK `adjudicate-dispute` has TWO very different meanings, and lumping them into one
# "différé/échec" message (while DISCARDING the chain's error) hid a C3-blocking condition:
#   deferred -> legitimate and expected: window still open, quorum not reached, already resolved.
#               Nothing to do, the next pass retries.
#   FAILED   -> real error (bad args, unfunded signer, signature...). If adjudication truly fails,
#               disputes NEVER close -> jobs_unresolved > 0 -> the C3 triplet is unreachable no matter
#               what else is fixed. This one must be LOUD and must carry the chain's own message.
_ADJ_DEFERRED_HINTS = ("window", "fenetre", "fenêtre", "quorum", "not disputed", "non disputé",
                       "already", "resolved", "too early", "pending", "not yet")


def adjudicate_outcome(out):
    """-> ("ok"|"deferred"|"FAILED", detail). Never swallows the chain's error text."""
    if _ok(out):
        return "ok", ""
    s = " ".join(str(out or "").split())[:400]
    low = s.lower()
    if not s:
        return "FAILED", "(no output from the tx)"
    return ("deferred", s) if any(h in low for h in _ADJ_DEFERRED_HINTS) else ("FAILED", s)


def decide_verdict(answer, prompt, backend, judge_model, twostage=True, gen_retries=3, gen_delay=2.0,
                   coherent_fn=None, judge_fn=None, relevant_fn=None, relevance=True,
                   selfconsist=True, sc_temperature=0.7, multiok=True, multiok_fn=None, trace=None):
    """Decides the verdict of an audited job. Returns True (VALID), False (INVALID/cheat) or None (ABSTENTION).

    4 STAGES:
      1. COHERENCE first on the PRIMARY's answer (anti-word-salad). Incoherent (mock garbage) -> False
         WITHOUT generating a ref. ORDER is the safety: garbage -- however "off-topic" -- stays slashed.
      2. RELEVANCE: TOTALLY off-topic vs the prompt -> ABSTENTION (None), without generating a ref.
         An ON-TOPIC lie continues to 3-4. Unreadable -> we proceed.
      3. SELF-CONSISTENCY (Layer A -- closes the ambiguous-BUT-on-topic that relevance lets through): I generate
         MY reference 2 times AT TEMPERATURE>0 (sc_temperature; the same-fact itself stays temp=0).
         (a) my 2 refs DIVERGE from each other -> I do not agree with MYSELF -> AMBIGUOUS question
             -> ABSTENTION (2 gen.) -- a whole committee would correlate on this same ambiguity, hence the veto
             that bit an honest party (vector proven by the GOLD negative artifacts);
         (b) 2 refs agree AND the answer agrees -> VALID (short-circuit, 2 gen.);
         (c) 2 refs agree BUT the answer diverges -> a 3rd ref CONFIRMS stability BEFORE slashing:
             3rd divergent -> not that stable -> ABSTENTION; 3 agreeing -> INVALID (cheater on a verifiable).
         Intentional PRO-HONEST ASYMMETRY: vindication early (2 gen.), slash only after confirmation (3 gen.).
      3-quater. MULTIPLICITY (Layer A′, after the FAILURE of the earlier gate at a 15-18% rate with Layer A active):
         at the slash THRESHOLD (3 agreeing refs + divergent answer, and there ONLY -- 0 call on (a)/(b)),
         the judge answers "does the question admit MULTIPLE correct answers including my ref AND the answer?"
         (llm_multi_ok, ANCHORED on both texts). YES or unreadable -> ABSTENTION; NO -> INVALID confirmed.
         Closes the Layer A hole: on "give a prime number", the judge is STABLE (r1=r2="7",
         strong mode at temp>0) -> it believed its ref reliable and slashed "13" though correct, CORRELATED
         on a mono-model committee (quorum veto). Self-consistency measures MY variance, not the multiplicity
         of correct answers. LIMITS (noted): monotonic loss of capture on an obscure "received idea"
         fact; the mono-model correlation is NOT closed (Layer C = structural fix).
      4. (rollback selfconsist=False = DENDRA_JUDGE_SELFCONSIST=0): a single reference -- the OLD
         behavior, documented as THE bug (a random ref on an ambiguous question -> false DIVERGENT).
    Layer A anti-evasion: the cheater only controls its ANSWER, not my generations (which start from the
    PROMPT fixed by the client) -> it cannot fabricate the ambiguity. Generalized abstention -> no-quorum ->
    payment reclaimed + silence_slash: always -EV. `*_fn` injectable (off-GPU selftests).
    `trace` (optional dict, instrumentation): filled with stage (EXIT stage:
    coherence|relevance|sc-diverge|sc-valid|sc-unstable|multiok|slash|legacy|gen-fail), refs (the
    generations), n_gen. No effect if None (default); the historical selftests stay intact."""
    coherent_fn = coherent_fn or llm_coherent
    judge_fn = judge_fn or llm_judge
    relevant_fn = relevant_fn or llm_relevant
    multiok_fn = multiok_fn or llm_multi_ok
    refs = []

    def _t(stage):  # instrumentation: EXIT stage + successful generations (no effect if trace is None)
        if trace is not None:
            trace["stage"] = stage
            trace["refs"] = list(refs)
            trace["n_gen"] = len(refs)

    if twostage and coherent_fn(answer, model=judge_model) is False:
        _t("coherence")
        return False
    if relevance and relevant_fn(prompt, answer, model=judge_model) is False:
        _t("relevance")
        return None  # pro-honest ABSTENTION: off-topic-vs-prompt is NOT proof of cheating

    def _gen():  # one ref generation, retry on EXCEPTION only (GPU timeout); temp>0 (sampled)
        for attempt in range(gen_retries):
            try:
                out = backend.generate(prompt, temperature=sc_temperature)
                refs.append(out)
                return out
            except Exception:
                if attempt + 1 < gen_retries:
                    time.sleep(gen_delay)
        return None

    if not selfconsist:  # ROLLBACK: 1-reference (the old bug, kept for A/B measurement on the hardened bench)
        mine = _gen()
        _t("legacy")
        return None if mine is None else bool(judge_fn(mine, answer, model=judge_model))

    r1 = _gen()
    if r1 is None:
        _t("gen-fail")
        return None
    r2 = _gen()
    if r2 is None:
        _t("gen-fail")
        return None
    if not judge_fn(r1, r2, model=judge_model):
        _t("sc-diverge")
        return None  # (a) I diverge from myself -> ambiguous -> ABSTENTION
    if judge_fn(r1, answer, model=judge_model):
        _t("sc-valid")
        return True  # (b) stable ref + agreeing answer -> VALID
    r3 = _gen()      # (c) confirmation BEFORE slashing
    if r3 is None:
        _t("gen-fail")
        return None
    if not judge_fn(r1, r3, model=judge_model):
        _t("sc-unstable")
        return None  # 3rd ref diverges -> not that stable -> ABSTENTION
    # (3-quater) Layer A′ -- LAST pro-honest line of defense BEFORE the "invalid" vote: the judge must
    # affirm that the question does NOT admit both my ref and the answer as correct answers (anchored).
    # YES (both correct -> multiple answers: my stable ref proves nothing) or unreadable -> ABSTENTION.
    if multiok:
        both = multiok_fn(prompt, r1, answer, model=judge_model)
        if both is not False:
            _t("multiok")
            return None
    _t("slash")
    return False     # 3 refs agree, divergent answer, no multiplicity -> INVALID


def _selftest():
    """Verifies the LOGIC of stages 1-2 + the LEGACY 1-ref path of decide_verdict, without Ollama or chain
    (injected stubs; the cases that generate use selfconsist=False = legacy path, Layer A having its
    own selftest `--selftest-sc`). `python3 judge_worker.py --selftest`."""
    class FB:  # simulated backend: fails `fail` times then returns `out`
        def __init__(self, fail=0, out="mine"):
            self.calls = 0; self.fail = fail; self.out = out
        def generate(self, _, temperature=None):
            self.calls += 1
            if self.calls <= self.fail:
                raise RuntimeError("timeout GPU simulé")
            return self.out
    coh_t = lambda *a, **k: True
    coh_f = lambda *a, **k: False
    coh_none = lambda *a, **k: None
    jv = lambda *a, **k: True   # same-fact VALID
    jf = lambda *a, **k: False  # same-fact DIVERGENT (cheater)
    rel_t = lambda *a, **k: True    # relevance: the answer addresses the prompt
    rel_f = lambda *a, **k: False   # relevance: clearly OFF-TOPIC
    rel_none = lambda *a, **k: None  # relevance unreadable -> does not block
    LG = {"selfconsist": False}  # LEGACY 1-ref path (Layer A = --selftest-sc)
    cases = []
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("coherent+pertinent+valid -> True, 1 infer", r is True and b.calls == 1))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_f, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("incoherent -> False SANS infer", r is False and b.calls == 0))
    b = FB(fail=99); r = decide_verdict("a", "p", b, "m", gen_retries=3, gen_delay=0, coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("infer KO -> abstain(None), 3 essais", r is None and b.calls == 3))
    b = FB(fail=2); r = decide_verdict("a", "p", b, "m", gen_retries=3, gen_delay=0, coherent_fn=coh_t, judge_fn=jf, relevant_fn=rel_t, **LG)
    cases.append(("retry puis tricheur -> False, 3 essais", r is False and b.calls == 3))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_none, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("coherence None -> juge le fait", r is True and b.calls == 1))
    b = FB(); seen = {"c": 0}
    def coh_count(*a, **k):
        seen["c"] += 1; return False
    r = decide_verdict("a", "p", b, "m", twostage=False, coherent_fn=coh_count, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("twostage OFF -> cohérence ignorée", r is True and seen["c"] == 0 and b.calls == 1))
    # RELEVANCE guard:
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jf, relevant_fn=rel_f, **LG)
    cases.append(("hors-sujet COHERENT -> ABSTENTION sans infer (pas de faux invalide)", r is None and b.calls == 0))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_f, judge_fn=jv, relevant_fn=rel_f, **LG)
    cases.append(("garbage incoherent + hors-sujet -> False (coherence PRIME, le mock reste slashé)", r is False and b.calls == 0))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_none, **LG)
    cases.append(("pertinence illisible -> pipeline continue (True)", r is True and b.calls == 1))
    b = FB(); seen_r = {"n": 0}
    def rel_count(*a, **k):
        seen_r["n"] += 1; return False
    r = decide_verdict("a", "p", b, "m", relevance=False, coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_count, **LG)
    cases.append(("relevance OFF -> garde ignorée (rollback strict)", r is True and seen_r["n"] == 0 and b.calls == 1))
    ok = True
    for name, p in cases:
        ok = ok and p
        print(f"  [{'OK' if p else 'FAIL'}] {name}")
    print("SELFTEST decide_verdict :", "VERT" if ok else "ROUGE")
    return 0 if ok else 1


def _selftest_selfconsist():
    """Layer A -- self-consistency. No Ollama/chain: sequential backend +
    stub judge. Verifies (verdict, number of generations) on 9 cases. `python3 judge_worker.py --selftest-sc`."""

    class SeqBackend:
        """Returns a pre-defined sequence (simulates temp>0 sampling). Counts the calls.
        An Exception element is raised (tests the timeout retry)."""
        def __init__(self, gens):
            self.gens = list(gens); self.i = 0; self.calls = 0
        def generate(self, prompt, temperature=0.0):
            self.calls += 1
            g = self.gens[self.i] if self.i < len(self.gens) else self.gens[-1]
            self.i += 1
            if isinstance(g, BaseException):
                raise g
            return g

    coh_t = lambda *a, **k: True
    coh_f = lambda *a, **k: False
    rel_t = lambda *a, **k: True
    rel_f = lambda *a, **k: False
    sf = lambda a, b, **k: a == b          # same-fact stub: equal string = same fact
    mko_t = lambda *a, **k: True           # multiplicity (A′): both answers correct
    mko_n = lambda *a, **k: None           # multiplicity unreadable
    mko_seen = {"n": 0}
    def mko_count(*a, **k):
        mko_seen["n"] += 1; return False

    # (label, gens, answer, kwargs, expected verdict, expected calls)
    # Cases 1-9 (verbatim, UNCHANGED outputs); the common kwargs stub multiok_fn=False
    # makes the A′ stage NEUTRAL for them (case 2 traverses it and slashes as before).
    # Cases 10-13 = Layer A′: multiplicity at the slash threshold.
    cases = [
        ("1 factuel VALIDE (réfs stables, réponse concorde)",
         ["Paris", "Paris"], "Paris", {}, True, 2),
        ("2 factuel TRICHEUR (réfs stables, réponse diverge, 3e confirme)",
         ["Paris", "Paris", "Paris"], "Berlin", {}, False, 3),
        ("3 AMBIGU, réponse diverge (2 réfs divergent → abstention)",
         ["Python", "Rust"], "JavaScript", {}, None, 2),
        ("4 AMBIGU, réponse = une réf (réfs divergent → abstention quand même)",
         ["Python", "Rust"], "Rust", {}, None, 2),
        ("5 quasi-slash SAUVÉ : 2 concordent, réponse diverge, 3e DIVERGE → abstention",
         ["Paris", "Paris", "Lyon"], "Berlin", {}, None, 3),
        ("6 COHÉRENCE d'abord : garbage → INVALIDE, 0 génération",
         ["x"], "garbage", {"coherent_fn": coh_f}, False, 0),
        ("7 PERTINENCE : hors-sujet total → abstention, 0 génération",
         ["x"], "hors-sujet", {"relevant_fn": rel_f}, None, 0),
        ("8 ROLLBACK selfconsist=0 : 1 réf, réponse diverge → INVALIDE (l'ANCIEN bug, documenté)",
         ["Python"], "JavaScript", {"selfconsist": False}, False, 1),
        ("9 TIMEOUT GPU sur la 1re réf (3 essais) → abstention",
         [TimeoutError(), TimeoutError(), TimeoutError()], "Paris", {}, None, 3),
        ("10 A′ RÉPONSES MULTIPLES : réfs stables «7», réponse «13», multi_ok=OUI → ABSTENTION (pas de slash)",
         ["7", "7"], "13", {"multiok_fn": mko_t}, None, 3),
        ("11 A′ consulté au seuil : tricheur vrai-faux, multi_ok=NON → INVALIDE (capture préservée)",
         ["Paris", "Paris", "Paris"], "Berlin", {"multiok_fn": mko_count}, False, 3),
        ("12 A′ ILLISIBLE → abstention pro-honnête (fail-open assumé, consigné)",
         ["7", "7", "7"], "13", {"multiok_fn": mko_n}, None, 3),
        ("13 A′ ROLLBACK multiok=0 : slash direct, multi_ok JAMAIS appelé",
         ["7", "7", "7"], "13", {"multiok": False, "multiok_fn": mko_count}, False, 3),
    ]

    ok = True
    for label, gens, ans, kw, want_v, want_c in cases:
        b = SeqBackend(gens)
        kwargs = dict(coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                      relevance=True, selfconsist=True, gen_delay=0,   # gen_delay=0: fast test
                      multiok_fn=lambda *a, **k: False)                # A′ NEUTRAL by default (cases 1-9 verbatim)
        kwargs.update(kw)
        got_v = decide_verdict(ans, "prompt", b, "m", **kwargs)
        good = (got_v is want_v) and (b.calls == want_c)  # `is`: strict True/False/None
        ok = ok and good
        print(f"  [{'OK' if good else 'FAIL'}] {label} -> verdict={got_v} (att {want_v}), gén={b.calls} (att {want_c})")
    # 11-bis: mko_count called EXACTLY 1× in case 11 (the stage is indeed consulted at the threshold), 0× in case 13 (rollback).
    good = mko_seen["n"] == 1
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 11-bis/13-bis multi_ok consulté 1× au seuil, 0× en rollback (obtenu {mko_seen['n']})")
    # 14: TRACE (instrumentation): exit stage + number of generations exposed.
    tr = {}
    b = SeqBackend(["7", "7"])
    got = decide_verdict("13", "prompt", b, "m", coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                         relevance=True, selfconsist=True, gen_delay=0, multiok_fn=mko_t, trace=tr)
    good = got is None and tr.get("stage") == "multiok" and tr.get("n_gen") == 3 and len(tr.get("refs", [])) == 3
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 14 trace remplie -> stage={tr.get('stage')} (att multiok), n_gen={tr.get('n_gen')} (att 3)")
    # 15: option α-(a) abstain_to_vote (PURE) -- vote "1" ONLY if armed AND ambiguity PROVEN.
    good = (abstain_to_vote("sc-diverge", True) is True and abstain_to_vote("multiok", True) is True
            and abstain_to_vote("relevance", True) is False and abstain_to_vote("gen-fail", True) is False
            and abstain_to_vote("sc-diverge", False) is False and abstain_to_vote("", True) is False)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 15 α-(a) abstain_to_vote : armée+ambigu=vote1, technique/désarmée=abstention")
    print("SELFTEST COUCHE A", "VERT" if ok else "ROUGE")
    return 0 if ok else 1


def abstain_to_vote(stage, enabled):
    """Option α-(a) (PURE, offline-testable): should an abstention become a "1" vote?
    True ONLY if the flag is armed AND the stage proves an AMBIGUITY (sc-diverge: the judge diverges from
    itself; multiok: multiple correct answers). relevance / gen-fail / others = technical
    undecidability -> we keep the abstention (never a false VALID out of complacency)."""
    return bool(enabled) and stage in ("sc-diverge", "multiok")


def _sha(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:16]


def _trace_write(dest, rec):
    """1 JSONL line per judged job (bench instrumentation). Best-effort: a trace
    that fails must NEVER prevent a verdict. INVARIANT #1: hashes only unless DENDRA_JUDGE_TRACE_PLAIN=1."""
    try:
        with open(dest, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[judge] trace non écrite ({type(e).__name__}) — verdict non affecté")


def main():
    if "--selftest" in sys.argv:  # off-GPU logic check, short-circuits the required args
        sys.exit(_selftest())
    if "--selftest-sc" in sys.argv:  # Layer A (self-consistency), off-GPU
        sys.exit(_selftest_selfconsist())
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="mineur enregistré qui agit comme membre du comité")
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--adjudicate", action="store_true", help="tenter adjudicate-dispute après le verdict (best-effort)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reveal-grace", type=int, default=5,
                    help="ADR-028 : nb de tentatives d'ouverture de la révélation avant de poster un verdict 0 "
                         "(un primaire muet/qui révèle du vide doit être jugé INVALIDE, pas innocenté)")
    ap.add_argument("--model-id", default="",
                    help="ADR-027 D4 : override MANUEL du modèle-juge. Par défaut le worker lit le modèle "
                         "ÉPINGLÉ on-chain (modelregistry.audit_judge_model) pour que tout le comité juge "
                         "avec le MÊME modèle ; ne le forcer que pour debug/tests.")
    a = ap.parse_args()

    passphrase = os.environ.get("DENDRA_MINER_PASSPHRASE", "")
    skpath = Path(a.keydir) / f"{a.id}.sk"
    if not skpath.exists():
        print(f"[judge] FATAL : clé X25519 {skpath} absente — lance d'abord miner.py --id {a.id}")
        sys.exit(3)
    sk = crypto.load_sk(str(skpath), passphrase)

    # inference backend to COMPUTE my own reference answer (the judge compares to mine).
    try:
        backend = Miner(a.id, backend="ollama").backend
        backend.generate("ok")
    except Exception as e:
        print(f"[judge] FATAL : Ollama injoignable ({type(e).__name__}) — le comité doit pouvoir inférer.")
        sys.exit(3)

    # consensual judge model resolved ONCE at startup (transparency + committee consistency).
    judge_model, judge_src = resolve_judge_model(a.model_id)
    # Layer C: the on-chain VERDICT ALWAYS carries the judge's model
    # (`Commit.ModelId` already exists in the proto -- verified msg_server_commit.go:93). Before: --model-id
    # was set only if env DENDRA_JUDGE_MODEL_ID -> the bench verdicts went out WITHOUT a model -> the
    # DIVERSITY tally (min_distinct_judge_models) would have had nothing to count. Now:
    # explicit env > resolved model. Zero data regeneration needed.
    flags = ["--model-id", (JUDGE_MODEL_ID or judge_model)]
    src_label = {"cli": "override --model-id", "chain": "on-chain (modelregistry.audit_judge_model)",
                 "env": "env DENDRA_JUDGE_MODEL_ID", "default": f"défaut judge.py ({DEFAULT_JUDGE_MODEL})"}
    print(f"[judge] modèle-juge = '{judge_model}' (source : {src_label.get(judge_src, judge_src)})")
    if judge_src == "default":
        print("[judge] AVERTISSEMENT : modèle-juge non épinglé on-chain — "
              "le comité risque des verdicts incohérents si les membres divergent (ADR-027 D4).")
    print(f"[judge] worker comité {a.id} prêt (relais {a.relay}) — juge les jobs +disputed")
    trace_dest = ""
    if TRACE_DEST:
        trace_dest = f"/tmp/judge-trace-{a.id}.jsonl" if TRACE_DEST == "1" else TRACE_DEST
        print(f"[judge] TRACE par job -> {trace_dest} ({'CLAIR (banc uniquement !)' if TRACE_PLAIN else 'hashes seuls'})")
    done = set()
    miss = {}  # job_id -> number of reveal-open failures (grace window)
    while True:
        try:
            # TWO PASSES: the "0" verdicts on ABSENT REVEAL are
            # FAST (one relay GET + one tx, NO inference); full judgments are SLOW (minutes
            # of generations). Handling them in a single queue made the "0" wait behind the
            # generations -> the audit timeout fired before quorum -> a MUTE resolved as no-quorum
            # (silence_slash) instead of the HARD slash. Pass 1 = triage + grace/immediate "0";
            # pass 2 = judgments. In PROD too: posting the "0" fast = fewer no-quorum on the mute ones.
            slow = []
            for job_id, state, primary in list_jobs():
                if not is_disputed(state) or job_id in done or primary == a.id:
                    continue
                if not rv.safe_job_id(job_id):   # job_id from on-chain -> dendrad argv + relay key
                    print(f"[judge] {a.id} job_id NON CONFORME ignoré (défense argv) : {str(job_id)[:40]!r}")
                    done.add(job_id)
                    continue
                rev = rv.open_reveal(a.relay, job_id, a.id, sk)
                if not rev or "prompt" not in rev:
                    # no EXPLOITABLE reveal. We leave a GRACE window for the honest primary
                    # (slow network), then we POST a "0" verdict (INVALID): a mute primary or one that reveals
                    # nothing is thus caught in QUORUM-CHEAT by the committee (non-falsifiable signal) -> hard slash,
                    # instead of being cleared by the timeout. This is what closes evasion by non-revelation.
                    miss[job_id] = miss.get(job_id, 0) + 1
                    if miss[job_id] < a.reveal_grace:
                        continue  # still within the grace window -> we retry
                    if NOREVEAL_ABSTAIN:
                        # Silence is handled by no-quorum + silence_slash, NOT by a cheat verdict we
                        # cannot justify (an absent reveal is indistinguishable from a relay fault).
                        print(f"[judge] {a.id} ABSTENTION (revelation absente apres {miss[job_id]} essais) "
                              f"pour {job_id} — silence traite par no-quorum + silence_slash")
                        if trace_dest:
                            _trace_write(trace_dest, {"ts": int(time.time()), "job_id": job_id,
                                                      "verdict": "abstain", "stage": "no-reveal", "n_gen": 0})
                        done.add(job_id)
                        continue
                    vkey = f"{job_id}__verdict__{a.id}"
                    if verdict_already_posted(vkey, "0"):
                        done.add(job_id)
                        continue
                    out = tx_from(a.id, "create-commit", vkey, "0", "0", "verdict", *flags)
                    if wait_tx(out):
                        print(f"[judge] {a.id} verdict=0 (RÉVÉLATION ABSENTE après {miss[job_id]} essais) pour {job_id}")
                        if trace_dest:
                            _trace_write(trace_dest, {"ts": int(time.time()), "job_id": job_id,
                                                      "verdict": "0", "stage": "no-reveal", "n_gen": 0})
                        done.add(job_id)
                        if a.adjudicate:
                            tx_from(a.id, "adjudicate-dispute", "--job-id", job_id)
                    continue
                slow.append((job_id, rev))
            for job_id, rev in slow:
                # 4-STAGE (coherence-first + relevance + self-consistency + multiplicity, cf.
                # decide_verdict). ref = MY answer (presumed correct), cand = the PRIMARY's answer.
                tr = {}   # always filled (stage required by α-(a)); the JSONL WRITE stays gated by trace_dest
                verdict = decide_verdict(rev["answer"], rev["prompt"], backend, judge_model,
                                         twostage=TWOSTAGE, gen_retries=GEN_RETRIES, gen_delay=GEN_DELAY,
                                         relevance=RELEVANCE, selfconsist=SELFCONSIST,
                                         sc_temperature=SC_TEMPERATURE, multiok=MULTIOK, trace=tr)
                if trace_dest:
                    rec = {"ts": int(time.time()), "job_id": job_id,
                           "verdict": {True: "1", False: "0", None: "abstain"}[verdict],
                           "stage": tr.get("stage", "?"), "n_gen": tr.get("n_gen", 0),
                           "prompt_sha": _sha(rev.get("prompt")), "answer_sha": _sha(rev.get("answer")),
                           "refs_sha": [_sha(r) for r in tr.get("refs", [])]}
                    if TRACE_PLAIN:  # BENCH ONLY (invariant #1: never cleartext in prod)
                        rec.update({"prompt": rev.get("prompt"), "answer": rev.get("answer"),
                                    "refs": tr.get("refs", [])})
                    _trace_write(trace_dest, rec)
                if verdict is None:
                    if abstain_to_vote(tr.get("stage", ""), ABSTAIN_VOTE):
                        # α-(a) ARMED: ambiguity PROVEN -> benefit of the doubt -> vote "1" (vindication).
                        print(f"[judge] {a.id} AMBIGU job {job_id} (étage {tr.get('stage')}) -> vote VALIDE "
                              f"(α-(a) bénéfice du doute, DENDRA_JUDGE_ABSTAIN_VOTE=1)")
                        verdict = True
                    else:
                        print(f"[judge] {a.id} ABSTENTION job {job_id} (étage {tr.get('stage', 'n/a')} : "
                              f"hors-sujet-vs-prompt [O1-2], juge en désaccord avec lui-même [Couche A], réponses "
                              f"multiples correctes [A′], ou inférence indécidable) — pas de verdict douteux")
                        continue
                valid = verdict
                v = verdict_commit(valid)
                vkey = f"{job_id}__verdict__{a.id}"
                if verdict_already_posted(vkey, v):
                    done.add(job_id)
                    continue
                out = tx_from(a.id, "create-commit", vkey, v, v, "verdict", *flags)
                if wait_tx(out):
                    print(f"[judge] {a.id} verdict={v} ({'OK' if valid else 'DIVERGENT'}) commit pour {job_id}")
                    done.add(job_id)
                    if a.adjudicate:
                        adj = tx_from(a.id, "adjudicate-dispute", "--job-id", job_id)
                        _st, _detail = adjudicate_outcome(adj)
                        if _st == "ok":
                            print(f"[judge] {a.id} adjudicate-dispute {job_id} -> OK (dispute close)")
                        elif _st == "deferred":
                            print(f"[judge] {a.id} adjudicate-dispute {job_id} -> differe (normal) : {_detail[:140]}")
                        else:
                            print(f"[judge] {a.id} adjudicate-dispute {job_id} -> ECHEC REEL — les disputes ne se "
                                  f"fermeront pas (jobs_unresolved>0 = C3 impossible) : {_detail}")
                else:
                    print(f"[judge] {a.id} verdict NON ancré pour {job_id} (réessai) ; "
                          f"si enforce_model_registry=ON, définir DENDRA_JUDGE_MODEL_ID")
        except Exception as e:
            print(f"[judge] {a.id} boucle: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
