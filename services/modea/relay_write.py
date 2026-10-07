"""Verification of a signed write at the relay — the COMPOSITION of the four building blocks.

WHY THIS FILE EXISTS
`cosmos_addr`, `relay_canon`, `registry_cache` and `relay_antireplay` are each tested on their own.
The faults that remain are not in the pieces: **they are in their ORDER**.

THE ORDER OF THE CHECKS IS A SECURITY DECISION, NOT AN IMPLEMENTATION DETAIL.

    (1) shape       — sizes and types, before any cryptography
    (2) SIGNATURE   — expensive (~475 us), but nothing is believed before it
    (3) attribution — address(pubkey) == miner.Operator, through the cached registry
    (4) replay      — cheap, and it WRITES: therefore last

**Why the signature comes BEFORE the replay guard**, although it costs twenty times more:
  - **oracle** — if the replay guard ran first, a third party holding no key would learn, from the
    response difference alone, whether a digest has already been seen. It would probe the store
    without ever signing anything;
  - **poisoning** — the replay guard WRITES (it memorises the digest). Placing it before the
    signature would let anyone fill the digest set with noise, up to exhausting memory or evicting
    legitimate digests. **A guard that writes must never run on unauthenticated input.**
The overhead is measured and accepted: 475 us of signature against 22 us of replay checking — at the
rate limit already in place (12 req/s), 0.6% of one core. Verification is paid for so that the
oracle is not offered.

THIS MODULE DOES NOT TOUCH THE STORE and decides no eviction. It returns a verdict, the REGIME it
was returned under, and **the sealed height it ruled on**. The relay stores that block with the
entry: `{"mode": "nominal"|"degraded", "height": N|null}`.

**`mode` IS `null` WHEN THE REPLAY GUARD DID NOT RUN** — an entry refused at (1)(2)(3) went through
no regime. Writing `"degraded"` there by default would be **false testimony**: a reader of the log
would believe a guard ran in degraded mode when it did not run at all. An empty field can be read; a
false field is never caught up with.

WHERE THE HEIGHT COMES FROM
The height handed to the replay guard is **READ BACK FROM THE SIGNED MESSAGE** (`seal_height`),
not taken from the call parameter. The two coincide today — and that is exactly why the read-back is
wired: a coincidence does not maintain itself. If `canon()` ever normalises or truncates the height,
the high-water mark would anchor on a value **the signature does not cover**. And the sealing happens
**after** (2), never before: reading a height out of bytes nobody has authenticated is no better than
receiving it from the caller.
"""
from __future__ import annotations

try:                                   # `modea` package
    from .relay_antireplay import DEGRADE, NOMINAL, seal_height
except ImportError:                    # flat files (sandbox, isolated tests)
    from relay_antireplay import DEGRADE, NOMINAL, seal_height   # type: ignore

# named refusal reasons — a silent refusal can neither be debugged nor audited
MAL_FORME = "MAL_FORME"
SIGNATURE_INVALIDE = "SIGNATURE_INVALIDE"
FOREIGN_KEY = "FOREIGN_KEY"          # valid signature, but address != operator
MINEUR_INCONNU = "MINEUR_INCONNU"        # registry read, this miner is not in it
REGISTRY_MUTE = "REGISTRY_MUTE"          # registry NEVER read — nothing can be attributed to anyone
UNREADABLE_HEIGHT = "UNREADABLE_HEIGHT"  # the signed message yields no height -> nothing is guessed
JOB_INCONNU = "JOB_INCONNU"              # job list READ, this job is not in it
CLIENT_ETRANGER = "CLIENT_ETRANGER"      # valid signature, but address != paying client
JOB_REGISTRY_MUTE = "JOB_REGISTRY_MUTE"  # never read, or not wired — the payer is unknown

# WHO MAY WRITE WHAT
# A `req` deposit is the REQUEST of a job: its legitimate author is **the client who pays for it**,
# not a miner operator. The other deposits (reveal, verdict) are produced by the miner.
#
# A TABLE, NOT AN `if kind == "req"` SCATTERED AROUND. A role test copied in three places diverges at
# the first added kind — and the forgotten kind would silently fall back to the default role, that is,
# to the wrong authority. Here the rule reads at a glance, and a kind absent from the table takes the
# default role explicitly.
ROLE_CLIENT = "client"
ROLE_OPERATOR = "operator"
ROLES_PAR_KIND = {"req": ROLE_CLIENT}
ROLE_PAR_DEFAUT = ROLE_OPERATOR


