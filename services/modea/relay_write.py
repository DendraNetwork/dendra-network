"""Verification of a signed write at the relay — the composition of the four building blocks.

Why this file exists
`cosmos_addr`, `relay_canon`, `registry_cache` and `relay_antireplay` are each tested on their own.
What remains to get right is the order in which they run, and that order is a security decision.

    (1) shape       — sizes and types, before any cryptography
    (2) signature   — expensive (~475 us), but nothing is believed before it
    (3) attribution — address(pubkey) == miner.Operator, through the cached registry
    (4) replay      — cheap, and it writes: therefore last

Why the signature comes before the replay guard, although it costs twenty times more:
  - oracle — if the replay guard ran first, a third party holding no key would learn, from the
    response difference alone, whether a digest has already been seen. It would probe the store
    without ever signing anything;
  - poisoning — the replay guard writes (it memorises the digest). Placing it before the
    signature would let anyone fill the digest set with noise, up to exhausting memory or evicting
    legitimate digests. A guard that writes must never run on unauthenticated input.
The overhead is measured and accepted: 475 us of signature against 22 us of replay checking — at the
rate limit already in place (12 req/s), 0.6% of one core. Verification is paid for so that the
oracle is not offered.

This module does not touch the store and decides no eviction. It returns a verdict, the regime it
was returned under, and the sealed height it ruled on. The relay stores that block with the
entry: `{"mode": "nominal"|"degraded", "height": N|null}`.

`mode` is `null` when the replay guard did not run, as for an entry refused at (1), (2) or (3).
A default of `"degraded"` would tell a reader of the log that the guard ran in degraded mode when it
did not run at all.

Where the height comes from
The height handed to the replay guard is read back from the signed message (`seal_height`) instead of
the call parameter. The two coincide today, but if `canon()` ever normalised or truncated the height,
the high-water mark would anchor on a value the signature does not cover. The read happens after (2):
a height read from unauthenticated bytes is no better than one supplied by the caller.
"""
from __future__ import annotations

try:                                   # `modea` package
    from .relay_antireplay import DEGRADED, NOMINAL, seal_height
except ImportError:                    # flat files (sandbox, isolated tests)
    from relay_antireplay import DEGRADED, NOMINAL, seal_height   # type: ignore

# Named refusal reasons, so that a refusal can be debugged and audited.
# The value travels (the relay's 401 `why`, /stats `signature.refusals`); an older relay sends French
# values for five of these (see `relay_antireplay` for the replay reasons).
MALFORMED = "MALFORMED"
INVALID_SIGNATURE = "INVALID_SIGNATURE"
FOREIGN_KEY = "FOREIGN_KEY"              # valid signature, but address != operator
UNKNOWN_MINER = "UNKNOWN_MINER"          # registry read, this miner is not in it
REGISTRY_MUTE = "REGISTRY_MUTE"          # registry never read — nothing can be attributed to anyone
UNREADABLE_HEIGHT = "UNREADABLE_HEIGHT"  # the signed message yields no height -> nothing is guessed
UNKNOWN_JOB = "UNKNOWN_JOB"              # job list read, this job is not in it
FOREIGN_CLIENT = "FOREIGN_CLIENT"        # valid signature, but address != paying client
JOB_REGISTRY_MUTE = "JOB_REGISTRY_MUTE"  # never read, or not wired — the payer is unknown

# Who may write what
# A `req` deposit is the request of a job: its legitimate author is the client who pays for it,
# not a miner operator. The other deposits (reveal, verdict) are produced by the miner.
#
# A table instead of `if kind == "req"` tests in several places: copies of a role test drift apart when a
# kind is added, and a forgotten kind would silently get the wrong authority. A kind absent from the
# table takes the default role explicitly.
ROLE_CLIENT = "client"
ROLE_OPERATOR = "operator"
ROLES_BY_KIND = {"req": ROLE_CLIENT}
DEFAULT_ROLE = ROLE_OPERATOR


def role_of_kind(kind) -> str:
    """Which authority must sign this kind. Type-insensitive: a non-textual kind takes the default
    role rather than raising — the shape is already checked at (1)."""
    return ROLES_BY_KIND.get(kind, DEFAULT_ROLE) if isinstance(kind, str) else DEFAULT_ROLE

