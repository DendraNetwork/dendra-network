"""Confinement au niveau PROCESSUS du mineur Mode A (CR-04/05, audit) — complète `hardening.py`.

`hardening.py` durcit l'inférence au niveau APPLICATIF et PAR-TAMPON (SecureBytes mlock + zéroïsation,
ConfidentialGuard no-egress/no-disk pendant un job). Ce module ajoute le durcissement de TOUT LE
PROCESSUS, appliqué UNE FOIS au démarrage du démon mineur :

  - PR_SET_DUMPABLE = 0   -> le noyau refuse qu'un débogueur du MÊME utilisateur (non-root) attache
                            ptrace, et n'écrit aucun core dump (mémoire = clair) sur disque. C'est
                            précisément le risque résiduel nommé dans MODE-A-SECURITE (« attacher un
                            débogueur au processus »). NB : root PEUT toujours outrepasser -> seul le
                            Mode B (MPC) ou un TEE ferme ce trou. Honnête : on relève la barre contre
                            le snooping même-utilisateur / accidentel, pas contre root.
  - PR_SET_NO_NEW_PRIVS=1 -> le processus (et ses enfants) ne peut PLUS gagner de privilèges (setuid,
                            capabilities) -> une exfiltration ne peut pas s'escalader.
  - RLIMIT_CORE = 0       -> ceinture+bretelles avec DUMPABLE=0 : aucun core dump.
  - mlockall (opt-in)     -> verrouille TOUTE la mémoire du processus (pas seulement les SecureBytes) :
                            activations / KV-cache hors SecureBytes ne partent pas en swap. Coûteux sur
                            un processus ML (peut échouer sans CAP_IPC_LOCK / RLIMIT_MEMLOCK) -> OFF par
                            défaut, activé par DENDRA_MLOCKALL=1. Best-effort, rapport honnête.
  - umask 0077            -> tout fichier créé est privé (0600) par défaut.

Tout est BEST-EFFORT et multiplateforme : sur Windows / sans libc, les contrôles indisponibles sont
simplement rapportés `False`. Aucun appel n'est fatal (le mineur démarre quand même, en rapportant ce
qui a pu être appliqué) — l'attestation de confinement expose l'état réel, vérifiable.

Le confinement OS FORT (namespace réseau sans route sauf relais, seccomp, FS read-only) relève de
l'OS et se pose via les lanceurs `modea_confine.sh` (bubblewrap) + `modea_egress.sh` (pare-feu nft).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import sys

# Constantes prctl Linux (asm-generic/uapi). Valeurs stables de l'ABI Linux.
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_MCL_CURRENT = 1
_MCL_FUTURE = 2


def _libc():
    if sys.platform.startswith("win"):
        return None
    try:
        return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except Exception:
        return None


def _prctl(libc, option: int, arg2: int = 0) -> bool:
    try:
        return libc.prctl(option, arg2, 0, 0, 0) == 0
    except Exception:
        return False


def set_non_dumpable() -> bool:
    """PR_SET_DUMPABLE=0 : pas de ptrace même-utilisateur, pas de core dump. Linux uniquement."""
    libc = _libc()
    if libc is None or platform.system() != "Linux":
        return False
    return _prctl(libc, _PR_SET_DUMPABLE, 0)


def set_no_new_privs() -> bool:
    """PR_SET_NO_NEW_PRIVS=1 : interdit toute élévation de privilèges ultérieure. Linux uniquement."""
    libc = _libc()
    if libc is None or platform.system() != "Linux":
        return False
    return _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1)


def disable_core_dumps() -> bool:
    """RLIMIT_CORE = 0 (POSIX)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return True
    except Exception:
        return False


def lock_all_memory() -> bool:
    """mlockall(MCL_CURRENT|MCL_FUTURE) : verrouille toute la mémoire (anti-swap). Best-effort."""
    libc = _libc()
    if libc is None:
        return False
    try:
        return libc.mlockall(_MCL_CURRENT | _MCL_FUTURE) == 0
    except Exception:
        return False


