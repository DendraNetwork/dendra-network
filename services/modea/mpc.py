"""Three-party MPC core (Mode B) — additive secret sharing over a finite field.

Guarantee: the client splits its input into 3 RANDOM ADDITIVE shares (mod P). Each party (miner)
receives only ONE share, statistically independent of the input, so only the collusion of all three
can reconstruct it. Linear layers (PUBLIC weights) are computed LOCALLY at each party (W @ share),
with no communication; the client sums the 3 result shares to obtain W @ x. This is the backbone of
Mode B (see CONFIDENTIALITE.md).

Fixed point: floats are encoded as integers (x * SCALE) in the field F_P (P a Mersenne prime). The
linear result is at scale SCALE^2 (decoded through decode_linear).
"""
from __future__ import annotations

import secrets

P = (1 << 61) - 1          # Mersenne prime, larger than the intermediate products (Python bigints)
SCALE = 1 << 16            # fixed-point factor (16 fractional bits)


# ----------------------------- fixed-point encoding ---------------------------
def enc(x: float) -> int:
    return round(x * SCALE) % P


def dec(v: int, scale_pow: int = 1) -> float:
    v %= P
    if v > P // 2:            # centred signed representation
        v -= P
    return v / (SCALE ** scale_pow)


def encode_vec(xs: list[float]) -> list[int]:
    return [enc(x) for x in xs]


def encode_mat(W: list[list[float]]) -> list[list[int]]:
    return [[enc(w) for w in row] for row in W]


# ----------------------------- 3-party additive sharing -----------------------
def share3(vec: list[int]) -> list[list[int]]:
    """vec (integers mod P) -> 3 random additive shares: s0 + s1 + s2 = vec (mod P)."""
    s0 = [secrets.randbelow(P) for _ in vec]
    s1 = [secrets.randbelow(P) for _ in vec]
    s2 = [(v - a - b) % P for v, a, b in zip(vec, s0, s1)]
    return [s0, s1, s2]


def reconstruct(shares: list[list[int]]) -> list[int]:
    """Sum of the shares (mod P). ALL shares are required to recover the value."""
    return [sum(col) % P for col in zip(*shares)]


# ----------------------------- linear layer (secret x public) -----------------
def linear_local(W_int: list[list[int]], share: list[int]) -> list[int]:
    """LOCAL computation at one party: (W @ share) mod P. No communication.
    W_int: m x n (encoded PUBLIC weights); share: length n -> output of length m."""
    n = len(share)
    return [sum(W_int[i][j] * share[j] for j in range(n)) % P for i in range(len(W_int))]


def decode_linear(result_vec: list[int]) -> list[float]:
    """The linear result is at scale SCALE^2 (W and x each carry one SCALE factor)."""
    return [dec(v, scale_pow=2) for v in result_vec]


def plain_linear(W: list[list[float]], x: list[float]) -> list[float]:
    """Cleartext reference, used to check the MPC result."""
    return [sum(W[i][j] * x[j] for j in range(len(x))) for i in range(len(W))]
