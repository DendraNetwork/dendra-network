#!/usr/bin/env python3
"""Committee worker: judges the audited sample and commits its verdict on-chain.

Run by an already-registered miner (reuses its chain key + its X25519 key from `keydir`), in addition
to `miner.py`. Loop:
  1. discovers the `+disputed` jobs (audit) via `query jobs list-job` -- only those whose anchored jury
     (`query jobs audit-committee`) seats me, except those where I am the primary, except those I already
     voted on;
  2. fetches the primary's sealed reveal (`reveal_helpers.open_reveal`) -> (prompt, answer), and checks it
     against what the primary anchored: the question's commitment and the answer's embedding;
  3. computes my own answer (Ollama) on the prompt, bounded like the audited request was;
  4. judges "primary's answer == same fact as mine?" (`modea.judge.llm_judge`);
  5. commits the verdict on-chain: `create-commit "<jobId>__verdict__<minerId>" <0|1> <0|1> verdict`
     (reuses the existing message; `AdjudicateDispute` tallies these verdicts weighted by stake);
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
# Needed to tell a refusal (HTTP 4xx/5xx: the backend answered) from an outage (no answer at all), so
# that a 404 is not reported as "Ollama unreachable".
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea import keyring as kring
from modea import dendrad_argv as da
from modea import chain_id as _cid
from modea.exposure import InvalidFlag, public_enabled
from modea.miner import Miner, answer_embedding
from modea.judge import (llm_judge, llm_coherent, llm_relevant, llm_multi_ok, verdict_commit,
                         DEFAULT_JUDGE_MODEL, judge_timeout_s)
import relay_client as relay_c
import reveal_helpers as rv
import reveal_marker as rmk

# ADR-048 item 8: the chain id is read (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
NODE = os.environ.get("DENDRA_NODE", "")
# Explicit keyring directory. Without these flags `dendrad` looks for the keys in its own default home,
# while the Docker kit sets `DENDRA_KEYRING_DIR=/data/keys/cosmos` (`deploy/testnet-miner/docker-compose.yml`):
# every `create-commit <job>__verdict__<id>` and `adjudicate-dispute` would fail with "key not found", and
# the judge would run a 19 GB model, anchor nothing, and leave its jury seat silent while the bar of
# ⌈2/3 of anchored seats⌉ still counts it. Empty variables add no flag, so judges already running are
# unaffected.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")
HOME_DIR = os.environ.get("DENDRA_HOME", "")


def _keys():
    """The home flag. The keyring flags come from `_keyring()`: backend and directory, read from the disk."""
    return ["--home", HOME_DIR] if HOME_DIR else []


# The keyring, as the daemon reads it (modea/keyring.py): the backend from the state of the disk, and in
# `file` mode the passphrase, handed to dendrad on stdin: the same source for every process that signs for
# this miner. A verdict signed from another keyring than the daemon's would be refused as not coming from
# the miner's operator, and the seat would stay silent.
_KR = None


def _keyring():
    global _KR
    if _KR is None:
        _KR = kring.resolve(KEYRING_DIR or HOME_DIR or None)
    return _KR

JUDGE_MODEL_ID = os.environ.get("DENDRA_JUDGE_MODEL_ID", "")  # required when enforce_model_registry=ON
# Two stages: a coherence stage (word salad) before the same-fact stage. Measured with mistral-nemo on 294
# cases: salad 0/98, false 0/98, honest false negatives 1/98, per-judge gate and committee green. On by
# default; DENDRA_TWOSTAGE=0 disables it.
TWOSTAGE = os.environ.get("DENDRA_TWOSTAGE", "1") != "0"
# Relevance guard, in favour of the honest miner: an answer off topic for the prompt gets an abstention,
# never an "invalid" vote on a confused question. On by default; DENDRA_JUDGE_RELEVANCE=0 restores the
# soft-launch behaviour.
RELEVANCE = os.environ.get("DENDRA_JUDGE_RELEVANCE", "1") != "0"
# Layer A, judge self-consistency: the reference is generated K times at a temperature above 0, and a judge
# that disagrees with itself means an ambiguous question, so it abstains and never votes "invalid". This
# covers what the relevance guard misses (a question that is on topic but ambiguous, where every juror
# diverges the same way). On by default; DENDRA_JUDGE_SELFCONSIST=0 falls back to a single reference, kept
# for A/B runs despite its known defect.
SELFCONSIST = os.environ.get("DENDRA_JUDGE_SELFCONSIST", "1") != "0"
SC_TEMPERATURE = float(os.environ.get("DENDRA_JUDGE_SC_TEMP", "0.7"))  # temperature of the references (the same-fact call stays at 0)
# An absent reveal gets an abstention, and the abstention does not rely on a compensating mechanism: there
# is no clawback on silence and no `silence_slash_bps`. A silent primary is reachable only through the slash
# branch, which needs ⌈2/3⌉ of "0" verdicts, and an abstaining worker posts none. So a primary that takes
# jobs and never reveals loses nothing here, and the client fee stays held. This is written down because a
# guard justified by a mechanism elsewhere can be removed from under it without anyone noticing.
#
# An absent reveal is not proof of cheating: an infrastructure outage looks exactly the same, and voting
# "invalid" on it would charge a cheater's full penalty for a fault the miner did not commit (invariant 1).
# Instead of hardening the threshold, two stages make the two cases distinguishable. Stage 1 is a reveal
# marker anchored on chain, batched per epoch (its absence cannot be blamed on the relay, since anchoring
# does not go through the relay). Stage 2 is a correlation test with no transaction: a relay outage hits
# every primary, a silent primary is isolated. See `noreveal_verdict()`. While stage 1 is not anchored,
# abstention stays the default. On by default; 0 restores the unconditional "0" vote.
NOREVEAL_ABSTAIN = os.environ.get("DENDRA_JUDGE_NOREVEAL_ABSTAIN", "1") != "0"
# The two stages are disarmed by default, on purpose. Arming them allows a "0" vote on an absent reveal, and
# so a slash: an economic decision that must be an explicit, traceable authorisation, never an inference.
# Independently of this flag, `marker_stage_usable` keeps stage 1 disarmed while no primary anchors, so a
# partial rollout cannot produce a mass slash.
TWOSTAGE_NOREVEAL = os.environ.get("DENDRA_JUDGE_TWOSTAGE_NOREVEAL", "0") == "1"
# Layer A', the multiplicity stage at the slash threshold. Layer A misses prompts with several short correct
# answers: the judge's references agree (r1 = r2, a "reliable" reference), so a valid but different answer
# would be slashed, and every seat of a single-model committee would make the same mistake. A' requires the
# judge to affirm that the question does not admit both answers before voting "invalid". On by default;
# DENDRA_JUDGE_MULTIOK=0 disables it (A/B measurement on the bench). It mitigates in favour of the honest
# miner; the structural fix is a heterogeneous committee (Layer C).
MULTIOK = os.environ.get("DENDRA_JUDGE_MULTIOK", "1") != "0"
# Benefit of the doubt, armed by default: when ambiguity is positively detected (the judge disagrees with
# itself, sc-diverge, or several answers are correct, multiok), vote "1" instead of abstaining. The audit
# then vindicates and the honest party keeps its fee, bond and points, which ends the triple penalty of a
# jury without quorum (clawback, compounded silence slash, points at zero: measured at about 52-59k lost per
# judge with no hard slash). On an ambiguous prompt cheating cannot be detected at all, since there is no
# ground truth, so vindicating is the only honest option, and a cheater is already paid on the roughly 90%
# of jobs that are not audited. Never on a technical failure (relevance, generation failure, timeout): that
# is not a proven ambiguity, and abstaining stays correct. The multiplicity reply is read as such a proof, so
# a wrong answer that talks the judge model into a yes there gets a valid vote: modea/judge.py declares that
# rate next to the prompts (the injection counts) and in llm_multi_ok.
# Rollback: DENDRA_JUDGE_ABSTAIN_VOTE=0. An empty value counts as unset (deploy/testnet-miner/docker-compose.yml
# forwards it empty when the .env names none, so that a value set there reaches the judge): the default holds.
ABSTAIN_VOTE = (os.environ.get("DENDRA_JUDGE_ABSTAIN_VOTE") or "1") == "1"
# A divergence does not slash until a C3 pass allows it. The guard is on by default: unset, empty, "0" or any
# value other than exactly "1" makes a divergence abstain.
# The committee judges with the one model the chain pins (`resolve_judge_model`), while the runbook requires a
# heterogeneous committee (the soft-launch runbook) and `decide_verdict` notes that the single-model
# correlation is not closed: jurors running one model share its mistakes, so one mistake on an honest answer
# is repeated by every seat, and the slash bar of a small jury is a few seats. Until a dated C3 pass
# ("single-model committee x the models served") says otherwise, an invalid that comes from comparing the
# answer with this judge's own references (`slash`, `legacy`) is withheld. An invalid from the coherence stage
# compares the answer with nothing this judge generated (it says the answer is word salad), so it stays a vote.
# Setting "1" is an explicit authorisation given after that pass, never a tuning knob.
DIVERGENCE_SLASH_ENV = "DENDRA_JUDGE_DIVERGENCE_SLASH"


def divergence_slash_armed(value) -> bool:
    """May a divergence vote invalid? Only for the exact value "1": anything else -- unset, "0", "yes",
    " 1" -- keeps the guard, because a value that reads as consent without being one must not open a slash."""
    return value == "1"


DIVERGENCE_SLASH = divergence_slash_armed(os.environ.get(DIVERGENCE_SLASH_ENV))
# Trace per judged job, for diagnosis (the artifact does not identify jobs).
# DENDRA_JUDGE_TRACE=<path|1> writes one JSONL line per job: verdict, exit stage, number of generations,
# sha256 of the prompt, answer and references. Invariant 1 (no cleartext in the logs): the content itself
# is written only with DENDRA_JUDGE_TRACE_PLAIN=1 (bench only, never in production). Off by default.
TRACE_DEST = os.environ.get("DENDRA_JUDGE_TRACE", "")
TRACE_PLAIN = os.environ.get("DENDRA_JUDGE_TRACE_PLAIN", "0") == "1"
# Reliability under GPU load: this judge's reference inference is retried a bounded number of times (a
# transient timeout must not leave a verdict missing and the quorum below its floor), with an explicit
# abstention when it stays undecidable (never a false "valid" that would let a cheater escape).
GEN_RETRIES = max(1, int(os.environ.get("DENDRA_JUDGE_GEN_RETRIES", "3")))
GEN_DELAY = float(os.environ.get("DENDRA_JUDGE_GEN_DELAY", "2.0"))


def _node():
    return ["--node", NODE] if NODE else []


def run(c, t=600, stdin=""):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t, input=stdin)
    return (r.stdout or "") + (r.stderr or "")


def resolve_judge_model(explicit: str = ""):
    """Chooses the same judge model for the whole committee by reading `audit_judge_model`
    pinned on-chain (`dendrad query modelregistry params`), instead of a divergent local config.

    Returns (model, source). Fallback chain (decreasing priority):
      1. `explicit`        -> manual override (--model-id), source "cli"
      2. on-chain          -> `audit_judge_model` from the modelregistry, source "chain"
      3. env               -> DENDRA_JUDGE_MODEL_ID, source "env"
      4. default           -> DEFAULT_JUDGE_MODEL (pinned MoE "qwen3:30b-a3b-instruct-2507-q4_K_M",
                              cf. modea/judge.py; the kit seats a judge only on the CPU, from
                              MOE_CPU_MIN_RAM_MB of RAM -- never qwen3:4b as a judge), source "default"

    It never fails hard: a missing dendrad, a failed query, unreadable JSON or an empty field
    falls back to the next tier, because the worker must keep running while the chain is
    unreachable."""
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
    # Nonce retries: the account is shared across processes, so "account sequence mismatch" is retried
    # (dendrad reads the sequence again on each attempt and passes once the transaction in flight is
    # included). The backoff is bounded.
    # Positionals go after `--`. A verdict is "0" or "1" and would be safe without it, but the terminator
    # is emitted unconditionally: a separator added only where the input looks risky is missing the day
    # that judgement is wrong. See modea/dendrad_argv.py.
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
    """The code a transaction response carries, in three states (the rule miner.py::_tx_code applies):
    the number on its `code:` line; 0 when the text is a transaction response (it names its `txhash:`) and
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
    # Never require a `code: 0` line: proto3 omits it on every success, and a vote that landed would read as
    # one that did not.
    return _tx_code(t) == 0


