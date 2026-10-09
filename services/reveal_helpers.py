"""Reveal to the anchored JURY of an AUDITED job (`+disputed`).

When an optimistic job (k=1) is drawn for audit, the chain moves it to `+disputed` and, in the same
step, ANCHORS the jury that will judge it (`audit_sampling.go::runOptimisticAudit`). The PRIMARY miner
must then REVEAL (prompt + its answer) to THAT jury -- SEALED (X25519) to each juror, never in
cleartext (the relay is hostile).

TO THE JURY, AND TO NOBODY ELSE. Only an anchored juror's verdict enters the tally
(`antievasion.go::auditVerdictTally`). A copy sealed to any other registered miner buys no judgement:
it hands a client's prompt to an operator with no role in the audit. The recipients are therefore READ
from the chain (`read_jury`), never derived from the miner registry, and a jury that cannot be read
seals NOTHING: there is no fallback to "every registered miner", in any state.

Imported by `reveal_worker.py` (the primary) and `judge_worker.py` (the juror opens its copy with
`open_reveal`). `read_jury` is the PRIMARY's reader of the anchor. A juror checks its own seat with a
reader of its own (`judge_worker.py::seat_reading`), which is not this function and does not follow
the same rule on an answer it cannot read (the juror judges anyway; the primary seals nothing). A
change to one of the two readers does not reach the other.

Confidentiality: revealing exposes content only for the jobs the chain samples for audit
(`audit_sample_bps`), sealed per juror (an accepted trade-off). `reveal_worker.py` re-derives the
cleartext in memory long enough to re-seal it and never writes it to disk; `JobCache` below keeps
cleartext only IN MEMORY, bounded in time (TTL) and in count, zeroized on purge.

Crypto = real API of `modea.crypto` (ECDH X25519 + HKDF + AES-256-GCM), verified.
"""
from __future__ import annotations

import hashlib
import json
import sys
import re
import time
from typing import NamedTuple

from modea import crypto
from modea import dendrad_argv as da
import relay_client as relay

# Derivation domain specific to reveal (independent of the client->miner channel).
REVEAL_INFO = b"dendra/reveal/v1"

# Defense in depth: a job_id comes from the CHAIN (potentially adversarial data)
# and ends up in `dendrad` ARGV and as a relay key. We accept only a flat
# identifier -- alphanum + . _ : -, NEVER starting with '-' (anti flag-injection),
# capped at 128. No shell involved (subprocess as a list), but we close the vector anyway.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def query_all(subcmd, field, node_args, runner, t=90, max_pages=400):
    """COMPLETE listing of a paginated `query jobs <subcmd>`. Returns `None` when unreadable, never [].

    WHY THIS FUNCTION EXISTS, AND WHY IT LIVES HERE RATHER THAN BEING COPIED INTO EACH WORKER.
    `dendrad query jobs list-job` WITHOUT pagination stops at 100 entries: `ListJob` calls
    `query.CollectionPaginate` (query_job.go) and the SDK applies `DefaultLimit = 100` when the request
    carries Limit=0. Past the 100th job, the judge and reveal workers would stop seeing part of the
    audits — SILENTLY, and on the reassuring side: truncation can only produce GOOD news (fewer
    disputes, fewer pending audits), so the failure mode is indistinguishable from a healthy network.
    `client.list_all` already did this work; copying it into the workers would have created a
    third source of truth that drifts. It therefore lives in the module BOTH workers import.

    AND IT RETURNS `None`, NOT `[]`. Returning `[]` on unreadable output turns "I could not read" into
    "nothing to judge" for a `for job in list_jobs()` loop, and the worker sleeps believing it has
    worked. Not knowing is not the same as knowing there is nothing.
    """
    out, key, pages = [], None, 0
    while pages < max_pages:
        cmd = ["dendrad", "query", "jobs", subcmd, "--page-limit", "500", "--output", "json", *node_args]
        if key:
            cmd += ["--page-key", key]
        try:
            d = json.loads(runner(cmd, t=t) if _accepts_t(runner) else runner(cmd))
        except Exception:
            return None
        if not isinstance(d, dict) or not d:
            return None
        out.extend(d.get(field) or d.get(field.capitalize()) or [])
        pg = d.get("pagination") or {}
        key = pg.get("next_key") or pg.get("nextKey")
        pages += 1
        if not key:
            return out
    sys.stderr.write(f"[helpers] {subcmd}: >{max_pages} pages -> INCOMPLETE read, refusing to return a prefix\n")
    return None


