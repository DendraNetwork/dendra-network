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
# Needed to tell a REFUSAL (HTTP 4xx/5xx, the backend answered) apart from an OUTAGE (no answer at
# all). Conflating them printed "Ollama unreachable" over a 404 and sent the search to the network.
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea.exposition import DrapeauInvalide, public_actif
from modea.miner import Miner
from modea.judge import (llm_judge, llm_coherent, llm_relevant, llm_multi_ok, verdict_commit,
                         DEFAULT_JUDGE_MODEL)
import relay_client as relay_c
import reveal_helpers as rv
import reveal_marker as rmk

CHAIN = "dendra"
NODE = os.environ.get("DENDRA_NODE", "")
# EXPLICIT keyring directory. Without these flags, `dendrad` looks for the keys in ITS OWN default home.
# The Docker onboarding kit sets `DENDRA_KEYRING_DIR=/data/keys/cosmos`
# (`deploy/testnet-miner/docker-compose.yml`), outside the `dendrad` home -> every
# `create-commit <job>__verdict__<id>` and every `adjudicate-dispute` fails with "key not found".
# The judge then infers (burning a 19 GB MoE), anchors NOTHING, and its jury seat stays MUTE: the
# ⌈2/3 of anchored seats⌉ bar rises for the whole network while no vote ever lands.
# EMPTY variables => no flag => the behavior of judges already in service is strictly UNCHANGED.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")
HOME_DIR = os.environ.get("DENDRA_HOME", "")


def _keys():
    """Keyring flags, empty when the environment does not set them (fallback = default dendrad home)."""
    out = []
    if KEYRING_DIR:
        out += ["--keyring-dir", KEYRING_DIR]
    if HOME_DIR:
        out += ["--home", HOME_DIR]
    return out

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
# ABSENT REVEAL -> ABSTENTION, and the abstention claims NO compensating mechanism behind it.
# There is no clawback-on-silence and no `silence_slash_bps`: the mute primary is only reachable
# through the slash branch, which requires ⌈2/3⌉ of "0" verdicts POSTED — exactly what an abstaining
# worker does not post. Consequence, stated plainly rather than hidden: a primary that takes jobs and
# NEVER reveals suffers nothing here, and the client fee stays held. A guard justified by a mechanism
# that lives elsewhere is a guard that can be silently removed from under it; the comment is part of
# the security surface.
#
# WHAT REMAINS TRUE: an absent reveal is NOT proof of cheating — an infrastructure outage produces
# exactly the same observation. Voting "invalid" on it would bill the cheater's full penalty to a
# fault the miner did not commit (invariant ①).
# WHAT REPLACES IT (two stages): do not harden the threshold, make the two states DISTINGUISHABLE.
# Stage 1 = reveal marker anchored on-chain, batched per epoch (its absence cannot be blamed on the
# relay, since anchoring does not go through the relay). Stage 2 = CORRELATION test, zero transactions:
# a relay outage hits ALL primaries, a mute primary is ISOLATED. See `noreveal_verdict()`.
# As long as stage 1 is not anchored, abstention stays the default. Default ON; =0 restores the
# unconditional 0-vote.
NOREVEAL_ABSTAIN = os.environ.get("DENDRA_JUDGE_NOREVEAL_ABSTAIN", "1") != "0"
# THE 2 STAGES — DISARMED BY DEFAULT, and that is not decorative caution. Arming them opens a "0"
# vote on an absent reveal, hence a possible slash: that is an economic decision, and it must be an
# explicit, traceable authorization, never an inference. Additional safety independent of this flag:
# `marker_stage_usable` disarms stage 1 as long as no primary anchors — a partial rollout therefore
# cannot produce a mass slash.
TWOSTAGE_NOREVEAL = os.environ.get("DENDRA_JUDGE_TWOSTAGE_NOREVEAL", "0") == "1"
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
           "--chain-id", CHAIN, "--gas", "auto", "--gas-adjustment", "1.6", "--yes",
           *_keys(), *_node()]
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


def _norm_keys(d):
    """Adds snake_case aliases for the camelCase keys of a flat CLI dict (jobId->job_id,
    minerId->miner_id, encPubkey->enc_pubkey, State->state). dendrad may emit camelCase; parsing that
    reads snake_case ONLY then sees ZERO jobs -> audits never resolved and an honest miner slashed over
    a JSON casing difference. Normalizing at the edge tolerates both conventions."""
    if not isinstance(d, dict):
        return d
    out = dict(d)
    for k, v in list(d.items()):
        snake = ""
        for ch in k:
            snake += ("_" + ch.lower()) if ch.isupper() else ch
        snake = snake.lstrip("_")
        if snake and snake not in out:
            out[snake] = v
    return out


def list_jobs():
    """[(job_id, state, miner_id)] via `list-job --output json` (the query EXISTS: query_job.go::ListJob)."""
    rows = list_jobs_full()
    return None if rows is None else [(j, s, m) for j, s, m, _h in rows]


def list_jobs_full():
    """[(job_id, state, miner_id, dispute_height)] — same query, one more field.

    `dispute_height` is required by STAGE 1: it determines the epochs in which the reveal marker may
    legitimately be found. Deriving it from the CURRENT height would be wrong for a long-disputed job,
    and a marker looked up in the wrong epoch reads as "absent" — that is, a slash on an honest primary.
    """
    # PAGINATED, and `None` when the read failed. Without pagination this call stops at 100 jobs
    # (SDK `DefaultLimit`); beyond that the judge would SILENTLY stop seeing part of the audits, and on
    # the wrong side: truncation only ever produces good news. `rv.query_all` lives in the module BOTH
    # workers import; copying it here would create a second source of truth that drifts.
    rows = rv.query_all("list-job", "job", _node(), run)
    if rows is None:
        return None
    res = []
    for j in rows:
        j = _norm_keys(j)
        try:
            dh = int(j.get("dispute_height") or 0)
        except (TypeError, ValueError):
            dh = 0
        res.append((j.get("job_id", ""), j.get("state", ""), j.get("miner_id", ""), dh))
    return res


def anchored_root(key: str) -> str:
    """Root anchored by a `create-commit` marker, or "" when there is none. Fail closed on unreadable
    output: something we cannot parse is NOT a present marker — otherwise a transport incident would
    clear a mute primary."""
    out = query("get-commit", key, "--output", "json") or ""
    try:
        d = json.loads(out)
    except Exception:
        return ""
    node = d.get("commit") if isinstance(d, dict) else None
    if not isinstance(node, dict):
        node = d if isinstance(d, dict) else {}
    for k in ("result", "resultCommit", "result_commit", "value"):
        v = node.get(k)
        if v is not None:
            return str(v).strip()
    return ""


