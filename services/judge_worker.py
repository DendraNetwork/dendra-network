#!/usr/bin/env python3
"""COMMITTEE WORKER: judges the audited sample and commits its verdict on-chain.

Run BY an already-registered miner (reuses its chain key + its X25519 key from `keydir`), IN ADDITION
to `miner.py`. Loop:
  1. discovers the `+disputed` jobs (audit) via `query jobs list-job` -- ONLY those whose ANCHORED jury
     (`query jobs audit-committee`) seats me, EXCEPT those where I am the primary, EXCEPT those I already
     voted on;
  2. fetches the primary's sealed REVEAL (`reveal_helpers.open_reveal`) -> (prompt, answer), and checks it
     against what the primary anchored: the question's commitment and the answer's embedding;
  3. computes MY own answer (Ollama) on the prompt, bounded like the audited request was;
  4. judges "primary's answer == same fact as mine?" (`modea.judge.llm_judge`);
  5. commits the verdict on-chain: `create-commit "<jobId>__verdict__<minerId>" <0|1> <0|1> verdict`
     (REUSES the existing message; `AdjudicateDispute` tallies these verdicts weighted by stake);
  6. best-effort: tries `adjudicate-dispute <jobId>` after the window (permissionless) to close it.

No new chain message. A verdict from outside the anchored jury is ignored on-chain, hence never judged here.

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
from modea import keyring as kring
from modea import dendrad_argv as da
from modea import chain_id as _cid
from modea.exposition import DrapeauInvalide, public_actif
from modea.miner import Miner, answer_embedding
from modea.judge import (llm_judge, llm_coherent, llm_relevant, llm_multi_ok, verdict_commit,
                         DEFAULT_JUDGE_MODEL, judge_timeout_s)
import relay_client as relay_c
import reveal_helpers as rv
import reveal_marker as rmk

# ADR-048 item 8: the chain id is READ (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
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
    """The home flag. The keyring flags come from `_keyring()`: backend and directory, read from the disk."""
    return ["--home", HOME_DIR] if HOME_DIR else []


# THE KEYRING, AS THE DAEMON READS IT (modea/keyring.py): the backend from the state of the disk, and in
# `file` mode the passphrase, handed to dendrad on stdin -- the same source for every process that signs
# for this miner. A verdict signed from another keyring than the daemon's would be refused as not the
# miner's operator, and the seat would stay mute.
_KR = None


def _keyring():
    global _KR
    if _KR is None:
        _KR = kring.resolve(KEYRING_DIR or HOME_DIR or None)
    return _KR

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
# Rollback: DENDRA_JUDGE_ABSTAIN_VOTE=0. An EMPTY value is unset (deploy/testnet-miner/docker-compose.yml forwards
# it empty when the .env names none, so that a value set there reaches the judge): the default holds.
ABSTAIN_VOTE = (os.environ.get("DENDRA_JUDGE_ABSTAIN_VOTE") or "1") == "1"
# A DIVERGENCE DOES NOT SLASH UNTIL A C3 PASS SAYS IT MAY. THE GUARD IS ON BY DEFAULT: unset, empty, "0" or
# any value other than exactly "1" = a divergence ABSTAINS.
# The committee judges with ONE model, the one the chain pins (`resolve_judge_model`), while the runbook makes
# a HETEROGENEOUS committee a requirement (docs/RUNBOOK-SOFT-LAUNCH.md) and `decide_verdict` says itself that
# the single-model correlation is not closed: jurors that run one model share its mistakes, so a single
# mistake on an honest answer is repeated by every seat, and the slash bar of a small jury is a few seats.
# Until a dated C3 pass "single-model committee x the models served"
# says otherwise, an INVALID that comes from comparing the answer with this judge's own references (`slash`,
# `legacy`) is withheld. An INVALID from the COHERENCE stage is not a comparison with anything this judge
# generated -- it says the answer is word salad -- and it stays a vote.
# Setting "1" is an explicit authorization given after that pass, never a tuning knob.
DIVERGENCE_SLASH_ENV = "DENDRA_JUDGE_DIVERGENCE_SLASH"


def divergence_slash_armed(value) -> bool:
    """May a divergence vote INVALID? Only for the exact value "1": anything else -- unset, "0", "yes",
    " 1" -- keeps the guard, because a value that reads as consent without being one must not open a slash."""
    return value == "1"


DIVERGENCE_SLASH = divergence_slash_armed(os.environ.get(DIVERGENCE_SLASH_ENV))
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


def run(c, t=600, stdin=""):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t, input=stdin)
    return (r.stdout or "") + (r.stderr or "")


def resolve_judge_model(explicit: str = ""):
    """Chooses THE SAME judge model for the whole committee by reading `audit_judge_model`
    PINNED on-chain (`dendrad query modelregistry params`), instead of a divergent local config.

    Returns (model, source). FALLBACK chain (decreasing priority):
      1. `explicit`        -> manual override (--model-id), source "cli"
      2. on-chain          -> `audit_judge_model` from the modelregistry, source "chain"
      3. env               -> DENDRA_JUDGE_MODEL_ID, source "env"
      4. default           -> DEFAULT_JUDGE_MODEL (pinned MoE "qwen3:30b-a3b-instruct-2507-q4_K_M",
                              cf. modea/judge.py; the kit seats a judge only on the CPU, from
                              MOE_CPU_MIN_RAM_MB of RAM -- NEVER qwen3:4b as a judge), source "default"

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


def tx_from(frm, sub, *positionals, flags=()):
    # robust NONCE: account shared across processes -> retry on "account sequence mismatch"
    # (dendrad re-fetches the sequence each attempt -> passes once the in-flight tx is included). Bounded backoff.
    # ⛔ POSITIONALS AFTER `--`: a verdict is "0"/"1" today and safe, but the terminator is emitted
    # unconditionally. A separator present only where someone judged the input risky is a separator
    # missing the day that judgement is wrong. See modea/dendrad_argv.py.
    cmd = da.dendrad_argv(
        ("dendrad", "tx", "jobs"), sub, positionals,
        [*flags, "--from", frm, *_keyring().flags(), "--chain-id", _cid.chain_id(tuple(_node())),
         "--gas", "auto", "--gas-adjustment", "1.6", "--yes", *_keys(), *_node()])
    o = ""
    for attempt in range(6):
        o = run(cmd, stdin=_keyring().stdin())
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    return o


def query(sub, *positionals, flags=()):
    return run(da.dendrad_argv(("dendrad", "query", "jobs"), sub, positionals, [*flags, *_node()]))


def _tx_code(t):
    """The code a transaction response carries, in THREE states (the rule miner.py::_tx_code applies):
    the number on its `code:` line; 0 when the text IS a transaction response (it names its `txhash:`) and
    has no `code:` line -- proto3 omits a field at its zero value, so an absent code is a success, not a
    failure; None when the text is no transaction response at all (nothing was read)."""
    t = t or ""
    m = re.search(r'(^|\n)code: (\d+)', t)
    if m:
        return int(m.group(2))
    if re.search(r'(^|\n)txhash:\s*"?[A-Fa-f0-9]{64}', t):
        return 0
    return None


def _ok(t):
    # It used to require the `code: 0` line itself, which proto3 omits on every success: a vote that landed
    # would have read as one that did not.
    return _tx_code(t) == 0


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
    out = query("get-commit", key, flags=("--output", "json")) or ""
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


def anchored_commit_fields(key: str) -> dict:
    """Both anchored fields of a `create-commit`, or {} when unreadable.

    Returned together on purpose: telling a real prompt commitment from the LEGACY duplicate needs
    both values, and reading them in two separate calls would let them come from two different
    states of the chain. Fail closed like `anchored_root`: what cannot be parsed is not a marker.
    """
    out = query("get-commit", key, flags=("--output", "json")) or ""
    try:
        d = json.loads(out)
    except Exception:
        return {}
    node = d.get("commit") if isinstance(d, dict) else None
    if not isinstance(node, dict):
        node = d if isinstance(d, dict) else {}

    def _pick(names):
        for n in names:
            v = node.get(n)
            if v is not None:
                return str(v).strip()
        return ""

    # Only the two real spellings of the field. A looser alias list on a SECURITY reader is a way to
    # latch onto an unrelated field and mistake it for a commitment.
    return {"prompt": _pick(("promptCommit", "prompt_commit")),
            "result": _pick(("result", "resultCommit", "result_commit", "value"))}


