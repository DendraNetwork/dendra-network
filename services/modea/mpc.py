"""Coeur MPC 3 parties (Mode B) — partage de secret additif sur corps fini.

Garantie : le client decoupe son entree en **3 parts additives aleatoires** (mod P). Chaque
partie (mineur) ne recoit qu'**UNE** part, statistiquement independante de l'entree -> seule
**la collusion des 3** permet de reconstruire. Les couches lineaires (poids PUBLICS) se
calculent **localement** chez chaque partie (W @ part), sans rien echanger ; le client somme
les 3 parts de resultat -> W @ x. C'est le backbone du Mode B (cf. CONFIDENTIALITE.md).

Point fixe : les flottants sont encodes en entiers (x * SCALE) dans le corps F_P (P premier de
Mersenne). Le resultat lineaire est a l'echelle SCALE^2 (decode via decode_linear).
"""
from __future__ import annotations

import secrets

P = (1 << 61) - 1          # premier de Mersenne, > produits intermediaires (Python = bigint)
SCALE = 1 << 16            # facteur de point fixe (16 bits de fraction)


# ----------------------------- encodage point fixe ----------------------------
def enc(x: float) -> int:
    return round(x * SCALE) % P


def dec(v: int, scale_pow: int = 1) -> float:
    v %= P
    if v > P // 2:            # representation signee centree
        v -= P
    return v / (SCALE ** scale_pow)


def encode_vec(xs: list[float]) -> list[int]:
    return [enc(x) for x in xs]


def encode_mat(W: list[list[float]]) -> list[list[int]]:
    return [[enc(w) for w in row] for row in W]


# ----------------------------- partage additif 3 parties ----------------------
def share3(vec: list[int]) -> list[list[int]]:
    """vec (entiers mod P) -> 3 parts additives aleatoires : s0 + s1 + s2 = vec (mod P)."""
    s0 = [secrets.randbelow(P) for _ in vec]
    s1 = [secrets.randbelow(P) for _ in vec]
    s2 = [(v - a - b) % P for v, a, b in zip(vec, s0, s1)]
    return [s0, s1, s2]


def reconstruct(shares: list[list[int]]) -> list[int]:
    """Somme des parts (mod P). Necessite TOUTES les parts pour retrouver la valeur."""
    return [sum(col) % P for col in zip(*shares)]


# ----------------------------- couche lineaire (secret x public) --------------
def linear_local(W_int: list[list[int]], share: list[int]) -> list[int]:
    """Calcul LOCAL chez une partie : (W @ part) mod P. Aucune communication.
    W_int : m x n (poids PUBLICS encodes) ; share : longueur n -> sortie longueur m."""
    n = len(share)
    return [sum(W_int[i][j] * share[j] for j in range(n)) % P for i in range(len(W_int))]


def decode_linear(result_vec: list[int]) -> list[float]:
    """Le resultat lineaire est a l'echelle SCALE^2 (W et x chacun x SCALE)."""
    return [dec(v, scale_pow=2) for v in result_vec]


def plain_linear(W: list[list[float]], x: list[float]) -> list[float]:
    """Reference en clair (pour verifier la justesse du MPC)."""
    return [sum(W[i][j] * x[j] for j in range(len(x))) for i in range(len(W))]