def primary_has_marker(primary, dispute_height, *, get_commit, epoch_blocks=None, span=2) -> bool:
    """Has this primary anchored AT LEAST ONE marker over the epochs covering this job?

    Used only by the rollout guard (`marker_stage_usable`): if NOBODY anchors, stage 1 is not deployed
    and its "absence" proves nothing about anyone."""
    eb = rmk.EPOCH_BLOCKS_DEFAULT if epoch_blocks is None else epoch_blocks
    return any((get_commit(rmk.marker_key(e, primary)) or "").strip()
               for e in rmk.covering_epochs(dispute_height, eb, span))


def list_miner_ids():
    # Same defect as `list-job`: without pagination the query stops at 100. The `[]` fallback is kept
    # deliberately here — this reader decides nothing, it only names peers — but it no longer returns a
    # PREFIX passed off as the whole list.
    rows = rv.query_all("list-miner", "miner", _node(), run)
    if rows is None:
        return []
    return [mm.get("miner_id", "") for mm in (_norm_keys(m) for m in rows) if mm.get("miner_id")]


def is_disputed(state: str) -> bool:
    return "+disputed" in state and "+resolved" not in state


# A non-OK `adjudicate-dispute` has TWO very different meanings, and lumping them into one
# "deferred/failed" message (while DISCARDING the chain's error) hides a blocking condition:
#   deferred -> legitimate and expected: window still open, quorum not reached, already resolved.
#               Nothing to do, the next pass retries.
#   FAILED   -> real error (bad args, unfunded signer, signature...). If adjudication truly fails,
#               disputes NEVER close -> jobs_unresolved > 0 -> the C3 triplet is unreachable no matter
#               what else is fixed. This one must be LOUD and must carry the chain's own message.
# These are MATCH PATTERNS against the node's own error text, not messages of ours. The French spellings
# stay: they cost nothing, and dropping them would reclassify a node that still answers in French as a
# REAL FAILURE — turning an expected deferral into a loud alarm on a chain that is behaving correctly.
_ADJ_DEFERRED_HINTS = ("window", "fenetre", "fenêtre", "quorum", "not disputed", "non disputé",
                       "already", "resolved", "too early", "pending", "not yet")


def _confirm_tx_text(out, timeout=24):
    """Confirms the EXECUTION of a broadcast tx via `query tx`. Returns (ok, block_text):
    ok=True iff the tx is included AND its EXECUTION code is 0; `block_text` = the `query tx` output
    (carries the raw_log, i.e. the failure REASON at block level), empty if never included. A broadcast
    `code: 0` only means "accepted into the mempool" and must never be read as success."""
    if not _ok(out):
        return False, ""
    h = re.search(r'txhash:\s*([A-Fa-f0-9]{64})', out)
    if not h:
        return False, ""
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return _ok(q), q   # included: ok per the EXECUTION code, and we return the block TEXT
        time.sleep(2)
    return False, ""           # never included (timeout)


def tx_reason(text):
    """Extracts (code, raw_log) from a `query tx` output. -> (int|None, str).

    `raw_log` is the ONLY field carrying the REASON, and dendrad's YAML output places it AFTER
    `events:` — a list thousands of characters long. Truncating the block text from the head (a `[:400]`
    window) therefore discards the reason EVERY TIME: a search for "already resolved" in that window
    cannot succeed by construction, and every dispute closed by another juror is then reported as a
    real failure. When looking for a field, use a NAMED extractor, never a positional truncation."""
    t = str(text or "")
    m = re.search(r'(?:^|\n)raw_log:\s*(.*?)(?=\n[a-z_]+:|\Z)', t, re.S)
    raw = " ".join((m.group(1) if m else "").split()).strip("'\" ")
    c = re.search(r'(?:^|\n)code:\s*"?(\d+)"?', t)
    return (int(c.group(1)) if c else None), raw


def marker_state(job_id, primary, dispute_height, *, get_commit, get_list,
                 epoch_blocks=None, span=2):
    """STAGE 1, judge side: has this primary anchored a marker covering this job? -> (state, reason).

    `True` = the job is present in an anchored marker AND verified against its on-chain root.
    `False` = the primary anchored nothing covering this job — an absence that CANNOT be blamed on the
              relay, since anchoring does not go through it. This is the only case where "0" is
              justified without further evidence.
    `None`  = UNDECIDABLE (root anchored but list missing or mismatching). No conclusion is drawn:
              stage 2 decides. An unavailability must never turn into a slash.

    The readers (`get_commit`, `get_list`) are INJECTED so the decision is testable without a chain or
    a relay, and so a production case can be replayed exactly.
    """
    eb = rmk.EPOCH_BLOCKS_DEFAULT if epoch_blocks is None else epoch_blocks
    ancre = False
    for e in rmk.covering_epochs(dispute_height, eb, span):
        root = (get_commit(rmk.marker_key(e, primary)) or "").strip().lower()
        if not root:
            continue
        ancre = True
        payload = get_list(rmk.list_key(e, primary))
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None, (f"marker anchored for epoch {e} but LIST NOT FOUND: membership can neither "
                          "be confirmed nor denied -> stage 2")
        if rmk.merkle_root(jobs) != root:
            return None, (f"the list for epoch {e} DOES NOT MATCH the anchored root: unreliable data, "
                          "no conclusion drawn -> stage 2")
        if job_id in jobs:
            return True, f"job present in the anchored marker of epoch {e} (root verified)"
    if ancre:
        return False, ("the primary anchored markers over the window, but NONE of them contains this "
                       "job: it revealed for others and not for this one")
    return False, "NO marker anchored by this primary over the epochs covering this job"


def marker_stage_usable(n_primaries_with_marker):
    """Is stage 1 USABLE on this network? -> bool.

    Rollout guard. On the day stage 1 is armed, no primary yet runs the version that anchors markers:
    "marker absent" would then be true for EVERYONE, and the committee would vote "0" against
    perfectly honest primaries — a mass slash caused by an incomplete rollout, not by cheating. This
    is stage 2's own reasoning applied to stage 1 itself: a universal absence is CORRELATED, therefore
    not attributable to an individual. As long as no primary anchors, stage 1 is declared unavailable
    and everything falls back to stage 2. It therefore arms itself as the rollout progresses, with no
    date and no switch to remember.
    """
    return int(n_primaries_with_marker) >= 1