def _accepts_t(fn):
    """The workers' `run()` does not have the same signature everywhere: inspect it, do not assume."""
    try:
        import inspect
        return "t" in inspect.signature(fn).parameters
    except Exception:
        return False


def safe_job_id(s) -> bool:
    """True if `s` is a job_id safe to pass as argv / relay key (see _JOB_ID_RE)."""
    return isinstance(s, str) and bool(_JOB_ID_RE.match(s))


# === THE ANCHORED JURY ============================================================================
# Three states, never two. "The chain says there is no jury" and "the chain could not be asked" lead to
# the same action today (nothing is sealed), but they are different facts, and a reader that merges
# them is one edit away from sealing to someone on the wrong one.
JURY_READ = "read"          # the chain answered: `members` is the anchored jury (possibly empty)
JURY_ABSENT = "absent"      # the chain answered NotFound: nothing is anchored under this key
JURY_UNKNOWN = "unknown"    # no trustworthy answer: unreachable node, timeout, unexpected output

# The gRPC status of a NotFound answer, as the CLI prints it on stderr. Only the STATUS CODE is read,
# never the message after it: the chain answers NotFound both for "no jury anchored" and for "unknown
# job", and both lead to the same action (nothing sealed). A status other than NotFound is UNKNOWN.
_NOT_FOUND_RE = re.compile(r"rpc error: code = NotFound desc")
# A dendrad built before `query jobs audit-committee` existed does not know the subcommand: cobra then
# takes it for an argument of `query jobs`, prints that command's usage and fails on the first flag
# ("unknown flag: --output"). The answer stays UNKNOWN -- nothing is sealed -- but its cause is named,
# because waiting does not cure it: only a dendrad that has the query does, and an unnamed
# "unknown flag" sends the operator looking at the network.
_CLI_LACKS_QUERY_RE = re.compile(r"^(?:Error: )?unknown (?:flag|shorthand flag|command)\b", re.M)
# The fields of `QueryAuditCommitteeResponse` (query.proto), in both spellings a CLI may print.
_JURY_FIELDS = frozenset({"members", "anchored_height", "anchoredHeight"})


class JuryRead(NamedTuple):
    state: str              # JURY_READ / JURY_ABSENT / JURY_UNKNOWN
    members: tuple          # the anchored jurors, in anchor order, deduplicated (JURY_READ only)
    detail: str             # why, when the state is not JURY_READ


def jury_argv(job_id: str, node_args, *, redo: bool = False) -> list:
    """`dendrad query jobs audit-committee` for one job. NOT `assigned-committee`: that query answers
    who was assigned the WORK, and reading it as the jury seals to the wrong set (the autocli help of
    both says so). Flags before `--`, the job id after it (`modea/dendrad_argv.py`)."""
    flags = ["--output", "json", *(["--redo"] if redo else []), *list(node_args or [])]
    return da.dendrad_argv(("dendrad", "query", "jobs"), "audit-committee", [job_id], flags)


