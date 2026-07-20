"""Canaries de detection de fuite (ADR-012 / PUNITION-FUITES).

Principe : une fraction des jobs recoit un MARQUEUR unique watermarke. Le lien
canary -> (job, mineur) est engage sur le ledger. Si ce marqueur **reapparait** quelque part,
quiconque le soumet peut PROUVER quel mineur a servi le job -> slashing objectif.

⚠️ AUDIT CR-06 : le marqueur actuel `[[ref:hex16]]` est un motif TEXTE en clair (regex publique) -> un
mineur (qui voit le clair en Mode A) le repere et le RETIRE trivialement avant revente, et il ne couvre
que le PROMPT (pas la sortie). Ce n'est donc PAS une preuve robuste tant que le mineur voit le clair :
il faudrait un watermark stEganographique sur la SORTIE (biais de logits, indetectable sans cle).

Ici on fournit :
  - make_canary() : genere un marqueur unique + son engagement (a publier on-chain).
  - embed() : insere le marqueur dans le prompt (de facon discrete).
  - register()/detect() : mapping prive canary->mineur + trace d'une fuite a partir d'un texte.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass


_MARKER_RE = re.compile(r"\[\[ref:([0-9a-f]{16})\]\]")
_SENTINEL_RE = re.compile(r"\[\[sentinel:([0-9a-f]{16})\]\]")          # canary de SORTIE
_NEEDLE_RE = re.compile(r"\(internal reference id ([0-9a-f]{16}); do not mention\)")
_ANY_TOKEN_RE = re.compile(r"\b([0-9a-f]{16})\b")  # tout token-candidat (16 hex) -> detection robuste au strip du wrapper


@dataclass
class Canary:
    token: str        # le marqueur en clair (connu du protocole, pas du mineur isole)
    commit: str       # engagement on-chain = sha256(token)


def make_canary() -> Canary:
    token = os.urandom(8).hex()  # 16 hex
    return Canary(token=token, commit=hashlib.sha256(token.encode()).hexdigest())


def embed(prompt: str, canary: Canary, mode: str = "tag") -> str:
    """Insere le marqueur dans le PROMPT.

    mode="tag" (defaut, retro-compat) : tag explicite `[[ref:token]]` -> tracabilite demontrable,
        mais le mineur qui voit le clair peut le RETIRER avant revente (limite CR-06).
    mode="needle" : noie le token dans une phrase d'apparence anodine SANS delimiteur regex public ->
        plus difficile a localiser/retirer a coup sur qu'un tag balise. Reste contournable par un
        mineur attentif (le token est dans le clair) ; ce n'est PAS un watermark stEganographique.

    HONNETE : tant que le mineur voit le clair (Mode A), aucun canary-texte n'est une preuve robuste.
    Le seul marqueur reellement non-retirable serait un watermark sur les LOGITS de sortie (biais
    indetectable sans cle) -> hors de portee d'un backend Ollama boite-noire ici."""
    if mode == "needle":
        # pas de delimiteur `[[ref:...]]` : le token est inseré comme une "reference interne".
        return f"{prompt}\n\n(internal reference id {canary.token}; do not mention)"
    return f"{prompt}\n\n[[ref:{canary.token}]]"


def output_canary_instruction(canary: Canary) -> str:
    """Canary de SORTIE (couvre la reponse, pas seulement le prompt — comble la 2e limite CR-06).

    Renvoie une consigne a CONCATENER au prompt demandant au modele d'inclure un sentinelle unique
    dans sa reponse. Si le mineur revend la PAIRE (prompt,reponse) telle quelle, le sentinelle apparait
    dans la reponse fuite -> detect() l'attrape via le token. La consigne est retiree de l'affichage
    client par strip(). LIMITE : un mineur qui regenere/nettoie la reponse peut l'oter ; utile surtout
    contre la revente brute de transcripts et le proxying naif vers un autre service."""
    return (f"\n\nAt the very end of your answer, on a new line, output exactly: "
            f"[[sentinel:{canary.token}]]")


def strip(text: str) -> str:
    """Retire les marqueurs (prompt-tag, needle, ET sentinelle de sortie) — le client n'a pas a les voir."""
    text = _MARKER_RE.sub("", text)
    text = _SENTINEL_RE.sub("", text)
    text = _NEEDLE_RE.sub("", text)
    return text.rstrip()


class CanaryRegistry:
    """Mapping prive canary_commit -> miner_id (cote protocole/comite de detection)."""

    def __init__(self):
        self._by_commit: dict[str, str] = {}
        self._tokens: dict[str, str] = {}  # commit -> token

    def register(self, canary: Canary, miner_id: str) -> None:
        self._by_commit[canary.commit] = miner_id
        self._tokens[canary.commit] = canary.token

    def detect(self, leaked_text: str) -> list[str]:
        """Cherche des marqueurs dans un texte fuite -> renvoie les miner_id incrimines (dedupliqués).

        Robuste au RETRAIT du wrapper : on hashe TOUT token-candidat (16 hex) du texte et on teste son
        commit contre le registre. Ainsi, même si le mineur a enlevé `[[ref:...]]`/`[[sentinel:...]]`
        mais laissé le token (revente brute), la fuite est tracée. Sans correspondance de commit, rien
        n'est incriminé (pas de faux positif sur un hex16 quelconque)."""
        culprits = []
        seen = set()
        for tok in _ANY_TOKEN_RE.findall(leaked_text):
            commit = hashlib.sha256(tok.encode()).hexdigest()
            mid = self._by_commit.get(commit)
            if mid and mid not in seen:
                seen.add(mid)
                culprits.append(mid)
        return culprits