def _norm_keys(d):
    """Adds snake_case aliases for the camelCase keys of a flat CLI dict (jobId->job_id,
    minerId->miner_id, encPubkey->enc_pubkey, State->state). dendrad may emit camelCase, and a parser that
    reads snake_case only would see zero jobs: audits never resolved, and an honest miner slashed over a
    JSON casing difference. Normalising at the edge accepts both conventions."""
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
    """[(job_id, state, miner_id)] via `list-job --output json` (the query exists: query_job.go::ListJob)."""
    rows = list_jobs_full()
    return None if rows is None else [(j, s, m) for j, s, m, _h in rows]


def list_jobs_full():
    """[(job_id, state, miner_id, dispute_height)] — same query, one more field.

    `dispute_height` is required by stage 1: it determines the epochs in which the reveal marker may
    legitimately be found. Deriving it from the current height would be wrong for a long-disputed job, and
    a marker looked up in the wrong epoch reads as "absent", that is a slash on an honest primary.
    """
    # Paginated, and None when the read failed. Without pagination this call stops at 100 jobs (the SDK's
    # `DefaultLimit`) and the judge would stop seeing part of the audits without a word: truncation only
    # ever looks like good news. `rv.query_all` lives in the module both workers import, so there is one
    # implementation.
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
    """Root anchored by a `create-commit` marker, or "" when there is none. Fails closed on unreadable
    output: what cannot be parsed is not a present marker, or a transport failure would clear a silent
    primary."""
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

    Returned together on purpose: telling a real prompt commitment from the legacy duplicate needs
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

    # Only the two real spellings of the field. A looser alias list on a security reader is a way to
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
    # The reveal is data shaped by the audited party, not only content it chose. It arrives as `json.loads`
    # of a blob the primary composed, so `psalt` and `prompt` can be any JSON type (a number, a list, an
    # object). The hashing primitive raises TypeError on those, and this function runs unguarded inside the
    # polling loop, so one fabricated reveal could stop all judging. Refusing a shape that cannot be hashed
    # is not an accusation: it only says there is nothing here to check.
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
    """Does the revealed question match the one the primary committed before knowing the answer?

    Three answers, and the third is why this is not a boolean.
      True  -- the commitment reproduces: the question shown is the question committed.
      False -- it does not. The primary revealed a question other than the one it anchored, which is
               the evasion this exists to catch: grading an answer against a question picked after
               the answer was known.
      None  -- unknowable. The shapes that reach it: a reveal with no `psalt`; an anchored prompt
               commitment equal to the result commitment, which is what a build that cannot commit to the
               question anchors instead; a commit with no prompt commitment; a commit that could not be
               read. None is not evidence against the primary, and reading it as False would slash honest
               miners for the age of their build.
    None is not a licence to grade the question the reveal shows either. Every primary of the kit commits to
    its question, so a reveal without a salt is what a primary that wants to choose its question after the
    fact would send, pairing a wrong answer with a question it does answer. `judge_revealed` therefore lets
    no vote rest on a question it could not verify: only the coherence stage, which reads the answer alone,
    may still vote (`prompt_check` says which None is final).
    """
    return prompt_check(job_id, primary, rev, get_fields=get_fields)[0]


# Anchor identity, not semantic similarity: the distinction decides the threshold.
# Scoring only the (prompt, answer) pair the audited party hands over through the reveal would let a primary
# that botched a job reveal a different, self-consistent pair, be acquitted and get paid: the audit would
# measure the coherence of a text of the primary's choosing, not the work delivered to the client. So the
# reveal is compared with the commit at `<jobId>__<primary>`, which carries the primary's embedding of its
# answer (miner.py, `res.content_embed`).
#
# Only a high cosine is informative, and even a high cosine is not a proof of identity. The juror computes
# the embedding with `modea.miner::answer_embedding`, the function the miner anchored with, under its own
# DENDRA_EMBED_MODE and engine, never with a method picked from the shape of the anchor, which the audited
# party supplies. Two machines configured with different embedders produce vectors that cannot be compared:
# different lengths abstain, and a low cosine cannot tell "the primary revealed something else" from "these
# machines embed differently", so it abstains too, since slashing on that ambiguity would punish an honest,
# misconfigured miner.
#
# A cosine at or above the bar establishes less than identity: the revealed answer lands where the anchored
# embedding lies. An edit that leaves the embedding in place passes (one changed figure in a long answer was
# measured at 0.99998, above the bar). The gate stops a reveal that is a different answer, not one that is
# the anchored answer with a detail corrected; closing that needs a commitment to the answer's exact bytes
# on chain, which the commit message does not carry.
#
# The bar sits close to 1 because two machines running the same function on the same text differ only by
# their engines' arithmetic (measured GPU against CPU with Ollama 0.32.1 and nomic-embed-text: above 0.99999,
# for that engine version only). It is deliberately not the chain's `semantic_threshold_bps`, which answers
# another question (are two answers the same fact). Lowering it widens the gap described above.
ANCHOR_IDENTITY_COS = 0.98


def anchor_reading(job_id, primary, answer, *, backend, get_root=None):
    """(cosine, stage, why) between the revealed answer and the commit the primary anchored.

    cosine None is an abstention, never a pass and never a slash, and `stage` says which state it is:
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
    """Has this primary anchored at least one marker over the epochs covering this job?

    Used only by the rollout guard (`marker_stage_usable`): if nobody anchors, stage 1 is not deployed
    and its "absence" proves nothing about anyone."""
    eb = rmk.EPOCH_BLOCKS_DEFAULT if epoch_blocks is None else epoch_blocks
    return any((get_commit(rmk.marker_key(e, primary)) or "").strip()
               for e in rmk.covering_epochs(dispute_height, eb, span))


def list_miner_ids():
    # Paginated like `list-job`, since the query stops at 100 otherwise. The `[]` fallback is kept on purpose,
    # because this reader decides nothing (it only names peers), but it never returns a prefix as the whole
    # list.
    rows = rv.query_all("list-miner", "miner", _node(), run)
    if rows is None:
        return []
    return [mm.get("miner_id", "") for mm in (_norm_keys(m) for m in rows) if mm.get("miner_id")]


def is_disputed(state: str) -> bool:
    return "+disputed" in state and "+resolved" not in state


# A non-OK `adjudicate-dispute` has two very different meanings, and one "deferred/failed" message that
# discards the chain's error would hide a blocking condition:
#   deferred -> legitimate and expected: window still open, quorum not reached, already resolved. Nothing
#               to do; the next pass retries.
#   FAILED   -> a real error (bad arguments, unfunded signer, signature). If adjudication really fails,
#               disputes never close, jobs_unresolved stays above 0 and the C3 triplet cannot be reached
#               whatever else is fixed, so this case is printed with the chain's own message.
# These are patterns matched against the node's error text, not messages of ours. They are English because
# the chain's texts are (no non-test Go file of chain/x or chain/app carries another language), and
# a node of an earlier consensus epoch cannot serve this network. The chain's deferrals end in "the audit
# timeout will defer" or "will decide", or say the adjudication "does not conclude"; "no open dispute" is a
# dispute another juror closed first (x/jobs/keeper/msg_server_adjudicate.go carries the texts).
_ADJ_DEFERRED_HINTS = ("window", "quorum", "not disputed",
                       "already", "resolved", "too early", "pending", "not yet",
                       "defer", "does not conclude", "will decide", "no open dispute", "carries no stake")


def _confirm_tx_text(out, timeout=24):
    """Confirms the execution of a broadcast tx via `query tx`. Returns (ok, block_text):
    ok=True iff the tx is included and its execution code is 0; `block_text` = the `query tx` output
    (carries the raw_log, i.e. the failure reason at block level), empty if never included. A broadcast
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
            return _ok(q), q   # included: ok per the execution code, and we return the block text
        time.sleep(2)
    return False, ""           # never included (timeout)


def tx_reason(text):
    """Extracts (code, raw_log) from a `query tx` output. -> (int|None, str).

    `raw_log` is the only field carrying the reason, and dendrad's YAML output places it after `events:`,
    a list thousands of characters long. A head truncation of the block text (a `[:400]` window) would
    discard the reason every time, and every dispute closed by another juror would read as a real failure.
    A field is read with a named extractor, never with a positional truncation."""
    t = str(text or "")
    m = re.search(r'(?:^|\n)raw_log:\s*(.*?)(?=\n[a-z_]+:|\Z)', t, re.S)
    raw = " ".join((m.group(1) if m else "").split()).strip("'\" ")
    c = re.search(r'(?:^|\n)code:\s*"?(\d+)"?', t)
    return (int(c.group(1)) if c else None), raw


def marker_state(job_id, primary, dispute_height, *, get_commit, get_list,
                 epoch_blocks=None, span=2):
    """Stage 1, judge side: has this primary anchored a marker covering this job? -> (state, reason).

    `True`  the job is present in an anchored marker, verified against its on-chain root.
    `False` the primary anchored nothing covering this job: an absence that cannot be blamed on the relay,
            since anchoring does not go through it. This is the only case where "0" is justified without
            further evidence.
    `None`  undecidable (a root anchored, but its list missing or not matching). No conclusion is drawn and
            stage 2 decides: an unavailability must never turn into a slash.

    The readers (`get_commit`, `get_list`) are injected so the decision is testable without a chain or
    a relay, and so a production case can be replayed exactly.
    """
    eb = rmk.EPOCH_BLOCKS_DEFAULT if epoch_blocks is None else epoch_blocks
    anchored_any = False
    for e in rmk.covering_epochs(dispute_height, eb, span):
        root = (get_commit(rmk.marker_key(e, primary)) or "").strip().lower()
        if not root:
            continue
        anchored_any = True
        payload = get_list(rmk.list_key(e, primary))
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None, (f"marker anchored for epoch {e} but list not found: membership can neither "
                          "be confirmed nor denied -> stage 2")
        if rmk.merkle_root(jobs) != root:
            return None, (f"the list for epoch {e} does not match the anchored root: unreliable data, "
                          "no conclusion drawn -> stage 2")
        if job_id in jobs:
            return True, f"job present in the anchored marker of epoch {e} (root verified)"
    if anchored_any:
        return False, ("the primary anchored markers over the window, but none of them contains this "
                       "job: it revealed for others and not for this one")
    return False, "no marker anchored by this primary over the epochs covering this job"