def read_jury(job_id: str, node_args, runner, *, redo: bool = False, t: float = 30) -> JuryRead:
    """The jury ANCHORED on `job_id` (`redo=True`: the re-adjudication jury of ADR-033).

    `runner(cmd, t)` returns `(returncode, stdout, stderr)`, with `returncode=None` when the command
    could not run at all. The streams stay apart: the JSON is read from stdout alone, the status from
    stderr alone, so a warning printed on stderr cannot make a valid answer unparsable.

    AN ABSENT `members` IS AN EMPTY JURY, AND ONLY IN THE MESSAGE THIS FUNCTION KNOWS. proto3 omits a
    repeated field that is empty, so `{}` and `{"anchored_height": ...}` ARE the encoding of an empty
    anchor -- the zero value of the type, read as such. Without `members`, that reading is allowed only
    when every field present belongs to the response (`_JURY_FIELDS`): an answer carrying any other key
    is not the message this code was written against, and is UNKNOWN, never "empty". With `members`
    present, an added field changes nothing: refusing it would stop every reveal on the first additive
    change of the response, and a primary that stops revealing is slashed. A member that is not a flat
    identifier (it ends up in a relay key) makes the whole answer UNKNOWN: a partial jury read as a
    whole one is a silent narrowing of the jury.
    """
    try:
        rc, out, err = runner(jury_argv(job_id, node_args, redo=redo), t)
    except Exception as e:  # a runner that raises is a read that did not happen
        return JuryRead(JURY_UNKNOWN, (), f"{type(e).__name__}: {e}"[:200])
    if rc is None:
        return JuryRead(JURY_UNKNOWN, (), f"the query did not run: {err}"[:200])
    if rc != 0:
        if _NOT_FOUND_RE.search(err or ""):
            return JuryRead(JURY_ABSENT, (), "NotFound: no jury anchored under this key")
        # The CLI prints its error LAST; a warning printed first must not take its place in the log.
        lines = [ln.strip() for ln in (err or out or "").splitlines() if ln.strip()]
        last = (lines[-1] if lines else "(no output)")[:200]
        if _CLI_LACKS_QUERY_RE.search(err or ""):
            return JuryRead(JURY_UNKNOWN, (), f"rc={rc}: {last} -- this dendrad does not know `query jobs "
                                              f"audit-committee`: install a dendrad built from the same "
                                              f"source as this worker; no reveal can be sealed until then")
        return JuryRead(JURY_UNKNOWN, (), f"rc={rc}: {last}")
    try:
        d = json.loads(out)
    except (ValueError, TypeError):
        return JuryRead(JURY_UNKNOWN, (), "rc=0 but stdout is not JSON")
    if not isinstance(d, dict):
        return JuryRead(JURY_UNKNOWN, (), "rc=0 but the JSON is not an object")
    if "members" in d:
        raw = d["members"]
    else:
        foreign = sorted(set(d) - _JURY_FIELDS)
        if foreign:
            return JuryRead(JURY_UNKNOWN, (), f"no `members` and unexpected field(s) {foreign[:3]}: "
                                              f"not the jury message")
        raw = []                                      # proto3: an omitted repeated field is []
    if not isinstance(raw, list):
        return JuryRead(JURY_UNKNOWN, (), "`members` is not a list")
    members = []
    for m in raw:
        if not safe_job_id(m):
            return JuryRead(JURY_UNKNOWN, (), f"malformed member {str(m)[:40]!r}")
        if m not in members:
            members.append(m)
    return JuryRead(JURY_READ, tuple(members), "")


# Decisions of `decide_reveal`. Only SEAL deposits anything.
SEAL = "seal"
NO_REVEAL_EMPTY_JURY = "no-reveal-empty-jury"
NO_REVEAL_HUMAN_DISPUTE = "no-reveal-human-dispute"
WAIT_NO_JURY = "wait-no-jury"
WAIT_UNREADABLE = "wait-unreadable"


class RevealPlan(NamedTuple):
    action: str             # one of the five decisions above
    members: tuple          # the jurors to seal to (SEAL only, never this miner)
    reason: str


