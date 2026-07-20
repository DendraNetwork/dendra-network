"""Étage ENTAILMENT par classifieur NLI — remplace le LLM-juge (et le cosinus mort) sur le même-fait.
Pivot internal audit 2026-06-22 (internal-notes) : la rotation de familles LLM plafonne (n_eff ~2 votes) ;
le vrai levier décorrélé = un SIGNAL D'AUTRE TYPE = un cross-encoder NLI.

PRINCIPE. premise = réponse de RÉFÉRENCE (la vérité présumée, celle du juge) ; hypothesis = réponse du
PRIMAIRE à juger. Le modèle sort 3 probas {contradiction, entailment, neutral}. On en tire un SCORE
monotone de cohérence factuelle, comparé à un SEUIL CALIBRÉ :
  - PARAPHRASE correcte  -> entailment haut          -> score haut  -> ACCEPTÉ
  - FAUX-FAIT            -> contradiction             -> score bas   -> REJETÉ
  - SALADE de mots       -> neutre (rien n'entaille)  -> score bas   -> REJETÉ

AVANTAGES (vs LLM-juge) : (1) décorrélé des génératifs ; (2) faux-accept 0 % devient une PROPRIÉTÉ DE
SEUIL MESURABLE (cf. nli_calibrate.py : on monte le seuil jusqu'à faux=0 sur les 294 cas, on lit le
FN-true) ; (3) ~0,2-0,4 B sur CPU -> activable sur CHAQUE job, pas seulement l'audit ; (4) déterministe.

MODÈLE par défaut : `cross-encoder/nli-deberta-v3-base` (Apache-2.0, labels {0:contradiction,1:entailment,
2:neutral}). Surcharge : DENDRA_NLI_MODEL ; si l'ordre des labels diffère, ajuste DENDRA_NLI_*_IDX.
ALTERNATIVES (internal audit) : LettuceDetect-large (MIT, grounding RAG) ou granite4.1-guardian (Ollama, binaire) —
ces dernières donnent un OUI/NON (pas un score continu) -> moins calibrables par seuil que le NLI.

DÉPENDANCES : pip install sentence-transformers torch   (CPU OK, pas de GPU requis).
Le SCORE et le SEUIL sont la logique calibrable ; seul le chargement du modèle exige torch (import paresseux).
"""
from __future__ import annotations

import math
import os

DEFAULT_NLI_MODEL = os.environ.get("DENDRA_NLI_MODEL", "cross-encoder/nli-deberta-v3-base")
ENTAIL_IDX = int(os.environ.get("DENDRA_NLI_ENTAIL_IDX", "1"))   # index de 'entailment' dans la sortie
CONTRA_IDX = int(os.environ.get("DENDRA_NLI_CONTRA_IDX", "0"))   # index de 'contradiction'

_model = None


def _load():
    """Charge le cross-encoder NLI (paresseux : torch/sentence-transformers requis SEULEMENT ici)."""
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder
        _model = CrossEncoder(DEFAULT_NLI_MODEL)
    return _model


def _softmax(xs):
    m = max(xs)
    es = [math.exp(x - m) for x in xs]
    s = sum(es) or 1.0
    return [e / s for e in es]


def nli_probs(premise, hypothesis):
    """(p_contradiction, p_entailment, p_neutral) — softmax des logits du cross-encoder pour (premise->hyp)."""
    logits = list(map(float, _load().predict([(premise, hypothesis)])[0]))
    return _softmax(logits)


def consistency_score(ref, cand):
    """Score monotone de cohérence factuelle de `cand` vs `ref` (premise=ref, hyp=cand), borné -1..1 :
    = P(entailment) - P(contradiction). Haut = paraphrase correcte ; bas = faux-fait (contra) ou salade
    (neutre). Le SEUIL d'acceptation se calibre (nli_calibrate.py) pour faux-accept = 0."""
    p = nli_probs(ref, cand)
    return p[ENTAIL_IDX] - p[CONTRA_IDX]


def entailment_judge(ref, cand, threshold=None):
    """Verdict binaire de l'étage entailment : True (valide) si cohérence >= seuil, sinon False.
    Le seuil vient de la calibration (DENDRA_NLI_THRESHOLD, défaut 0.0). Déterministe.
    DESTINÉ à remplacer `modea.judge.llm_judge` comme étage MÊME-FAIT une fois le seuil calibré."""
    if threshold is None:
        threshold = float(os.environ.get("DENDRA_NLI_THRESHOLD", "0.0"))
    return consistency_score(ref, cand) >= threshold


if __name__ == "__main__":
    # petit smoke-test (exige le modèle + torch installés)
    import sys
    if "--smoke" in sys.argv:
        for ref, cand in [("Le ciel est bleu.", "Le ciel a une couleur bleue."),
                          ("Le ciel est bleu.", "Le ciel est vert."),
                          ("Le ciel est bleu.", "ciel réponse bleu phrase le couleur est.")]:
            print(f"  score({cand[:34]!r:36}) = {consistency_score(ref, cand):+.3f}")