def noreveal_verdict(job_id, primary, *, marker, others_ok, others_missing, my_missing_for_primary,
                     min_others=2):
    """THE 2 STAGES, as ONE pure and testable function. -> ("0"|"abstention", reason).
    Posts nothing, reads nothing: it DECIDES, and it is tested on its own.

    THE PROBLEM IT SOLVES. "Absent reveal" covers two opposite worlds: a primary that did nothing
    (cheating) and an infrastructure outage (invariant ①: never billed to the honest party). Hardening
    a threshold does not separate them — the two states must be made DISTINGUISHABLE.

    STAGE 1 — ANCHORED MARKER (deterministic). `marker` is True/False/None:
      · False = the primary anchored NO marker covering this job before the deadline. That absence
        does not go through the relay, so it cannot be blamed on a relay outage: the only case where
        "0" is justified without further evidence. Deterministic = every judge reads the same chain,
        votes the same way, and quorum is REACHABLE.
      · None = stage 1 not anchored on-chain yet -> it is not used.
      · True = marker present. NOT SUFFICIENT: the marker is posted by the party under scrutiny
        (self-issued evidence). A cheater posts it and never publishes. Hence stage 2.

    STAGE 2 — CORRELATION (not falsifiable by the party under scrutiny, zero transactions). A relay
    outage is CORRELATED: it hits ALL primaries. A mute primary is ISOLATED. Therefore:
      · other primaries deliver normally (`others_ok >= min_others`) AND this one is mute
        -> it is NOT the relay -> "0".
      · everything is missing at once -> infrastructure -> ABSTENTION (invariant ① preserved).
      · too few other primaries observed -> no conclusion -> ABSTENTION. This is a refusal to decide
        without data: with a single witness, "isolated" and "correlated" are indistinguishable.

    WHY BOTH. Marker alone is self-issued. Correlation alone means each judge observes a different
    sample, votes diverge and quorum is NEVER reached. Together: determinism (stage 1) plus
    non-falsifiability (stage 2).
    """
    if marker is False:
        return "0", (f"stage 1: NO reveal marker anchored by {primary} covers {job_id} — an absence "
                     "that cannot be blamed on the relay (anchoring does not go through it)")
    tot = others_ok + others_missing
    if tot < min_others:
        return "abstention", (f"stage 2 undecidable: only {tot} other primary/primaries observed "
                              f"(min {min_others}) — isolated and correlated are indistinguishable here")
    if others_missing and others_ok == 0:
        return "abstention", (f"stage 2: CORRELATED — {others_missing} other primary/primaries are mute "
                              "too, none delivers: infrastructure outage, never billed to the honest party")
    if others_ok >= min_others and my_missing_for_primary > 0:
        return "0", (f"stage 2: ISOLATED — {others_ok} other primary/primaries deliver normally while "
                     f"{primary} is mute on {my_missing_for_primary} job(s): this is not the relay")
    return "abstention", "stage 2: signal too weak to tell isolated from correlated"


def noreveal_action(now, first_seen, misses, *, grace, abandon_after=0.0,
                    backoff_base=30.0, backoff_max=600.0):
    """What to do when the reveal is NOT readable? -> (action, delay_before_next_attempt).

    Abstention is a STATE, not a verdict. Marking the job as permanently done on abstention turns a
    TRANSIENT outage into a PERMANENT one: a reveal that becomes readable minutes later is never
    picked up again, the jury seat stays empty for good, and the held fee stays frozen. The worker
    therefore retries with a bounded exponential backoff, and gives up only if the operator has
    explicitly set a horizon.

    `abandon_after=0` (DEFAULT) = never give up on our own: the CHAIN decides when an audit is closed
    (the job leaves `+disputed` and disappears from the loop). A worker that gives up must never be
    what decides the fate of an audit. The cost is negligible: one relay GET every `backoff_max`
    seconds.
    """
    if misses < grace:
        return "patienter", 0.0
    if abandon_after and (now - first_seen) >= abandon_after:
        return "abandonner", 0.0
    # Exponential backoff from the first attempt after the grace window, capped.
    # The EXPONENT is bounded BEFORE the computation: `2 ** (misses - grace)` raises OverflowError past
    # roughly a thousand attempts (Python ints are unbounded, floats are not). Since the default is to
    # retry WITHOUT a horizon, a hundred thousand attempts is the nominal mode, not a corner case.
    exp = min(20, max(0, misses - grace))          # 2**20 x 30 s >> backoff_max: the cap governs
    return "reessayer", min(backoff_max, backoff_base * (2 ** exp))