def prompt_check(job_id, primary, rev, *, get_fields=None):
    """`revealed_prompt_matches` with the reason of its answer -> (True | False | None, reason).

    The reason separates what a later attempt can change from what it cannot. `unreadable`: the commit could
    not be read, a read tried again may settle it. Every other None is final for this reveal: `no-salt` (no
    `psalt`, or a field of a shape that cannot be hashed), `legacy` (the anchored prompt commitment equals the
    result commitment), `no-prompt-commit` (a commit read, with no prompt commitment in it)."""
    if not isinstance(rev, dict):
        return None, "no-salt"
    # THE REVEAL IS ATTACKER-SHAPED DATA, NOT JUST ATTACKER-CHOSEN CONTENT.
    # It arrives as `json.loads` of a blob the audited primary composed, so `psalt` and `prompt` can be
    # any JSON type -- a number, a list, an object -- not merely a wrong string. Feeding one of those to
    # the hashing primitive raises TypeError, and this function is called unguarded inside the polling
    # loop: a single fabricated reveal would take the whole judging channel down, for every job, until
    # an operator noticed. Refusing a shape we cannot hash is not an accusation: it says nothing about the
    # primary, only that there is nothing here to check.
    salt, question = rev.get("psalt"), rev.get("prompt")
    if not isinstance(salt, str) or not salt:
        return None, "no-salt"
    if question is not None and not isinstance(question, str):
        return None, "no-salt"
    f = (get_fields or anchored_commit_fields)(f"{job_id}__{primary}")
    if not isinstance(f, dict) or not f:
        return None, "unreadable"
    pc, rc = f.get("prompt") or "", f.get("result") or ""
    if not pc:
        return None, "no-prompt-commit"
    if rc and pc == rc:
        return None, "legacy"
    return (True, "match") if pc == rv.prompt_commitment(salt, question or "") else (False, "mismatch")


def revealed_prompt_matches(job_id, primary, rev, *, get_fields=None):
    """Does the revealed QUESTION match the one the primary committed BEFORE knowing the answer?

    THREE ANSWERS, AND THE THIRD IS WHY THIS IS NOT A BOOLEAN.
      True  -- the commitment reproduces: the question shown is the question committed.
      False -- it does not. The primary revealed a question other than the one it anchored, which is
               the evasion this exists to catch: grading an answer against a question picked after
               the answer was known.
      None  -- UNKNOWABLE. The shapes that reach it: a reveal with no `psalt`; an anchored prompt
               commitment EQUAL to the result commitment, which is what a build that cannot commit to the
               question anchors instead; a commit with no prompt commitment; a commit that could not be
               read. None is not evidence against the primary, and reading it as False would slash honest
               miners for the age of their build.
    ⛔ AND NONE IS NOT A LICENCE TO GRADE THE QUESTION THE REVEAL SHOWS. Every primary of the kit commits to
    its question since the prompt commitment shipped, so a reveal without a salt is what a primary that wants
    to choose its question after the fact sends: a wrong answer revealed with a question it answers right was
    voted VALID. `judge_revealed` therefore lets no vote rest on a question it could not verify: only the
    coherence stage, which reads the answer alone, may still vote (`prompt_check` says which None is final).
    """
    return prompt_check(job_id, primary, rev, get_fields=get_fields)[0]


# ANCHOR IDENTITY, NOT SEMANTIC SIMILARITY -- and the distinction decides the threshold.
# The judge used to score the (prompt, answer) pair the AUDITED party handed it through the reveal
# channel, without ever looking at what that party had ANCHORED on chain. A primary that botched a job
# could reveal a different, self-consistent pair, be acquitted, and get paid: the audit measured the
# internal coherence of a text of the primary's choosing, not the work delivered to the client.
#
# So the reveal is confronted with the commit at `<jobId>__<primary>`, which carries the primary's
# embedding of its answer (miner.py, `res.content_embed`).
#
# ⛔ ONLY A HIGH COSINE IS INFORMATIVE -- AND A HIGH COSINE IS NOT A PROOF OF IDENTITY EITHER.
# The juror computes the embedding with `modea.miner::answer_embedding`, the very function the miner anchored
# with, under ITS OWN DENDRA_EMBED_MODE and ITS OWN engine -- never with a method picked from the shape of the
# anchor, which the audited party supplies. Two machines configured with different embedders still produce
# vectors that cannot be compared: different lengths ABSTAIN, and a LOW cosine is indistinguishable between
# "the primary revealed something else" and "these two machines embed differently", so it ABSTAINS too --
# slashing on that ambiguity would punish an honest, misconfigured miner.
#
# What a cosine at or above the bar establishes is NARROWER than identity: the revealed answer lands where
# the anchored embedding lies. An edit that leaves the embedding in place passes -- one changed figure in a
# long answer was measured at 0.99998, above this bar. The gate stops a reveal that is a DIFFERENT answer; it
# does not stop a reveal that is the anchored answer with a detail corrected. Closing that takes a commitment
# to the answer's exact bytes on chain, which the commit message does not carry.
#
# The bar sits close to 1 because two machines running the same function on the same text differ only by
# their engines' arithmetic (measured GPU against CPU with Ollama 0.32.1 and nomic-embed-text: above 0.99999;
# that measurement covers that engine version only, another version or engine is outside it). It is
# deliberately NOT the chain's `semantic_threshold_bps`, which answers a different question (are two ANSWERS the same
# fact) and would be a number borrowed from another purpose. Lowering it widens the hole described above.
ANCHOR_IDENTITY_COS = 0.98


def anchor_reading(job_id, primary, answer, *, backend, get_root=None):
    """(cosine, stage, why) between the revealed answer and the commit the primary anchored.

    cosine None is an ABSTENTION, never a pass and never a slash, and `stage` says which state it is:
      anchor-unreadable  no readable anchor, or this judge's embedder would not run -- possibly transient;
      anchor-shape       the two vectors differ in length: the two machines embed with different settings.
    A computed cosine comes with stage "" and the caller compares it with ANCHOR_IDENTITY_COS."""
    root = (get_root or anchored_root)(f"{job_id}__{primary}")
    if not root:
        return None, "anchor-unreadable", "no readable anchor for the primary's answer"
    try:
        mine = answer_embedding(answer, backend)
    except Exception as e:  # noqa: BLE001 -- an embedder that will not run is a state, not a crash
        return None, "anchor-unreadable", (f"this judge could not embed the revealed answer "
                                           f"({type(e).__name__}: {' '.join(str(e).split())[:160]})")
    try:
        a = [float(x) for x in str(root).split(",") if x.strip()]
        b = [float(x) for x in str(mine).split(",") if x.strip()]
    except ValueError:
        return None, "anchor-unreadable", "the anchor is not a vector"
    if not a or not b:
        return None, "anchor-unreadable", "an empty vector"
    if len(a) != len(b):
        return None, "anchor-shape", (f"the anchor has {len(a)} dimensions and this judge's embedding {len(b)}: "
                                      "the two machines do not embed with the same setting "
                                      "(DENDRA_EMBED_MODE, DENDRA_EMBED_API_MODEL)")
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return None, "anchor-unreadable", "a zero vector"
    cos = sum(x * y for x, y in zip(a, b)) / (na * nb)
    return cos, "", f"cos={cos:.6f}"