# Vocabulary of the persisted field. Deliberately different from the replay guard's internal constants
# (`NOMINAL`/`DEGRADED`): those are a module detail, this one is a store format. Conflating them would
# make the on-disk format depend on an internal rename.
MODE_NOMINAL = "nominal"
MODE_DEGRADED = "degraded"
_MODES = {NOMINAL: MODE_NOMINAL, DEGRADED: MODE_DEGRADED}


class WriteResult:
    __slots__ = ("accepted", "reason", "regime_registry", "replay_regime", "operator", "detail",
                 "height", "client", "role")

    def __init__(self, accepted, reason=None, regime_registry=None, replay_regime=None,
                 operator=None, detail="", height=None, client=None, role=None):
        self.accepted = accepted
        self.reason = reason
        self.regime_registry = regime_registry    # `NEVER_READ`, `FEES` or `STALE` (registry_cache states)
        self.replay_regime = replay_regime          # NOMINAL / DEGRADED / None ((4) not reached)
        self.operator = operator                # address of the miner's operator, or None
        self.detail = detail
        self.height = height                    # Sealed height, or None when never sealed
        self.client = client                      # address of the paying client, or None
        self.role = role                          # which authority had to sign: client/operator

    def attributed_to(self):
        """The address this write is attributed to, according to the required role.

        Two distinct fields and not a single "author": `operator` and `client` are two different
        authorities, read from two different registries. Merging them would lose, when a log is read
        back, the information of which one was verified.
        """
        return self.client if self.role == ROLE_CLIENT else self.operator

    def mode(self):
        """`"nominal"` / `"degraded"` / `None`. `None` is not a degraded mode — see the header."""
        return _MODES.get(self.replay_regime)

    def to_persist(self) -> dict:
        """What the relay must store with the entry. Without it, a post-mortem on a specific entry
        cannot say which regime accepted it, nor on which height.

        `height` is a bare `int`, never a `SealedHeight`: the store format must not depend on a
        Python type, and a provenance marker read back from disk no longer proves anything it proved
        on the way in.
        """
        return {"regime": {"mode": self.mode(),
                           "height": None if self.height is None else int(self.height)},
                "registry": self.regime_registry,
                "operator": self.operator,
                "attribution": {"role": self.role, "address": self.attributed_to()}}

    def __repr__(self):
        return (f"WriteResult({'ACCEPTED' if self.accepted else 'REFUSED'}"
                f"{'' if self.reason is None else ' ' + self.reason}"
                f" registry={self.regime_registry} mode={self.mode()} height={self.height}"
                f" role={self.role})")


class Summary:
    """The count per mode — because a regime reported entry by entry does not say how many.

    This is the only figure that answers the question that eventually matters: *what share of the log
    was validated blind?* A regime persisted on each entry answers it too, but only by re-reading the
    whole store — that is, never in practice, as long as nobody knows it has to be done.

    `no_regime` counts the entries refused before the replay guard. They are neither nominal nor
    degraded, and pouring them into either bucket would inflate a security figure with entries that
    never reached the guard.
    """

    __slots__ = ("accepted", "refused", "no_regime", "reasons")

    def __init__(self):
        self.accepted = {MODE_NOMINAL: 0, MODE_DEGRADED: 0}
        self.refused = {MODE_NOMINAL: 0, MODE_DEGRADED: 0}
        self.no_regime = 0
        self.reasons: dict[str, int] = {}

    def record(self, r: "WriteResult") -> "WriteResult":
        m = r.mode()
        if m is None:
            self.no_regime += 1
        elif r.accepted:
            self.accepted[m] += 1
        else:
            self.refused[m] += 1
        if r.reason is not None:
            self.reasons[r.reason] = self.reasons.get(r.reason, 0) + 1
        return r

    @property
    def total(self) -> int:
        return sum(self.accepted.values()) + sum(self.refused.values()) + self.no_regime

    def render(self) -> str:
        m = " ".join(f"{k}={v}" for k, v in sorted(self.reasons.items())) or "none"
        return (f"WRITE_SUMMARY total={self.total} "
                f"accepted={sum(self.accepted.values())} "
                f"acc_nominal={self.accepted[MODE_NOMINAL]} "
                f"acc_degraded={self.accepted[MODE_DEGRADED]} "
                f"ref_nominal={self.refused[MODE_NOMINAL]} "
                f"ref_degraded={self.refused[MODE_DEGRADED]} "
                f"no_regime={self.no_regime} reasons[{m}]")