def decide_reveal(audit: JuryRead, redo, my_id: str) -> RevealPlan:
    """PURE decision: to whom the reveal of one disputed job is sealed -- or why nothing is sealed now.

      SEAL                      audit jury read, with at least one juror other than this miner;
      NO_REVEAL_EMPTY_JURY      audit jury read and empty: nobody can judge, nothing is sealed;
      NO_REVEAL_HUMAN_DISPUTE   no audit jury, a re-adjudication jury anchored: a HUMAN dispute;
      WAIT_NO_JURY              no audit jury and no re-adjudication jury;
      WAIT_UNREADABLE           a read failed, or an input this function does not know.

    `redo` is the read of the re-adjudication anchor, or None when it was not needed (the audit anchor
    answered something other than NotFound).

    THE HUMAN DISPUTE. `DisputeVerdict` anchors a re-adjudication jury (`<job>__redo`) and no audit
    jury. That jury decides from re-commits (`AdjudicateDispute`), and this kit ships no worker that
    re-adjudicates, so a copy sealed to it would have no reader with a role: NOTHING IS EVER SEALED TO
    THE RE-ADJUDICATION JURY. That decision is final. What is not final is the absence of an AUDIT
    jury: the sampling lottery still audits a job a human disputed (`runOptimisticAudit` skips only a
    RESOLVED job), and a lottery deferred for want of a seed or of a pool seats its jury later. A
    primary that stopped looking would be silent before a jury that counts silence as invalid. The
    caller therefore keeps re-reading the AUDIT anchor at its slow cadence until the job is resolved.
    """
    if audit.state == JURY_READ:
        members = tuple(m for m in audit.members if m != my_id)
        if members:
            return RevealPlan(SEAL, members, f"{len(members)} anchored juror(s)")
        return RevealPlan(NO_REVEAL_EMPTY_JURY, (), "the anchored audit jury has no member to seal to")
    if audit.state == JURY_ABSENT:
        if redo is not None and redo.state == JURY_READ:
            return RevealPlan(NO_REVEAL_HUMAN_DISPUTE, (),
                              "human dispute: re-adjudication jury anchored, no audit jury; nothing "
                              "is sealed to a re-adjudication jury")
        if redo is not None and redo.state == JURY_ABSENT:
            return RevealPlan(WAIT_NO_JURY, (), "no audit jury and no re-adjudication jury anchored")
        why = redo.detail if redo is not None else "not read"
        return RevealPlan(WAIT_UNREADABLE, (), f"no audit jury; re-adjudication anchor unreadable ({why})")
    if audit.state == JURY_UNKNOWN:
        return RevealPlan(WAIT_UNREADABLE, (), f"audit jury unreadable ({audit.detail})")
    return RevealPlan(WAIT_UNREADABLE, (), f"unknown jury state {str(audit.state)[:40]!r}")


def plan_reveal(job_id: str, my_id: str, node_args, runner) -> RevealPlan:
    """Reads the audit anchor, and the re-adjudication anchor only when the first answers NotFound."""
    audit = read_jury(job_id, node_args, runner)
    redo = read_jury(job_id, node_args, runner, redo=True) if audit.state == JURY_ABSENT else None
    return decide_reveal(audit, redo, my_id)


# How long a job that was NOT sealed waits before its jury is read again: poll, 2*poll, 4*poll...,
# capped. The cap bounds how late a jury seated AFTER a first look is served, so it must stay a small
# part of the audit window (`audit_resolve_timeout`, counted in blocks).
RECHECK_CAP_S = 60.0


def recheck_delay(attempt: int, poll: float, cap: float = RECHECK_CAP_S) -> float:
    """Back-off before re-reading the jury of an unsealed job (attempt = 0 for the first wait)."""
    base = max(float(poll), 0.5)
    return min(float(cap), base * (2 ** max(0, min(int(attempt), 16))))


class JobCache:
    """Bounded IN-MEMORY cache `jobId -> (prompt, answer)` so we can reveal if the job is audited.
    Plaintext in RAM only (never disk), purged by TTL and bounded by `max_items`."""

    def __init__(self, max_items: int = 256, ttl: float = 3600.0):
        self._d: dict[str, list] = {}   # jobId -> [prompt, answer, ts]
        self.max_items = max_items
        self.ttl = ttl

    def put(self, job_id: str, prompt: str, answer: str) -> None:
        self._gc()
        self._d[job_id] = [prompt, answer, time.time()]
        while len(self._d) > self.max_items:
            oldest = min(self._d.items(), key=lambda kv: kv[1][2])[0]
            self._zap(oldest)

    def get(self, job_id: str):
        v = self._d.get(job_id)
        return (v[0], v[1]) if v else None

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, v in self._d.items() if now - v[2] > self.ttl]:
            self._zap(k)

    def _zap(self, k: str) -> None:
        v = self._d.pop(k, None)
        if v:   # best-effort zeroization of the cleartext
            try:
                v[0] = "\x00" * len(v[0])
                v[1] = "\x00" * len(v[1])
            except Exception:
                pass