def marker_stage_usable(n_primaries_with_marker):
    """Is stage 1 usable on this network? -> bool.

    Rollout guard. On the day stage 1 is armed, no primary runs the version that anchors markers yet, so
    "marker absent" would be true for everyone and the committee would vote "0" against honest primaries:
    a mass slash caused by an incomplete rollout. This is stage 2's reasoning applied to stage 1: a
    universal absence is correlated, so it cannot be attributed to anyone. While no primary anchors,
    stage 1 is declared unavailable and everything falls back to stage 2; it arms itself as the rollout
    progresses, with no date and no switch to remember.
    """
    return int(n_primaries_with_marker) >= 1


def noreveal_verdict(job_id, primary, *, marker, others_ok, others_missing, my_missing_for_primary,
                     min_others=2):
    """The two stages as one pure, testable function -> ("0"|"abstention", reason).
    It posts nothing and reads nothing: it decides, and it is tested on its own.

    "Absent reveal" covers two opposite cases: a primary that did nothing (cheating) and an infrastructure
    outage, which is never charged to the honest party (invariant 1). Hardening a threshold does not
    separate them; the two states have to be made distinguishable.

    Stage 1, the anchored marker (deterministic). `marker` is True, False or None:
      · False: the primary anchored no marker covering this job before the deadline. That absence does not
        go through the relay, so a relay outage cannot explain it: the only case where "0" is justified
        without further evidence. Every judge reads the same chain and votes the same way, so a quorum can
        be reached.
      · None: stage 1 is not anchored on chain yet, so it is not used.
      · True: a marker is present. Not sufficient, because the audited party posts it itself: a cheater
        posts the marker and never publishes. Hence stage 2.

    Stage 2, correlation (the audited party cannot fake it, and it costs no transaction). A relay outage
    hits every primary; a silent primary is isolated. So:
      · other primaries deliver normally (`others_ok >= min_others`) and this one is silent: it is not the
        relay, "0";
      · everything is missing at once: infrastructure, abstention (invariant 1 holds);
      · too few other primaries observed: no conclusion, abstention, since with a single witness
        "isolated" and "correlated" cannot be told apart.

    Both stages are needed: a marker alone is self-issued, and correlation alone means each judge observes
    a different sample, votes diverge and no quorum is reached. Together they give determinism (stage 1)
    and evidence the audited party cannot fake (stage 2).
    """
    if marker is False:
        return "0", (f"stage 1: no reveal marker anchored by {primary} covers {job_id} — an absence "
                     "that cannot be blamed on the relay (anchoring does not go through it)")
    tot = others_ok + others_missing
    if tot < min_others:
        return "abstention", (f"stage 2 undecidable: only {tot} other primary/primaries observed "
                              f"(min {min_others}) — isolated and correlated are indistinguishable here")
    if others_missing and others_ok == 0:
        return "abstention", (f"stage 2: correlated — {others_missing} other primary/primaries are mute "
                              "too, none delivers: infrastructure outage, never billed to the honest party")
    if others_ok >= min_others and my_missing_for_primary > 0:
        return "0", (f"stage 2: isolated — {others_ok} other primary/primaries deliver normally while "
                     f"{primary} is mute on {my_missing_for_primary} job(s): this is not the relay")
    return "abstention", "stage 2: signal too weak to tell isolated from correlated"


def noreveal_action(now, first_seen, misses, *, grace, abandon_after=0.0,
                    backoff_base=30.0, backoff_max=600.0):
    """What to do when the reveal is not readable? -> (action, delay_before_next_attempt).

    Abstention is a state, not a verdict. Marking the job done for good on abstention would turn a
    transient outage into a permanent one: a reveal that becomes readable minutes later would never be
    picked up, the jury seat would stay empty and the held fee frozen. So the worker retries with a bounded
    exponential backoff, and gives up only when the operator has set a horizon.

    `abandon_after=0` (the default) never gives up: the chain decides when an audit is closed (the job
    leaves `+disputed` and disappears from the loop), and a worker giving up must never decide the fate of
    an audit. The cost is one relay GET every `backoff_max` seconds.
    """
    if misses < grace:
        return "wait", 0.0
    if abandon_after and (now - first_seen) >= abandon_after:
        return "give_up", 0.0
    # Exponential backoff from the first attempt after the grace window, capped. The exponent is bounded
    # before the computation, because the float product overflows past roughly a thousand attempts, and
    # retrying without a horizon (the default) makes a very large count the normal case.
    exp = min(20, max(0, misses - grace))          # 2**20 x 30 s >> backoff_max: the cap governs
    return "retry", min(backoff_max, backoff_base * (2 ** exp))


def adjudicate_outcome(out):
    """-> ("ok"|"deferred"|"FAILED", detail). Confirms execution (query tx), not broadcast.

    Three properties it keeps: (1) `code: 0` at broadcast means mempool, not execution, so "ok" is reported
    only once confirmed at block level, otherwise a dispute stays open without a word; (2) the deferred or
    FAILED reason lives in the block text (raw_log via query tx), not in the broadcast output; (3) that text
    is read through `tx_reason()` (a named field), never through a head truncation that would drop the
    raw_log."""
    s = " ".join(str(out or "").split())[:400]
    if not s:
        return "FAILED", "(no output from the tx)"
    if not _ok(out):
        # rejected at broadcast (mempool): the reason is read here (window or quorum: deferred, else failure)
        low = s.lower()
        return ("deferred", s) if any(h in low for h in _ADJ_DEFERRED_HINTS) else ("FAILED", s)
    ok, blocktext = _confirm_tx_text(out)
    if ok:
        return "ok", ""
    code, raw = tx_reason(blocktext)
    # Judge on the raw_log when it exists; only without it fall back to the truncated raw text.
    detail = raw or " ".join(str(blocktext or "").split())[:400] or s
    low = detail.lower()
    if any(h in low for h in _ADJ_DEFERRED_HINTS):
        return "deferred", detail
    if not raw:
        # No readable raw_log: the reason is unknown, and the outcome says so rather than report a real
        # failure, since a dispute closed by another juror produces exactly this shape.
        return "FAILED", (detail + f" (execution not confirmed at block, code={code}, "
                                   "no readable raw_log: reason UNKNOWN, not established)")
    return "FAILED", detail + f" (failed at block, code={code})"


def word_salad(answer, judge_model, *, coherent_fn=None, twostage=True) -> bool:
    """Stage 1 of `decide_verdict` on its own: True when the coherence stage reads `answer` as word salad.
    Only a definite False from `coherent_fn` (default `llm_coherent`) is word salad; True and None
    (unreadable) are not. `twostage` False switches the stage off, and `coherent_fn` is then not called.
    It needs no reference and no request cap, which is why `judge_revealed` can run it when those are
    missing."""
    return bool(twostage) and (coherent_fn or llm_coherent)(answer, model=judge_model) is False