def verify_write(kind, key, body, miner_id, height, pubkey, signature,
                 registry, antireplay, head=None, *,
                 canon, verify_signature, address_from_key, read_height,
                 require_fee_registry=False, summary=None,
                 job_registry=None, job_from_key=None):
    """Full verdict on a signed write. The dependencies are injected.

    `canon(kind, key, body, miner_id, height) -> (message, digest)`
    `verify_signature(pubkey, message, signature) -> bool`   (must never raise)
    `address_from_key(pubkey, hrp) -> str`
    `read_height(message) -> int`  — the height carried by the signed message
    `registry`: object with `operator(miner_id) -> (address|None, state)`
    `antireplay`: object with `verify(key, sealed_height, digest, head) -> Verdict`
    `summary`: optional — object with `record(result)`, for the per-mode count.
    `job_registry`: object with `client(job_id) -> (address|None, state)` and
        `list_truncated() -> bool` — required for the kinds attributed to the client (`req`). Absent
        means those writes are refused, never fallen back. An object without `list_truncated`, or
        answering anything but a bool, is an error (it raises): the truncation of the list is never
        assumed to be "complete" because nobody could say.
    `job_from_key(key, miner_id) -> job_id`: the naming convention, injected so that it lives in a
        single place (`modea.job_registry.job_from_key`).

    `require_fee_registry=False` by default: a stale registry does not forbid writing. The trade-off
    is explicit — a halted chain must not destroy audit evidence. But a registry never read is
    refused: nothing can be attributed to anyone, and accepting then would mean running a relay
    without attribution that nothing distinguishes from a healthy one. Declining to fail closed does
    not authorise failing silently.

    Thin envelope, deliberately. The body has about ten exits; counting inside each one means missing
    one at the first addition — and a security counter that misses a path lies in the reassuring
    direction. Here no exit escapes the count, by construction rather than by vigilance.
    """
    r = _verdict(kind, key, body, miner_id, height, pubkey, signature, registry, antireplay, head,
                 canon=canon, verify_signature=verify_signature, address_from_key=address_from_key,
                 read_height=read_height, require_fee_registry=require_fee_registry,
                 job_registry=job_registry, job_from_key=job_from_key)
    if summary is not None:
        summary.record(r)
    return r


def _verdict(kind, key, body, miner_id, height, pubkey, signature, registry, antireplay, head, *,
             canon, verify_signature, address_from_key, read_height, require_fee_registry,
             job_registry, job_from_key):
    # -- (1) shape, before any cryptography ------------------------------------------------------
    if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != 33:
        return WriteResult(False, MALFORMED, detail=f"pubkey of {len(pubkey or b'')} bytes, 33 expected")
    if not isinstance(signature, (bytes, bytearray)) or not signature:
        return WriteResult(False, MALFORMED, detail="empty signature")
    try:
        message, digest = canon(kind, key, body, miner_id, height)
    except ValueError as e:
        return WriteResult(False, MALFORMED, detail=f"canonical message impossible: {e}")

    # -- (2) signature — nothing is believed before it. See the header: oracle + poisoning. -------
    try:
        valid = bool(verify_signature(bytes(pubkey), message, bytes(signature)))
    except Exception as e:  # noqa: BLE001
        return WriteResult(False, INVALID_SIGNATURE, detail=f"{type(e).__name__}")
    if not valid:
        return WriteResult(False, INVALID_SIGNATURE, detail="signature does not cover this message")

    # -- (2bis) sealing — the height is read back from the bytes the signature covers -------------
    # Not earlier, because the signature just verified is what makes this height trustworthy; not later,
    # because the refusals of (3) are logged with their height. From here on the value read back is
    # used, never the `height` parameter.
    try:
        sealed_height = seal_height(message, read_height)
    except Exception as e:  # noqa: BLE001
        return WriteResult(False, UNREADABLE_HEIGHT,
                           detail=f"height not readable back from the signed message: {e}")

    # -- (3) attribution: does the key sign for the authority this kind requires? -----------------
    # One branch decides who had to sign, and everything after it is shared. With the address check and
    # the replay guard duplicated on two paths, one path would eventually lose stage (4). The policy
    # varies; the guards do not.
    role = role_of_kind(kind)
    if role == ROLE_CLIENT:
        expected, reg_state, failure = _client_authority(key, miner_id, job_registry, job_from_key)
    else:
        expected, reg_state, failure = _operator_authority(miner_id, registry)
    if failure is not None:
        reason, detail = failure
        return WriteResult(False, reason, regime_registry=reg_state, height=sealed_height,
                           role=role, detail=detail)
    if require_fee_registry and reg_state != "FEES":
        return WriteResult(False, REGISTRY_MUTE, regime_registry=reg_state, height=sealed_height,
                           role=role, detail="registry not fresh while freshness is required by configuration")

    carried = {"role": role,
               "operator": expected if role == ROLE_OPERATOR else None,
               "client": expected if role == ROLE_CLIENT else None}
    try:
        hrp = expected[:expected.rfind("1")]
        calc = address_from_key(bytes(pubkey), hrp)
    except Exception as e:  # noqa: BLE001
        return WriteResult(False, MALFORMED, regime_registry=reg_state, height=sealed_height,
                           detail=f"address derivation impossible: {type(e).__name__}", **carried)
    if calc != expected:
        # The signature was valid. It is the binding that refuses — a signature proves possession of a
        # key, never the right to write on behalf of a given miner or client. The reason states which
        # of the two bindings failed: a foreign key and a foreign client do not call for the same
        # investigation.
        return WriteResult(False, FOREIGN_KEY if role == ROLE_OPERATOR else FOREIGN_CLIENT,
                           regime_registry=reg_state, height=sealed_height,
                           detail=f"valid signature but address != {role}", **carried)

    # -- (4) replay guard last: it writes, hence never on an unauthenticated entry ----------------
    #    `sealed_height`, not `height`: the high-water mark anchors on what the signature covers.
    v = antireplay.verify(bytes(pubkey), sealed_height, digest, head=head)
    if not v.accepted:
        return WriteResult(False, v.reason, regime_registry=reg_state, replay_regime=v.regime,
                           detail=v.detail, height=sealed_height, **carried)

    return WriteResult(True, None, regime_registry=reg_state, replay_regime=v.regime,
                       height=sealed_height, **carried)


