"""Étage COHÉRENCE par PERPLEXITÉ — anti-SALADE déterministe (pivot internal audit #4, 2026-06-22).

Une SALADE de mots-clés en désordre a une PERPLEXITÉ ÉLEVÉE sous un petit modèle de langue ; une phrase
bien formée (même courte) a une perplexité BASSE. Verdict : coherent si perplexité <= SEUIL (calibré).
Zéro appel LLM génératif, DÉTERMINISTE, CPU. Complète `judge_nli` (anti-faux) pour un juge 100 % déterministe :
  cohérence (perplexité, ICI)  ->  même-fait (NLI, judge_nli)  ->  verdict.

Modèle par défaut : `Qwen/Qwen2.5-0.5B` (Apache-2.0, multilingue dont FR, 0,5 B). Surcharge DENDRA_PPL_MODEL.
Dépendances : pip install transformers torch   (CPU OK). cf. ppl_calibrate.py pour le seuil.
"""
from __future__ import annotations

import math
import os

DEFAULT_PPL_MODEL = os.environ.get("DENDRA_PPL_MODEL", "Qwen/Qwen2.5-0.5B")
_tok = None
_model = None


def _load():
    global _tok, _model
    if _model is None:
        from transformers import AutoModelForCausalLM, AutoTokenizer   # lazy : torch requis ici
        _tok = AutoTokenizer.from_pretrained(DEFAULT_PPL_MODEL)
        _model = AutoModelForCausalLM.from_pretrained(DEFAULT_PPL_MODEL)
        _model.eval()
    return _tok, _model


def perplexity(text):
    """Perplexité du texte sous le petit LM = exp(cross-entropy moyenne). Haute = désordre/salade.
    Un texte < 2 tokens (un mot, un nombre) -> 1.0 (trop court pour une perplexité fiable = traité cohérent)."""
    import torch
    tok, model = _load()
    ids = tok(text or " ", return_tensors="pt").input_ids
    # GARDE DE LONGUEUR : < min_tok tokens (un mot/nombre/date) -> ne peut pas être une salade -> cohérent d'office.
    if ids.shape[1] < int(os.environ.get("DENDRA_COH_MIN_TOK", "4")):
        return 1.0
    with torch.no_grad():
        loss = model(ids, labels=ids).loss
    return float(math.exp(min(float(loss), 20.0)))   # borne anti-overflow


def coherent(text, threshold=None):
    """True = bien formé (perplexité <= seuil), False = SALADE (perplexité > seuil). None jamais (déterministe).
    Seuil = DENDRA_PPL_THRESHOLD (calibré par ppl_calibrate.py ; défaut +inf = tout cohérent si non réglé).
    DESTINÉ à remplacer `modea.judge.llm_coherent` (étage cohérence) quand DENDRA_JUDGE_BACKEND=nli."""
    if threshold is None:
        threshold = float(os.environ.get("DENDRA_PPL_THRESHOLD", "inf"))
    return perplexity(text) <= threshold


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:   # exige transformers+torch+le modèle
        for t in ["Paris est la capitale de la France.", "Paris.", "42",
                  "France capitale la phrase Paris une est réponds.", "bleu ciel le couleur est réponse une."]:
            print(f"  perplexité({t[:38]!r:40}) = {perplexity(t):8.1f}")