def role_du_kind(kind) -> str:
    """Which authority must sign this kind. Type-insensitive: a non-textual kind takes the default
    role rather than raising — the shape is already checked at (1)."""
    return ROLES_PAR_KIND.get(kind, ROLE_PAR_DEFAUT) if isinstance(kind, str) else ROLE_PAR_DEFAUT

# Vocabulary of the persisted field. Deliberately DIFFERENT from the replay guard's internal constants
# (`NOMINAL`/`DEGRADE`): those are a module detail, this one is a STORE format. Conflating them would
# make the on-disk format depend on an internal rename.
MODE_NOMINAL = "nominal"
MODE_DEGRADE = "degraded"
_MODES = {NOMINAL: MODE_NOMINAL, DEGRADE: MODE_DEGRADE}


class Resultat:
    __slots__ = ("accepte", "motif", "regime_registry", "regime_rejeu", "operator", "detail",
                 "height", "client", "role")

    def __init__(self, accepte, motif=None, regime_registry=None, regime_rejeu=None,
                 operator=None, detail="", height=None, client=None, role=None):
        self.accepte = accepte
        self.motif = motif
        self.regime_registry = regime_registry    # NEVER_READ / FEES / STALE
        self.regime_rejeu = regime_rejeu          # NOMINAL / DEGRADE / None ((4) not reached)
        self.operator = operator                # address of the miner's OPERATOR, or None
        self.detail = detail
        self.height = height                    # SEALED height, or None when never sealed
        self.client = client                      # address of the PAYING CLIENT, or None
        self.role = role                          # which authority had to sign: client/operator

    def attribue_a(self):
        """The address this write is attributed to, according to the required role.

        Two distinct fields and NOT a single "author": `operator` and `client` are two different
        authorities, read from two different registries. Merging them would lose, when a log is read
        back, the information of which one was verified.
        """
        return self.client if self.role == ROLE_CLIENT else self.operator

    def mode(self):
        """`"nominal"` / `"degraded"` / `None`. `None` is NOT a degraded mode — see the header."""
        return _MODES.get(self.regime_rejeu)

    def a_persister(self) -> dict:
        """What the relay MUST store with the entry. Without it, a post-mortem on a specific entry
        cannot say which regime accepted it, nor on which height.

        `height` is a bare `int`, never a `HauteurScellee`: the store format must not depend on a
        Python type, and a provenance marker read back from disk no longer proves anything it proved
        on the way in.
        """
        return {"regime": {"mode": self.mode(),
                           "height": None if self.height is None else int(self.height)},
                "registry": self.regime_registry,
                "operator": self.operator,
                "attribution": {"role": self.role, "adresse": self.attribue_a()}}

    def __repr__(self):
        return (f"Resultat({'ACCEPTED' if self.accepte else 'REFUSED'}"
                f"{'' if self.motif is None else ' ' + self.motif}"
                f" registry={self.regime_registry} mode={self.mode()} height={self.height}"
                f" role={self.role})")


class Summary:
    """The COUNT per mode — because a regime reported entry by entry does not say HOW MANY.

    This is the only figure that answers the question that eventually matters: *what share of the log
    was validated blind?* A regime persisted on each entry answers it too, but only by re-reading the
    whole store — that is, never in practice, as long as nobody knows it has to be done.

    `no_regime` counts the entries refused BEFORE the replay guard. They are neither nominal nor
    degraded, and pouring them into either bucket would inflate a security figure with entries that
    never reached the guard.
    """

    __slots__ = ("accepted", "refused", "no_regime", "reasons")

    def __init__(self):
        self.accepted = {MODE_NOMINAL: 0, MODE_DEGRADE: 0}
        self.refused = {MODE_NOMINAL: 0, MODE_DEGRADE: 0}
        self.no_regime = 0
        self.reasons: dict[str, int] = {}

    def record(self, r: "Resultat") -> "Resultat":
        m = r.mode()
        if m is None:
            self.no_regime += 1
        elif r.accepte:
            self.accepted[m] += 1
        else:
            self.refused[m] += 1
        if r.motif is not None:
            self.reasons[r.motif] = self.reasons.get(r.motif, 0) + 1
        return r

    @property
    def total(self) -> int:
        return sum(self.accepted.values()) + sum(self.refused.values()) + self.no_regime

    def render(self) -> str:
        m = " ".join(f"{k}={v}" for k, v in sorted(self.reasons.items())) or "none"
        return (f"WRITE_SUMMARY total={self.total} "
                f"accepted={sum(self.accepted.values())} "
                f"acc_nominal={self.accepted[MODE_NOMINAL]} "
                f"acc_degraded={self.accepted[MODE_DEGRADE]} "
                f"ref_nominal={self.refused[MODE_NOMINAL]} "
                f"ref_degraded={self.refused[MODE_DEGRADE]} "
                f"no_regime={self.no_regime} reasons[{m}]")