# The two authority resolutions
# Each returns `(expected_address, registry_state, failure)` where `failure` is `None` or
# `(reason, detail)`. They compare nothing and write nothing: they only answer "who should have signed
# this". The decision stays in the main flow, in a single place.

def _operator_authority(miner_id, registry):
    operator, state = registry.operator(miner_id)
    if operator is None:
        # "Unknown" and "never known" call for different decisions, so the two causes stay distinct.
        if state == "NEVER_READ":
            return None, state, (REGISTRY_MUTE, "registry never read - no attribution possible")
        return None, state, (UNKNOWN_MINER, f"{miner_id} absent from the registry")
    return operator, state, None


def _client_authority(key, miner_id, job_registry, job_from_key):
    """Who pays for the job designated by this key.

    No job registry wired means refusal, never a fallback to the operator: that fallback would accept a
    `req` signed by any operator, and the rule that the payer signs would no longer hold.
    """
    if job_registry is None:
        return None, None, (JOB_REGISTRY_MUTE,
                            "no job registry wired - impossible to know who pays for this job; "
                            "refused rather than falling back to the miner authority")
    try:
        job_id = job_from_key(key, miner_id)
    except Exception as e:  # noqa: BLE001
        return None, None, (MALFORMED, f"key not readable as a job: {e}")
    client, state = job_registry.client(job_id)
    if client is None:
        if state == "NEVER_READ":
            return None, state, (JOB_REGISTRY_MUTE,
                                "job list never read - the payer is unknown")
        # An unknown job means refusal: losing a legitimate write (noisy, repairable) is preferable to
        # accepting an illegitimate one (silent, permanent).
        # The chain paginates (`query_job.go`, ListJob through `query.CollectionPaginate`) and exposes
        # `GetJob` by id, so a job missing from an incomplete list comes from a consumer that did not
        # follow `pagination.next_key`, never from a gap in the chain. `JobRegistry.list_truncated()`
        # tells from the payload, and the reason says so. There is no default: the call is direct, so a
        # missing method raises, and an answer that is not a bool raises too instead of being read by
        # its truthiness.
        truncated = job_registry.list_truncated()
        if not isinstance(truncated, bool):
            raise TypeError(f"list_truncated() answered {type(truncated).__name__}, a bool is required")
        return None, state, (UNKNOWN_JOB,
                            f"job {job_id!r:.40} absent" +
                            (" - the list read announced an unread continuation (next_key): "
                             "the consumer did not paginate" if truncated else
                             " from the list read, which was complete (no continuation announced)"))
    return client, state, None
