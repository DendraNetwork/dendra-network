"""Backends d'inference pour le mineur.

- OllamaBackend : appelle l'Ollama local (reutilise le modele deja installe).
- MockBackend   : reponse deterministe sans GPU (pour tests/sandbox).

REGLE : aucun backend ne **journalise le contenu**. Les prompts/sorties ne transitent que par
la memoire du processus. (Le durcissement OS — mlock, sandbox, no-egress — est decrit dans
MODE-A-SECURITE et s'ajoute au client de production.)
"""
from __future__ import annotations

import hashlib
import os

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


class MockBackend:
    name = "mock"

    def generate(self, prompt: str, max_out: int = 0, temperature=None) -> str:
        # `temperature` accepté (Couche A, juge) mais IGNORÉ : le mock reste déterministe.
        # reponse deterministe, sans rien logguer
        h = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        out = f"[mock:{h}] reponse simulee a la requete (longueur {len(prompt)})."
        self.in_tok = max(1, len(prompt) // 4)   # estimation (pas de vrai modele en mock)
        self.out_tok = max(1, len(out) // 4)
        return out


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str = "", endpoint: str = ""):
        # configurables par env (utile WSL -> Ollama Windows) : OLLAMA_ENDPOINT / OLLAMA_MODEL
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")
        self.endpoint = endpoint or os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434")

    def generate(self, prompt: str, max_out: int = 0, temperature=None) -> str:
        if requests is None:
            raise RuntimeError("module requests requis pour OllamaBackend")
        # plafond de sortie : demande par le client (max_out) sinon defaut ; borne dure anti-abus
        np = max_out or int(os.environ.get("OLLAMA_NUM_PREDICT", "2048"))
        np = min(max(64, np), int(os.environ.get("OLLAMA_NUM_PREDICT_MAX", "8192")))
        # SERVICE (défaut, temperature=None) : décodage DÉTERMINISTE (temp 0, top_k 1, seed 0) — S2 exige la
        # reproductibilité du travail servi. ÉCHANTILLONNAGE (Couche A, juge : temperature>0) : temp posée ET
        # top_k/seed RETIRÉS (les garder rendrait les K tirages identiques → l'ambiguïté ne surfacerait jamais).
        if temperature is None:
            opts = {"temperature": 0.0, "top_k": 1, "seed": 0, "num_predict": np}
        else:
            opts = {"temperature": float(temperature), "num_predict": np}
        payload = {
            "model": self.model, "prompt": prompt, "stream": False,
            "options": opts,
        }
        # OLLAMA_TIMEOUT (2026-07-03, fiabilité banc) : 600 s fixe = un juge dont l'Ollama CPU sature reste
        # bloqué 10 min PAR génération (cascade : quorum raté -> no-quorum). Configurable, défaut INCHANGÉ ;
        # le kit de banc pose ~240 s pour les juges (mieux vaut un retry/abstention qu'un blocage).
        r = requests.post(f"{self.endpoint}/api/generate", json=payload,
                          timeout=int(os.environ.get("OLLAMA_TIMEOUT", "600")))
        r.raise_for_status()
        j = r.json()
        # VRAIS comptes de tokens du modele (tarification exacte au token)
        self.in_tok = int(j.get("prompt_eval_count", 0)) or max(1, len(prompt) // 4)
        self.out_tok = int(j.get("eval_count", 0)) or max(1, len(j.get("response", "")) // 4)
        return j.get("response", "")

    def embed(self, text: str):
        """Embedding via Ollama /api/embeddings (modele dedie DENDRA_EMBED_API_MODEL). None -> repli local."""
        if requests is None:
            return None
        model = os.environ.get("DENDRA_EMBED_API_MODEL", "")
        if not model:
            return None
        try:
            r = requests.post(f"{self.endpoint}/api/embeddings",
                              json={"model": model, "prompt": text}, timeout=120)
            r.raise_for_status()
            return r.json().get("embedding")
        except Exception:
            return None


class OpenAIBackend:
    """Backend OpenAI-compatible (INT-7) : couvre LocalAI, vLLM, llama.cpp (llama-server), LM Studio, TGI...
    Unifie chat (/v1/chat/completions) ET embeddings (/v1/embeddings) derriere UN endpoint (cf. LocalAI),
    ce qui simplifie le mineur. Backend ENFICHABLE : on ne depend plus d'un seul moteur. Rien n'est journalise."""
    name = "openai"

    def __init__(self, model: str = "", endpoint: str = "", embed_model: str = ""):
        self.endpoint = (endpoint or os.environ.get("DENDRA_INFER_URL")
                         or os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
        self.model = (model or os.environ.get("DENDRA_INFER_MODEL")
                      or os.environ.get("OPENAI_MODEL", "llama3.1:8b-instruct-q4_K_M"))
        self.embed_model = embed_model or os.environ.get("DENDRA_EMBED_API_MODEL", "")
        self.api_key = os.environ.get("OPENAI_API_KEY", "")  # souvent vide en local

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def generate(self, prompt: str, max_out: int = 0, temperature=None) -> str:
        if requests is None:
            raise RuntimeError("module requests requis pour OpenAIBackend")
        np = max_out or int(os.environ.get("OLLAMA_NUM_PREDICT", "2048"))
        np = min(max(64, np), int(os.environ.get("OLLAMA_NUM_PREDICT_MAX", "8192")))
        # Même règle que OllamaBackend : défaut = déterministe (service S2) ; temperature>0 = échantillonnage
        # (Couche A) sans seed fixe (sinon les K tirages seraient identiques).
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "top_p": 1.0, "max_tokens": np, "stream": False,
        }
        if temperature is None:
            payload["temperature"] = 0.0
            payload["seed"] = 0
        else:
            payload["temperature"] = float(temperature)
        r = requests.post(f"{self.endpoint}/chat/completions", json=payload,
                          headers=self._headers(), timeout=600)
        r.raise_for_status()
        j = r.json()
        usage = j.get("usage", {}) or {}
        choices = j.get("choices") or [{}]
        txt = ((choices[0].get("message") or {}).get("content", "")) or ""
        self.in_tok = int(usage.get("prompt_tokens", 0)) or max(1, len(prompt) // 4)
        self.out_tok = int(usage.get("completion_tokens", 0)) or max(1, len(txt) // 4)
        return txt

    def embed(self, text: str):
        """Embedding via le MEME endpoint (/v1/embeddings) -> chat+embeddings unifies. None -> repli local."""
        if requests is None or not self.embed_model:
            return None
        try:
            r = requests.post(f"{self.endpoint}/embeddings",
                              json={"model": self.embed_model, "input": text},
                              headers=self._headers(), timeout=120)
            r.raise_for_status()
            data = r.json().get("data") or [{}]
            return data[0].get("embedding")
        except Exception:
            return None


# Backends OpenAI-compatibles (meme protocole ; endpoints/ports differents selon le moteur).
_OPENAI_ALIASES = {"openai", "localai", "vllm", "llamacpp", "llama.cpp", "lmstudio", "tgi", "sglang"}


def get_backend(name: str = ""):
    """Fabrique de backend ENFICHABLE (INT-7). name vide -> env DENDRA_INFER_BACKEND (defaut 'ollama')."""
    name = (name or os.environ.get("DENDRA_INFER_BACKEND", "ollama")).lower()
    if name == "mock":
        return MockBackend()
    if name == "ollama":
        return OllamaBackend()
    if name in _OPENAI_ALIASES:
        return OpenAIBackend()
    raise ValueError(f"backend inconnu: {name!r}")