def adjudicate_outcome(out):
    """-> ("ok"|"deferred"|"FAILED", detail). Confirms EXECUTION (query tx), not broadcast.

    Three properties this function must keep. (1) `code: 0` at broadcast means MEMPOOL, not execution:
    confirm at block level before reporting "ok", otherwise a dispute stays open and silent. (2) The
    deferred/FAILED reason lives in the BLOCK TEXT (raw_log via query tx), not in the broadcast output.
    (3) That text is read through `tx_reason()` (a NAMED field), never a head truncation that would
    discard the raw_log."""
    s = " ".join(str(out or "").split())[:400]
    if not s:
        return "FAILED", "(no output from the tx)"
    if not _ok(out):
        # rejected at broadcast (mempool) -> read the reason here (window/quorum = deferred, else failure)
        low = s.lower()
        return ("deferred", s) if any(h in low for h in _ADJ_DEFERRED_HINTS) else ("FAILED", s)
    ok, blocktext = _confirm_tx_text(out)
    if ok:
        return "ok", ""
    code, raw = tx_reason(blocktext)
    # Judge on the RAW_LOG when it exists; only then fall back to the truncated raw text.
    detail = raw or " ".join(str(blocktext or "").split())[:400] or s
    low = detail.lower()
    if any(h in low for h in _ADJ_DEFERRED_HINTS):
        return "deferred", detail
    if not raw:
        # No readable raw_log: the reason is unknown. Say so, rather than claim a real failure — a
        # dispute closed by another juror produces exactly this shape.
        return "FAILED", (detail + f" (execution not confirmed at block, code={code}, "
                                   "NO readable raw_log: reason UNKNOWN, not established)")
    return "FAILED", detail + f" (failed at block, code={code})"


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
      3d. MULTIPLICITY (Layer A'). Layer A alone leaves a 15-18% false-slash rate; this gate closes it:
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
    # (3d) Layer A' -- LAST pro-honest line of defense BEFORE the "invalid" vote: the judge must
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
                raise RuntimeError("simulated GPU timeout")
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
    cases.append(("coherent+relevant+valid -> True, 1 inference", r is True and b.calls == 1))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_f, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("incoherent -> False WITHOUT inference", r is False and b.calls == 0))
    b = FB(fail=99); r = decide_verdict("a", "p", b, "m", gen_retries=3, gen_delay=0, coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("inference down -> abstain(None), 3 attempts", r is None and b.calls == 3))
    b = FB(fail=2); r = decide_verdict("a", "p", b, "m", gen_retries=3, gen_delay=0, coherent_fn=coh_t, judge_fn=jf, relevant_fn=rel_t, **LG)
    cases.append(("retry then cheater -> False, 3 attempts", r is False and b.calls == 3))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_none, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("coherence None -> the judge decides on the fact", r is True and b.calls == 1))
    b = FB(); seen = {"c": 0}
    def coh_count(*a, **k):
        seen["c"] += 1; return False
    r = decide_verdict("a", "p", b, "m", twostage=False, coherent_fn=coh_count, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("twostage OFF -> coherence stage skipped", r is True and seen["c"] == 0 and b.calls == 1))
    # RELEVANCE guard:
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jf, relevant_fn=rel_f, **LG)
    cases.append(("COHERENT off-topic -> ABSTENTION without inference (no false invalid)", r is None and b.calls == 0))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_f, judge_fn=jv, relevant_fn=rel_f, **LG)
    cases.append(("incoherent garbage + off-topic -> False (coherence WINS, garbage stays slashed)", r is False and b.calls == 0))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_none, **LG)
    cases.append(("relevance unreadable -> pipeline continues (True)", r is True and b.calls == 1))
    b = FB(); seen_r = {"n": 0}
    def rel_count(*a, **k):
        seen_r["n"] += 1; return False
    r = decide_verdict("a", "p", b, "m", relevance=False, coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_count, **LG)
    cases.append(("relevance OFF -> guard skipped (strict rollback)", r is True and seen_r["n"] == 0 and b.calls == 1))

    # --- tx_reason / adjudicate_outcome: the raw_log comes AFTER `events:` ------------------------
    # The YAML reproduces what dendrad actually returns: an `events:` list several thousand characters
    # long, THEN the raw_log. That is what makes a `[:400]` head truncation structurally blind — the
    # third assertion PROVES it instead of asserting it.
    TX = ("code: 18\ncodespace: sdk\ndata: \"\"\nevents:\n"
          + "".join(f"- attributes:\n  - index: true\n    key: k{i}\n    value: v{i}\n  type: tx\n"
                    for i in range(60))
          + "gas_used: \"77\"\nheight: \"15040\"\ninfo: \"\"\nlogs: []\n"
            "raw_log: 'failed to execute message; message index: 0: dispute already resolved'\n"
            "timestamp: \"\"\ntxhash: AB\n")
    _c, _raw = tx_reason(TX)
    cases.append(("tx_reason reads the execution code", _c == 18))
    cases.append(("tx_reason FINDS the raw_log placed after events", "already resolved" in _raw))
    cases.append(("PROOF of the defect: a [:400] truncation CANNOT see the reason",
                  "already resolved" not in " ".join(TX.split())[:400]))
    cases.append(("raw_log absent -> reason EMPTY, never invented",
                  tx_reason("code: 5\nevents: []\n")[1] == ""))
    # --- marker_state: stage 1, judge side ------------------------------------------------------------
    ROOT = rmk.merkle_root(["jobA", "jobB"])
    def gc_ok(key):    # the primary anchored for the epoch in which the job was disputed
        return ROOT if key == rmk.marker_key(rmk.epoch_of(6000, 600), "p1") else ""
    def gl_ok(key):
        return {"root": ROOT, "jobs": ["jobA", "jobB"]}
    cases.append(("stage 1: job IN the anchored marker -> True",
                  marker_state("jobA", "p1", 6000, get_commit=gc_ok, get_list=gl_ok)[0] is True))
    cases.append(("stage 1: marker anchored but job ABSENT from the list -> False (it revealed for "
                  "others, not for this one)",
                  marker_state("jobZ", "p1", 6000, get_commit=gc_ok, get_list=gl_ok)[0] is False))
    cases.append(("stage 1: NO anchored marker -> False",
                  marker_state("jobA", "p1", 6000, get_commit=lambda k: "", get_list=gl_ok)[0] is False))
    cases.append(("stage 1: root anchored but LIST not found -> None (undecidable, not a slash)",
                  marker_state("jobA", "p1", 6000, get_commit=gc_ok, get_list=lambda k: None)[0] is None))
    cases.append(("stage 1: list that DOES NOT MATCH the root -> None (unreliable data)",
                  marker_state("jobA", "p1", 6000, get_commit=gc_ok,
                               get_list=lambda k: {"jobs": ["other"]})[0] is None))
    # epoch boundary: disputed just before, revealed AFTER -> the marker lands in the NEXT epoch
    def gc_next(key):
        # the job is disputed at 5999 (epoch 9): its marker falls into epoch 10, the NEXT one
        return ROOT if key == rmk.marker_key(rmk.epoch_of(5999, 600) + 1, "p1") else ""
    cases.append(("stage 1: marker in the NEXT epoch -> found (no false absence at the boundary)",
                  marker_state("jobA", "p1", 5999, get_commit=gc_next, get_list=gl_ok,
                               epoch_blocks=600)[0] is True))
    # the rollout guard: as long as NOBODY anchors, stage 1 is unusable
    cases.append(("stage 1 DISARMED while no primary anchors (otherwise a mass slash at rollout)",
                  marker_stage_usable(0) is False and marker_stage_usable(1) is True))

    # --- noreveal_verdict: the 2 stages ---------------------------------------------------------------
    NV = dict(job_id="j1", primary="p1", min_others=2)
    cases.append(("stage 1: marker ABSENT -> \"0\" without further evidence (not blamable on the relay)",
                  noreveal_verdict(marker=False, others_ok=0, others_missing=0,
                                   my_missing_for_primary=1, **NV)[0] == "0"))
    cases.append(("stage 1: marker PRESENT is NOT enough (self-issued evidence)",
                  noreveal_verdict(marker=True, others_ok=0, others_missing=3,
                                   my_missing_for_primary=1, **NV)[0] == "abstention"))
    cases.append(("stage 2: ISOLATED (2 others deliver, this one mute) -> \"0\"",
                  noreveal_verdict(marker=None, others_ok=2, others_missing=0,
                                   my_missing_for_primary=1, **NV)[0] == "0"))
    cases.append(("stage 2: CORRELATED (nobody delivers) -> ABSTENTION (invariant ① preserved)",
                  noreveal_verdict(marker=None, others_ok=0, others_missing=4,
                                   my_missing_for_primary=1, **NV)[0] == "abstention"))
    cases.append(("stage 2: a single witness -> ABSTENTION (isolated and correlated indistinguishable)",
                  noreveal_verdict(marker=None, others_ok=1, others_missing=0,
                                   my_missing_for_primary=1, **NV)[0] == "abstention"))
    # A production shape, replayed: four audits with zero voters, ALL on the same primary, while the
    # other primaries were receiving their verdicts. Correlation decides this one correctly.
    cases.append(("four audits, same mute primary, the others deliver -> \"0\"",
                  noreveal_verdict(job_id="job1784919287094", primary="minerB", marker=None,
                                   others_ok=3, others_missing=0, my_missing_for_primary=4,
                                   min_others=2)[0] == "0"))
    cases.append(("a genuine relay collapse does NOT become a slash (all mute, 0 delivery)",
                  noreveal_verdict(job_id="j", primary="p", marker=None, others_ok=0,
                                   others_missing=7, my_missing_for_primary=9,
                                   min_others=2)[0] == "abstention"))

    # --- noreveal_action: abstention is a REVERSIBLE STATE, not a permanent strike-off --------------
    NA = dict(grace=5, backoff_base=30.0, backoff_max=600.0)
    cases.append(("below the grace window -> wait", noreveal_action(100, 100, 3, **NA)[0] == "patienter"))
    cases.append(("at the grace window -> retry with the base backoff",
                  noreveal_action(100, 100, 5, **NA) == ("reessayer", 30.0)))
    cases.append(("the backoff grows then CAPS", noreveal_action(1e9, 0, 9, **NA)[1] == 480.0
                  and noreveal_action(1e9, 0, 30, **NA)[1] == 600.0))
    # The regression guard: with no explicit horizon, the worker NEVER gives up on its own.
    cases.append(("abandon_after=0 -> NEVER 'abandonner', even after ten years and 10^5 attempts",
                  all(noreveal_action(3.2e8, 0, m, **NA)[0] != "abandonner" for m in (5, 100, 100000))))
    cases.append(("no OverflowError at 10^6 attempts (the exponent is bounded BEFORE the computation)",
                  noreveal_action(3.2e8, 0, 1000000, **NA)[1] == 600.0))
    cases.append(("horizon set AND exceeded -> give up",
                  noreveal_action(1000, 0, 6, abandon_after=900, **NA)[0] == "abandonner"))
    cases.append(("horizon set but NOT reached -> retry",
                  noreveal_action(1000, 500, 6, abandon_after=900, **NA)[0] == "reessayer"))
    cases.append(("the horizon does not short-circuit the grace window (3 attempts < 5 -> wait)",
                  noreveal_action(1e9, 0, 3, abandon_after=1, **NA)[0] == "patienter"))

    _saved, _bc = _confirm_tx_text, "code: 0\ntxhash: " + "A" * 64 + "\n"
    try:
        globals()["_confirm_tx_text"] = lambda out, timeout=24: (False, TX)
        _st, _det = adjudicate_outcome(_bc)
        cases.append(("dispute closed by ANOTHER juror -> 'deferred', not a real failure",
                      _st == "deferred" and "already resolved" in _det))
        globals()["_confirm_tx_text"] = lambda out, timeout=24: (False, "code: 7\nevents: []\n")
        _st2, _det2 = adjudicate_outcome(_bc)
        cases.append(("no raw_log -> FAILED that SAYS the reason is unknown",
                      _st2 == "FAILED" and "UNKNOWN" in _det2))
    finally:
        globals()["_confirm_tx_text"] = _saved
    ok = True
    for name, p in cases:
        ok = ok and p
        print(f"  [{'OK' if p else 'FAIL'}] {name}")
    print("SELFTEST decide_verdict:", "GREEN" if ok else "RED")
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
        ("1 factual VALID (stable refs, answer agrees)",
         ["Paris", "Paris"], "Paris", {}, True, 2),
        ("2 factual CHEATER (stable refs, answer diverges, 3rd confirms)",
         ["Paris", "Paris", "Paris"], "Berlin", {}, False, 3),
        ("3 AMBIGUOUS, answer diverges (2 refs diverge -> abstention)",
         ["Python", "Rust"], "JavaScript", {}, None, 2),
        ("4 AMBIGUOUS, answer equals one ref (refs diverge -> abstention anyway)",
         ["Python", "Rust"], "Rust", {}, None, 2),
        ("5 near-slash SAVED: 2 agree, answer diverges, 3rd DIVERGES -> abstention",
         ["Paris", "Paris", "Lyon"], "Berlin", {}, None, 3),
        ("6 COHERENCE first: garbage -> INVALID, 0 generations",
         ["x"], "garbage", {"coherent_fn": coh_f}, False, 0),
        ("7 RELEVANCE: fully off-topic -> abstention, 0 generations",
         ["x"], "hors-sujet", {"relevant_fn": rel_f}, None, 0),
        ("8 ROLLBACK selfconsist=0: 1 ref, answer diverges -> INVALID (the documented bug)",
         ["Python"], "JavaScript", {"selfconsist": False}, False, 1),
        ("9 GPU TIMEOUT on the 1st ref (3 attempts) -> abstention",
         [TimeoutError(), TimeoutError(), TimeoutError()], "Paris", {}, None, 3),
        ("10 A' MULTIPLE ANSWERS: stable refs \"7\", answer \"13\", multi_ok=YES -> ABSTENTION (no slash)",
         ["7", "7"], "13", {"multiok_fn": mko_t}, None, 3),
        ("11 A' consulted at the threshold: real cheater, multi_ok=NO -> INVALID (capture preserved)",
         ["Paris", "Paris", "Paris"], "Berlin", {"multiok_fn": mko_count}, False, 3),
        ("12 A' UNREADABLE -> pro-honest abstention (fail open, deliberate and recorded)",
         ["7", "7", "7"], "13", {"multiok_fn": mko_n}, None, 3),
        ("13 A' ROLLBACK multiok=0: direct slash, multi_ok NEVER called",
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
        print(f"  [{'OK' if good else 'FAIL'}] {label} -> verdict={got_v} (want {want_v}), gen={b.calls} (want {want_c})")
    # 11-bis: mko_count called EXACTLY 1× in case 11 (the stage is indeed consulted at the threshold), 0× in case 13 (rollback).
    good = mko_seen["n"] == 1
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 11-bis/13-bis multi_ok consulted 1x at the threshold, 0x on rollback (got {mko_seen['n']})")
    # 14: TRACE (instrumentation): exit stage + number of generations exposed.
    tr = {}
    b = SeqBackend(["7", "7"])
    got = decide_verdict("13", "prompt", b, "m", coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                         relevance=True, selfconsist=True, gen_delay=0, multiok_fn=mko_t, trace=tr)
    good = got is None and tr.get("stage") == "multiok" and tr.get("n_gen") == 3 and len(tr.get("refs", [])) == 3
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 14 trace filled -> stage={tr.get('stage')} (want multiok), n_gen={tr.get('n_gen')} (want 3)")
    # 15: option α-(a) abstain_to_vote (PURE) -- vote "1" ONLY if armed AND ambiguity PROVEN.
    good = (abstain_to_vote("sc-diverge", True) is True and abstain_to_vote("multiok", True) is True
            and abstain_to_vote("relevance", True) is False and abstain_to_vote("gen-fail", True) is False
            and abstain_to_vote("sc-diverge", False) is False and abstain_to_vote("", True) is False)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 15 α-(a) abstain_to_vote: armed+ambiguous=vote 1, technical/disarmed=abstention")
    print("SELFTEST LAYER A", "GREEN" if ok else "RED")
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
        print(f"[judge] trace not written ({type(e).__name__}) — verdict unaffected")


def main():
    if "--selftest" in sys.argv:  # off-GPU logic check, short-circuits the required args
        sys.exit(_selftest())
    if "--selftest-sc" in sys.argv:  # Layer A (self-consistency), off-GPU
        sys.exit(_selftest_selfconsist())
    # INVARIANT #1 (no cleartext in any log) — ENFORCED, not merely documented. `TRACE_PLAIN` writes the
    # prompt and the answer IN CLEAR to a JSONL file: it is the only path in the whole stack by which
    # cleartext can reach a log. "Bench only" in a comment is not a guard — under public exposure the
    # refusal is pronounced at startup.
    try:
        _public = public_actif(os.environ.get("DENDRA_PUBLIC"))
    except DrapeauInvalide as e:
        print(f"[judge] REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    if _public and TRACE_PLAIN:
        print("[judge] REFUSED: DENDRA_PUBLIC=1 together with DENDRA_JUDGE_TRACE_PLAIN=1 — the CLEARTEXT "
              "trace would write the user's prompt and answer to a file. Cleartext touches neither the "
              "chain, nor the relay, nor a log. Remove DENDRA_JUDGE_TRACE_PLAIN (the hash-only trace "
              "remains available via DENDRA_JUDGE_TRACE).", file=sys.stderr)
        sys.exit(2)
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="registered miner acting as a committee member")
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--adjudicate", action="store_true", help="attempt adjudicate-dispute after the verdict (best effort)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--epoch-blocks", type=int, default=rmk.EPOCH_BLOCKS_DEFAULT,
                    help="size of the reveal-marker epoch, in blocks. MUST match the value used by the "
                         "reveal workers: two different partitions look for the marker in two different "
                         "epochs, and 'not found' reads as 'absent'.")
    ap.add_argument("--abandon-after", type=float, default=0.0, metavar="SECONDS",
                    help="horizon past which the worker stops retrying an unreadable reveal. DEFAULT 0 = "
                         "never give up on our own: the CHAIN closes an audit (the job leaves +disputed "
                         "and exits the loop). A worker that gives up must not be what decides the fate "
                         "of an audit — an unavailability of a few minutes would otherwise cost a jury "
                         "seat permanently.")
    ap.add_argument("--reveal-grace", type=int, default=5,
                    help="ADR-028: number of reveal-open attempts before posting a 0 verdict (a primary "
                         "that stays mute or reveals nothing must be judged INVALID, not cleared)")
    ap.add_argument("--model-id", default="",
                    help="ADR-027 D4: MANUAL override of the judge model. By default the worker reads the "
                         "model PINNED on-chain (modelregistry.audit_judge_model) so the whole committee "
                         "judges with the SAME model; force it only for debugging and tests.")
    a = ap.parse_args()

    passphrase = os.environ.get("DENDRA_MINER_PASSPHRASE", "")
    skpath = Path(a.keydir) / f"{a.id}.sk"
    if not skpath.exists():
        print(f"[judge] FATAL: X25519 key {skpath} missing — run miner.py --id {a.id} first")
        sys.exit(3)
    sk = crypto.load_sk(str(skpath), passphrase)

    # inference backend to COMPUTE my own reference answer (the judge compares to mine).
    # ⛔ "UNREACHABLE" WAS THE WRONG WORD, AND IT HID A DEAD JUDGE FOR HOURS.
    # This probe runs in the JUDGE process, whose OLLAMA_ENDPOINT is the JUDGE endpoint
    # (entrypoint-services.sh sets `OLLAMA_ENDPOINT="$JEP"`), while the model it asks for is the
    # MINER's `DENDRA_MODEL_ID`. Once the two backends were split — GPU for mining, CPU for judging —
    # the judge endpoint stopped carrying the miner's model. Ollama then answers 404 "model not
    # found", urllib raises HTTPError, and every failure was printed as "Ollama unreachable".
    # Measured on the project's own node: `/api/tags` returned 200 with the judge model present, and
    # `/api/generate` returned 404, once every restart, for hours — while the operator read
    # "unreachable" and checked a network that was fine. A diagnosis that names the wrong subsystem
    # is worse than none: it sends the search away from the cause.
    _ep = os.environ.get("OLLAMA_ENDPOINT", "?")
    try:
        backend = Miner(a.id, backend="ollama").backend
        backend.generate("ok")
    except urllib.error.HTTPError as e:
        _corps = ""
        try:
            _corps = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 — reading the body must never mask the real error
            pass
        print(f"[judge] FATAL: the inference backend ANSWERED, and REFUSED (HTTP {e.code}) at {_ep}.")
        print(f"        This is NOT a connectivity problem. Response: {_corps or '(empty)'}")
        if e.code == 404:
            print(f"        A 404 from Ollama means THE MODEL IS NOT PULLED ON THAT ENDPOINT.")
            print(f"        This process asks for DENDRA_MODEL_ID={os.environ.get('DENDRA_MODEL_ID', '?')!r},")
            print(f"        and it asks the JUDGE endpoint. If mining and judging run on two backends,")
            print(f"        that model has to exist on BOTH, or this probe has to use the judge's own.")
            print(f"        Check: curl -s {_ep}/api/tags")
        sys.exit(3)
    except Exception as e:  # noqa: BLE001 — genuine connectivity failures land here
        print(f"[judge] FATAL: cannot reach the inference backend at {_ep} ({type(e).__name__}: {e})")
        print(f"        — the committee must be able to infer.")
        sys.exit(3)

    # consensual judge model resolved ONCE at startup (transparency + committee consistency).
    judge_model, judge_src = resolve_judge_model(a.model_id)
    # Layer C: the on-chain VERDICT ALWAYS carries the judge's model
    # (`Commit.ModelId` already exists in the proto -- verified msg_server_commit.go:93). Before: --model-id
    # was set only if env DENDRA_JUDGE_MODEL_ID -> the bench verdicts went out WITHOUT a model -> the
    # DIVERSITY tally (min_distinct_judge_models) would have had nothing to count. Now:
    # explicit env > resolved model. Zero data regeneration needed.
    flags = ["--model-id", (JUDGE_MODEL_ID or judge_model)]
    src_label = {"cli": "--model-id override", "chain": "on-chain (modelregistry.audit_judge_model)",
                 "env": "env DENDRA_JUDGE_MODEL_ID", "default": f"judge.py default ({DEFAULT_JUDGE_MODEL})"}
    print(f"[judge] judge model = '{judge_model}' (source: {src_label.get(judge_src, judge_src)})")
    if judge_src == "default":
        print("[judge] WARNING: judge model not pinned on-chain — the committee risks inconsistent "
              "verdicts if its members diverge (ADR-027 D4).")
    # WHERE THE TWO KINDS OF CALLS GO — stated, not assumed.
    # This process queries Ollama for two unrelated things: its REFERENCE answer (the MINER's model,
    # ~5 GB of VRAM) and its VERDICT (the judge model, 19 GB). On an 8 GB card the two never fit
    # together; sending them to the same endpoint guarantees permanent eviction, and that failure is
    # SILENT — the chain advances, the judges run, and no job is served any more. The split is
    # configured through DENDRA_JUDGE_ENDPOINT.
    # Setting the variable does not prove the code reads it: with an older modea/judge.py the export
    # has no effect and everything still looks normal. These lines make the code state what it actually
    # resolved, and the fallback below names an outdated judge.py explicitly.
    _ep_ref = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434")
    _m_ref = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")
    try:
        from modea.judge import judge_endpoint as _je
        _ep_verdict = _je()
    except ImportError:
        _ep_verdict = f"{_ep_ref}  [!] outdated modea/judge.py: split IMPOSSIBLE, the variable is ignored"
    print(f"[judge] verdict endpoint   = {_ep_verdict}  (model {judge_model})")
    print(f"[judge] reference endpoint = {_ep_ref}  (model {_m_ref})")
    # WHAT IS DANGEROUS IS NOT "the same endpoint", IT IS "TWO MODELS ON THE SAME ONE".
    # The canonical launcher sets OLLAMA_MODEL to the judge model AND both endpoints to the CPU
    # instance: a perfectly healthy configuration — one resident model, the card entirely free. An
    # alarm that fires on a correct configuration costs more than no alarm at all: it is learned to be
    # ignored, and it is ignored again on the day it is right. The guard therefore tests the REAL
    # eviction condition: two distinct models served by the same instance.
    if _ep_verdict == _ep_ref and _m_ref != judge_model:
        print(f"[judge] WARNING: TWO different models on the SAME Ollama ({_ep_ref}) — "
              f"'{_m_ref}' for the reference and '{judge_model}' for the verdict. They evict each "
              f"other; if this instance is the GPU one, the miner is starved and jobs stay `open`. "
              f"Point DENDRA_JUDGE_ENDPOINT at a second instance, or align OLLAMA_MODEL with the "
              f"judge model.")
    print(f"[judge] committee worker {a.id} ready (relay {a.relay}) — judging +disputed jobs")
    trace_dest = ""
    if TRACE_DEST:
        trace_dest = f"/tmp/judge-trace-{a.id}.jsonl" if TRACE_DEST == "1" else TRACE_DEST
        print(f"[judge] per-job TRACE -> {trace_dest} ({'CLEARTEXT (bench only)' if TRACE_PLAIN else 'hashes only'})")
    done = set()
    miss = {}        # job_id -> number of reveal-open failures (grace window)
    first_seen = {}  # job_id -> ts of the first attempt (abandon horizon, if the operator sets one)
    next_try = {}    # job_id -> ts before which we do not retry (growing backoff)
    abstained = set()  # job_id already announced as abstained: do not reprint on every attempt
    while True:
        try:
            # TWO PASSES: the "0" verdicts on ABSENT REVEAL are
            # FAST (one relay GET + one tx, NO inference); full judgments are SLOW (minutes
            # of generations). Handling them in a single queue made the "0" wait behind the
            # generations -> the audit timeout fired before quorum -> a MUTE resolved as no-quorum
            # (silence_slash) instead of the HARD slash. Pass 1 = triage + grace/immediate "0";
            # pass 2 = judgments. In PROD too: posting the "0" fast = fewer no-quorum on the mute ones.
            # -- PASS 1a: OBSERVE, decide nothing. --------------------------------------------------
            # STAGE 2 (correlation) needs to know what the OTHER primaries do in the SAME pass:
            # "isolated" and "correlated" can only be told apart by comparison. Deciding job by job
            # makes that comparison structurally impossible.
            slow = []
            observed = []
            _rows = list_jobs_full()
            if _rows is None:
                # "the read failed" is NOT "nothing to judge": skip the round and SAY so, instead of
                # iterating an empty list and sleeping as if work had been done.
                print(f"[judge] {a.id} list-job UNREADABLE — round skipped (neither green nor red: not measured)")
                # `--poll` is the only interval this parser declares. Any other attribute name raises
                # AttributeError and kills the JUROR exactly when the chain becomes unreadable — the
                # moment its seat must survive to vote next round. A dead seat is a mute seat, and a
                # mute seat raises the two-thirds bar for the whole network.
                time.sleep(a.poll)
                continue
            for job_id, state, primary, dispute_h in _rows:
                if not is_disputed(state) or job_id in done or primary == a.id:
                    continue
                if not rv.safe_job_id(job_id):   # job_id from on-chain -> dendrad argv + relay key
                    print(f"[judge] {a.id} NON-CONFORMING job_id ignored (argv defense): {str(job_id)[:40]!r}")
                    done.add(job_id)
                    continue
                _now = time.time()
                if _now < next_try.get(job_id, 0.0):
                    continue          # backing off: the reveal was not there, we will come back
                first_seen.setdefault(job_id, _now)
                rev = rv.open_reveal(a.relay, job_id, a.id, sk)
                observed.append((job_id, primary, dispute_h, rev, _now))

            # -- CORRELATION statistics, over this pass only ------------------------------------------
            ok_by, miss_by = {}, {}
            for jid, prim, _dh, rv_, _t in observed:
                if rv_ and "prompt" in rv_:
                    ok_by[prim] = ok_by.get(prim, 0) + 1
                else:
                    miss_by[prim] = miss_by.get(prim, 0) + 1
            # -- STAGE 1: is it even DEPLOYED? (mass-slash guard) --------------------------------------
            n_anchor, marker_cache = 0, {}
            if TWOSTAGE_NOREVEAL:
                for jid, prim, dh, rv_, _t in observed:
                    if prim in marker_cache or (rv_ and "prompt" in rv_):
                        continue
                    marker_cache[prim] = primary_has_marker(prim, dh, get_commit=anchored_root,
                                                            epoch_blocks=a.epoch_blocks)
                n_anchor = sum(1 for v in marker_cache.values() if v)
            stage1_ok = TWOSTAGE_NOREVEAL and marker_stage_usable(n_anchor)

            # -- PASS 1b: DECIDE ----------------------------------------------------------------------
            for job_id, primary, dispute_h, rev, _now in observed:
                if rev and "prompt" in rev and job_id in abstained:
                    # The point of treating abstention as a state: this job was abstained, the reveal
                    # has since arrived, so it is JUDGED AGAIN. A permanent strike-off would make this
                    # path unreachable.
                    print(f"[judge] {a.id} RESUMING {job_id}: the reveal arrived after "
                          f"{miss.get(job_id, 0)} attempts — the job is judgeable again")
                    abstained.discard(job_id)
                if rev and "prompt" in rev:
                    miss.pop(job_id, None)
                    next_try.pop(job_id, None)
                if not rev or "prompt" not in rev:
                    # no EXPLOITABLE reveal. We leave a GRACE window for the honest primary
                    # (slow network), then we POST a "0" verdict (INVALID): a mute primary or one that reveals
                    # nothing is thus caught in QUORUM-CHEAT by the committee (non-falsifiable signal) -> hard slash,
                    # instead of being cleared by the timeout. This is what closes evasion by non-revelation.
                    miss[job_id] = miss.get(job_id, 0) + 1
                    if NOREVEAL_ABSTAIN:
                        # Silence is handled by no-quorum + silence_slash, NOT by a cheat verdict we
                        # cannot justify (an absent reveal is indistinguishable from a relay fault).
                        # Abstention is a REVERSIBLE STATE, not a strike-off (see noreveal_action) —
                        # otherwise an unavailability of a few seconds permanently costs a jury seat,
                        # and the corresponding held fee stays frozen.
                        act, delay = noreveal_action(_now, first_seen[job_id], miss[job_id],
                                                     grace=a.reveal_grace,
                                                     abandon_after=a.abandon_after)
                        if act == "patienter":
                            continue
                        if act == "abandonner":
                            print(f"[judge] {a.id} GIVING UP on {job_id} after "
                                  f"{int(_now - first_seen[job_id])} s without a readable reveal "
                                  f"(--abandon-after={a.abandon_after:g}s) — no further attempts")
                            done.add(job_id)
                            continue
                        next_try[job_id] = _now + delay
                        # -- THE 2 STAGES ------------------------------------------------------------
                        # They apply only AFTER the grace window, and only when ARMED. DEFAULT:
                        # DISARMED. Arming this path opens a "0" vote, hence a possible slash, and
                        # that is an economic decision that must be authorized explicitly.
                        if TWOSTAGE_NOREVEAL:
                            mk, mk_why = (None, "stage 1 not deployed (no primary anchors) -> "
                                                "CORRELATED absence, not attributable to this primary")
                            if stage1_ok:
                                mk, mk_why = marker_state(
                                    job_id, primary, dispute_h,
                                    get_commit=anchored_root,
                                    get_list=lambda k: relay_c.get(a.relay, "reveal", k),
                                    epoch_blocks=a.epoch_blocks)
                            vote, why = noreveal_verdict(
                                job_id, primary, marker=mk,
                                others_ok=sum(v for p, v in ok_by.items() if p != primary),
                                others_missing=sum(v for p, v in miss_by.items() if p != primary),
                                my_missing_for_primary=miss_by.get(primary, 0))
                            if vote == "0":
                                print(f"[judge] {a.id} 2-STAGE -> verdict 0 for {job_id} "
                                      f"(primary {primary}): {why} | stage 1: {mk_why}")
                                # fall through to the "0" vote path below
                                miss[job_id] = miss.get(job_id, 0)
                            else:
                                if job_id not in abstained:
                                    abstained.add(job_id)
                                    print(f"[judge] {a.id} 2-STAGE -> ABSTENTION for {job_id}: {why}")
                                continue
                        else:
                            if job_id not in abstained:
                                abstained.add(job_id)
                                print(f"[judge] {a.id} ABSTENTION (reveal absent after {miss[job_id]} attempts) "
                                      f"for {job_id} — an absent reveal is not proof of cheating; "
                                      f"STILL RETRYING (next attempt in {delay:g} s)")
                                if trace_dest:
                                    _trace_write(trace_dest, {"ts": int(time.time()), "job_id": job_id,
                                                              "verdict": "abstain", "stage": "no-reveal", "n_gen": 0})
                            continue
                    vkey = f"{job_id}__verdict__{a.id}"
                    if verdict_already_posted(vkey, "0"):
                        done.add(job_id)
                        continue
                    out = tx_from(a.id, "create-commit", vkey, "0", "0", "verdict", *flags)
                    if wait_tx(out):
                        print(f"[judge] {a.id} verdict=0 (REVEAL ABSENT after {miss[job_id]} attempts) for {job_id}")
                        if trace_dest:
                            _trace_write(trace_dest, {"ts": int(time.time()), "job_id": job_id,
                                                      "verdict": "0", "stage": "no-reveal", "n_gen": 0})
                        done.add(job_id)
                        if a.adjudicate:
                            # Do NOT discard the return value: a broadcast `code: 0` proves nothing.
                            # Execution is confirmed exactly as at the other adjudication site.
                            _st, _detail = adjudicate_outcome(tx_from(a.id, "adjudicate-dispute", "--job-id", job_id))
                            if _st == "ok":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> OK (dispute closed)")
                            elif _st == "deferred":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> deferred: {_detail[:140]}")
                            else:
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> FAILED: {_detail[:140]}")
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
                        print(f"[judge] {a.id} AMBIGUOUS job {job_id} (stage {tr.get('stage')}) -> VALID vote "
                              f"(α-(a) benefit of the doubt, DENDRA_JUDGE_ABSTAIN_VOTE=1)")
                        verdict = True
                    else:
                        print(f"[judge] {a.id} ABSTENTION job {job_id} (stage {tr.get('stage', 'n/a')}: "
                              f"off-topic vs prompt, judge disagreeing with itself [Layer A], multiple "
                              f"correct answers [A'], or undecidable inference) — no doubtful verdict")
                        continue
                valid = verdict
                v = verdict_commit(valid)
                vkey = f"{job_id}__verdict__{a.id}"
                if verdict_already_posted(vkey, v):
                    done.add(job_id)
                    continue
                out = tx_from(a.id, "create-commit", vkey, v, v, "verdict", *flags)
                if wait_tx(out):
                    print(f"[judge] {a.id} verdict={v} ({'OK' if valid else 'DIVERGENT'}) commit for {job_id}")
                    done.add(job_id)
                    if a.adjudicate:
                        adj = tx_from(a.id, "adjudicate-dispute", "--job-id", job_id)
                        _st, _detail = adjudicate_outcome(adj)
                        if _st == "ok":
                            print(f"[judge] {a.id} adjudicate-dispute {job_id} -> OK (dispute closed)")
                        elif _st == "deferred":
                            print(f"[judge] {a.id} adjudicate-dispute {job_id} -> deferred (expected): {_detail[:140]}")
                        else:
                            print(f"[judge] {a.id} adjudicate-dispute {job_id} -> REAL FAILURE — disputes will "
                                  f"not close (jobs_unresolved>0): {_detail}")
                else:
                    # NEVER LOSE THE REASON. A message that only says "the tx did not go through" and
                    # offers one plausible cause sends the investigation to the wrong place: the real
                    # cause has been "key not found" (keyring outside dendrad's default home). A guess
                    # printed in place of the REAL reason is worse than silence.
                    print(f"[judge] {a.id} verdict NOT anchored for {job_id} (will retry) — dendrad output: "
                          f"{' '.join((out or '(empty)').split())[:400]}")
        except Exception as e:
            print(f"[judge] {a.id} loop: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
