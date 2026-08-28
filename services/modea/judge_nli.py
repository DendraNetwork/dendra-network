"""ENTAILMENT stage backed by an NLI classifier — replaces the LLM judge on the same-fact question.

Rotating LLM families plateaus at about two effective votes, because generative models share their
errors. The decorrelated lever is a SIGNAL OF A DIFFERENT KIND: a cross-encoder NLI model.

PRINCIPLE. premise = the REFERENCE answer (the presumed truth, the judge's); hypothesis = the PRIMARY's
answer under review. The model emits three probabilities {contradiction, entailment, neutral}, from
which a monotone factual-consistency SCORE is derived and compared against a CALIBRATED THRESHOLD:
  - a correct PARAPHRASE -> high entailment              -> high score -> ACCEPTED
  - a FALSE FACT         -> contradiction                -> low score  -> REJECTED
  - WORD SALAD           -> neutral (nothing is entailed) -> low score  -> REJECTED

Advantages over an LLM judge: (1) it is decorrelated from the generative models; (2) a 0 % false-accept
rate becomes a MEASURABLE THRESHOLD PROPERTY (see nli_calibrate.py: raise the threshold until false
accepts reach zero over the case set, then read the resulting false-negative rate); (3) at roughly
0.2-0.4 B parameters on CPU it can run on EVERY job, not only on audited ones; (4) it is deterministic.

Default model: `cross-encoder/nli-deberta-v3-base` (Apache-2.0, labels {0: contradiction,
1: entailment, 2: neutral}). Override with DENDRA_NLI_MODEL; if the label order differs, adjust
DENDRA_NLI_*_IDX. Alternatives such as LettuceDetect-large (MIT, RAG grounding) or granite4.1-guardian
(Ollama) return a yes/no rather than a continuous score, which makes them less calibrable by threshold.

Dependencies: pip install sentence-transformers torch (CPU is enough, no GPU required).
The SCORE and the THRESHOLD are the calibrable logic; only loading the model requires torch, which is
why the import is lazy.
"""
from __future__ import annotations

import math
import os

DEFAULT_NLI_MODEL = os.environ.get("DENDRA_NLI_MODEL", "cross-encoder/nli-deberta-v3-base")
ENTAIL_IDX = int(os.environ.get("DENDRA_NLI_ENTAIL_IDX", "1"))   # index of 'entailment' in the output
CONTRA_IDX = int(os.environ.get("DENDRA_NLI_CONTRA_IDX", "0"))   # index of 'contradiction'

_model = None


def _load():
    """Load the NLI cross-encoder lazily: torch/sentence-transformers are required ONLY here."""
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
    """(p_contradiction, p_entailment, p_neutral) — softmax of the cross-encoder logits for (premise -> hypothesis)."""
    logits = list(map(float, _load().predict([(premise, hypothesis)])[0]))
    return _softmax(logits)


def consistency_score(ref, cand):
    """Monotone factual-consistency score of `cand` against `ref` (premise=ref, hypothesis=cand),
    bounded to -1..1: P(entailment) - P(contradiction). High means a correct paraphrase; low means a
    false fact (contradiction) or word salad (neutral). The acceptance THRESHOLD is calibrated by
    nli_calibrate.py so that false accepts reach zero."""
    p = nli_probs(ref, cand)
    return p[ENTAIL_IDX] - p[CONTRA_IDX]


def entailment_judge(ref, cand, threshold=None):
    """Binary verdict of the entailment stage: True (valid) when consistency >= threshold, else False.
    The threshold comes from calibration (DENDRA_NLI_THRESHOLD, default 0.0). Deterministic.
    Intended to replace `modea.judge.llm_judge` as the same-fact stage once the threshold is
    calibrated."""
    if threshold is None:
        threshold = float(os.environ.get("DENDRA_NLI_THRESHOLD", "0.0"))
    return consistency_score(ref, cand) >= threshold


if __name__ == "__main__":
    # Smoke test; requires the model and torch to be installed.
    import sys
    if "--smoke" in sys.argv:
        for ref, cand in [("The sky is blue.", "The sky has a blue colour."),
                          ("The sky is blue.", "The sky is green."),
                          ("The sky is blue.", "sky answer blue sentence the colour is.")]:
            print(f"  score({cand[:34]!r:36}) = {consistency_score(ref, cand):+.3f}")