def _seal_to(pub_hex: str, obj: dict) -> dict:
    """Seals `obj` (JSON) to an X25519 pub (hex 32B) with an ephemeral key -> forward secrecy.
    Returns the transportable dict {client_eph_pk, nonce, ct} (same fields as the req channel)."""
    eph_sk, eph_pk = crypto.gen_keypair()
    key = crypto.derive_session_key(eph_sk, bytes.fromhex(pub_hex), info=REVEAL_INFO)
    sealed = crypto.encrypt(key, json.dumps(obj).encode())
    crypto.zeroize(bytearray(key))
    return {"client_eph_pk": eph_pk.hex(), "nonce": sealed.nonce.hex(), "ct": sealed.ct.hex()}


_PUB_HEX_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


def usable_pub(pub) -> bool:
    """True if a reveal can be sealed to `pub`: 64 hex digits (the 32 bytes the chain checks in
    `msg_server_miner.go::CreateMiner`) AND a point X25519 accepts.

    The relay serves `pub/<mid>` as it was stored and checks nothing about the value, and the chain
    checks the length of an anchored key, not the point. A string that is not a key ("zz") or a
    low-order point (all zeroes, whose shared secret X25519 refuses) makes `_seal_to` raise, in the
    middle of a reveal. A key that fails here is NO key: the juror is reported as unkeyed and the others
    are served."""
    if not isinstance(pub, str) or not _PUB_HEX_RE.match(pub):
        return False
    try:
        eph_sk, _ = crypto.gen_keypair()
        crypto.zeroize(bytearray(crypto.derive_session_key(eph_sk, bytes.fromhex(pub), info=REVEAL_INFO)))
    except Exception:
        return False
    return True


def committee_pubs(relay_url: str, my_id: str, miners, *, jury) -> dict[str, str]:
    """X25519 pubs of the anchored JURORS (reveal targets) -- and of nobody else.

    `jury` is REQUIRED and has no default: the members `read_jury` returned for this job. A default
    meaning "every registered miner" would be the exact fallback this module forbids, reachable by
    simply forgetting an argument. `None` is refused; an empty jury yields no target. `miners` (the
    registry) only supplies the KEYS: a registered miner outside the jury is never a target, and a juror
    absent from the registry has no anchored key, so nothing can be sealed to it.

    SOURCE OF TRUTH = the ON-CHAIN ANCHORED pub (`enc_pubkey` of the miner registry) — the very key the
    client already encrypts to, so a pub substituted at the relay is ineffective here too (it used to be
    the one hole left in the reveal path, while `client.submit_job` already refused the relay pub).

    It also removes a failure mode that puts an honest miner's stake at risk: the relay keeps `pub/<mid>` in MEMORY
    (relay.py STORE), so restarting the relay wiped every published key; miners were then
    structurally UNABLE to seal a reveal, and the committee charged them for that infrastructure fault.
    Reading the anchor makes the reveal path survive any relay restart.

    `miners` accepts registry records ({miner_id, enc_pubkey}) or a legacy list of ids. The volatile
    relay cache is kept ONLY as a fallback, for a JUROR that anchored NO key -- and that copy is sealed to
    a key the relay serves without any proof that the juror wrote it, so a relay that substitutes it can
    open that one copy. The anchored key, when there is one, is never replaced by the relay's: an
    anchored key that is not usable leaves the juror UNKEYED. Every key passes `usable_pub`.
    """
    if jury is None or isinstance(jury, (str, bytes)):
        raise ValueError("committee_pubs: `jury` must be the anchored members of this job, never None")
    wanted = {str(j) for j in jury if j and str(j) != my_id}
    pubs: dict[str, str] = {}
    for m in miners or []:
        if isinstance(m, dict):
            mid = m.get("miner_id") or m.get("id") or ""
            onchain = (m.get("enc_pubkey") or "").strip()
        else:
            mid, onchain = str(m or ""), ""
        if not mid or mid == my_id or mid not in wanted:
            continue
        if onchain:
            if usable_pub(onchain):
                pubs[mid] = onchain      # anchored on-chain: authoritative, restart-proof
            continue                     # anchored but unusable: unkeyed, never the relay's key instead
        r = relay.get(relay_url, "pub", mid)   # fallback: volatile relay cache, juror with no anchor
        if isinstance(r, dict) and usable_pub(r.get("pub")):
            pubs[mid] = r["pub"]
    return pubs