def apply_process_confinement(mlockall: bool | None = None) -> dict:
    """Applique le durcissement processus au démarrage et renvoie un rapport HONNÊTE de l'état réel.

    `mlockall` : None -> lit DENDRA_MLOCKALL (défaut OFF, car coûteux sur un processus ML)."""
    if mlockall is None:
        mlockall = os.environ.get("DENDRA_MLOCKALL", "0") == "1"
    try:
        os.umask(0o077)
        umask_ok = True
    except Exception:
        umask_ok = False
    rep = {
        "platform": platform.system(),
        "non_dumpable": set_non_dumpable(),       # anti-debugger même-utilisateur + anti core dump
        "no_new_privs": set_no_new_privs(),        # pas d'escalade
        "core_dumps_disabled": disable_core_dumps(),
        "umask_0077": umask_ok,
        "mlockall": (lock_all_memory() if mlockall else False),
        "mlockall_requested": bool(mlockall),
    }
    # Risque résiduel TOUJOURS vrai (honnêteté) : un opérateur ROOT contourne tout ceci.
    rep["residual_root_risk"] = True
    rep["note"] = ("durcissement processus best-effort : élève la barre contre le snooping "
                   "même-utilisateur/non-root et les core dumps ; un opérateur root reste capable "
                   "de lire la mémoire (seul Mode B/MPC ou TEE ferme ce trou).")
    return rep


def confinement_attestation(sk=None) -> dict:
    """Attestation logicielle de confinement : mesure le code (manifeste) + l'état de confinement +
    l'égalité d'égress/disque. Sert à PROUVER (de façon best-effort, non matérielle) qu'un client
    standard, non modifié et confiné, tourne. Si `sk` (clé X25519) est fourni, joint la pub pour lier
    l'attestation à l'identité on-chain (enc_pubkey). HONNÊTE : auto-mesure logicielle -> détecte une
    modification du code ou un confinement absent, mais PAS un patch mémoire post-attestation (TOCTOU)
    ni un opérateur root menteur. La racine de confiance matérielle (TEE/TPM) n'est pas implémentée."""
    from . import hardening
    rep = {
        "code_manifest": hardening.build_manifest(),       # sha256 de chaque .py du paquet
        "confinement": apply_process_confinement_report_cached(),
    }
    if sk is not None:
        try:
            from . import crypto
            rep["enc_pubkey"] = crypto.pub_bytes(sk).hex()  # lie l'attestation à l'identité on-chain (MM-02)
        except Exception:
            pass
    return rep


# Cache du rapport (apply ne doit s'exécuter qu'une fois ; l'attestation le relit sans ré-appliquer).
_CACHED_REPORT: dict | None = None


def apply_process_confinement_report_cached() -> dict:
    global _CACHED_REPORT
    if _CACHED_REPORT is None:
        _CACHED_REPORT = apply_process_confinement()
    return _CACHED_REPORT


# =============================================================================
#  ATTESTATION LOGICIELLE MESURÉE + SIGNÉE (B0.5)
#  But : avant d'assigner un job « confidentiel », le relais/la chaîne veut une preuve que le mineur
#  fait tourner un CLIENT CONNU (code mesuré), la BONNE version de modèle et une config attendue, et
#  qu'il a appliqué le confinement. On produit un HASH MESURÉ canonique sur ces éléments, signé par
#  une clé Ed25519 du mineur. Le relais le VÉRIFIE (signature + hash attendu) avant l'assignation.
#
#  ⚠️ HONNÊTE (ne pas sur-vendre) : c'est de la DISSUASION/DÉTECTION logicielle, PAS une garantie
#  cryptographique d'exécution. Sans racine de confiance matérielle (TEE/TPM), un opérateur root peut
#  (a) signer un hash « propre » tout en exécutant autre chose, ou (b) patcher la mémoire APRÈS
#  l'attestation (TOCTOU). Ce que ça apporte réellement : un mineur ne peut pas faire tourner un binaire
#  ARBITRAIRE non reconnu sans le déclarer, l'admin doit MENTIR ACTIVEMENT (clé liée à son identité
#  on-chain -> son stake est en jeu si on prouve la fuite), et une dérive de version/config est
#  détectable. La vraie garantie reste le Mode B (MPC) ou un TEE matériel (datacenter).
# =============================================================================

# Version du schéma d'attestation : à inclure dans le hash mesuré pour qu'un vieux et un nouveau
# format ne produisent jamais collision/confusion.
ATTEST_SCHEMA = "dendra-modea-attest-1"


