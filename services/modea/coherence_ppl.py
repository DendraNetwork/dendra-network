"""COHERENCE stage by PERPLEXITY — deterministic word-salad detection.

A disordered keyword salad has a HIGH PERPLEXITY under a small language model; a well-formed sentence
(even a short one) has a LOW perplexity. Verdict: coherent when perplexity <= THRESHOLD (calibrated).
No generative LLM call, DETERMINISTIC, CPU-only. It complements `judge_nli` (which detects falsehood)
to make a fully deterministic judge:
  coherence (perplexity, HERE)  ->  same-fact (NLI, judge_nli)  ->  verdict.

Default model: `Qwen/Qwen2.5-0.5B` (Apache-2.0, multilingual, 0.5B). Override with DENDRA_PPL_MODEL.
Dependencies: pip install transformers torch   (CPU is fine). See ppl_calibrate.py for the threshold.
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
        from transformers import AutoModelForCausalLM, AutoTokenizer   # lazy: torch is required here
        _tok = AutoTokenizer.from_pretrained(DEFAULT_PPL_MODEL)
        _model = AutoModelForCausalLM.from_pretrained(DEFAULT_PPL_MODEL)
        _model.eval()
    return _tok, _model


def perplexity(text):
    """Perplexity of the text under the small LM = exp(mean cross-entropy). High = disorder/salad.
    A text shorter than the minimum token count (a word, a number) returns 1.0: too short for a
    reliable perplexity, so it is treated as coherent."""
    import torch
    tok, model = _load()
    ids = tok(text or " ", return_tensors="pt").input_ids
    # LENGTH GUARD: fewer than min_tok tokens (a word/number/date) cannot be a salad -> coherent outright.
    if ids.shape[1] < int(os.environ.get("DENDRA_COH_MIN_TOK", "4")):
        return 1.0
    with torch.no_grad():
        loss = model(ids, labels=ids).loss
    return float(math.exp(min(float(loss), 20.0)))   # anti-overflow bound


def coherent(text, threshold=None):
    """True = well formed (perplexity <= threshold), False = SALAD (perplexity > threshold). Never
    None: this stage is deterministic. Threshold = DENDRA_PPL_THRESHOLD (calibrated by
    ppl_calibrate.py; default +inf, i.e. everything coherent while unset). INTENDED to replace
    `modea.judge.llm_coherent` (the coherence stage) when DENDRA_JUDGE_BACKEND=nli."""
    if threshold is None:
        threshold = float(os.environ.get("DENDRA_PPL_THRESHOLD", "inf"))
    return perplexity(text) <= threshold


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:   # requires transformers + torch + the model
        for t in ["Paris is the capital of France.", "Paris.", "42",
                  "France capital the sentence Paris a is answer.", "blue sky the colour is answer a."]:
            print(f"  perplexity({t[:38]!r:40}) = {perplexity(t):8.1f}")