def reveal_matches_anchor(job_id, primary, answer, *, backend, get_root=None):
    """The cosine of `anchor_reading`, or None when it cannot be known (an abstention)."""
    return anchor_reading(job_id, primary, answer, backend=backend, get_root=get_root)[0]


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
# The chain's own deferrals end in "the audit timeout will defer" / "will decide", or say the adjudication
# "does not conclude"; "no open dispute" is a dispute another juror closed first. None of these matched the
# list as it stood, and every normal deferral of an audit printed REAL FAILURE
# (x/jobs/keeper/msg_server_adjudicate.go carries the texts).
_ADJ_DEFERRED_HINTS = ("window", "fenetre", "fenêtre", "quorum", "not disputed", "non disputé",
                       "already", "resolved", "too early", "pending", "not yet",
                       "defer", "does not conclude", "will decide", "no open dispute", "carries no stake")


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


def word_salad(answer, judge_model, *, coherent_fn=None, twostage=True) -> bool:
    """Stage 1 of `decide_verdict` on its own: True when the COHERENCE stage reads `answer` as word salad.
    Only a definite False from `coherent_fn` (default `llm_coherent`) is word salad; True and None
    (unreadable) are not. `twostage` False switches the stage off, and `coherent_fn` is then not called.
    It needs no reference and no request cap, which is why `judge_revealed` can run it when those are
    missing."""
    return bool(twostage) and (coherent_fn or llm_coherent)(answer, model=judge_model) is False