def verify_write(kind, key, body, miner_id, height, pubkey, signature,
                 registry, antireplay, tete=None, *,
                 canon, verify_signature, address_from_key, read_height,
                 require_fee_registry=False, summary=None,
                 job_registry=None, job_from_key=None):
    """Full verdict on a signed write. The dependencies are INJECTED.

    `canon(kind, key, body, miner_id, height) -> (message, digest)`
    `verify_signature(pubkey, message, signature) -> bool`   (must never raise)
    `address_from_key(pubkey, hrp) -> str`
    `read_height(message) -> int`  — the height CARRIED by the signed message
    `registry`: object with `operator(miner_id) -> (address|None, state)`
    `antireplay`: object with `verifier(key, sealed_height, digest, tete) -> Verdict`
    `summary`: optional — object with `record(result)`, for the per-mode COUNT.
    `job_registry`: object with `client(job_id) -> (address|None, state)` — REQUIRED for the kinds
        attributed to the client (`req`). Absent means those writes are REFUSED, never fallen back.
    `job_from_key(key, miner_id) -> job_id`: the naming convention, injected so that it lives in a
        single place (`modea.job_registry.job_from_key`).

    `require_fee_registry=False` by default: a STALE registry does not forbid writing. The trade-off
    is explicit — a halted chain must not destroy audit evidence. But a registry **NEVER READ** is
    refused: nothing can be attributed to anyone, and accepting then would mean running a relay
    without attribution that nothing distinguishes from a healthy one. Declining to fail closed does
    not authorise failing silently.

    THIN ENVELOPE, DELIBERATELY. The body has about ten exits; counting inside each one means missing
    one at the first addition — and a security counter that misses a path lies in the reassuring
    direction. Here **no exit escapes the count**, by construction rather than by vigilance.
    """
    r = _verdict(kind, key, body, miner_id, height, pubkey, signature, registry, antireplay, tete,
                 canon=canon, verify_signature=verify_signature, address_from_key=address_from_key,
                 read_height=read_height, require_fee_registry=require_fee_registry,
                 job_registry=job_registry, job_from_key=job_from_key)
    if summary is not None:
        summary.record(r)
    return r


def _verdict(kind, key, body, miner_id, height, pubkey, signature, registry, antireplay, tete, *,
             canon, verify_signature, address_from_key, read_height, require_fee_registry,
             job_registry, job_from_key):
    # -- (1) shape, before any cryptography ------------------------------------------------------
    if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != 33:
        return Resultat(False, MAL_FORME, detail=f"pubkey of {len(pubkey or b'')} bytes, 33 expected")
    if not isinstance(signature, (bytes, bytearray)) or not signature:
        return Resultat(False, MAL_FORME, detail="empty signature")
    try:
        message, empreinte = canon(kind, key, body, miner_id, height)
    except ValueError as e:
        return Resultat(False, MAL_FORME, detail=f"canonical message impossible: {e}")

    # -- (2) SIGNATURE — nothing is believed before it. See the header: oracle + poisoning. -------
    try:
        valide = bool(verify_signature(bytes(pubkey), message, bytes(signature)))
    except Exception as e:  # noqa: BLE001
        return Resultat(False, SIGNATURE_INVALIDE, detail=f"{type(e).__name__}")
    if not valide:
        return Resultat(False, SIGNATURE_INVALIDE, detail="signature does not cover this message")

    # -- (2bis) SEALING — the height is READ BACK FROM THE BYTES THE SIGNATURE COVERS -------------
    # Here and not earlier: what makes this height trustworthy is the signature just verified. Here
    # and not later: the refusals of (3) deserve to be logged WITH their height. And above all — it is
    # NOT the `height` parameter that continues, it is the value read back.
    try:
        sealed_height = seal_height(message, read_height)
    except Exception as e:  # noqa: BLE001
        return Resultat(False, UNREADABLE_HEIGHT,
                        detail=f"height not readable back from the signed message: {e}")

    # -- (3) attribution: does the key sign FOR THE AUTHORITY THIS KIND REQUIRES? -----------------
    # A SINGLE BRANCH RESOLVES "WHO HAD TO SIGN"; everything that follows is COMMON. Duplicating the
    # address comparison and the replay guard across two paths would accept that one of them
    # eventually loses stage (4) — the same fault as counting in each `return` instead of counting in
    # the envelope. The policy varies; the guards never do.
    role = role_du_kind(kind)
    if role == ROLE_CLIENT:
        expected, reg_state, failure = _client_authority(key, miner_id, job_registry, job_from_key)
    else:
        expected, reg_state, failure = _operator_authority(miner_id, registry)
    if failure is not None:
        motif, detail = failure
        return Resultat(False, motif, regime_registry=reg_state, height=sealed_height,
                        role=role, detail=detail)
    if require_fee_registry and reg_state != "FEES":
        return Resultat(False, REGISTRY_MUTE, regime_registry=reg_state, height=sealed_height,
                        role=role, detail="registry not fresh while freshness is REQUIRED by configuration")

    porte = {"role": role,
             "operator": expected if role == ROLE_OPERATOR else None,
             "client": expected if role == ROLE_CLIENT else None}
    try:
        hrp = expected[:expected.rfind("1")]
        calc = address_from_key(bytes(pubkey), hrp)
    except Exception as e:  # noqa: BLE001
        return Resultat(False, MAL_FORME, regime_registry=reg_state, height=sealed_height,
                        detail=f"address derivation impossible: {type(e).__name__}", **porte)
    if calc != expected:
        # The signature was VALID. It is the BINDING that refuses — a signature proves possession of a
        # key, never the right to write on behalf of a given miner or client. The reason states WHICH
        # of the two bindings failed: a foreign key and a foreign client do not call for the same
        # investigation.
        return Resultat(False, FOREIGN_KEY if role == ROLE_OPERATOR else CLIENT_ETRANGER,
                        regime_registry=reg_state, height=sealed_height,
                        detail=f"valid signature but address != {role}", **porte)

    # -- (4) replay guard LAST: it WRITES, hence never on an unauthenticated entry ----------------
    #    `sealed_height`, NOT `height`: the high-water mark anchors on what the signature covers.
    v = antireplay.verifier(bytes(pubkey), sealed_height, empreinte, tete=tete)
    if not v.accepte:
        return Resultat(False, v.motif, regime_registry=reg_state, regime_rejeu=v.regime,
                        detail=v.detail, height=sealed_height, **porte)

    return Resultat(True, None, regime_registry=reg_state, regime_rejeu=v.regime,
                    height=sealed_height, **porte)


