"""COHERENCE stage scored by MLM pseudo-perplexity (PLL) — the deterministic anti-word-salad filter.

This replaces AUTOREGRESSIVE perplexity (`coherence_ppl`, which plateaus around 87 %: a left-to-right LM
FORGIVES disorder once the keywords are in place) with MASKED MLM scoring (Salazar et al. 2020, PLL):
every token is masked in turn and its log-probability is scored conditioned on ALL the rest
(bidirectional), which makes the score sensitive to word ORDER — worth about 10 points of acceptability
in measurement. A word salad drives the PLL down far more than a fluent-but-false answer does.
Deterministic, CPU-only. The interface is IDENTICAL to `coherence_ppl` (`perplexity`/`coherent`), so it is
a drop-in replacement selected by DENDRA_PPL_BACKEND=pll.

Default model: `camembert-base` (French, RoBERTa MLM, MIT). Override with DENDRA_PLL_MODEL (for example
the multilingual `xlm-roberta-base`). Do NOT substitute a DeBERTa-v3: it is pre-trained with replaced-token
detection rather than MLM, so it has no token-prediction head.
Dependencies: pip install transformers torch   (CPU is sufficient).
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
        from transformers import AutoModelForMaskedLM, AutoTokenizer   # imported lazily: torch is needed here
        _tok = AutoTokenizer.from_pretrained(DEFAULT_PLL_MODEL)
        _model = AutoModelForMaskedLM.from_pretrained(DEFAULT_PLL_MODEL)
        _model.eval()
    return _tok, _model


def pseudo_perplexity(text):
    """MLM pseudo-perplexity = exp(-PLL/N), where PLL sums the log-probabilities of each token masked in
    turn (bidirectional). HIGH means word salad or disorder, LOW means well formed. Truncated at
    DENDRA_PLL_MAX_TOK tokens, which bounds the CPU cost."""
    import torch
    tok, model = _load()
    max_tok = int(os.environ.get("DENDRA_PLL_MAX_TOK", "128"))
    enc = tok(text or " ", return_tensors="pt", truncation=True, max_length=max_tok)
    ids = enc["input_ids"][0]
    n = int(ids.shape[0])
    special = set(tok.all_special_ids)
    positions = [i for i in range(n) if int(ids[i]) not in special]
    # LENGTH GUARD: an answer with fewer than min_tok content tokens (a single word, a number, a date)
    # CANNOT be a word salad — shuffling requires several words — so it is coherent by construction.
    # Without this, terse but correct answers score an enormous pseudo-perplexity and are rejected as
    # false negatives.
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
    pll = total / len(positions)                       # mean log-probability per token
    return float(math.exp(min(-pll, 20.0)))            # pseudo-perplexity, clamped against overflow


# Drop-in alias: `coherence_ppl.perplexity` <-> `coherence_pll.perplexity`
def perplexity(text):
    return pseudo_perplexity(text)


def coherent(text, threshold=None):
    """True means well formed (pseudo-perplexity <= threshold), False means word salad. Deterministic.
    The threshold comes from DENDRA_PPL_THRESHOLD, calibrated by ppl_calibrate.py with
    DENDRA_PPL_BACKEND=pll."""
    if threshold is None:
        threshold = float(os.environ.get("DENDRA_PPL_THRESHOLD", "inf"))
    return pseudo_perplexity(text) <= threshold


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:   # requires transformers, torch and the model to be present
        # The samples stay in FRENCH: the default scorer is `camembert-base`, a FRENCH MLM, and they are
        # the very sentences the coherence stage was calibrated on (modea/judge.py COHERENCE_PROMPT).
        # Scoring English text with a French MLM would return a pseudo-perplexity that means nothing here.
        for t in ["Paris est la capitale de la France.", "Paris.", "42",
                  "France capitale la phrase Paris une est réponds.", "bleu ciel le couleur est réponse une."]:
            print(f"  pseudo-ppl({t[:38]!r:40}) = {pseudo_perplexity(t):10.1f}")