def measured_hash(*, model_id: str = "", weights_hash: str = "",
                  extra_config: dict | None = None, pkg_dir=None) -> tuple[str, dict]:
    """Calcule le HASH MESURÉ (sha256) liant : schéma + manifeste du code (sha256 de chaque .py) +
    model_id + weights_hash + config supplémentaire + état de confinement. Renvoie (hash_hex, mesure).

    `mesure` est le dict canonique haché (utile pour le debug / la republication on-chain). La
    sérialisation est DÉTERMINISTE (json trié, séparateurs compacts) -> le même client mesuré donne
    toujours le même hash, comparable à une valeur attendue (allow-list de binaires reconnus)."""
    from . import hardening
    measure = {
        "schema": ATTEST_SCHEMA,
        "code_manifest": hardening.build_manifest(pkg_dir),
        "model_id": model_id or "",
        "weights_hash": weights_hash or "",
        "config": dict(sorted((extra_config or {}).items())),
        "confinement": apply_process_confinement_report_cached(),
    }
    blob = json.dumps(measure, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest(), measure


def _ed25519_sk_from_seed(seed32: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(seed32)


def load_or_create_attest_key(keydir: str, miner_id: str) -> tuple[object, str]:
    """Charge (ou crée) la clé Ed25519 D'ATTESTATION du mineur (distincte de la clé X25519 de
    chiffrement et de la clé Cosmos). Persistée en 0600 sous `<keydir>/<miner_id>.attestkey`.
    Renvoie (sk_ed25519, pubkey_hex). En production, cette pub est à ancrer on-chain (à côté de
    enc_pubkey / vrf_pubkey) pour que le relais sache QUI a signé."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    from pathlib import Path
    p = Path(keydir) / f"{miner_id}.attestkey"
    if p.exists():
        seed = bytes.fromhex(p.read_text().strip())
        sk = _ed25519_sk_from_seed(seed)
    else:
        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, seed.hex().encode())
        finally:
            os.close(fd)
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return sk, pub


def signed_attestation(attest_sk, *, miner_id: str, model_id: str = "", weights_hash: str = "",
                       extra_config: dict | None = None, enc_sk=None, pkg_dir=None) -> dict:
    """Produit l'attestation SIGNÉE à envoyer au relais. Contient : miner_id, hash mesuré, la mesure
    détaillée, la pub Ed25519, (option) la pub X25519 de chiffrement, et la SIGNATURE Ed25519 du hash.

    Le relais vérifie ensuite (verify_attestation) : signature valide + hash dans son allow-list de
    binaires reconnus + (option) pub liée à l'identité on-chain. `attest_sk` = clé d'attestation
    (load_or_create_attest_key)."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    h, measure = measured_hash(model_id=model_id, weights_hash=weights_hash,
                               extra_config=extra_config, pkg_dir=pkg_dir)
    pub = attest_sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    sig = attest_sk.sign(bytes.fromhex(h)).hex()
    att = {
        "schema": ATTEST_SCHEMA,
        "miner_id": miner_id,
        "measured_hash": h,
        "measure": measure,
        "attest_pubkey": pub,
        "signature": sig,
    }
    if enc_sk is not None:
        try:
            from . import crypto
            att["enc_pubkey"] = crypto.pub_bytes(enc_sk).hex()   # lie à l'identité de chiffrement (MM-02)
        except Exception:
            pass
    return att


def verify_attestation(att: dict, *, allowed_hashes=None, expected_pubkey: str | None = None,
                       recompute_measure: bool = True) -> tuple[bool, str]:
    """VÉRIFICATION CÔTÉ RELAIS/CHAÎNE d'une attestation signée. Renvoie (ok, raison).

    Contrôles :
      1) la SIGNATURE Ed25519 du `measured_hash` est valide pour `attest_pubkey` ;
      2) si `recompute_measure`, le `measured_hash` correspond bien au sha256 de `measure` joint
         (le mineur ne peut pas annoncer un hash propre avec une mesure sale) ;
      3) si `allowed_hashes` est fourni, le hash mesuré DOIT y figurer (allow-list des binaires/configs
         reconnus -> un client modifié/inconnu est refusé pour les jobs confidentiels) ;
      4) si `expected_pubkey` est fourni, la pub signataire DOIT correspondre (binding à l'identité
         on-chain : on n'accepte que l'attestation signée par CE mineur).

    ⚠️ HONNÊTE : prouve « un client mesuré X, signé par la clé Y, a été présenté », PAS « ce client
    tourne réellement et n'a pas été patché ensuite » (pas de racine de confiance matérielle)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        h = att["measured_hash"]
        pub_hex = att["attest_pubkey"]
        sig = bytes.fromhex(att["signature"])
    except Exception:
        return False, "attestation malformee (champs manquants)"
    if att.get("schema") != ATTEST_SCHEMA:
        return False, f"schema inattendu ({att.get('schema')!r})"
    if expected_pubkey is not None and pub_hex != expected_pubkey:
        return False, "pub signataire != identite attendue"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(sig, bytes.fromhex(h))
    except (InvalidSignature, Exception):
        return False, "signature invalide"
    if recompute_measure and "measure" in att:
        blob = json.dumps(att["measure"], sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(blob).hexdigest() != h:
            return False, "hash mesure != mesure jointe (mesure falsifiee)"
    if allowed_hashes is not None and h not in set(allowed_hashes):
        return False, "hash mesure absent de l'allow-list (client/config non reconnu)"
    return True, "ok"