def decide_verdict(answer, prompt, backend, judge_model, twostage=True, gen_retries=3, gen_delay=2.0,
                   coherent_fn=None, judge_fn=None, relevant_fn=None, relevance=True,
                   selfconsist=True, sc_temperature=0.7, multiok=True, multiok_fn=None, trace=None,
                   max_out=None):
    """Decides the verdict of an audited job. Returns True (valid), False (invalid, a cheat) or None (abstention).
    What it returns is not yet what is posted: `judge_revealed` passes an invalid through `divergence_guard`.

    `max_out` bounds each reference generation exactly as the audited request bounded the primary's answer
    (its `max_out`; 0 means the request set no cap, so the engine's own default, the miner's reading of the
    same request). It is never a constant: paid tiers go far past what a free request asks for, a reference
    longer than the answer it is compared with costs the judging machine minutes for nothing, and one cut
    shorter could miss the fact the answer states. None means the caller supplies no request (offline
    benches); the worker always passes the request's value.

    Stages:
      1. Coherence of the primary's answer first (word salad). An incoherent answer (mock garbage) is False
         without generating a reference. The order is what keeps it safe: garbage stays slashed, however
         off topic it is.
      2. Relevance: an answer entirely off topic for the prompt gets an abstention (None), without a
         reference. An on-topic lie goes on to 3 and 4. An unreadable result goes on as well.
      3. Self-consistency (Layer A, for the ambiguous but on-topic case that relevance lets through): this
         judge generates its reference twice at a temperature above 0 (sc_temperature; the same-fact call
         itself stays at 0).
         (a) the two references diverge: the judge disagrees with itself, the question is ambiguous, so it
             abstains (2 generations). A whole committee would share that ambiguity and veto an honest
             party together, as the gold negative artifacts showed;
         (b) the two references agree and the answer agrees: valid (2 generations);
         (c) the two references agree but the answer diverges: a third reference checks stability before
             any slash. A third that diverges means the reference is not that stable, so abstain; three
             that agree mean invalid (a cheater on a verifiable question).
         The asymmetry favours the honest miner on purpose: vindication early (2 generations), a slash only
         after confirmation (3 generations).
      3d. Multiplicity (Layer A'). Layer A alone leaves a false-slash rate of 15-18%, which this gate
         closes: at the slash threshold only (three agreeing references and a divergent answer; no call in
         (a) or (b)), the judge is asked whether the question admits several correct answers including its
         reference and the answer (llm_multi_ok, given both texts). A `YES` returns None at stage `multiok`,
         which `judge_revealed` posts as a valid vote (abstain_to_vote, the benefit of the doubt); an
         unreadable reply abstains at its own stage; a `NO` confirms invalid. On "give a prime number" the
         judge is stable (r1 = r2 = "7", a strong mode even at a
         temperature above 0), trusts its reference, and would slash "13" although it is correct, with every
         seat of a single-model committee doing the same. Self-consistency measures this judge's variance,
         not how many answers are correct. Known limits: some loss of detection on an obscure fact that
         people commonly get wrong, and the single-model correlation is not closed (Layer C is the
         structural fix).
      4. With selfconsist=False (DENDRA_JUDGE_SELFCONSIST=0): a single reference, kept for A/B runs, whose
         defect is documented (a random reference on an ambiguous question gives a false divergence). An
         unreadable comparison abstains at `legacy-unreadable`, never a vote.

    What Layer A bounds, and what it does not. The prompt handed to `decide_verdict` comes from
    `reveal_helpers.open_reveal`, that is from the sealed reveal the audited primary composed, so that party
    supplies both sides of the comparison: `reveal_matches_anchor` checks the answer against the on-chain
    commit, never the question. Layer A measures how well an answer matches a question; it cannot tell
    whether the question is the one the client asked.
    A separate gate bounds that, before any generation: `revealed_prompt_matches` reproduces the salted
    commitment the primary anchored in `prompt_commit` at commit time, and the loop stops the job when the
    revealed question does not open it. It is a duty of the caller, which is its first limit: the chain does
    not require a juror to run it, so a certificate says nothing about whether it ran. Its second limit is
    the fleet: a miner build that cannot compute the commitment anchors the answer commitment twice, which
    reads as unknown and abstains, so the gate only applies once the audited miners commit to their
    questions.
    Widespread abstention means no quorum: the audit is deferred with the fee still held, and nobody is
    paid or slashed for it (audit_sampling.go). The `*_fn` callables are injectable for selftests without a
    GPU.
    `trace` (an optional dict, for instrumentation) receives stage (the exit stage:
    coherence|relevance|sc-diverge|sc-valid|sc-unstable|sc-unreadable|multiok|multiok-unreadable|slash|
    legacy|legacy-unreadable|gen-fail), refs (the
    generations) and n_gen. No effect when None, the default."""
    coherent_fn = coherent_fn or llm_coherent
    judge_fn = judge_fn or llm_judge
    relevant_fn = relevant_fn or llm_relevant
    multiok_fn = multiok_fn or llm_multi_ok
    refs = []

    def _t(stage):  # instrumentation: exit stage + successful generations (no effect if trace is None)
        if trace is not None:
            trace["stage"] = stage
            trace["refs"] = list(refs)
            trace["n_gen"] = len(refs)

    if word_salad(answer, judge_model, coherent_fn=coherent_fn, twostage=twostage):
        _t("coherence")
        return False
    if relevance and relevant_fn(prompt, answer, model=judge_model) is False:
        _t("relevance")
        return None  # abstention: an answer off topic for the prompt is not proof of cheating

    def _gen():  # one reference generation, retried on an exception only (GPU timeout); sampled above temperature 0
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

    if not selfconsist:  # rollback: a single reference (a known defect, kept for A/B measurement on the bench)
        mine = _gen()
        _t("legacy")
        if mine is None:
            return None
        # Three outputs, like every other reading here: bool(None) would be False, an invalid vote that the
        # divergence guard posts once armed. An unreadable reading proves nothing, so it abstains.
        same = judge_fn(mine, answer, model=judge_model)
        if same is None:
            _t("legacy-unreadable")
            return None
        return bool(same)

    r1 = _gen()
    if r1 is None:
        _t("gen-fail")
        return None
    r2 = _gen()
    if r2 is None:
        _t("gen-fail")
        return None
    # (a) tests None first, like (b) below: an unreadable comparison of two of this judge's own references
    # proves nothing, and filed under `sc-diverge` it would become a valid vote through abstain_to_vote.
    _same = judge_fn(r1, r2, model=judge_model)
    if _same is None:
        _t("sc-unreadable")
        return None  # the reading of the judge's own two references failed: nothing proven, abstain
    if not _same:
        _t("sc-diverge")
        return None  # (a) the judge disagrees with itself: ambiguous, so abstain
    # Three outputs here too, tested explicitly. `judge_fn` answers True, False or None (unreadable). Used
    # as a plain boolean, None is only falsy: it would skip `sc-valid`, go on into (c) and could end at
    # `slash`, an affirmative "invalid" vote that counts toward the 2/3 slash bar. The sibling checks land
    # None on the abstaining side (`sc-diverge`, `sc-unstable`, `multiok-unreadable`), two of them only
    # because they are written under `not`; this one has the opposite polarity, so it tests None first.
    # `sc-unreadable` is deliberately absent from `abstain_to_vote`: an unreadable comparison proves
    # nothing either way.
    _sc = judge_fn(r1, answer, model=judge_model)
    if _sc is None:
        _t("sc-unreadable")
        return None  # this judge's reference-vs-answer reading failed: nothing proven, abstain
    if _sc:
        _t("sc-valid")
        return True  # (b) stable reference and agreeing answer: valid
    r3 = _gen()      # (c) confirmation before any slash
    if r3 is None:
        _t("gen-fail")
        return None
    if not judge_fn(r1, r3, model=judge_model):
        _t("sc-unstable")
        return None  # the third reference diverges: not stable enough, abstain
    # (3d) Layer A', the last check in favour of the honest miner before an "invalid" vote: the judge must
    # affirm that the question does not admit both its reference and the answer as correct. A `YES` (several
    # correct answers, so a stable reference proves nothing) ends at `multiok`, which the benefit of the doubt
    # posts as a valid vote; an unreadable reply abstains.
    if multiok:
        both = multiok_fn(prompt, r1, answer, model=judge_model)
        # Three outputs, three stages. `multiok_fn` answers True (several correct answers), False (one) or
        # None (unreadable). True and None both abstain here, but `abstain_to_vote` turns the `multiok`
        # stage into a "1" vote, so an unreadable check sharing that stage would acquit the audited miner
        # on an unknown. The unreadable case keeps its own stage, which `abstain_to_vote` does not list.
        if both is True:
            _t("multiok")
            return None
        if both is None:
            _t("multiok-unreadable")
            return None
    _t("slash")
    return False     # three references agree, the answer diverges, no multiplicity: invalid


def _selftest():
    """Checks stages 1 and 2 and the single-reference path of decide_verdict, without Ollama or a chain
    (injected stubs; the cases that generate use selfconsist=False, since Layer A has its own selftest,
    `--selftest-sc`). `python3 judge_worker.py --selftest`."""
    class FB:  # simulated backend: fails `fail` times, then returns `out`
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
    jv = lambda *a, **k: True   # same-fact valid
    jf = lambda *a, **k: False  # same-fact divergent (cheater)
    rel_t = lambda *a, **k: True    # relevance: the answer addresses the prompt
    rel_f = lambda *a, **k: False   # relevance: clearly off-topic
    rel_none = lambda *a, **k: None  # relevance unreadable: does not block
    LG = {"selfconsist": False}  # single-reference path (Layer A has --selftest-sc)
    cases = []
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("coherent+relevant+valid -> True, 1 inference", r is True and b.calls == 1))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_f, judge_fn=jv, relevant_fn=rel_t, **LG)
    cases.append(("incoherent -> False without inference", r is False and b.calls == 0))
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
    cases.append(("twostage off -> coherence stage skipped", r is True and seen["c"] == 0 and b.calls == 1))
    # Relevance guard:
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jf, relevant_fn=rel_f, **LG)
    cases.append(("coherent off-topic -> abstention without inference (no false invalid)", r is None and b.calls == 0))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_f, judge_fn=jv, relevant_fn=rel_f, **LG)
    cases.append(("incoherent garbage + off-topic -> False (coherence wins, garbage stays slashed)", r is False and b.calls == 0))
    b = FB(); r = decide_verdict("a", "p", b, "m", coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_none, **LG)
    cases.append(("relevance unreadable -> pipeline continues (True)", r is True and b.calls == 1))
    b = FB(); seen_r = {"n": 0}
    def rel_count(*a, **k):
        seen_r["n"] += 1; return False
    r = decide_verdict("a", "p", b, "m", relevance=False, coherent_fn=coh_t, judge_fn=jv, relevant_fn=rel_count, **LG)
    cases.append(("relevance off -> guard skipped (strict rollback)", r is True and seen_r["n"] == 0 and b.calls == 1))

    # --- tx_reason / adjudicate_outcome: the raw_log comes after `events:` ------------------------
    # The YAML reproduces what dendrad returns: an `events:` list several thousand characters long, then the
    # raw_log. A `[:400]` head truncation cannot see the reason, and the third case shows it.
    TX = ("code: 18\ncodespace: sdk\ndata: \"\"\nevents:\n"
          + "".join(f"- attributes:\n  - index: true\n    key: k{i}\n    value: v{i}\n  type: tx\n"
                    for i in range(60))
          + "gas_used: \"77\"\nheight: \"15040\"\ninfo: \"\"\nlogs: []\n"
            "raw_log: 'failed to execute message; message index: 0: dispute already resolved'\n"
            "timestamp: \"\"\ntxhash: AB\n")
    _c, _raw = tx_reason(TX)
    cases.append(("tx_reason reads the execution code", _c == 18))
    cases.append(("tx_reason finds the raw_log placed after events", "already resolved" in _raw))
    cases.append(("proof of the defect: a [:400] truncation cannot see the reason",
                  "already resolved" not in " ".join(TX.split())[:400]))
    cases.append(("raw_log absent -> reason empty, never invented",
                  tx_reason("code: 5\nevents: []\n")[1] == ""))
    # --- marker_state: stage 1, judge side ------------------------------------------------------------
    ROOT = rmk.merkle_root(["jobA", "jobB"])
    def gc_ok(key):    # the primary anchored for the epoch in which the job was disputed
        return ROOT if key == rmk.marker_key(rmk.epoch_of(6000, 600), "p1") else ""
    def gl_ok(key):
        return {"root": ROOT, "jobs": ["jobA", "jobB"]}
    cases.append(("stage 1: job in the anchored marker -> True",
                  marker_state("jobA", "p1", 6000, get_commit=gc_ok, get_list=gl_ok)[0] is True))
    cases.append(("stage 1: marker anchored but job absent from the list -> False (it revealed for "
                  "others, not for this one)",
                  marker_state("jobZ", "p1", 6000, get_commit=gc_ok, get_list=gl_ok)[0] is False))
    cases.append(("stage 1: no anchored marker -> False",
                  marker_state("jobA", "p1", 6000, get_commit=lambda k: "", get_list=gl_ok)[0] is False))
    cases.append(("stage 1: root anchored but list not found -> None (undecidable, not a slash)",
                  marker_state("jobA", "p1", 6000, get_commit=gc_ok, get_list=lambda k: None)[0] is None))
    cases.append(("stage 1: list that does not match the root -> None (unreliable data)",
                  marker_state("jobA", "p1", 6000, get_commit=gc_ok,
                               get_list=lambda k: {"jobs": ["other"]})[0] is None))
    # epoch boundary: disputed just before, revealed after -> the marker lands in the next epoch
    def gc_next(key):
        # the job is disputed at 5999 (epoch 9): its marker falls into epoch 10, the next one
        return ROOT if key == rmk.marker_key(rmk.epoch_of(5999, 600) + 1, "p1") else ""
    cases.append(("stage 1: marker in the next epoch -> found (no false absence at the boundary)",
                  marker_state("jobA", "p1", 5999, get_commit=gc_next, get_list=gl_ok,
                               epoch_blocks=600)[0] is True))
    # the rollout guard: as long as nobody anchors, stage 1 is unusable
    cases.append(("stage 1 disarmed while no primary anchors (otherwise a mass slash at rollout)",
                  marker_stage_usable(0) is False and marker_stage_usable(1) is True))

    # --- noreveal_verdict: the 2 stages ---------------------------------------------------------------
    NV = dict(job_id="j1", primary="p1", min_others=2)
    cases.append(("stage 1: marker absent -> \"0\" without further evidence (not blamable on the relay)",
                  noreveal_verdict(marker=False, others_ok=0, others_missing=0,
                                   my_missing_for_primary=1, **NV)[0] == "0"))
    cases.append(("stage 1: a present marker is not enough (self-issued evidence)",
                  noreveal_verdict(marker=True, others_ok=0, others_missing=3,
                                   my_missing_for_primary=1, **NV)[0] == "abstention"))
    cases.append(("stage 2: isolated (2 others deliver, this one mute) -> \"0\"",
                  noreveal_verdict(marker=None, others_ok=2, others_missing=0,
                                   my_missing_for_primary=1, **NV)[0] == "0"))
    cases.append(("stage 2: correlated (nobody delivers) -> abstention (invariant 1 preserved)",
                  noreveal_verdict(marker=None, others_ok=0, others_missing=4,
                                   my_missing_for_primary=1, **NV)[0] == "abstention"))
    cases.append(("stage 2: a single witness -> abstention (isolated and correlated indistinguishable)",
                  noreveal_verdict(marker=None, others_ok=1, others_missing=0,
                                   my_missing_for_primary=1, **NV)[0] == "abstention"))
    # A shape seen in production: four audits with no voter, all on the same primary, while the other
    # primaries received their verdicts. Correlation decides it correctly.
    cases.append(("four audits, same mute primary, the others deliver -> \"0\"",
                  noreveal_verdict(job_id="job1784919287094", primary="minerB", marker=None,
                                   others_ok=3, others_missing=0, my_missing_for_primary=4,
                                   min_others=2)[0] == "0"))
    cases.append(("a genuine relay collapse does not become a slash (all mute, 0 delivery)",
                  noreveal_verdict(job_id="j", primary="p", marker=None, others_ok=0,
                                   others_missing=7, my_missing_for_primary=9,
                                   min_others=2)[0] == "abstention"))

    # --- noreveal_action: abstention is a reversible state, not a permanent strike-off --------------
    NA = dict(grace=5, backoff_base=30.0, backoff_max=600.0)
    cases.append(("below the grace window -> wait", noreveal_action(100, 100, 3, **NA)[0] == "wait"))
    cases.append(("at the grace window -> retry with the base backoff",
                  noreveal_action(100, 100, 5, **NA) == ("retry", 30.0)))
    cases.append(("the backoff grows then caps", noreveal_action(1e9, 0, 9, **NA)[1] == 480.0
                  and noreveal_action(1e9, 0, 30, **NA)[1] == 600.0))
    # The regression guard: with no explicit horizon, the worker never gives up on its own.
    cases.append(("abandon_after=0 -> never 'give_up', even after ten years and 10^5 attempts",
                  all(noreveal_action(3.2e8, 0, m, **NA)[0] != "give_up" for m in (5, 100, 100000))))
    cases.append(("no OverflowError at 10^6 attempts (the exponent is bounded before the computation)",
                  noreveal_action(3.2e8, 0, 1000000, **NA)[1] == 600.0))
    cases.append(("horizon set and exceeded -> give up",
                  noreveal_action(1000, 0, 6, abandon_after=900, **NA)[0] == "give_up"))
    cases.append(("horizon set but not reached -> retry",
                  noreveal_action(1000, 500, 6, abandon_after=900, **NA)[0] == "retry"))
    cases.append(("the horizon does not short-circuit the grace window (3 attempts < 5 -> wait)",
                  noreveal_action(1e9, 0, 3, abandon_after=1, **NA)[0] == "wait"))

    _saved, _bc = _confirm_tx_text, "code: 0\ntxhash: " + "A" * 64 + "\n"
    try:
        globals()["_confirm_tx_text"] = lambda out, timeout=24: (False, TX)
        _st, _det = adjudicate_outcome(_bc)
        cases.append(("dispute closed by another juror -> 'deferred', not a real failure",
                      _st == "deferred" and "already resolved" in _det))
        globals()["_confirm_tx_text"] = lambda out, timeout=24: (False, "code: 7\nevents: []\n")
        _st2, _det2 = adjudicate_outcome(_bc)
        cases.append(("no raw_log -> FAILED that says the reason is unknown",
                      _st2 == "FAILED" and "UNKNOWN" in _det2))
    finally:
        globals()["_confirm_tx_text"] = _saved
    ok = True
    for name, p in cases:
        ok = ok and p
        print(f"  [{'OK' if p else 'FAIL'}] {name}")
    print("selftest decide_verdict:", "GREEN" if ok else "RED")
    return 0 if ok else 1


