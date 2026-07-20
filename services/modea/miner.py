"""Mineur Mode A (durci).

Recoit une requete CHIFFREE, derive la cle de session (ECDH cle d'identite + cle EPHEMERE du
client), dechiffre EN MEMOIRE VERROUILLEE (SecureBytes : mlock/VirtualLock + zeroisation),
infere, re-chiffre la sortie, puis efface clair + cle. Ne journalise jamais le contenu.
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
    """Embedding semantique deterministe par HACHAGE DE TRAITS (feature hashing) : chaque mot
    incremente le seau md5(mot)%dim. Vecteur comparable entre mineurs pour N'IMPORTE QUEL texte
    (aucun vocabulaire fige). Deux reponses de meme sens -> vecteurs proches (cosinus eleve).
    Renvoie le vecteur d'entiers en chaine "n0,n1,..." (ancrable on-chain, verif. par cosinus)."""
    vec = [0] * dim
    for w in re.findall(r"[a-z0-9àâäéèêëîïôöùûüç]+", text.lower()):
        b = int(hashlib.md5(w.encode()).hexdigest()[:8], 16) % dim
        vec[b] += 1
    return ",".join(map(str, vec))


# --- DOC-13 : embedder SÉMANTIQUE réel (sentence-transformers), avec repli -----------------------
# Le feature-hashing ci-dessus est LEXICAL : deux réponses de même SENS mais mots différents ->
# vecteurs éloignés -> un mineur honnête risque le slash. Un vrai embedder sémantique corrige ça.
# Quantifié en entiers SIGNÉS bornés (la chaîne accepte les négatifs depuis DOC-13 inc.1 ; le cosinus
# est invariant d'échelle). NB : tous les mineurs d'un comité doivent utiliser le MÊME embedder (vrai
# sur une machine ; à épingler via le registre de modèles pour le multi-machines).
_DOC13_MODEL = os.environ.get("DENDRA_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
_DOC13_SCALE = 1_000_000
_DOC13_MAXMAG = 1 << 20  # doit matcher embedMaxMag on-chain (x/jobs verify_semantic)
_ST_MODEL = None
_ST_TRIED = False


def _quantize_embed(vec) -> str:
    """Vecteur de flottants -> entiers SIGNÉS bornés "n0,n1,..." (borne = embedMaxMag on-chain)."""
    out = []
    for x in list(vec)[:384]:  # cap = embedMaxDim on-chain (384) ; nomic=768 -> tronqué (parseIntVec rejette >384 + tx plus légère)
        q = int(round(float(x) * _DOC13_SCALE))
        if q > _DOC13_MAXMAG:
            q = _DOC13_MAXMAG
        elif q < -_DOC13_MAXMAG:
            q = -_DOC13_MAXMAG
        out.append(q)
    return ",".join(map(str, out))


def _semantic_embed(text: str):
    """Vrai embedding sémantique (modèle chargé une seule fois). None si indisponible -> repli."""
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
        vec = _ST_MODEL.encode(text, normalize_embeddings=True)  # vecteur unitaire signé
        return _quantize_embed(vec)
    except Exception:
        return None


_EMBED_MODE = os.environ.get("DENDRA_EMBED_MODE", "hash").lower()  # "st"|"hash"|"api"|"backend" ; defaut hash (cohérent devnet)

# AUDIT A→Z (P0) — HONNÊTETÉ LOUD. Le défaut "hash" est un embedder LEXICAL (feature-hashing) : la
# vérification mesure la ressemblance de MOTS, pas le SENS/la qualité. On le dit franchement au démarrage.
# Le TESTNET, lui, tourne en embedder SÉMANTIQUE RÉEL (deploy/testnet-miner = backend + nomic-embed-text).
if _EMBED_MODE == "hash":
    import sys as _sys
    print("[dendra] ⚠️  EMBEDDER=hash (LEXICAL) : la verification ne mesure PAS le sens, juste la "
          "ressemblance de mots. Vraie verification semantique -> DENDRA_EMBED_MODE=st (sentence-transformers) "
          "ou =backend (+DENDRA_EMBED_API_MODEL, ex: nomic-embed-text via Ollama).", file=_sys.stderr)


def _embed(text: str) -> str:
    """Embedder EXPLICITE (anti slash-trap DOC-13b). DENDRA_EMBED_MODE=st -> sentence-transformers EXIGÉ
    (échec DUR si absent, AUCUN repli silencieux vers hash : un vecteur hash vs ST donne cosinus ≈ 0 ->
    slash du mineur honnête mal configuré). Défaut "hash" = feature-hashing déterministe (mineurs cohérents)."""
    if _EMBED_MODE == "st":
        v = _semantic_embed(text)
        if v is None:
            raise RuntimeError(
                "DENDRA_EMBED_MODE=st mais sentence-transformers indisponible -> REFUS de committer "
                "(anti slash-trap DOC-13b). Installez sentence-transformers ou passez DENDRA_EMBED_MODE=hash.")
        return v
    return _feature_embed(text)


@dataclass
class MinerResult:
    sealed_result: Sealed
    result_commit: str         # hash du CHIFFRE (binding C1)
    content_commit: str = ""   # hash du CLAIR (mode HASH -> redondance C2/testnet)
    content_embed: str = ""    # embedding du CLAIR (mode SEMANTIQUE -> free-form, H5)
    in_tok: int = 0            # tokens d'entree (compte modele) -> tarification au token
    out_tok: int = 0           # tokens de sortie (compte modele)


class Miner:
    def __init__(self, miner_id: str, backend: str = "mock", hardened: bool = False, sk=None):
        self.miner_id = miner_id
        self.backend = get_backend(backend)
        self.hardened = hardened  # C4 : enveloppe l'inference dans ConfidentialGuard (anti-fuite)
        if sk is not None:        # identite PERSISTANTE (testnet : cle chargee depuis le keystore)
            self._sk, self.pub = sk, crypto.pub_bytes(sk)
        else:
            self._sk, self.pub = crypto.gen_keypair()  # cle d'identite (pub publiee + attestee)

    def _embed_output(self, text: str) -> str:
        """INT-7 : embedding UNIFIE via le BACKEND (LocalAI/OpenAI-compat /v1/embeddings) quand
        DENDRA_EMBED_MODE in {api,backend}. Echec DUR si le backend ne sait pas embedder (anti
        slash-trap : un repli silencieux vers hash donnerait un cosinus ~0 vs les autres mineurs ->
        slash d'un honnete mal configure). Sinon -> embedder local (_embed : hash|st).
        Defaut DENDRA_EMBED_MODE=hash inchange => e2e/devnet intacts."""
        if _EMBED_MODE in ("api", "backend"):
            fn = getattr(self.backend, "embed", None)
            vec = fn(text) if fn is not None else None
            if vec:
                return _quantize_embed(vec)
            raise RuntimeError(
                "DENDRA_EMBED_MODE=api mais le backend ne fournit pas d'embedding -> REFUS de committer "
                "(anti slash-trap). Configurez DENDRA_EMBED_API_MODEL + un backend OpenAI-compat (LocalAI), "
                "ou repassez DENDRA_EMBED_MODE=hash.")
        return _embed(text)

    def handle_job(self, job_id: str, client_eph_pk: bytes, sealed_prompt: Sealed, max_out: int = 0) -> MinerResult:
        aad = job_id.encode()
        key = crypto.derive_session_key(self._sk, client_eph_pk, info=aad)
        # decrypt renvoie un bytes immuable (copie transitoire Python, cf. MODE-A-SECURITE) ;
        # on le bascule aussitot dans un tampon VERROUILLE et zeroisable.
        transient = crypto.decrypt(key, sealed_prompt, aad=aad)
        out_bytes = b""
        guard = ConfidentialGuard() if self.hardened else nullcontext()
        try:
            with guard, SecureBytes.copy_from(transient) as sb:
                prompt = sb.view().decode("utf-8")
                output = self.backend.generate(prompt, max_out=max_out)   # inference (plafond demande par le client)
                in_tok = getattr(self.backend, "in_tok", 0) or max(1, len(prompt) // 4)
                out_tok = getattr(self.backend, "out_tok", 0) or max(1, len(output) // 4)
                out_bytes = output.encode("utf-8")
                content_commit = hashlib.sha256(out_bytes).hexdigest()  # mode HASH
                content_embed = self._embed_output(output)              # mode SEMANTIQUE (DOC-13 + INT-7 backend unifie)
                sealed_result = crypto.encrypt(key, out_bytes, aad=aad)
                result_commit = hashlib.sha256(
                    sealed_result.nonce + sealed_result.ct).hexdigest()
            return MinerResult(sealed_result=sealed_result, result_commit=result_commit,
                               content_commit=content_commit, content_embed=content_embed,
                               in_tok=in_tok, out_tok=out_tok)
        finally:
            # zeroisation post-job : cle de session + tampons CLAIRS (prompt dechiffre + reponse encodee).
            # SecureBytes zeroise deja sa copie verrouillee ; ici on ecrase les bytes immuables
            # transitoires pour reduire la fenetre ou le clair survit en RAM avant GC (limite GC
            # documentee dans hardening.best_effort_wipe : best-effort, pas une garantie d'effacement).
            crypto.zeroize(bytearray(key))
            best_effort_wipe(transient)
            best_effort_wipe(out_bytes)

    def attest(self, expected_manifest: dict) -> tuple[bool, list]:
        """Auto-attestation : le code du paquet correspond-il au binaire enregistre ? (ADR-011)"""
        from .hardening import self_attest
        return self_attest(expected_manifest)