# The two primitives live in `modea.crypto`, where BOTH the miner (which computes the commitment
# while the plaintext is still inside its locked buffer) and this module can reach them without one
# importing the other. Re-exported here because the reveal contract is what carries the salt.
prompt_salt = crypto.prompt_salt
prompt_commitment = crypto.prompt_commitment


def reveal_job(relay_url: str, job_id: str, prompt: str, answer: str, target_pubs: dict[str, str],
               author_id: str, psalt: str = "") -> int:
    """Posts the sealed reveal (prompt+answer) to each juror of `target_pubs` (`committee_pubs`, so
    the anchored jury and no one else). Returns the number DELIVERED.

    DELIVERED, NOT NEWLY WRITTEN -- and the difference decides whether a completed job looks failed.
    Every seal draws a fresh ephemeral key (`_seal_to`), so re-sealing the same content produces
    different bytes; the relay's write-once guard then answers 409 for a juror whose reveal is ALREADY
    stored. Counting that as a failure means a retry after a lost response -- or simply a second run of
    this worker -- reports fewer deliveries than there are readable reveals, and the caller concludes
    the reveal did not land while the audit can read it perfectly well.

    A juror whose slot already holds a sealed artifact HAS something to judge, which is what this
    count exists to measure. What it does not prove is WHO sealed it: the relay cannot yet establish
    that the writer is the job's primary, so a squatted slot would also count here. That gap is real
    and belongs to the relay, not to this counter -- and it is not an argument for going back to
    reporting stored reveals as missing.
    """
    # `psalt` travels WITH the reveal: it is what lets a juror recompute the commitment the primary
    # anchored. Absent -- an older primary -- the juror cannot verify the question, and must abstain
    # rather than judge a prompt nobody vouched for.
    obj = {"prompt": prompt, "answer": answer}
    if psalt:
        obj["psalt"] = psalt
    n = 0
    for mid, pub in target_pubs.items():
        # ADR-045 (14) : LE DERNIER SEGMENT EST L AUTEUR, POUR TOUS LES GENRES SANS EXCEPTION.
        # The key was `<jid>__<recipient>`: SIGNED by the primary, KEYED by its reader. But the relay
        # attributes a deposit by taking the segment after the last `__` -- right for `res`, `pub` and
        # `attest`, wrong for a reveal. Measured under `enforce`: the primary signing ITS OWN reveal got
        # 401, the recipient signing the same deposit got 200.
        # Putting the author last makes the invariant UNIFORM instead of asking the relay for a
        # per-kind special case: a rule with no exception cannot forget one.
        key_ = f"{job_id}__{mid}__{author_id}"
        st = relay.put_status(relay_url, "reveal", key_, _seal_to(pub, obj))
        if st in ("ok", "exists"):
            n += 1
    return n


def open_reveal(relay_url: str, job_id: str, my_id: str, my_sk, author_id: str):
    """Juror side: fetches + decrypts the reveal sealed to `my_id`. None if missing/unreadable -- and
    a miner the primary did not find on the anchored jury has no copy to fetch."""
    # The reader names the AUTHOR it expects, and that is a property rather than a constraint: a reveal
    # posted by anyone else no longer occupies the same key, so a squatter can no longer fill the slot
    # the primary was meant to fill.
    r = relay.get(relay_url, "reveal", f"{job_id}__{my_id}__{author_id}")
    if not r or "ct" not in r:
        return None
    try:
        key = crypto.derive_session_key(my_sk, bytes.fromhex(r["client_eph_pk"]), info=REVEAL_INFO)
        pt = crypto.decrypt(key, crypto.Sealed(bytes.fromhex(r["nonce"]), bytes.fromhex(r["ct"])))
        crypto.zeroize(bytearray(key))
        return json.loads(pt)
    except Exception:
        return None
