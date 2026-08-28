"""Mode A miner (hardened).

Receives an ENCRYPTED request, derives the session key (ECDH between the identity key and the
client's EPHEMERAL key), decrypts in LOCKED MEMORY (SecureBytes: mlock/VirtualLock plus zeroisation),
runs inference, re-encrypts the output, then wipes both the plaintext and the key. Content is never
written to logs.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import nullcontext
from dataclasses import dataclass

from . import crypto
from .crypto import Sealed
from .hardening import ConfidentialGuard, SecureBytes, best_effort_wipe
from .inference import get_backend


def _feature_embed(text: str, dim: int = 64) -> str:
    """Deterministic embedding by FEATURE HASHING: each word increments the bucket md5(word) % dim.

    The resulting vector is comparable across miners for ANY text, with no fixed vocabulary to agree
    on. Answers that share wording land on nearby vectors (high cosine). Returns the integer vector
    as the string "n0,n1,...", the form the chain anchors and compares by cosine."""
    vec = [0] * dim
    # This class DEFINES what a word is; the accented letters are part of the rule, not decoration.
    # Narrowing it fragments every accented word into pieces and moves the buckets, so two miners
    # running different versions of this tokeniser would read divergence where there is none. Change
    # it only for the whole committee at once.
    for w in re.findall(r"[a-z0-9àâäéèêëîïôöùûüç]+", text.lower()):
        b = int(hashlib.md5(w.encode()).hexdigest()[:8], 16) % dim
        vec[b] += 1
    return ",".join(map(str, vec))


# --- Real SEMANTIC embedder (sentence-transformers), with fallback ------------------------------
# The feature hashing above is LEXICAL: two answers with the same MEANING but different words land
# on distant vectors, and an honest miner risks a slash. A real semantic embedder fixes that.
# Values are quantised to bounded SIGNED integers (the chain accepts negatives, and cosine is
# scale-invariant). Every miner of a committee must use the SAME embedder; across machines that
# agreement has to be pinned through the model registry rather than assumed.
_DOC13_MODEL = os.environ.get("DENDRA_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
_DOC13_SCALE = 1_000_000
_DOC13_MAXMAG = 1 << 20  # must match embedMaxMag on-chain (x/jobs verify_semantic)
_ST_MODEL = None
_ST_TRIED = False


def _quantize_embed(vec) -> str:
    """Float vector -> bounded SIGNED integers "n0,n1,..." (the bound is embedMaxMag on-chain)."""
    out = []
    for x in list(vec)[:384]:  # cap = embedMaxDim on-chain; longer models are truncated (parseIntVec rejects >384, and the tx stays smaller)
        q = int(round(float(x) * _DOC13_SCALE))
        if q > _DOC13_MAXMAG:
            q = _DOC13_MAXMAG
        elif q < -_DOC13_MAXMAG:
            q = -_DOC13_MAXMAG
        out.append(q)
    return ",".join(map(str, out))


def _semantic_embed(text: str):
    """Real semantic embedding (the model is loaded once). Returns None when unavailable."""
    global _ST_MODEL, _ST_TRIED
    if _ST_MODEL is None:
        if _ST_TRIED:
            return None
        _ST_TRIED = True
        try:
            from sentence_transformers import SentenceTransformer
            _ST_MODEL = SentenceTransformer(_DOC13_MODEL)
        except Exception:
            return None
    try:
        vec = _ST_MODEL.encode(text, normalize_embeddings=True)  # signed unit vector
        return _quantize_embed(vec)
    except Exception:
        return None


_EMBED_MODE = os.environ.get("DENDRA_EMBED_MODE", "hash").lower()  # "st"|"hash"|"api"|"backend"; default hash

# The "hash" default is a LEXICAL embedder: verification measures word overlap, not meaning or
# quality. That limitation is announced loudly at startup rather than left for an operator to
# discover from a slash. A testnet deployment runs a real semantic embedder instead.
if _EMBED_MODE == "hash":
    import sys as _sys
    print("[dendra] WARNING: EMBEDDER=hash (LEXICAL). Verification does NOT measure meaning, only "
          "word overlap. For real semantic verification set DENDRA_EMBED_MODE=st (sentence-transformers) "
          "or =backend (with DENDRA_EMBED_API_MODEL, e.g. nomic-embed-text via Ollama).", file=_sys.stderr)


def _embed(text: str) -> str:
    """Explicit embedder selection, to avoid a slash trap.

    With DENDRA_EMBED_MODE=st, sentence-transformers is REQUIRED: its absence is a hard failure and
    never a silent fallback to hashing. A hash vector compared against sentence-transformer vectors
    yields a cosine near zero, so the fallback would slash the honest but misconfigured miner. The
    default "hash" is deterministic feature hashing, which keeps miners mutually consistent."""
    if _EMBED_MODE == "st":
        v = _semantic_embed(text)
        if v is None:
            raise RuntimeError(
                "DENDRA_EMBED_MODE=st but sentence-transformers is unavailable -> REFUSING to commit. "
                "Committing a hash vector against sentence-transformer peers would slash this miner. "
                "Install sentence-transformers or set DENDRA_EMBED_MODE=hash.")
        return v
    return _feature_embed(text)


@dataclass
class MinerResult:
    sealed_result: Sealed
    result_commit: str         # hash of the CIPHERTEXT (binds the answer to this job)
    content_commit: str = ""   # hash of the PLAINTEXT (hash verification mode)
    content_embed: str = ""    # embedding of the PLAINTEXT (semantic mode, free-form answers)
    # Salted commitment to the QUESTION. Computed here because this is the only place the plaintext
    # prompt exists, and it must not leave: what leaves is a hash a juror can check against the
    # reveal, and that nobody else can test without the miner's key.
    prompt_commit: str = ""
    in_tok: int = 0            # input tokens as counted by the model, for per-token pricing
    out_tok: int = 0           # output tokens as counted by the model


class Miner:
    def __init__(self, miner_id: str, backend: str = "mock", hardened: bool = False, sk=None):
        self.miner_id = miner_id
        self.backend = get_backend(backend)
        self.hardened = hardened  # wraps inference in ConfidentialGuard to limit leakage
        if sk is not None:        # PERSISTENT identity: the key is loaded from the keystore
            self._sk, self.pub = sk, crypto.pub_bytes(sk)
        else:
            self._sk, self.pub = crypto.gen_keypair()  # identity key; the public half is published and attested

    def _embed_output(self, text: str) -> str:
        """Embed the answer through the BACKEND (OpenAI-compatible /v1/embeddings) when
        DENDRA_EMBED_MODE is `api` or `backend`. A backend that cannot embed is a HARD failure, never
        a silent fallback to hashing: such a fallback yields a cosine near zero against the other
        miners and slashes an honest but misconfigured node. Any other mode uses the local embedder.
        """
        if _EMBED_MODE in ("api", "backend"):
            fn = getattr(self.backend, "embed", None)
            vec = fn(text) if fn is not None else None
            if vec:
                return _quantize_embed(vec)
            raise RuntimeError(
                "DENDRA_EMBED_MODE=api but the backend provides no embedding -> REFUSING to commit. "
                "Set DENDRA_EMBED_API_MODEL and point at an OpenAI-compatible backend, or set "
                "DENDRA_EMBED_MODE=hash.")
        return _embed(text)

    def handle_job(self, job_id: str, client_eph_pk: bytes, sealed_prompt: Sealed, max_out: int = 0) -> MinerResult:
        aad = job_id.encode()
        key = crypto.derive_session_key(self._sk, client_eph_pk, info=aad)
        # decrypt returns immutable bytes, i.e. a transient Python copy; it is moved at once into a
        # LOCKED, zeroisable buffer.
        transient = crypto.decrypt(key, sealed_prompt, aad=aad)
        out_bytes = b""
        guard = ConfidentialGuard() if self.hardened else nullcontext()
        try:
            with guard, SecureBytes.copy_from(transient) as sb:
                prompt = sb.view().decode("utf-8")
                output = self.backend.generate(prompt, max_out=max_out)   # inference, capped as the client requested
                in_tok = getattr(self.backend, "in_tok", 0) or max(1, len(prompt) // 4)
                out_tok = getattr(self.backend, "out_tok", 0) or max(1, len(output) // 4)
                out_bytes = output.encode("utf-8")
                content_commit = hashlib.sha256(out_bytes).hexdigest()  # hash verification mode
                content_embed = self._embed_output(output)              # semantic verification mode
                # THE QUESTION IS COMMITTED TOO, AND FROM INSIDE THE GUARD.
                # The juror receives the prompt from the reveal, i.e. from the audited party. Without
                # a commitment made BEFORE the answer is known, it grades an answer against whatever
                # question the audited party chooses to show. The plaintext still never leaves: only
                # this salted digest does.
                prompt_commit = crypto.prompt_commitment(
                    crypto.prompt_salt(self._sk, job_id), prompt)
                sealed_result = crypto.encrypt(key, out_bytes, aad=aad)
                result_commit = hashlib.sha256(
                    sealed_result.nonce + sealed_result.ct).hexdigest()
            return MinerResult(sealed_result=sealed_result, result_commit=result_commit,
                               prompt_commit=prompt_commit,
                               content_commit=content_commit, content_embed=content_embed,
                               in_tok=in_tok, out_tok=out_tok)
        finally:
            # Post-job zeroisation: session key plus the PLAINTEXT buffers (decrypted prompt and
            # encoded answer). SecureBytes already zeroes its locked copy; this overwrites the
            # transient immutable bytes to shorten the window in which plaintext survives in RAM
            # before garbage collection. hardening.best_effort_wipe documents the limit: this is
            # best effort, not a guarantee of erasure.
            crypto.zeroize(bytearray(key))
            best_effort_wipe(transient)
            best_effort_wipe(out_bytes)

    def attest(self, expected_manifest: dict) -> tuple[bool, list]:
        """Self-attestation: does the package code match the registered manifest? (ADR-011)"""
        from .hardening import self_attest
        return self_attest(expected_manifest)
