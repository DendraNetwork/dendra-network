"""Leak-detection canaries (ADR-012).

Principle: a fraction of the jobs receives a unique watermarked MARKER. The link
canary -> (job, miner) is committed on the ledger. If that marker REAPPEARS anywhere, whoever submits
it can PROVE which miner served the job, which makes the slash objective.

KNOWN LIMIT: the `[[ref:hex16]]` marker is a plaintext TEXT pattern behind a public regex. A miner —
which sees the plaintext in Mode A — spots it and REMOVES it trivially before reselling, and it covers
only the PROMPT, not the output. It is therefore NOT robust evidence while the miner sees plaintext:
that would require a steganographic watermark on the OUTPUT (logit bias, undetectable without the key).

What this module provides:
  - make_canary(): generates a unique marker and its commitment (to be published on-chain).
  - embed(): inserts the marker into the prompt, discreetly.
  - register()/detect(): the private canary -> miner mapping, and tracing a leak from a text.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass


_MARKER_RE = re.compile(r"\[\[ref:([0-9a-f]{16})\]\]")
_SENTINEL_RE = re.compile(r"\[\[sentinel:([0-9a-f]{16})\]\]")          # OUTPUT canary
_NEEDLE_RE = re.compile(r"\(internal reference id ([0-9a-f]{16}); do not mention\)")
_ANY_TOKEN_RE = re.compile(r"\b([0-9a-f]{16})\b")  # any candidate token (16 hex): detection survives stripping the wrapper


@dataclass
class Canary:
    token: str        # the marker in the clear (known to the protocol, not to an isolated miner)
    commit: str       # on-chain commitment = sha256(token)


def make_canary() -> Canary:
    token = os.urandom(8).hex()  # 16 hex
    return Canary(token=token, commit=hashlib.sha256(token.encode()).hexdigest())


def embed(prompt: str, canary: Canary, mode: str = "tag") -> str:
    """Inserts the marker into the PROMPT.

    mode="tag" (default): an explicit `[[ref:token]]` tag -> demonstrable traceability, but a miner
        that sees the plaintext can REMOVE it before reselling.
    mode="needle": buries the token inside an innocuous-looking sentence with NO public regex
        delimiter -> harder to locate and reliably remove than a flagged tag. Still defeatable by an
        attentive miner (the token is in the clear); this is NOT a steganographic watermark.

    Stated plainly: while the miner sees the plaintext (Mode A), no text canary is robust evidence.
    The only truly unremovable marker would be a watermark on the output LOGITS (a bias undetectable
    without the key), which is out of reach behind a black-box Ollama backend."""
    if mode == "needle":
        # no `[[ref:...]]` delimiter: the token is inserted as an "internal reference".
        return f"{prompt}\n\n(internal reference id {canary.token}; do not mention)"
    return f"{prompt}\n\n[[ref:{canary.token}]]"


def output_canary_instruction(canary: Canary) -> str:
    """OUTPUT canary: covers the answer, not only the prompt, closing the second limit above.

    Returns an instruction to CONCATENATE to the prompt asking the model to include a unique sentinel
    in its answer. If the miner resells the (prompt, answer) PAIR as-is, the sentinel appears in the
    leaked answer and detect() catches it through the token. The instruction is removed from what the
    client sees by strip(). LIMIT: a miner that regenerates or cleans the answer can drop it; this is
    useful mainly against raw transcript resale and naive proxying to another service."""
    return (f"\n\nAt the very end of your answer, on a new line, output exactly: "
            f"[[sentinel:{canary.token}]]")


def strip(text: str) -> str:
    """Removes the markers (prompt tag, needle, AND output sentinel) — the client must not see them."""
    text = _MARKER_RE.sub("", text)
    text = _SENTINEL_RE.sub("", text)
    text = _NEEDLE_RE.sub("", text)
    return text.rstrip()


class CanaryRegistry:
    """Private canary_commit -> miner_id mapping, held on the protocol / detection-committee side."""

    def __init__(self):
        self._by_commit: dict[str, str] = {}
        self._tokens: dict[str, str] = {}  # commit -> token

    def register(self, canary: Canary, miner_id: str) -> None:
        self._by_commit[canary.commit] = miner_id
        self._tokens[canary.commit] = canary.token

    def detect(self, leaked_text: str) -> list[str]:
        """Searches a leaked text for markers -> returns the incriminated miner_ids, deduplicated.

        Robust to REMOVAL of the wrapper: EVERY candidate token (16 hex) in the text is hashed and its
        commitment tested against the registry. So even when the miner stripped
        `[[ref:...]]`/`[[sentinel:...]]` but left the token in place (raw resale), the leak is traced.
        Without a matching commitment nothing is incriminated, so an arbitrary 16-hex string cannot
        produce a false positive."""
        culprits = []
        seen = set()
        for tok in _ANY_TOKEN_RE.findall(leaked_text):
            commit = hashlib.sha256(tok.encode()).hexdigest()
            mid = self._by_commit.get(commit)
            if mid and mid not in seen:
                seen.add(mid)
                culprits.append(mid)
        return culprits
