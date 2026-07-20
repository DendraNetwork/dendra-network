"""Étage COHÉRENCE par PSEUDO-PERPLEXITÉ MLM (PLL) — anti-SALADE déterministe v2 (méthodo internal audit 2026-06-22).

Remplace la perplexité AUTORÉGRESSIVE (`coherence_ppl`, plafonnait ~87 % : un LM gauche→droite PARDONNE le
désordre une fois les mots-clés posés) par le scoring MLM MASQUÉ (Salazar et al. 2020, PLL) : on masque CHAQUE
token et on score sa log-prob conditionnée sur TOUT le reste (bidirectionnel) → sensible à l'ORDRE des mots
(+10 pts mesuré sur l'acceptabilité). Une salade fait CHUTER la PLL bien plus qu'un faux-fait fluide.
Déterministe, CPU. Interface IDENTIQUE à `coherence_ppl` (`perplexity`/`coherent`) = drop-in via DENDRA_PPL_BACKEND=pll.

Modèle par défaut : `camembert-base` (FR, MLM RoBERTa, MIT). Surcharge DENDRA_PLL_MODEL (ex. `xlm-roberta-base`
multilingue). NE PAS prendre un DeBERTa-v3 (pré-entraîné RTD, pas MLM → pas de têtes de prédiction de token).
Dépendances : pip install transformers torch   (CPU OK).
"""
from __future__ import annotations

import math
import os

DEFAULT_PLL_MODEL = os.environ.get("DENDRA_PLL_MODEL", "camembert-base")
_tok = None
_model = None


def _load():
    global _tok, _model
    if _model is None:
        from transformers import AutoModelForMaskedLM, AutoTokenizer   # lazy : torch requis ici
        _tok = AutoTokenizer.from_pretrained(DEFAULT_PLL_MODEL)
        _model = AutoModelForMaskedLM.from_pretrained(DEFAULT_PLL_MODEL)
        _model.eval()
    return _tok, _model


def pseudo_perplexity(text):
    """Pseudo-perplexité MLM = exp(-PLL/N), PLL = somme des log-probs des tokens masqués un par un (bidirectionnel).
    HAUTE = salade/désordre, BASSE = bien formé. Tronqué à DENDRA_PLL_MAX_TOK tokens (borne le coût CPU)."""
    import torch
    tok, model = _load()
    max_tok = int(os.environ.get("DENDRA_PLL_MAX_TOK", "128"))
    enc = tok(text or " ", return_tensors="pt", truncation=True, max_length=max_tok)
    ids = enc["input_ids"][0]
    n = int(ids.shape[0])
    special = set(tok.all_special_ids)
    positions = [i for i in range(n) if int(ids[i]) not in special]
    # GARDE DE LONGUEUR : une réponse de < min_tok tokens-contenu (un mot, un nombre, une date) ne peut PAS
    # être une SALADE (il faut plusieurs mots à mélanger) -> COHÉRENTE d'office. Évite le FN-true des réponses
    # terses (« Paris. », « 42 ») qui ont une pseudo-ppl énorme et plombaient la calibration.
    if len(positions) < int(os.environ.get("DENDRA_COH_MIN_TOK", "4")) or tok.mask_token_id is None:
        return 1.0
    batch = ids.unsqueeze(0).repeat(len(positions), 1)
    for i, p in enumerate(positions):
        batch[i, p] = tok.mask_token_id
    attn = enc.get("attention_mask")
    attn = attn.repeat(len(positions), 1) if attn is not None else None
    with torch.no_grad():
        logits = model(batch, attention_mask=attn).logits
    total = 0.0
    for i, p in enumerate(positions):
        total += float(torch.log_softmax(logits[i, p], dim=-1)[ids[p]])
    pll = total / len(positions)                       # log-prob moyenne par token
    return float(math.exp(min(-pll, 20.0)))            # pseudo-perplexité (bornée anti-overflow)


# alias drop-in : `coherence_ppl.perplexity` <-> `coherence_pll.perplexity`
def perplexity(text):
    return pseudo_perplexity(text)


def coherent(text, threshold=None):
    """True = bien formé (pseudo-ppl <= seuil), False = SALADE (pseudo-ppl > seuil). Déterministe.
    Seuil = DENDRA_PPL_THRESHOLD (calibré par ppl_calibrate.py avec DENDRA_PPL_BACKEND=pll)."""
    if threshold is None:
        threshold = float(os.environ.get("DENDRA_PPL_THRESHOLD", "inf"))
    return pseudo_perplexity(text) <= threshold


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:   # exige transformers+torch+le modèle
        for t in ["Paris est la capitale de la France.", "Paris.", "42",
                  "France capitale la phrase Paris une est réponds.", "bleu ciel le couleur est réponse une."]:
            print(f"  pseudo-ppl({t[:38]!r:40}) = {pseudo_perplexity(t):10.1f}")