def _selftest_selfconsist():
    """Layer A, self-consistency, without Ollama or a chain: a sequential backend and a stub judge. Checks
    (verdict, number of generations) case by case. `python3 judge_worker.py --selftest-sc`."""

    class SeqBackend:
        """Returns a predefined sequence (simulating sampling above temperature 0) and counts the calls.
        An Exception element is raised, to test the timeout retry."""
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
    # Readable between two identical references, unreadable between reference and answer: isolates the one
    # call site where `None` could otherwise go on toward the slash instead of abstaining.
    sf_none = lambda a, b, **k: True if a == b else None
    mko_t = lambda *a, **k: True           # multiplicity (A′): both answers correct
    mko_n = lambda *a, **k: None           # multiplicity unreadable
    mko_seen = {"n": 0}
    def mko_count(*a, **k):
        mko_seen["n"] += 1; return False

    # (label, gens, answer, kwargs, expected verdict, expected calls)
    # Cases 1-9: the common stub multiok_fn=False makes the A' stage neutral for them (case 2 goes through it
    # and slashes). Cases 10-13: Layer A', multiplicity at the slash threshold.
    cases = [
        ("1 factual valid (stable refs, answer agrees)",
         ["Paris", "Paris"], "Paris", {}, True, 2),
        ("2 factual cheater (stable refs, answer diverges, 3rd confirms)",
         ["Paris", "Paris", "Paris"], "Berlin", {}, False, 3),
        ("3 ambiguous, answer diverges (2 refs diverge -> abstention)",
         ["Python", "Rust"], "JavaScript", {}, None, 2),
        ("4 ambiguous, answer equals one ref (refs diverge -> abstention anyway)",
         ["Python", "Rust"], "Rust", {}, None, 2),
        ("5 near-slash saved: 2 agree, answer diverges, 3rd diverges -> abstention",
         ["Paris", "Paris", "Lyon"], "Berlin", {}, None, 3),
        ("6 coherence first: garbage -> invalid, 0 generations",
         ["x"], "garbage", {"coherent_fn": coh_f}, False, 0),
        ("7 relevance: fully off-topic -> abstention, 0 generations",
         ["x"], "off-topic", {"relevant_fn": rel_f}, None, 0),
        ("8 rollback selfconsist=0: 1 ref, answer diverges -> invalid (the documented bug)",
         ["Python"], "JavaScript", {"selfconsist": False}, False, 1),
        ("9 GPU timeout on the 1st ref (3 attempts) -> abstention",
         [TimeoutError(), TimeoutError(), TimeoutError()], "Paris", {}, None, 3),
        ("10 A' multiple answers: stable refs \"7\", answer \"13\", multi_ok=YES -> abstention (no slash)",
         ["7", "7"], "13", {"multiok_fn": mko_t}, None, 3),
        ("11 A' consulted at the threshold: real cheater, multi_ok=NO -> invalid (capture preserved)",
         ["Paris", "Paris", "Paris"], "Berlin", {"multiok_fn": mko_count}, False, 3),
        ("12 A' unreadable -> pro-honest abstention (fail open, deliberate and recorded)",
         ["7", "7", "7"], "13", {"multiok_fn": mko_n}, None, 3),
        ("13 A' rollback multiok=0: direct slash, multi_ok never called",
         ["7", "7", "7"], "13", {"multiok": False, "multiok_fn": mko_count}, False, 3),
        # The third state of the reference-vs-answer check: stable references, and the comparison with the
        # answer is unreadable. Nothing is proven, so it abstains after 2 generations; read as a plain
        # boolean it would go on to the confirmation and end at `slash`, an "invalid" vote drawn from an
        # unknown. The expected values tell the cases apart: (None, 2) here, (False, 3) for a real divergence.
        ("self-consistency unreadable, ref-vs-answer -> abstention at 2 generations, never a slash",
         ["Paris", "Paris", "Paris"], "Berlin", {"judge_fn": sf_none}, None, 2),
    ]

    ok = True
    for label, gens, ans, kw, want_v, want_c in cases:
        b = SeqBackend(gens)
        kwargs = dict(coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                      relevance=True, selfconsist=True, gen_delay=0,   # gen_delay=0: fast test
                      multiok_fn=lambda *a, **k: False)                # A' neutral by default (cases 1-9)
        kwargs.update(kw)
        got_v = decide_verdict(ans, "prompt", b, "m", **kwargs)
        good = (got_v is want_v) and (b.calls == want_c)  # `is`: strict True/False/None
        ok = ok and good
        print(f"  [{'OK' if good else 'FAIL'}] {label} -> verdict={got_v} (want {want_v}), gen={b.calls} (want {want_c})")
    # 11-bis: mko_count is called once in case 11 (the stage is consulted at the threshold) and never in case 13 (rollback).
    good = mko_seen["n"] == 1
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 11-bis/13-bis multi_ok consulted 1x at the threshold, 0x on rollback (got {mko_seen['n']})")
    # 14: trace (instrumentation): exit stage + number of generations exposed.
    tr = {}
    b = SeqBackend(["7", "7"])
    got = decide_verdict("13", "prompt", b, "m", coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                         relevance=True, selfconsist=True, gen_delay=0, multiok_fn=mko_t, trace=tr)
    good = got is None and tr.get("stage") == "multiok" and tr.get("n_gen") == 3 and len(tr.get("refs", [])) == 3
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 14 trace filled -> stage={tr.get('stage')} (want multiok), n_gen={tr.get('n_gen')} (want 3)")
    # 14-bis: an unreadable multiplicity check gets its own stage, and that stage never votes. Case 12
    # covers `decide_verdict` and case 15 covers `abstain_to_vote`; this case drives the whole chain, so the
    # stage carried from one to the other is checked as well.
    tr2 = {}
    b2 = SeqBackend(["7", "7"])
    got2 = decide_verdict("13", "prompt", b2, "m", coherent_fn=coh_t, judge_fn=sf, relevant_fn=rel_t,
                          relevance=True, selfconsist=True, gen_delay=0,
                          multiok_fn=lambda *a, **k: None, trace=tr2)   # A' unreadable
    good = (got2 is None and tr2.get("stage") == "multiok-unreadable"
            and abstain_to_vote(tr2.get("stage", ""), True) is False)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 14-bis unreadable A' -> stage={tr2.get('stage')} (want multiok-unreadable) "
          f"and never a vote")
    # 15: abstain_to_vote (pure): vote "1" only when armed and the ambiguity is proven.
    good = (abstain_to_vote("sc-diverge", True) is True and abstain_to_vote("multiok", True) is True
            and abstain_to_vote("relevance", True) is False and abstain_to_vote("gen-fail", True) is False
            and abstain_to_vote("sc-diverge", False) is False and abstain_to_vote("", True) is False
            # An unreadable multiplicity check never becomes a vote: it proves nothing, and a "1" would be
            # an acquittal based on an unknown.
            and abstain_to_vote("multiok-unreadable", True) is False)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] 15 α-(a) abstain_to_vote: armed+ambiguous=vote 1, technical/disarmed=abstention")
    # 16: the divergence guard (pure): a divergence abstains unless armed, word salad stays invalid, an invalid
    # from an unknown stage never passes, and a withheld invalid never becomes a valid vote.
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
    print(f"  [{'OK' if good else 'FAIL'}] 16 divergence guard: held unless exactly 1, coherence stays invalid")
    print("selftest layer A", "GREEN" if ok else "RED")
    return 0 if ok else 1