# THE TWO AUTHORITY RESOLUTIONS
# Each returns `(expected_address, registry_state, failure)` where `failure` is `None` or
# `(reason, detail)`. They compare NOTHING and write NOTHING: they only answer "who should have signed
# this". The decision stays in the main flow, in a single place.

def _operator_authority(miner_id, registry):
    operator, state = registry.operator(miner_id)
    if operator is None:
        # Distinguishing the two causes is the point: "unknown" and "never known" do not call for the
        # same decision, and conflating them erases the attribution silently.
        if state == "NEVER_READ":
            return None, state, (REGISTRY_MUTE, "registry never read - no attribution possible")
        return None, state, (MINEUR_INCONNU, f"{miner_id} absent from the registry")
    return operator, state, None


def _client_authority(key, miner_id, job_registry, job_from_key):
    """Who PAYS for the job designated by this key.

    NO JOB REGISTRY WIRED means REFUSAL, never a fallback to the operator. Falling back to the miner
    authority would accept a `req` signed by any operator: the "the payer signs" rule would become
    decorative exactly where it matters. A policy with a silent fallback is not a policy, it is a
    defect.
    """
    if job_registry is None:
        return None, None, (JOB_REGISTRY_MUTE,
                            "no job registry wired - impossible to know who pays for this job; "
                            "REFUSED rather than falling back to the miner authority")
    try:
        job_id = job_from_key(key, miner_id)
    except Exception as e:  # noqa: BLE001
        return None, None, (MAL_FORME, f"key not readable as a job: {e}")
    client, state = job_registry.client(job_id)
    if client is None:
        if state == "NEVER_READ":
            return None, state, (JOB_REGISTRY_MUTE,
                                "job list never read - the payer is unknown")
        # An unknown job means REFUSAL: losing a legitimate write (noisy, repairable) is preferable to
        # accepting an illegitimate one (silent, permanent).
        # The cause is READ, not assumed: the chain paginates (`query_job.go`, ListJob through
        # `query.CollectionPaginate`) and exposes `GetJob` by id. An "unknown" therefore comes from a
        # consumer that did not follow `pagination.next_key`, not from a gap in the chain.
        # `JobRegistry.liste_tronquee()` PROVES it from the payload; the reason says so.
        suite = getattr(job_registry, "liste_tronquee", lambda: False)()
        return None, state, (JOB_INCONNU,
                            f"job {job_id!r:.40} absent" +
                            (" - the list read announced an UNREAD CONTINUATION (next_key): "
                             "the consumer did not paginate" if suite else
                             " from the list read, which was complete (no continuation announced)"))
    return client, state, None