def decide_verdict(answer, prompt, backend, judge_model, twostage=True, gen_retries=3, gen_delay=2.0,
                   coherent_fn=None, judge_fn=None, relevant_fn=None, relevance=True,
                   selfconsist=True, sc_temperature=0.7, multiok=True, multiok_fn=None, trace=None,
                   max_out=None):
    """Decides the verdict of an audited job. Returns True (VALID), False (INVALID/cheat) or None (ABSTENTION).
    What it returns is not yet what is POSTED: `judge_revealed` passes an INVALID through `divergence_guard`.

    `max_out` bounds each REFERENCE generation exactly as the audited request bounded the primary's answer
    (its `max_out`, 0 = the request set no cap, so the engine's own default -- the miner's reading of the same
    request). It is never a constant: the paid tiers go far past what a free request asks for, and a reference
    longer than the answer it is compared with costs the judging machine minutes for nothing, while one cut
    shorter could miss the fact the answer states. None = the caller supplies no request (offline benches);
    the worker always passes the request's value.

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
    ⛔ WHAT LAYER A BOUNDS, AND WHAT IT DOES NOT.
    The prompt handed to `decide_verdict` comes from `reveal_helpers.open_reveal`, i.e. from the
    sealed reveal composed by the AUDITED PRIMARY. On its own, that party supplies both sides of the
    comparison: `reveal_matches_anchor` confronts the ANSWER with the on-chain commit, never the
    question, so nothing in this layer distinguishes a question the client asked from one chosen to
    fit an answer already known. Layer A measures how well an answer matches a question; it cannot
    measure whether the question is the right one.
    What bounds that is a separate gate, run BEFORE any generation: `revealed_prompt_matches`
    reproduces the salted commitment the primary anchored in `prompt_commit` at commit time, and the
    loop stops the job when the revealed question does not open it. It is a caller-side duty, and
    that is its limit -- the chain does not require a juror to run it, so a certificate says nothing
    about whether it was run. Its second limit is the fleet: a miner build that cannot compute the
    commitment anchors the answer commitment twice, which reads as UNKNOWN and abstains, so the gate
    only bites once the miners it audits can commit to their questions.
    Generalized abstention -> no-quorum -> payment reclaimed + silence_slash: `*_fn` injectable
    (off-GPU selftests).
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

    if word_salad(answer, judge_model, coherent_fn=coherent_fn, twostage=twostage):
        _t("coherence")
        return False
    if relevance and relevant_fn(prompt, answer, model=judge_model) is False:
        _t("relevance")
        return None  # pro-honest ABSTENTION: off-topic-vs-prompt is NOT proof of cheating

    def _gen():  # one ref generation, retry on EXCEPTION only (GPU timeout); temp>0 (sampled)
        for attempt in range(gen_retries):
            try:
                if max_out is None:
                    out = backend.generate(prompt, temperature=sc_temperature)
                else:
                    out = backend.generate(prompt, max_out=max_out, temperature=sc_temperature)
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
    # THREE OUTPUTS HERE TOO, AND THE POLARITY MUST NOT DECIDE FOR US. `judge_fn` answers True /
    # False / None (unreadable). Consumed as a plain boolean, None is merely falsy: it does not
    # short-circuit to `sc-valid`, it walks on into (c) and can end at `slash`, which returns False --
    # an AFFIRMATIVE "invalid" vote that counts toward the 2/3 slash bar, not an abstention. The three
    # sibling checks in this function all land None on the abstaining side (`sc-diverge`,
    # `sc-unstable`, `multiok-unreadable`), two of them only because they are written under `not`.
    # This one is the odd polarity, so it says what it means. `sc-unreadable` is deliberately ABSENT
    # from `abstain_to_vote`: an unreadable comparison proves nothing, in either direction.
    _sc = judge_fn(r1, answer, model=judge_model)
    if _sc is None:
        _t("sc-unreadable")
        return None  # my own ref-vs-answer read failed -> nothing proven -> ABSTENTION
    if _sc:
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
        # THREE OUTPUTS, THREE STAGES. `multiok_fn` answers True (multiplicity PROVEN), False (none) or
        # None (its own verdict was UNREADABLE). Folding True and None into one stage looks harmless
        # here -- both abstain -- but `abstain_to_vote` turns this stage into a "1" VALID vote, so an
        # UNREADABLE check acquitted the audited miner. That is an unknown read as reassurance, on the
        # criterion that decides whether a cheater is slashed. The unreadable case keeps its own stage,
        # which `abstain_to_vote` does not list, so it stays what it is: an abstention.
        if both is True:
            _t("multiok")
            return None
        if both is None:
            _t("multiok-unreadable")
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
    # Readable between two identical refs, UNREADABLE between ref and answer: isolates the ONE call
    # site whose polarity used to let `None` walk on toward the slash instead of abstaining.
    sf_none = lambda a, b, **k: True if a == b else None
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
        # THE THIRD STATE OF THE REF-VS-ANSWER CHECK. Stable refs, and the comparison with the answer
        # is UNREADABLE: nothing is proven, so this abstains at 2 generations. Consumed as a plain
        # boolean it would instead fall through to the confirmation ref and end at `slash` -- an
        # affirmative "invalid" vote drawn from an unknown. The two expected values separate the
        # cases on their own: (None, 2) here against (False, 3) for a real divergence.
        ("SC-UNREADABLE ref-vs-answer -> abstention at 2 generations, never a slash",
         ["Paris", "Paris", "Paris"], "Berlin", {"judge_fn": sf_none}, None, 2),
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
    # 14-bis: AN UNREADABLE MULTIPLICITY CHECK GETS ITS OWN STAGE, AND THAT STAGE MUST NOT VOTE.
    # The two halves were each covered and the SEAM between them was not: `decide_verdict` abstains
    # (case 12) and `abstain_to_vote` is pure (case 15), but nothing checked that the stage carried
    # from one to the other refuses to become a vote. It did not: True and None shared a stage, so an
    # unreadable check cast a "1" = VALID for the audited miner. This case drives the whole chain.
    tr2 = {}
    b2 = SeqBackend(["7", "7"])
    got2 = decide_verdict("13", "prompt", b2, "m", coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                          relevance=True, selfconsist=True, gen_delay=0,
                          multiok_fn=lambda *a, **k: None, trace=tr2)   # A' UNREADABLE
    good = (got2 is None and tr2.get("stage") == "multiok-unreadable"
            and abstain_to_vote(tr2.get("stage", ""), True) is False)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 14-bis unreadable A' -> stage={tr2.get('stage')} (want multiok-unreadable) "
          f"and NEVER a vote")
    # 15: option α-(a) abstain_to_vote (PURE) -- vote "1" ONLY if armed AND ambiguity PROVEN.
    good = (abstain_to_vote("sc-diverge", True) is True and abstain_to_vote("multiok", True) is True
            and abstain_to_vote("relevance", True) is False and abstain_to_vote("gen-fail", True) is False
            and abstain_to_vote("sc-diverge", False) is False and abstain_to_vote("", True) is False
            # AN UNREADABLE MULTIPLICITY CHECK MUST NEVER BECOME A VOTE: it proves nothing, and the
            # vote it used to cast was "1" = VALID, i.e. an acquittal bought with an unknown.
            and abstain_to_vote("multiok-unreadable", True) is False)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 15 α-(a) abstain_to_vote: armed+ambiguous=vote 1, technical/disarmed=abstention")
    # 16: THE DIVERGENCE GUARD (PURE): a divergence abstains unless armed, word salad stays INVALID, an INVALID
    # from an unknown stage never passes, and a withheld INVALID never becomes a VALID vote.
    good = (divergence_guard(False, "slash", False) == (None, "divergence-held")
            and divergence_guard(False, "legacy", False) == (None, "divergence-held")
            and divergence_guard(False, "slash", True) == (False, "slash")
            and divergence_guard(False, "coherence", False) == (False, "coherence")
            and divergence_guard(False, "", True) == (None, "invalid-unattributed")
            and divergence_guard(True, "sc-valid", False) == (True, "sc-valid")
            and divergence_guard(None, "sc-unstable", False) == (None, "sc-unstable")
            and abstain_to_vote("divergence-held", True) is False
            and divergence_slash_armed("1") and not divergence_slash_armed(None)
            and not divergence_slash_armed("true") and not divergence_slash_armed(" 1"))
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 16 divergence guard: held unless exactly 1, coherence stays INVALID")
    print("SELFTEST LAYER A", "GREEN" if ok else "RED")
    return 0 if ok else 1


def abstain_to_vote(stage, enabled):
    """Option α-(a) (PURE, offline-testable): should an abstention become a "1" vote?
    True ONLY if the flag is armed AND the stage proves an AMBIGUITY (sc-diverge: the judge diverges from
    itself; multiok: multiple correct answers). relevance / gen-fail / others = technical
    undecidability -> we keep the abstention (never a false VALID out of complacency)."""
    # `multiok-unreadable` is deliberately ABSENT from this list: it means the multiplicity check could
    # not be read, which proves nothing. Only a PROVEN ambiguity may become a vote.
    return bool(enabled) and stage in ("sc-diverge", "multiok")


# ── THE DIVERGENCE GUARD (see DIVERGENCE_SLASH), as a pure function ───────────────────────────────────────
# The exit stages whose INVALID comes from comparing the answer with this judge's OWN references.
DIVERGENCE_STAGES = ("slash", "legacy")


def divergence_guard(verdict, stage, armed):
    """The verdict this judge may POST, given what `decide_verdict` returned and its exit stage.
    -> (verdict, stage). Pure, like `abstain_to_vote`, and only an INVALID is ever changed:
      coherence        stays INVALID: word salad is not a comparison with anything this judge generated;
      slash, legacy    stay INVALID only when `armed` (DENDRA_JUDGE_DIVERGENCE_SLASH exactly "1"),
                       otherwise ABSTENTION at stage `divergence-held`;
      any other stage  ABSTENTION at stage `invalid-unattributed`, armed or not: an INVALID this function
                       cannot attribute to a stage it knows is not one it may let through.
    VALID and abstentions pass unchanged. Neither new stage is listed by `abstain_to_vote`: a withheld
    INVALID never turns into a VALID vote."""
    if verdict is not False:
        return verdict, stage
    if stage == "coherence":
        return False, stage
    if stage in DIVERGENCE_STAGES:
        return (False, stage) if armed else (None, "divergence-held")
    return None, "invalid-unattributed"


# ── WHERE THIS JUDGE SITS, AND WHETHER IT ALREADY VOTED ───────────────────────────────────────────────────
def _run3(c, t=60):
    """(returncode, stdout, stderr) of one command, returncode None when it could not run. The streams stay
    apart: an answer is read from stdout alone and a status from stderr alone, so a warning on stderr cannot
    make a valid answer unparsable."""
    try:
        r = subprocess.run(c, capture_output=True, text=True, timeout=t, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", f"{type(e).__name__}: {e}"
    return r.returncode, r.stdout or "", r.stderr or ""


# The fields of `QueryAuditCommitteeResponse` (proto/dendra/jobs/v1/query.proto), in both spellings a CLI prints.
_JURY_FIELDS = frozenset({"members", "anchored_height", "anchoredHeight"})


def seat_reading(rc, out, err, my_id):
    """Does the jury ANCHORED on an audit seat this judge? Reads the result of `dendrad query jobs
    audit-committee <job> --output json` (`read_seat`). -> (state, why):
      seated      the anchored jury names this judge;
      not-seated  it does not. The chain counts no verdict from outside it (antievasion.go::auditVerdictTally),
                  so judging would spend an engine on a vote that weighs nothing;
      no-jury     the chain answers NotFound: no jury is anchored on this job (yet), nothing to judge now;
      unknown     anything else -- transport, an older dendrad, unreadable output. The judge JUDGES ANYWAY:
                  "could not read the jury" is not "not on the jury", and a seat skipped on a read failure
                  is a mute seat, which raises the bar for the whole jury.
    `members` absent from the answer is the EMPTY jury (proto3 omits an empty list), which seats nobody."""
    if rc is None:
        return "unknown", f"the jury query did not run ({' '.join(str(err).split())[:160]})"
    if rc != 0:
        if "code = NotFound" in str(err):
            return "no-jury", "no jury anchored on this job yet (NotFound)"
        lines = [ln.strip() for ln in str(err or out).splitlines() if ln.strip()]
        return "unknown", f"the anchored jury could not be read (rc={rc}: {(lines[-1] if lines else '')[:160]})"
    try:
        d = json.loads(out)
    except ValueError:
        return "unknown", "the anchored jury could not be read (rc=0, stdout is not JSON)"
    if not isinstance(d, dict):
        return "unknown", "the anchored jury could not be read (rc=0, not a JSON object)"
    if "members" not in d and set(d) - _JURY_FIELDS:
        # The empty jury is read from an absent `members` ONLY in the message this reader knows: an answer
        # carrying other keys is not that message, and reading it as "empty" would skip a seat on a guess.
        return "unknown", f"no members and unexpected field(s) {sorted(set(d) - _JURY_FIELDS)[:3]}: not the jury"
    members = d.get("members", [])
    if not isinstance(members, list):
        return "unknown", "the anchored jury's members are not a list"
    names = {str(m).strip() for m in members}
    if my_id in names:
        return "seated", f"seated among {len(names)} anchored juror(s)"
    return "not-seated", f"not among the {len(names)} anchored juror(s)"


def read_seat(job_id, my_id, runner=None):
    """`seat_reading` of the jury anchored on `job_id`, as the chain answers now."""
    argv = da.dendrad_argv(("dendrad", "query", "jobs"), "audit-committee", [job_id],
                           ["--output", "json", *_node()])
    return seat_reading(*(runner or _run3)(argv), my_id)


def own_verdict_reading(rc, out, err):
    """Is this judge's verdict already on chain? Reads the result of `dendrad query jobs get-commit
    <job>__verdict__<me> --output json` (`read_own_verdict`). -> "present" | "absent" | "unknown".
      present  a commit came back. The chain refuses a second one ("index already set"), so judging again
               would spend minutes of an engine on a vote that cannot land -- and a fresh judgment that
               came out differently could never replace the first one anyway;
      absent   the chain answers NotFound;
      unknown  anything else: the judge goes on, and the chain's own refusal settles a duplicate.
    "present" needs a PARSED commit object, never a substring: an older test that looked for a '0' or a '1'
    in the merged output read transport noise ("connection refused", a gas figure, an address) as "already
    voted" -- the judge skipped its vote and the jury lost a seat. When the answer is not definite, the
    judge goes on: a duplicate is refused by the chain, a skipped vote is lost."""
    if rc is None:
        return "unknown"
    if rc != 0:
        return "absent" if "code = NotFound" in str(err) else "unknown"
    try:
        d = json.loads(out)
    except ValueError:
        return "unknown"
    return "present" if isinstance(d, dict) and isinstance(d.get("commit"), dict) else "unknown"


def read_own_verdict(vkey, runner=None):
    """`own_verdict_reading` of the commit `vkey`, as the chain answers now."""
    argv = da.dendrad_argv(("dendrad", "query", "jobs"), "get-commit", [vkey], ["--output", "json", *_node()])
    return own_verdict_reading(*(runner or _run3)(argv))


def request_max_out(envelope):
    """The output cap of the audited request, read from its envelope at the relay (`req/<job>__<primary>`,
    deposited by client.submit_job with `max_out` in clear) EXACTLY as the miner read it
    (miner.py: `int(req.get("max_out", 0))`), so the references are bounded like the answer was.
    -> int, or None when unknown: no envelope, or a value the miner could not have read either. An envelope
    without the field is a request that set no cap (0), which is what the miner served -- the same reading
    on both sides, not a default."""
    if not isinstance(envelope, dict):
        return None
    try:
        return int(envelope.get("max_out", 0))
    except (TypeError, ValueError, OverflowError):
        return None


# ── ABSTENTIONS: WHICH ONES ARE RETRIED ───────────────────────────────────────────────────────────────────
# The abstentions that mean THIS JUDGE COULD NOT JUDGE -- an engine, the relay or the chain did not answer,
# or the judge model answered something no verdict can be read from (`sc-unreadable`, `multiok-unreadable`:
# the judgment stopped there exactly as it stops on a reference that could not be generated, `gen-fail`).
# They are retried after a growing pause (`retry_delay`). Every other abstention IS a judgment, and a judgment
# is given once per process: judging the same audit again until a sample comes out differently would turn
# sampling noise into a vote -- and with the divergence guard on, the only vote that noise can produce is
# VALID. A completed judgment is therefore final; a failed one is not.
RETRY_STAGES = frozenset({"judge-error", "gen-fail", "request-unreadable", "anchor-unreadable",
                          "sc-unreadable", "multiok-unreadable", "prompt-unreadable"})
# ⛔ A RETRY MUST NOT BE A SECOND DRAW AT AN ACQUITTAL. `sc-unreadable` and `multiok-unreadable` are reached only
# AFTER this judge's references agreed with each other (`decide_verdict`, (b) and (3d)): the question was not
# ambiguous to it. Judged again, the same audit samples its references again at a temperature above zero, and a
# divergence that comes out then is sampling noise -- which option alpha-(a) would turn into a VALID vote, the
# one vote noise can buy under the divergence guard. So once an audit has ended at one of these stages, its
# later attempts in this process may not vote on a proven ambiguity (main(), `agreed_refs`).
AGREED_REFS_STAGES = frozenset({"sc-unreadable", "multiok-unreadable"})


def ambiguity_vote_allowed(job_id, agreed_refs, flag) -> bool:
    """May option alpha-(a) turn a proven ambiguity into a VALID vote on this attempt? Only when the flag is on
    AND no earlier attempt on this audit ended after its references agreed (AGREED_REFS_STAGES)."""
    return bool(flag) and job_id not in agreed_refs


# ── THE COHERENCE READ, ONCE PER ANSWER ───────────────────────────────────────────────────────────────────
# The coherence stage is the one stage that votes INVALID under the divergence guard. Every retried attempt ran
# it again, so an answer that went through several retries drew several times at a false "word salad" -- and a
# retry stage such as `request-unreadable` can be retried until the chain closes the audit. A DEFINITE reading
# (coherent or not) of one answer, for one audit and one judge model, is made once per process and reused; an
# unreadable reading is not kept, it decides nothing. Bounded: the oldest reading goes first.
_COHERENCE_READ = {}
_COHERENCE_READ_MAX = 4096


def coherence_memo(job_id, answer, judge_model, coherent_fn=None):
    """A coherence function for `word_salad` / `decide_verdict` that reads `answer` at most once definitely for
    (job_id, sha256(answer), judge_model), from `coherent_fn` (default `llm_coherent`)."""
    key = (str(job_id), hashlib.sha256(str(answer).encode("utf-8", "replace")).hexdigest(), str(judge_model))

    def read(text, model=None):
        base = coherent_fn or llm_coherent
        if text != answer:
            return base(text, model=model)
        if key in _COHERENCE_READ:
            return _COHERENCE_READ[key]
        v = base(text, model=model)
        if v is True or v is False:
            while len(_COHERENCE_READ) >= _COHERENCE_READ_MAX:
                _COHERENCE_READ.pop(next(iter(_COHERENCE_READ)))
            _COHERENCE_READ[key] = v
        return v
    return read


def retry_delay(attempt, base=60.0, cap=1800.0):
    """Pause before trying again an audit this judge could not judge: `base` seconds, doubling at each
    attempt, capped at `cap`. The exponent is bounded BEFORE the power (see `noreveal_action`)."""
    exp = min(20, max(0, int(attempt) - 1))
    return min(cap, base * (2 ** exp))


# ── WHAT BECAME OF A VERDICT TRANSACTION ──────────────────────────────────────────────────────────────────
# Refusals at broadcast that say nothing about the verdict: the account's three processes racing for a
# sequence, or a mempool momentarily full. They are posted again later; counting them as the chain refusing
# a vote would raise the one red state on contention. MATCH PATTERNS against the node's own text.
_TRANSIENT_BROADCAST = ("account sequence mismatch", "tx already exists in cache", "mempool is full")


def _tx_inclusion(out, timeout=24):
    """The `query tx` text of the transaction broadcast in `out` once it is included at a height > 0, or ""
    when it was not seen included within `timeout` polls."""
    h = re.search(r'txhash:\s*"?([A-Fa-f0-9]{64})', out or "")
    if not h:
        return ""
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return q
        time.sleep(2)
    return ""


def verdict_post_outcome(broadcast, include=None):
    """-> (state, detail) for a `create-commit <job>__verdict__<me>` broadcast:
      anchored     included at a block, execution code 0 (absent = 0, see `_tx_code`);
      already-set  refused because this judge's verdict for the audit is already on chain ("index already
                   set"): the vote is there, the audit is done for this judge;
      refused      the chain refused it for another reason, which `detail` carries -- the one RED state;
      unconfirmed  no code was read: dendrad did not get it to the chain, or its inclusion was not seen.
    `include` is `_tx_inclusion`, injectable for the benches."""
    out = str(broadcast or "")
    code = _tx_code(out)
    if code is None:
        usage = da.cli_usage_error(out)
        return "unconfirmed", usage or (" ".join(out.split())[:300] or "(no output from the tx)")
    if code != 0:
        _c, raw = tx_reason(out)
        if "index already set" in out:
            return "already-set", raw or "index already set"
        if any(t in out for t in _TRANSIENT_BROADCAST):
            return "unconfirmed", f"{raw or ' '.join(out.split())[:300]} (code={code}, transient, at broadcast)"
        return "refused", f"{raw or ' '.join(out.split())[:300]} (code={code}, at broadcast)"
    block = (include or _tx_inclusion)(out)
    if not block:
        return "unconfirmed", "broadcast accepted, inclusion not seen within the wait"
    bcode = _tx_code(block)
    if bcode == 0:
        return "anchored", ""
    _c, raw = tx_reason(block)
    if "index already set" in block:
        return "already-set", raw or "index already set"
    return "refused", f"{raw or ' '.join(block.split())[:300]} (code={bcode}, at block)"


# ── WHAT THE JUDGE HAS DONE ───────────────────────────────────────────────────────────────────────────────
class JudgeLedger:
    """What this judge has done since its process started, written into its state file
    (modea/heartbeat.py::update_judge_counters).

    COUNTS, NOT A VERDICT ON THE JUDGE. Seats without a verdict are the normal state of a juror whose audits
    are still open, deferred, or abstained on: zero verdicts is reported, never alarmed. The one state this
    worker calls RED is a verdict the CHAIN REFUSED -- that one says a vote is not landing, whatever the
    judge decided. An abstention is printed ONCE per audit and stage, and counted at every attempt."""

    def __init__(self, now=None):
        self.since = int(time.time() if now is None else now)
        self.seated, self.seat_unread, self.unseated, self.already = set(), set(), set(), set()
        self.anchored = {"0": 0, "1": 0}
        self.refused = 0
        self.abstentions = {}
        self.last_refusal = None
        self._said = set()
        self.changed = False

    def seat(self, job_id, state):
        bucket = {"seated": self.seated, "unknown": self.seat_unread, "not-seated": self.unseated}.get(state)
        if bucket is not None and job_id not in bucket:
            bucket.add(job_id)
            self.changed = True

    def abstain(self, job_id, stage) -> bool:
        """Counts one abstention; True when this (audit, stage) has not been printed yet."""
        self.abstentions[stage] = self.abstentions.get(stage, 0) + 1
        self.changed = True
        if (job_id, stage) in self._said:
            return False
        self._said.add((job_id, stage))
        return True

    def verdict(self, v):
        self.anchored[v] = self.anchored.get(v, 0) + 1
        self.changed = True

    def already_on_chain(self, job_id):
        if job_id not in self.already:
            self.already.add(job_id)
            self.changed = True

    def refusal(self, job_id, detail, now=None):
        self.refused += 1
        self.last_refusal = {"at": int(time.time() if now is None else now), "job": job_id,
                             "why": " ".join(str(detail).split())[:240]}
        self.changed = True

    def doc(self) -> dict:
        return {"since": self.since, "seats_seen": len(self.seated), "seats_unread": len(self.seat_unread),
                "not_seated": len(self.unseated), "verdicts_anchored": dict(self.anchored),
                "verdicts_already_on_chain": len(self.already), "verdicts_refused": self.refused,
                "abstentions": dict(sorted(self.abstentions.items())), "last_refusal": self.last_refusal}

    def line(self) -> str:
        d = self.doc()
        return (f"seats {d['seats_seen']} (+{d['seats_unread']} unread), verdicts anchored "
                f"1:{d['verdicts_anchored'].get('1', 0)} 0:{d['verdicts_anchored'].get('0', 0)}, already on chain "
                f"{d['verdicts_already_on_chain']}, refused {d['verdicts_refused']}, abstentions {d['abstentions']}")


# ── ONE REVEALED AUDIT, DECIDED ───────────────────────────────────────────────────────────────────────────
def judge_revealed(job_id, primary, rev, *, backend, judge_model, max_out, get_root=None, get_fields=None,
                   divergence_armed=None, abstain_vote=None, verdict_fns=None):
    """The whole decision on ONE revealed audit -> (verdict True/False/None, stage, why, trace). Posts nothing.

    In order, cheapest first, and the judgment never runs on a text nobody committed to:
      1. the QUESTION: the revealed prompt must open the commitment the primary anchored (prompt-mismatch);
      2. the ANSWER: its embedding, computed here exactly as the miner anchored it, must reach
         ANCHOR_IDENTITY_COS (anchor-unreadable, anchor-shape, anchor-mismatch);
      3. a question that could not be VERIFIED is not graded: a commit that could not be read is tried again
         (prompt-unreadable); any other unverifiable question leaves the COHERENCE stage alone to vote, and
         abstains otherwise (prompt-unverified, final);
      4. the request's output cap must be known (request-unreadable), since the references are bounded by it;
         when it is not, the COHERENCE stage still runs on its own (`word_salad`): it needs no reference;
      5. the judgment (`decide_verdict`), then the divergence guard, then option alpha-(a).
    The coherence reading of the answer is made once per process (`coherence_memo`), retries included.
    `max_out` None = the request's envelope could not be read. `verdict_fns` passes stage functions to
    `decide_verdict` (offline benches); the guards default to this process's flags."""
    armed = DIVERGENCE_SLASH if divergence_armed is None else divergence_armed
    vote_ambiguity = ABSTAIN_VOTE if abstain_vote is None else abstain_vote
    tr = {}
    if not isinstance(rev, dict) or not isinstance(rev.get("answer"), str) or not isinstance(rev.get("prompt"), str):
        return None, "reveal-shape", "the reveal does not carry a text question and a text answer", tr
    # THE QUESTION FIRST: grading an answer against a question the audited party chose measures nothing. A
    # proven mismatch ABSTAINS rather than voting to slash, deliberately. It is strong evidence -- only the
    # holder of the miner key can compute the salt -- but not the ONLY path to a mismatch: a primary that
    # rotates its reveal key between the commit and the reveal derives a different salt and fails this check
    # having cheated at nothing. Withholding a certificate is reversible; a slash is not.
    pm, pm_why = prompt_check(job_id, primary, rev, get_fields=get_fields)
    if pm is False:
        return None, "prompt-mismatch", (f"the revealed QUESTION does not match the one {primary} committed on "
                                         "chain: an answer is not graded against a question chosen after the "
                                         "fact"), tr
    cos, astage, why = anchor_reading(job_id, primary, rev["answer"], backend=backend, get_root=get_root)
    if cos is None:
        return None, astage, why, tr
    if cos < ANCHOR_IDENTITY_COS:
        return None, "anchor-mismatch", (f"{why} < {ANCHOR_IDENTITY_COS}: the revealed answer is not the one "
                                         "anchored -- or the two machines embed differently, which this "
                                         "judge cannot tell apart, hence no vote"), tr
    if pm is None and pm_why == "unreadable":
        # Before the coherence stage: a later attempt reads the commit again, and runs that stage then.
        return None, "prompt-unreadable", (f"the commit {primary} anchored could not be read, so the revealed "
                                           "question could not be checked against it (tried again later)"), tr
    coherent = coherence_memo(job_id, rev["answer"], judge_model, (verdict_fns or {}).get("coherent_fn"))
    if max_out is None or pm is None:
        # WORD SALAD NEEDS NO CAP AND NO QUESTION. Under the divergence guard the coherence stage is the only one
        # that can still vote INVALID, and it reads the answer alone. An envelope missing at the relay -- aged
        # out of its retention, never deposited by a job opened outside the gateway, or held by another relay --
        # must not switch it off, and neither must a question that cannot be verified: it runs here, and only
        # what grades the answer against the question waits for both.
        if word_salad(rev["answer"], judge_model, coherent_fn=coherent, twostage=TWOSTAGE):
            tr.update(stage="coherence", refs=[], n_gen=0)
            verdict, stage = divergence_guard(False, "coherence", armed)
            return verdict, stage, (f"anchor cos {cos:.6f} >= {ANCHOR_IDENTITY_COS}, judged at stage {stage}, "
                                    "which reads the answer alone"), tr
        if pm is None:
            # ⛔ THE GRADING STOPS HERE: the question shown was not verified against the one committed (`pm_why`
            # says why), so grading the answer against it would grade it against a question its author chose.
            return None, "prompt-unverified", (f"the revealed question opens no prompt commitment of {primary} "
                                               f"({pm_why}): no vote on a question it may have chosen after the "
                                               "fact; the coherence stage read no word salad"), tr
        return None, "request-unreadable", ("the audited request's envelope could not be read at the relay: "
                                            "its output cap is unknown, and the references are bounded by it "
                                            "(the coherence stage read no word salad; tried again later)"), tr
    verdict = decide_verdict(rev["answer"], rev["prompt"], backend, judge_model,
                             twostage=TWOSTAGE, gen_retries=GEN_RETRIES, gen_delay=GEN_DELAY,
                             relevance=RELEVANCE, selfconsist=SELFCONSIST, sc_temperature=SC_TEMPERATURE,
                             multiok=MULTIOK, trace=tr, max_out=max_out,
                             **dict(verdict_fns or {}, coherent_fn=coherent))
    verdict, stage = divergence_guard(verdict, tr.get("stage", ""), armed)
    if verdict is None and abstain_to_vote(stage, vote_ambiguity):
        return True, stage, "ambiguity PROVEN -> VALID vote (alpha-(a), benefit of the doubt)", tr
    if stage == "divergence-held":
        return None, stage, (f"the answer diverges from this judge's references, and {DIVERGENCE_SLASH_ENV} is "
                             'not "1": a divergence abstains until a C3 pass authorizes it'), tr
    if verdict is None:
        return None, stage, _ABSTAIN_WHY.get(stage, "no verdict this judge can stand behind"), tr
    return verdict, stage, f"anchor cos {cos:.6f} >= {ANCHOR_IDENTITY_COS}, judged at stage {stage}", tr


# What each abstaining exit stage of `decide_verdict` means, for the one line printed per audit.
_ABSTAIN_WHY = {
    "relevance": "the answer is off-topic for the question: no vote on a confused question",
    "sc-diverge": "this judge disagrees with itself: the question is ambiguous",
    "sc-unstable": "the confirming reference diverged: not stable enough to vote",
    "sc-unreadable": "the comparison of the answer with this judge's reference was unreadable (tried again later)",
    "multiok": "the question admits several correct answers",
    "multiok-unreadable": "the multiplicity check was unreadable (tried again later)",
    "gen-fail": "this judge's references could not be generated (tried again later)",
    "invalid-unattributed": "an INVALID from a stage this judge does not know: never posted",
}


def _flush_ledger(ledger, force=False):
    """Writes the ledger's counts on this process's judge state when they changed. A failed write is said
    and changes nothing else: the counts are for the reader, the worker judges without them."""
    if not (ledger.changed or force):
        return
    try:
        from modea import heartbeat as _hb
        _hb.update_judge_counters(ledger.doc())
    except Exception as e:  # noqa: BLE001
        print(f"[judge] WARNING: the judge counters were not written ({type(e).__name__}: {e})")
    ledger.changed = False


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
    # THE BOUND ON ONE JUDGE CALL (DENDRA_JUDGE_TIMEOUT_S), checked before anything is judged: a value written
    # wrong is said here, once, rather than turning every audit into a "judge-error" abstention.
    try:
        _judge_timeout = judge_timeout_s()
    except ValueError as e:
        print(f"[judge] REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="registered miner acting as a committee member")
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--adjudicate", action="store_true", help="attempt adjudicate-dispute after the verdict (best effort)")
    ap.add_argument("--once", action="store_true", help="one pass, then exit (the same as --passes 1)")
    ap.add_argument("--passes", type=int, default=0, metavar="N",
                    help="exit after N passes; DEFAULT 0 = run until stopped. What a judge keeps between "
                         "passes (verdicts decided, judgments given, pauses) lives in this process only.")
    ap.add_argument("--retry-pause", type=float, default=60.0, metavar="SECONDS",
                    help="first pause before an audit this judge could not judge is tried again; it doubles "
                         "at each attempt, capped at 30 minutes. 0 = the next pass.")
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
    if a.passes < 0:
        ap.error(f"--passes {a.passes}: a number of passes is 0 (run until stopped) or more")
    if not (0.0 <= a.retry_pause < float("inf")):
        ap.error(f"--retry-pause {a.retry_pause}: a pause is a finite number of seconds, 0 or more")
    if a.once:
        a.passes = 1

    skpath = Path(a.keydir) / f"{a.id}.sk"
    if not skpath.exists():
        print(f"[judge] FATAL: X25519 key {skpath} missing — run miner.py --id {a.id} first")
        sys.exit(3)
    # Opened with the KEYRING's passphrase (modea/keyring.py), the one the daemon sealed it under; never
    # re-encrypted from here -- one writer per file, the daemon.
    try:
        sk = crypto.load_sk(str(skpath), _keyring().open_with)
    except (kring.KeyringError, crypto.KeyEnvelopeError) as e:
        print(f"[judge] FATAL: the keys cannot be opened -- {e} {getattr(e, 'hint', '')}".rstrip(), flush=True)
        sys.exit(3)

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
    # THE MODEL THIS PROCESS JUDGES WITH, WRITTEN WHERE ITS CONTAINER'S SELF-TEST READS IT (modea/heartbeat.py,
    # write_judge_state). It is resolved once, above, and every verdict uses it: the judge role the capacity
    # report declares (miner_selftest.py --judge-role) is read from THIS, never from a second resolution made
    # later against the chain of that moment. A failed write changes nothing here; the role then reads unknown.
    try:
        from modea import heartbeat as _judge_state
        _judge_state.write_judge_state(judge_model, judge_src)
    except Exception as e:  # noqa: BLE001 -- the state is for the self-test; the worker judges without it
        print(f"[judge] WARNING: the judge state was not written ({type(e).__name__}: {e}): "
              "the capacity report will declare this judge role unknown")
    # WHAT THIS PROCESS DOES, COUNTED FROM NOW and written ON the state above, never in its place: counts are
    # published only on a state this process wrote (modea/heartbeat.py::update_judge_counters).
    ledger = JudgeLedger()
    _flush_ledger(ledger, force=True)
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
    print(f"[judge] judge calls bounded at {_judge_timeout:g} s (DENDRA_JUDGE_TIMEOUT_S); a call that "
          "fails abstains on that audit only ('judge-error'), and the audit is tried again later")
    _dv_raw = os.environ.get(DIVERGENCE_SLASH_ENV)
    if DIVERGENCE_SLASH:
        print(f"[judge] {DIVERGENCE_SLASH_ENV}=1: an answer that diverges from this judge's references is "
              "voted INVALID")
    else:
        print(f"[judge] divergence guard ON ({DIVERGENCE_SLASH_ENV} is not 1): a divergence ABSTAINS; "
              "word salad is still voted INVALID")
        if _dv_raw not in (None, "", "0"):
            print(f"[judge] WARNING: {DIVERGENCE_SLASH_ENV}={_dv_raw!r} is neither 0 nor 1 -- read as the "
                  "guard ON")
    trace_dest = ""
    if TRACE_DEST:
        trace_dest = f"/tmp/judge-trace-{a.id}.jsonl" if TRACE_DEST == "1" else TRACE_DEST
        print(f"[judge] per-job TRACE -> {trace_dest} ({'CLEARTEXT (bench only)' if TRACE_PLAIN else 'hashes only'})")
    done = set()
    miss = {}        # job_id -> number of reveal-open failures (grace window)
    first_seen = {}  # job_id -> ts of the first attempt (abandon horizon, if the operator sets one)
    next_try = {}    # job_id -> ts before which we do not retry (growing backoff)
    abstained = set()  # job_id already announced as abstained: do not reprint on every attempt
    seat_of = {}     # job_id -> "seated" | "not-seated": an anchored jury does not change, it is read once
    tries = {}       # job_id -> attempts that ended without a judgment (RETRY_STAGES, no jury yet)
    decided = {}     # job_id -> "0"|"1" decided but not yet seen on chain: posted again, NEVER judged again
    agreed_refs = set()  # job_id whose attempt ended after its references agreed (AGREED_REFS_STAGES)
    passes = 0       # counted at the top, so a round skipped below (`continue`) is a pass too
    while not (a.passes and passes >= a.passes):
        passes += 1
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
                # ONLY THIS JUDGE'S SEATS. The chain counts a verdict only from the jury it ANCHORED on the
                # audit (antievasion.go::auditVerdictTally); every other audit costs an engine for a vote
                # that weighs nothing, and its absent reveal would skew the stage-2 statistics below. The
                # filter comes BEFORE the reveal is opened. A jury that cannot be read is judged anyway.
                seat = seat_of.get(job_id)
                if seat is None:
                    seat, seat_why = read_seat(job_id, a.id)
                    if seat in ("seated", "not-seated"):
                        seat_of[job_id] = seat
                    if seat == "unknown" and job_id not in ledger.seat_unread:
                        print(f"[judge] {a.id} {job_id}: {seat_why} -- judging it anyway (an unread jury is not "
                              "an absent seat)")
                ledger.seat(job_id, seat)
                if seat == "not-seated":
                    done.add(job_id)
                    continue
                if seat == "no-jury":
                    tries[job_id] = tries.get(job_id, 0) + 1
                    next_try[job_id] = _now + retry_delay(tries[job_id], base=a.retry_pause)
                    continue
                first_seen.setdefault(job_id, _now)
                rev = rv.open_reveal(a.relay, job_id, a.id, sk, primary)
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
                    if read_own_verdict(vkey) == "present":
                        ledger.already_on_chain(job_id)
                        done.add(job_id)
                        continue
                    _st0, _why0 = verdict_post_outcome(
                        tx_from(a.id, "create-commit", vkey, "0", "0", "verdict", flags=flags))
                    if _st0 == "already-set":
                        ledger.already_on_chain(job_id)
                        done.add(job_id)
                    elif _st0 == "refused":
                        ledger.refusal(job_id, _why0)
                        print(f"[judge] {a.id} VERDICT REFUSED BY THE CHAIN for {job_id} (no-reveal 0): {_why0}")
                    elif _st0 == "unconfirmed":
                        print(f"[judge] {a.id} verdict 0 for {job_id} not confirmed (will retry): {_why0}")
                    if _st0 == "anchored":
                        ledger.verdict("0")
                        print(f"[judge] {a.id} verdict=0 (REVEAL ABSENT after {miss[job_id]} attempts) for {job_id}")
                        if trace_dest:
                            _trace_write(trace_dest, {"ts": int(time.time()), "job_id": job_id,
                                                      "verdict": "0", "stage": "no-reveal", "n_gen": 0})
                        done.add(job_id)
                        if a.adjudicate:
                            # Do NOT discard the return value: a broadcast `code: 0` proves nothing.
                            # Execution is confirmed exactly as at the other adjudication site.
                            _st, _detail = adjudicate_outcome(tx_from(a.id, "adjudicate-dispute", flags=("--job-id", job_id)))
                            if _st == "ok":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> OK (dispute closed)")
                            elif _st == "deferred":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> deferred: {_detail[:140]}")
                            else:
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> FAILED: {_detail[:140]}")
                    continue
                slow.append((job_id, primary, rev, dispute_h))
            # -- PASS 2: JUDGE, the oldest dispute first (an unknown height, read as 0, last) -------------
            # Each audit runs under its OWN guard: an exception -- a judge call past its bound, an engine
            # that drops the connection -- becomes an abstention on THAT audit ("judge-error"), tried again
            # after a pause, instead of ending the pass and throwing away the references already generated.
            for job_id, primary, rev, _dh in sorted(slow, key=lambda s: (s[3] <= 0, s[3])):
                vkey = f"{job_id}__verdict__{a.id}"
                try:
                    # HAS THIS JUDGE ALREADY VOTED? Asked BEFORE judging, never after: the chain keeps the
                    # first verdict ("index already set"), so a second judgment can never land -- it only
                    # spends an engine, and one that came out differently would be refused on every pass.
                    if read_own_verdict(vkey) == "present":
                        ledger.already_on_chain(job_id)
                        decided.pop(job_id, None)
                        done.add(job_id)
                        continue
                    v = decided.get(job_id)
                    if v is None:
                        mo = request_max_out(relay_c.get(a.relay, "req", f"{job_id}__{primary}"))
                        verdict, stage, why, tr = judge_revealed(
                            job_id, primary, rev, backend=backend, judge_model=judge_model, max_out=mo,
                            abstain_vote=ambiguity_vote_allowed(job_id, agreed_refs, ABSTAIN_VOTE))
                        if trace_dest:
                            _rd = rev if isinstance(rev, dict) else {}   # the reveal is attacker-shaped data
                            rec = {"ts": int(time.time()), "job_id": job_id,
                                   "verdict": {True: "1", False: "0", None: "abstain"}[verdict],
                                   "stage": stage or "?", "n_gen": tr.get("n_gen", 0),
                                   "prompt_sha": _sha(str(_rd.get("prompt", ""))),
                                   "answer_sha": _sha(str(_rd.get("answer", ""))),
                                   "refs_sha": [_sha(r) for r in tr.get("refs", [])]}
                            if TRACE_PLAIN:  # BENCH ONLY (invariant #1: never cleartext in prod)
                                rec.update({"prompt": _rd.get("prompt"), "answer": _rd.get("answer"),
                                            "refs": tr.get("refs", [])})
                            _trace_write(trace_dest, rec)
                        if verdict is None:
                            if ledger.abstain(job_id, stage):
                                print(f"[judge] {a.id} ABSTENTION on {job_id} (stage {stage}): {why}")
                            if stage in AGREED_REFS_STAGES:
                                agreed_refs.add(job_id)
                            if stage in RETRY_STAGES:
                                tries[job_id] = tries.get(job_id, 0) + 1
                                next_try[job_id] = time.time() + retry_delay(tries[job_id], base=a.retry_pause)
                            else:
                                done.add(job_id)   # a judgment is given once per process (RETRY_STAGES)
                            continue
                        if verdict is True and stage in ("sc-diverge", "multiok"):
                            print(f"[judge] {a.id} AMBIGUOUS job {job_id} (stage {stage}) -> VALID vote "
                                  "(alpha-(a) benefit of the doubt, DENDRA_JUDGE_ABSTAIN_VOTE=1)")
                        v = verdict_commit(verdict)
                        decided[job_id] = v
                    st, detail = verdict_post_outcome(
                        tx_from(a.id, "create-commit", vkey, v, v, "verdict", flags=flags))
                    if st == "anchored":
                        ledger.verdict(v)
                        decided.pop(job_id, None)
                        done.add(job_id)
                        print(f"[judge] {a.id} verdict={v} ({'VALID' if v == '1' else 'INVALID'}) anchored for {job_id}")
                        if a.adjudicate:
                            adj = tx_from(a.id, "adjudicate-dispute", flags=("--job-id", job_id))
                            _st, _detail = adjudicate_outcome(adj)
                            if _st == "ok":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} -> OK (dispute closed)")
                            elif _st == "deferred":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} -> deferred (expected): {_detail[:140]}")
                            else:
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} -> REAL FAILURE — disputes will "
                                      f"not close (jobs_unresolved>0): {_detail}")
                    elif st == "already-set":
                        ledger.already_on_chain(job_id)
                        decided.pop(job_id, None)
                        done.add(job_id)
                        print(f"[judge] {a.id} {job_id}: this judge's verdict is already on chain -- done")
                    else:
                        # NEVER LOSE THE REASON: the real cause has been "key not found" (a keyring outside
                        # dendrad's default home) while the log offered a plausible guess. The verdict decided
                        # is KEPT and posted again; the audit is not judged again.
                        tries[job_id] = tries.get(job_id, 0) + 1
                        next_try[job_id] = time.time() + retry_delay(tries[job_id], base=a.retry_pause)
                        if st == "refused":
                            ledger.refusal(job_id, detail)
                            print(f"[judge] {a.id} VERDICT REFUSED BY THE CHAIN for {job_id} (posted again "
                                  f"later, not judged again): {detail}")
                        else:
                            print(f"[judge] {a.id} verdict for {job_id} NOT confirmed (posted again later): {detail}")
                except Exception as e:  # noqa: BLE001 -- one audit's failure never ends the pass
                    if ledger.abstain(job_id, "judge-error"):
                        print(f"[judge] {a.id} ABSTENTION on {job_id} (stage judge-error): "
                              f"{type(e).__name__}: {' '.join(str(e).split())[:200]} -- tried again later")
                    tries[job_id] = tries.get(job_id, 0) + 1
                    next_try[job_id] = time.time() + retry_delay(tries[job_id], base=a.retry_pause)
        except Exception as e:
            print(f"[judge] {a.id} loop: {type(e).__name__}: {e}")
        if ledger.changed:
            print(f"[judge] {a.id} counters: {ledger.line()}")
        _flush_ledger(ledger)
        if a.passes and passes >= a.passes:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