def abstain_to_vote(stage, enabled):
    """Benefit of the doubt (pure, testable offline): should an abstention become a "1" vote?
    True only when the flag is armed and the stage proves an ambiguity (sc-diverge: the judge disagrees with
    itself; multiok: several correct answers). relevance, gen-fail and the other stages are technical
    failures, which keep the abstention (never a false "valid" out of leniency)."""
    # `multiok-unreadable` is deliberately absent from this list: it means the multiplicity check could
    # not be read, which proves nothing. Only a proven ambiguity may become a vote.
    return bool(enabled) and stage in ("sc-diverge", "multiok")


# -- The divergence guard (see DIVERGENCE_SLASH), as a pure function ---------------------------------------
# The exit stages whose invalid comes from comparing the answer with this judge's own references.
DIVERGENCE_STAGES = ("slash", "legacy")


def divergence_guard(verdict, stage, armed):
    """The verdict this judge may POST, given what `decide_verdict` returned and its exit stage.
    -> (verdict, stage). Pure, like `abstain_to_vote`, and only an invalid is ever changed:
      coherence        stays invalid: word salad is not a comparison with anything this judge generated;
      slash, legacy    stay invalid only when `armed` (DENDRA_JUDGE_DIVERGENCE_SLASH exactly "1"),
                       otherwise abstention at stage `divergence-held`;
      any other stage  abstention at stage `invalid-unattributed`, armed or not: an invalid this function
                       cannot attribute to a stage it knows is not one it may let through.
    Valid and abstentions pass unchanged. Neither new stage is listed by `abstain_to_vote`: a withheld
    invalid never turns into a valid vote."""
    if verdict is not False:
        return verdict, stage
    if stage == "coherence":
        return False, stage
    if stage in DIVERGENCE_STAGES:
        return (False, stage) if armed else (None, "divergence-held")
    return None, "invalid-unattributed"


# -- Where this judge sits, and whether it already voted ---------------------------------------------------
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
    """Does the jury anchored on an audit seat this judge? Reads the result of `dendrad query jobs
    audit-committee <job> --output json` (`read_seat`). -> (state, why):
      seated      the anchored jury names this judge;
      not-seated  it does not. The chain counts no verdict from outside it (antievasion.go::auditVerdictTally),
                  so judging would spend an engine on a vote that weighs nothing;
      no-jury     the chain answers NotFound: no jury is anchored on this job (yet), nothing to judge now;
      unknown     anything else (transport, an older dendrad, unreadable output). The judge judges anyway:
                  "could not read the jury" is not "not on the jury", and a seat skipped on a read failure
                  is a silent seat, which raises the bar for the whole jury.
    `members` absent from the answer is the empty jury (proto3 omits an empty list), which seats nobody."""
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
        # The empty jury is read from an absent `members` only in the message this reader knows: an answer
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
               would spend minutes of an engine on a vote that cannot land, and a new judgment could never
               replace the first one anyway;
      absent   the chain answers NotFound;
      unknown  anything else: the judge goes on, and the chain's own refusal settles a duplicate.
    "present" needs a parsed commit object, never a substring: looking for a '0' or a '1' in the merged
    output would read transport noise ("connection refused", a gas figure, an address) as "already voted",
    and the jury would lose a seat. When the answer is not definite the judge goes on: a duplicate is
    refused by the chain, while a skipped vote is lost."""
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
    deposited by client.submit_job with `max_out` unencrypted) exactly as the miner read it
    (miner.py: `int(req.get("max_out", 0))`), so the references are bounded like the answer was.
    -> int, or None when unknown: no envelope, or a value the miner could not have read either. An envelope
    without the field is a request that set no cap (0), which is what the miner served: the same reading on
    both sides, not a default."""
    if not isinstance(envelope, dict):
        return None
    try:
        return int(envelope.get("max_out", 0))
    except (TypeError, ValueError, OverflowError):
        return None


# -- Abstentions: which ones are retried -------------------------------------------------------------------
# Some abstentions mean this judge could not judge: an engine, the relay or the chain did not answer, or the
# judge model answered something no verdict can be read from (`sc-unreadable`, `multiok-unreadable`,
# `legacy-unreadable`, where the judgment stops exactly as it does on a reference that could not be
# generated, `gen-fail`). Those are retried
# after a growing pause (`retry_delay`). Every other abstention is a judgment, given once per process: judging
# the same audit again until a sample comes out differently would turn sampling noise into a vote, and with
# the divergence guard on, the only vote noise can produce is "valid". A completed judgment is final; a failed
# one is not.
RETRY_STAGES = frozenset({"judge-error", "gen-fail", "request-unreadable", "anchor-unreadable",
                          "sc-unreadable", "multiok-unreadable", "legacy-unreadable", "prompt-unreadable"})
# A retry must not be a second draw at an acquittal. `sc-unreadable` and `multiok-unreadable` are reached only
# after this judge's references agreed with each other (`decide_verdict`, (b) and (3d)): the question was not
# ambiguous to it. Judged again, the audit samples its references again above temperature 0, and a divergence
# then is sampling noise, which the benefit of the doubt would turn into a valid vote. So once an audit has
# ended at one of these stages, its later attempts in this process may not vote on a proven ambiguity
# (main(), `agreed_refs`).
AGREED_REFS_STAGES = frozenset({"sc-unreadable", "multiok-unreadable"})


def ambiguity_vote_allowed(job_id, agreed_refs, flag) -> bool:
    """May the benefit of the doubt turn a proven ambiguity into a valid vote on this attempt? Only when the flag
    is on and no earlier attempt on this audit ended after its references agreed (AGREED_REFS_STAGES)."""
    return bool(flag) and job_id not in agreed_refs


# -- The coherence read, once per answer -------------------------------------------------------------------
# The coherence stage is the one stage that votes invalid under the divergence guard. Running it again at every
# retry would give an answer several draws at a false "word salad", and a stage such as `request-unreadable`
# can be retried until the chain closes the audit. So a definite reading (coherent or not) of one answer, for
# one audit and one judge model, is made once per process and reused; an unreadable reading decides nothing and
# is not kept. The memo is bounded, and the oldest reading goes first.
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
    attempt, capped at `cap`. The exponent is bounded before the power (see `noreveal_action`)."""
    exp = min(20, max(0, int(attempt) - 1))
    return min(cap, base * (2 ** exp))


# -- What became of a verdict transaction ------------------------------------------------------------------
# Refusals at broadcast that say nothing about the verdict: the account's three processes racing for a
# sequence, or a mempool that is momentarily full. They are posted again later; counting them as the chain
# refusing a vote would raise the one red state on mere contention. Patterns matched against the node's text.
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
      refused      the chain refused it for another reason, which `detail` carries -- the one red state;
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


# -- What the judge has done -------------------------------------------------------------------------------
class JudgeLedger:
    """What this judge has done since its process started, written into its state file
    (modea/heartbeat.py::update_judge_counters).

    Counts, not a verdict on the judge. Seats without a verdict are the normal state of a juror whose audits
    are still open, deferred, or abstained on: zero verdicts is reported, never raised as an alarm. The one
    state this worker calls red is a verdict the chain refused, which means a vote is not landing whatever the
    judge decided. An abstention is printed once per audit and stage, and counted at every attempt."""

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


# -- One revealed audit, decided ---------------------------------------------------------------------------
def judge_revealed(job_id, primary, rev, *, backend, judge_model, max_out, get_root=None, get_fields=None,
                   divergence_armed=None, abstain_vote=None, verdict_fns=None):
    """The whole decision on one revealed audit -> (verdict True/False/None, stage, why, trace). Posts nothing.

    In order, cheapest first, and the judgment never runs on a text nobody committed to:
      1. the question: the revealed prompt must open the commitment the primary anchored (prompt-mismatch);
      2. the answer: its embedding, computed here exactly as the miner anchored it, must reach
         ANCHOR_IDENTITY_COS (anchor-unreadable, anchor-shape, anchor-mismatch);
      3. a question that could not be verified is not graded: a commit that could not be read is tried again
         (prompt-unreadable); any other unverifiable question leaves the coherence stage alone to vote, and
         abstains otherwise (prompt-unverified, final);
      4. the request's output cap must be known (request-unreadable), since the references are bounded by it;
         when it is not, the coherence stage still runs on its own (`word_salad`): it needs no reference;
      5. the judgment (`decide_verdict`), then the divergence guard, then the benefit of the doubt.
    The coherence reading of the answer is made once per process (`coherence_memo`), retries included.
    `max_out` None = the request's envelope could not be read. `verdict_fns` passes stage functions to
    `decide_verdict` (offline benches); the guards default to this process's flags."""
    armed = DIVERGENCE_SLASH if divergence_armed is None else divergence_armed
    vote_ambiguity = ABSTAIN_VOTE if abstain_vote is None else abstain_vote
    tr = {}
    if not isinstance(rev, dict) or not isinstance(rev.get("answer"), str) or not isinstance(rev.get("prompt"), str):
        return None, "reveal-shape", "the reveal does not carry a text question and a text answer", tr
    # The question first: grading an answer against a question the audited party chose measures nothing. A
    # proven mismatch abstains rather than voting to slash, on purpose. It is strong evidence (only the holder
    # of the miner key can compute the salt), but not the only way to a mismatch: a primary that rotates its
    # reveal key between the commit and the reveal derives a different salt and fails this check without
    # having cheated. Withholding a certificate can be undone; a slash cannot.
    pm, pm_why = prompt_check(job_id, primary, rev, get_fields=get_fields)
    if pm is False:
        return None, "prompt-mismatch", (f"the revealed question does not match the one {primary} committed on "
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
        # Word salad needs no cap and no question. Under the divergence guard the coherence stage is the only one
        # that can still vote invalid, and it reads the answer alone. An envelope missing at the relay (past its
        # retention, never deposited by a job opened outside the gateway, or held by another relay) must not
        # switch it off, and neither must a question that cannot be verified: it runs here, and only the stages
        # that grade the answer against the question wait for both.
        if word_salad(rev["answer"], judge_model, coherent_fn=coherent, twostage=TWOSTAGE):
            tr.update(stage="coherence", refs=[], n_gen=0)
            verdict, stage = divergence_guard(False, "coherence", armed)
            return verdict, stage, (f"anchor cos {cos:.6f} >= {ANCHOR_IDENTITY_COS}, judged at stage {stage}, "
                                    "which reads the answer alone"), tr
        if pm is None:
            # The grading stops here: the question shown was not verified against the one committed (`pm_why`
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
        return True, stage, "ambiguity proven -> valid vote (alpha-(a), benefit of the doubt)", tr
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
    "legacy-unreadable": "the comparison of the answer with this judge's single reference was unreadable "
                         "(tried again later)",
    "gen-fail": "this judge's references could not be generated (tried again later)",
    "invalid-unattributed": "an invalid vote from a stage this judge does not know: never posted",
}


def _flush_ledger(ledger, force=False):
    """Writes the ledger's counts on this process's judge state when they changed. A failed write is logged
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
    """One JSONL line per judged job (bench instrumentation). Best effort: a failed trace must never prevent
    a verdict. Invariant 1: hashes only, unless DENDRA_JUDGE_TRACE_PLAIN=1."""
    try:
        with open(dest, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[judge] trace not written ({type(e).__name__}) — verdict unaffected")


def main():
    if "--selftest" in sys.argv:  # logic check without a GPU; skips the required arguments
        sys.exit(_selftest())
    if "--selftest-sc" in sys.argv:  # Layer A (self-consistency), without a GPU
        sys.exit(_selftest_selfconsist())
    # Invariant 1 (no cleartext in any log), enforced rather than documented. `TRACE_PLAIN` writes the prompt
    # and the answer in plaintext to a JSONL file, the only path in the stack by which cleartext can reach a log,
    # so under public exposure the start is refused.
    try:
        _public = public_enabled(os.environ.get("DENDRA_PUBLIC"))
    except InvalidFlag as e:
        print(f"[judge] refused: {e}", file=sys.stderr)
        sys.exit(2)
    if _public and TRACE_PLAIN:
        print("[judge] refused: DENDRA_PUBLIC=1 together with DENDRA_JUDGE_TRACE_PLAIN=1 — the cleartext "
              "trace would write the user's prompt and answer to a file. Cleartext touches neither the "
              "chain, nor the relay, nor a log. Remove DENDRA_JUDGE_TRACE_PLAIN (the hash-only trace "
              "remains available via DENDRA_JUDGE_TRACE).", file=sys.stderr)
        sys.exit(2)
    # The bound on one judge call (DENDRA_JUDGE_TIMEOUT_S), checked before anything is judged: a value written
    # wrong is reported here, once, rather than turning every audit into a "judge-error" abstention.
    try:
        _judge_timeout = judge_timeout_s()
    except ValueError as e:
        print(f"[judge] refused: {e}", file=sys.stderr)
        sys.exit(2)
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="registered miner acting as a committee member")
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--adjudicate", action="store_true", help="attempt adjudicate-dispute after the verdict (best effort)")
    ap.add_argument("--once", action="store_true", help="one pass, then exit (the same as --passes 1)")
    ap.add_argument("--passes", type=int, default=0, metavar="N",
                    help="exit after N passes; the default, 0, runs until stopped. What a judge keeps between "
                         "passes (verdicts decided, judgments given, pauses) lives in this process only.")
    ap.add_argument("--retry-pause", type=float, default=60.0, metavar="SECONDS",
                    help="first pause before an audit this judge could not judge is tried again; it doubles "
                         "at each attempt, capped at 30 minutes. 0 = the next pass.")
    ap.add_argument("--epoch-blocks", type=int, default=rmk.EPOCH_BLOCKS_DEFAULT,
                    help="size of the reveal-marker epoch, in blocks. It must match the value the reveal "
                         "workers use: two different partitions look for the marker in two different epochs, "
                         "and 'not found' reads as 'absent'.")
    ap.add_argument("--abandon-after", type=float, default=0.0, metavar="SECONDS",
                    help="horizon past which the worker stops retrying an unreadable reveal. The default, 0, "
                         "never gives up: the chain closes an audit (the job leaves +disputed and exits the "
                         "loop). A worker giving up must not decide the fate of an audit, or a few minutes of "
                         "unavailability would cost a jury seat for good.")
    ap.add_argument("--reveal-grace", type=int, default=5,
                    help="ADR-028: number of reveal-open attempts before posting a 0 verdict (a primary "
                         "that stays silent or reveals nothing must be judged invalid, not cleared)")
    ap.add_argument("--model-id", default="",
                    help="ADR-027 D4: manual override of the judge model. By default the worker reads the "
                         "model pinned on chain (modelregistry.audit_judge_model), so that the whole committee "
                         "judges with the same model; force it only for debugging and tests.")
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
    # Opened with the keyring's passphrase (modea/keyring.py), the one the daemon sealed it under, and never
    # re-encrypted from here: one writer per file, the daemon.
    try:
        sk = crypto.load_sk(str(skpath), _keyring().open_with)
    except (kring.KeyringError, crypto.KeyEnvelopeError) as e:
        print(f"[judge] FATAL: the keys cannot be opened -- {e} {getattr(e, 'hint', '')}".rstrip(), flush=True)
        sys.exit(3)

    # The inference backend that computes this judge's own reference answer.
    # The probe runs in the judge process, whose OLLAMA_ENDPOINT is the judge endpoint
    # (entrypoint-services.sh sets `OLLAMA_ENDPOINT="$JEP"`), while the model it asks for is the miner's
    # `DENDRA_MODEL_ID`. When mining and judging use two backends (GPU and CPU), that model may be absent
    # from the judge endpoint: Ollama answers 404 "model not found" and urllib raises HTTPError. That case
    # is reported as a refusal, not as "unreachable", so the operator does not go looking for a network
    # problem that does not exist.
    _ep = os.environ.get("OLLAMA_ENDPOINT", "?")
    try:
        backend = Miner(a.id, backend="ollama").backend
        backend.generate("ok")
    except urllib.error.HTTPError as e:
        _body = ""
        try:
            _body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 — reading the body must never mask the real error
            pass
        print(f"[judge] FATAL: the inference backend answered, and refused (HTTP {e.code}) at {_ep}.")
        print(f"        This is not a connectivity problem. Response: {_body or '(empty)'}")
        if e.code == 404:
            print(f"        A 404 from Ollama means the model is not pulled on that endpoint.")
            print(f"        This process asks for DENDRA_MODEL_ID={os.environ.get('DENDRA_MODEL_ID', '?')!r},")
            print(f"        and it asks the judge endpoint. If mining and judging run on two backends,")
            print(f"        that model has to exist on both, or this probe has to use the judge's own.")
            print(f"        Check: curl -s {_ep}/api/tags")
        sys.exit(3)
    except Exception as e:  # noqa: BLE001 — genuine connectivity failures land here
        print(f"[judge] FATAL: cannot reach the inference backend at {_ep} ({type(e).__name__}: {e})")
        print(f"        — the committee must be able to infer.")
        sys.exit(3)

    # The committee's judge model, resolved once at startup so that it is printed and stays the same.
    judge_model, judge_src = resolve_judge_model(a.model_id)
    # Layer C: every on-chain verdict carries the judge's model (`Commit.ModelId`, see
    # msg_server_commit.go), so that the diversity tally (min_distinct_judge_models) has something to
    # count. An explicit DENDRA_JUDGE_MODEL_ID wins over the resolved model.
    flags = ["--model-id", (JUDGE_MODEL_ID or judge_model)]
    src_label = {"cli": "--model-id override", "chain": "on-chain (modelregistry.audit_judge_model)",
                 "env": "env DENDRA_JUDGE_MODEL_ID", "default": f"judge.py default ({DEFAULT_JUDGE_MODEL})"}
    print(f"[judge] judge model = '{judge_model}' (source: {src_label.get(judge_src, judge_src)})")
    if judge_src == "default":
        print("[judge] WARNING: judge model not pinned on-chain — the committee risks inconsistent "
              "verdicts if its members diverge (ADR-027 D4).")
    # The model this process judges with, written where its container's self-test reads it (modea/heartbeat.py,
    # write_judge_state). It is resolved once, above, and every verdict uses it: the judge role the capacity
    # report declares (miner_selftest.py --judge-role) is read from this, never from a second resolution made
    # later against the chain of that moment. A failed write changes nothing here; the role then reads unknown.
    try:
        from modea import heartbeat as _judge_state
        _judge_state.write_judge_state(judge_model, judge_src)
    except Exception as e:  # noqa: BLE001 -- the state is for the self-test; the worker judges without it
        print(f"[judge] WARNING: the judge state was not written ({type(e).__name__}: {e}): "
              "the capacity report will declare this judge role unknown")
    # What this process does, counted from now and written into the state above, never in its place: counts are
    # published only on a state this process wrote (modea/heartbeat.py::update_judge_counters).
    ledger = JudgeLedger()
    _flush_ledger(ledger, force=True)
    # Where the two kinds of calls go, printed rather than assumed.
    # This process asks Ollama for two unrelated things: its reference answer (the miner's model, about
    # 5 GB of VRAM) and its verdict (the judge model, 19 GB). On an 8 GB card the two never fit together,
    # and sending both to one endpoint makes them evict each other without any error: the chain advances,
    # the judges run, and no job is served. The split is configured through DENDRA_JUDGE_ENDPOINT.
    # Setting the variable does not prove the code reads it (an older modea/judge.py ignores it), so these
    # lines print what was actually resolved, and the fallback names an outdated judge.py.
    _ep_ref = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434")
    _m_ref = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")
    try:
        from modea.judge import judge_endpoint as _je
        _ep_verdict = _je()
    except ImportError:
        _ep_verdict = f"{_ep_ref}  [!] outdated modea/judge.py: split impossible, the variable is ignored"
    print(f"[judge] verdict endpoint   = {_ep_verdict}  (model {judge_model})")
    print(f"[judge] reference endpoint = {_ep_ref}  (model {_m_ref})")
    # One endpoint for both calls is fine; two different models on one endpoint is the problem. The
    # standard launcher sets OLLAMA_MODEL to the judge model and both endpoints to the CPU instance, which
    # is healthy (one resident model, the card left free). An alarm on that configuration would teach
    # operators to ignore it, so the check tests the actual eviction condition: two distinct models
    # served by the same instance.
    if _ep_verdict == _ep_ref and _m_ref != judge_model:
        print(f"[judge] WARNING: two different models on the same Ollama ({_ep_ref}) — "
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
              "voted invalid")
    else:
        print(f"[judge] divergence guard on ({DIVERGENCE_SLASH_ENV} is not 1): a divergence abstains; "
              "word salad is still voted invalid")
        if _dv_raw not in (None, "", "0"):
            print(f"[judge] WARNING: {DIVERGENCE_SLASH_ENV}={_dv_raw!r} is neither 0 nor 1 -- read as the "
                  "guard on")
    trace_dest = ""
    if TRACE_DEST:
        trace_dest = f"/tmp/judge-trace-{a.id}.jsonl" if TRACE_DEST == "1" else TRACE_DEST
        print(f"[judge] per-job trace -> {trace_dest} ({'cleartext (bench only)' if TRACE_PLAIN else 'hashes only'})")
    done = set()
    miss = {}        # job_id -> number of reveal-open failures (grace window)
    first_seen = {}  # job_id -> ts of the first attempt (abandon horizon, if the operator sets one)
    next_try = {}    # job_id -> ts before which we do not retry (growing backoff)
    abstained = set()  # job_id already announced as abstained: do not reprint on every attempt
    seat_of = {}     # job_id -> "seated" | "not-seated": an anchored jury does not change, it is read once
    tries = {}       # job_id -> attempts that ended without a judgment (RETRY_STAGES, no jury yet)
    decided = {}     # job_id -> "0"|"1" decided but not yet seen on chain: posted again, never judged again
    agreed_refs = set()  # job_id whose attempt ended after its references agreed (AGREED_REFS_STAGES)
    passes = 0       # counted at the top, so a round skipped below (`continue`) is a pass too
    while not (a.passes and passes >= a.passes):
        passes += 1
        try:
            # Two passes: a "0" verdict on an absent reveal is fast (one relay GET and one transaction, no
            # inference), while a full judgment is slow (minutes of generation). In a single queue the "0"
            # would wait behind the generations, the audit timeout would fire before the quorum, and a
            # silent primary would be resolved without quorum (silence_slash) instead of the hard slash.
            # Pass 1 triages and posts the grace or immediate "0"; pass 2 judges.
            # -- Pass 1a: observe, decide nothing. --------------------------------------------------
            # Stage 2 (correlation) needs to know what the other primaries do in the same pass: "isolated"
            # and "correlated" can only be told apart by comparison, which deciding job by job would prevent.
            slow = []
            observed = []
            _rows = list_jobs_full()
            if _rows is None:
                # "The read failed" is not "nothing to judge": the round is skipped and said, instead of
                # iterating an empty list and sleeping as if work had been done.
                print(f"[judge] {a.id} list-job unreadable — round skipped (neither green nor red: not measured)")
                # `--poll` is the only interval this parser declares; any other attribute would raise and
                # stop the juror just when the chain is unreadable, while its seat must survive to vote in
                # the next round. A dead seat is a silent seat, which raises the two-thirds bar for everyone.
                time.sleep(a.poll)
                continue
            for job_id, state, primary, dispute_h in _rows:
                if not is_disputed(state) or job_id in done or primary == a.id:
                    continue
                if not rv.safe_job_id(job_id):   # an on-chain job_id ends up in dendrad argv and in a relay key
                    print(f"[judge] {a.id} non-conforming job_id ignored (argv defense): {str(job_id)[:40]!r}")
                    done.add(job_id)
                    continue
                _now = time.time()
                if _now < next_try.get(job_id, 0.0):
                    continue          # backing off: the reveal was not there, it is tried again later
                # Only this judge's seats. The chain counts a verdict only from the jury it anchored on the
                # audit (antievasion.go::auditVerdictTally); every other audit costs an engine for a vote
                # that weighs nothing, and its absent reveal would skew the stage-2 statistics below. The
                # filter comes before the reveal is opened. A jury that cannot be read is judged anyway.
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

            # -- Correlation statistics, over this pass only ------------------------------------------
            ok_by, miss_by = {}, {}
            for jid, prim, _dh, rv_, _t in observed:
                if rv_ and "prompt" in rv_:
                    ok_by[prim] = ok_by.get(prim, 0) + 1
                else:
                    miss_by[prim] = miss_by.get(prim, 0) + 1
            # -- Stage 1: is it even deployed? (mass-slash guard) --------------------------------------
            n_anchor, marker_cache = 0, {}
            if TWOSTAGE_NOREVEAL:
                for jid, prim, dh, rv_, _t in observed:
                    if prim in marker_cache or (rv_ and "prompt" in rv_):
                        continue
                    marker_cache[prim] = primary_has_marker(prim, dh, get_commit=anchored_root,
                                                            epoch_blocks=a.epoch_blocks)
                n_anchor = sum(1 for v in marker_cache.values() if v)
            stage1_ok = TWOSTAGE_NOREVEAL and marker_stage_usable(n_anchor)

            # -- Pass 1b: decide ----------------------------------------------------------------------
            for job_id, primary, dispute_h, rev, _now in observed:
                if rev and "prompt" in rev and job_id in abstained:
                    # Abstention is a state: this job was abstained on, the reveal has since arrived, so it is
                    # judged again. Striking the job off for good would make this path unreachable.
                    print(f"[judge] {a.id} resuming {job_id}: the reveal arrived after "
                          f"{miss.get(job_id, 0)} attempts — the job is judgeable again")
                    abstained.discard(job_id)
                if rev and "prompt" in rev:
                    miss.pop(job_id, None)
                    next_try.pop(job_id, None)
                if not rev or "prompt" not in rev:
                    # No usable reveal. After a grace window for an honest primary (a slow network), a "0"
                    # verdict (invalid) is posted, so that a primary that stays silent or reveals nothing is
                    # caught by the committee's quorum and hard-slashed instead of being cleared by the
                    # timeout. This closes evasion by non-reveal.
                    miss[job_id] = miss.get(job_id, 0) + 1
                    if NOREVEAL_ABSTAIN:
                        # Silence is handled by the no-quorum path and silence_slash, not by a cheat verdict
                        # that cannot be justified (an absent reveal looks the same as a relay fault).
                        # Abstention is a reversible state (see noreveal_action), or a few seconds of
                        # unavailability would cost a jury seat for good and freeze the held fee.
                        act, delay = noreveal_action(_now, first_seen[job_id], miss[job_id],
                                                     grace=a.reveal_grace,
                                                     abandon_after=a.abandon_after)
                        if act == "wait":
                            continue
                        if act == "give_up":
                            print(f"[judge] {a.id} giving up on {job_id} after "
                                  f"{int(_now - first_seen[job_id])} s without a readable reveal "
                                  f"(--abandon-after={a.abandon_after:g}s) — no further attempts")
                            done.add(job_id)
                            continue
                        next_try[job_id] = _now + delay
                        # -- The two stages ----------------------------------------------------------
                        # They apply only after the grace window, and only when armed (they are disarmed by
                        # default). Arming this path allows a "0" vote and so a slash, an economic decision
                        # that must be authorised explicitly.
                        if TWOSTAGE_NOREVEAL:
                            mk, mk_why = (None, "stage 1 not deployed (no primary anchors) -> "
                                                "correlated absence, not attributable to this primary")
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
                                print(f"[judge] {a.id} two-stage -> verdict 0 for {job_id} "
                                      f"(primary {primary}): {why} | stage 1: {mk_why}")
                                # fall through to the "0" vote path below
                                miss[job_id] = miss.get(job_id, 0)
                            else:
                                if job_id not in abstained:
                                    abstained.add(job_id)
                                    print(f"[judge] {a.id} two-stage -> abstention for {job_id}: {why}")
                                continue
                        else:
                            if job_id not in abstained:
                                abstained.add(job_id)
                                print(f"[judge] {a.id} abstention (reveal absent after {miss[job_id]} attempts) "
                                      f"for {job_id} — an absent reveal is not proof of cheating; "
                                      f"still retrying (next attempt in {delay:g} s)")
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
                        print(f"[judge] {a.id} verdict refused by the chain for {job_id} (no-reveal 0): {_why0}")
                    elif _st0 == "unconfirmed":
                        print(f"[judge] {a.id} verdict 0 for {job_id} not confirmed (will retry): {_why0}")
                    if _st0 == "anchored":
                        ledger.verdict("0")
                        print(f"[judge] {a.id} verdict=0 (reveal absent after {miss[job_id]} attempts) for {job_id}")
                        if trace_dest:
                            _trace_write(trace_dest, {"ts": int(time.time()), "job_id": job_id,
                                                      "verdict": "0", "stage": "no-reveal", "n_gen": 0})
                        done.add(job_id)
                        if a.adjudicate:
                            # The return value is read, since a broadcast `code: 0` proves nothing: execution
                            # is confirmed exactly as at the other adjudication site.
                            _st, _detail = adjudicate_outcome(tx_from(a.id, "adjudicate-dispute", flags=("--job-id", job_id)))
                            if _st == "ok":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> OK (dispute closed)")
                            elif _st == "deferred":
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> deferred: {_detail[:140]}")
                            else:
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} (no-reveal) -> FAILED: {_detail[:140]}")
                    continue
                slow.append((job_id, primary, rev, dispute_h))
            # -- Pass 2: judge, the oldest dispute first (an unknown height, read as 0, last) -------------
            # Each audit runs under its own guard: an exception (a judge call past its bound, an engine that
            # drops the connection) becomes an abstention on that audit ("judge-error"), tried again after a
            # pause, instead of ending the pass and throwing away the references already generated.
            for job_id, primary, rev, _dh in sorted(slow, key=lambda s: (s[3] <= 0, s[3])):
                vkey = f"{job_id}__verdict__{a.id}"
                try:
                    # Has this judge already voted? Asked before judging, never after: the chain keeps the first
                    # verdict ("index already set"), so a second judgment can never land; it only spends an
                    # engine, and one that came out differently would be refused on every pass.
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
                            if TRACE_PLAIN:  # bench only (invariant 1: never cleartext in production)
                                rec.update({"prompt": _rd.get("prompt"), "answer": _rd.get("answer"),
                                            "refs": tr.get("refs", [])})
                            _trace_write(trace_dest, rec)
                        if verdict is None:
                            if ledger.abstain(job_id, stage):
                                print(f"[judge] {a.id} abstention on {job_id} (stage {stage}): {why}")
                            if stage in AGREED_REFS_STAGES:
                                agreed_refs.add(job_id)
                            if stage in RETRY_STAGES:
                                tries[job_id] = tries.get(job_id, 0) + 1
                                next_try[job_id] = time.time() + retry_delay(tries[job_id], base=a.retry_pause)
                            else:
                                done.add(job_id)   # a judgment is given once per process (RETRY_STAGES)
                            continue
                        if verdict is True and stage in ("sc-diverge", "multiok"):
                            print(f"[judge] {a.id} ambiguous job {job_id} (stage {stage}) -> valid vote "
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
                                print(f"[judge] {a.id} adjudicate-dispute {job_id} -> failed, and disputes will "
                                      f"not close (jobs_unresolved>0): {_detail}")
                    elif st == "already-set":
                        ledger.already_on_chain(job_id)
                        decided.pop(job_id, None)
                        done.add(job_id)
                        print(f"[judge] {a.id} {job_id}: this judge's verdict is already on chain -- done")
                    else:
                        # The chain's reason is always printed (it can be "key not found", a keyring outside
                        # dendrad's default home). The decided verdict is kept and posted again; the audit is not
                        # judged again.
                        tries[job_id] = tries.get(job_id, 0) + 1
                        next_try[job_id] = time.time() + retry_delay(tries[job_id], base=a.retry_pause)
                        if st == "refused":
                            ledger.refusal(job_id, detail)
                            print(f"[judge] {a.id} verdict refused by the chain for {job_id} (posted again "
                                  f"later, not judged again): {detail}")
                        else:
                            print(f"[judge] {a.id} verdict for {job_id} not confirmed (posted again later): {detail}")
                except Exception as e:  # noqa: BLE001 -- one audit's failure never ends the pass
                    if ledger.abstain(job_id, "judge-error"):
                        print(f"[judge] {a.id} abstention on {job_id} (stage judge-error): "
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
