# Reference stack (Mode A) — confidential inference

Reference implementation of **Mode A** confidential inference (see the litepaper for the design and the two-tier confidentiality model). On consumer GPUs the privacy guarantee is **hardened deterrence**, not a cryptographic one; the cryptographic guarantee against a root miner is **Mode B (MPC)** or the opt-in datacenter TEE tier.

Flow: the **client encrypts** (ephemeral X25519 + AES-GCM) → the **miner decrypts in RAM**, infers, re-encrypts, then **zeroizes** → the **client decrypts**. The ledger only ever sees **commitments** (hashes). A **leak is traced** by a canary → the responsible miner is identified (slashing).

## Install & tests

```bash
cd services            # this folder
pip install -r requirements.txt
pytest -q              # full suite: crypto, MPC, coordinator, network, persistence, hardening, chain bridge
```

> The old standalone demos (`demo.py`, `*_demo.py`) were removed during a repo cleanup (recoverable from git history). The logic they illustrated now lives in the **`modea/` package** (covered by `tests/`) and runs for real through the deployment kits.

## Running the real stack

- **All-in-one (Docker):** `docker compose up -d` from the repo root brings up the chain + Ollama + relay + miners + gateway (~1s blocks).
- **Join a public network:** `deploy/join.sh` (miner / validator / judge — see `deploy/README.md`).
- **Components (in this folder):** `gateway.py` (OpenAI-compatible gateway), `miner.py` (persistent miner), `relay.py` (sealed bus), `client.py` (client engine), `cli.py` (CLI), `exporter.py` (Prometheus metrics).

## Building blocks (`modea/`)

**Mode B (MPC)** (`modea/mpc.py` · `tests/test_mpc.py`): additive 3-party secret sharing over a finite field. The client splits its input into 3 random shares → one per miner; each miner computes `W @ share` **locally, without seeing the input**; the client reconstructs `y = W·x`. Guarantee: no single miner (nor any two) can reconstruct the input. This is the linear backbone; the non-linearities (softmax/GeLU) are the costly part.

**Distributed demonstrator** (`modea/agent.py` · `tests/test_net.py` + `tests/test_persistence.py`): miners register and **heartbeat** → real liveness; confidential jobs are **assigned then settled** (commitments in a **persistent ledger** + reputation); the ledger holds only hashes and **survives restarts**; a miner that stops beating **leaves the healthy set**.

**Coordinator** (`modea/coordinator.py` · `tests/test_coordinator.py`): miner discovery, routing (Mode A), and **Mode B selection** = 3 miners in the **same region (low-RTT) + distinct operators** (anti-collusion). It only ever sees metadata, never content.

**Hardening** (`modea/hardening.py` · `tests/test_hardening.py`): locked memory (`mlock`/`VirtualLock`) + zeroized, anti core-dump, **software self-attestation** of the binary.

**Networked mode** (`modea/server.py` + `modea/net_client.py`): the miner is an HTTP server, the client connects over the network. Check: **the bytes actually sent on the wire are opaque** — the secret never appears in the network request.

**Inference↔chain bridge** (`modea/chain_bridge.py` · `tests/test_chain_bridge.py`): confidential inference → on-chain payment → anti-Sybil subsidy → leak → slash, with a confidentiality self-check.

## What the tests prove (confidentiality self-checks)

1. **No plaintext in the ledger** (nothing in clear on-chain).
2. **The ciphertext does not contain the plaintext.**
3. **A unique ephemeral key per job** (forward secrecy).
4. **Canary: a leak → the miner is identified** (objective proof).
5. **Verifiable commitments** (256-bit hashes).

## Modules (`modea/`)

| File | Role |
|---|---|
| `crypto.py` | ephemeral X25519, ECDH, HKDF, AES-GCM, zeroization |
| `ledger.py` | simulated on-chain registry — **hashes only** |
| `canary.py` | traceable markers + leak detection |
| `inference.py` | `ollama` (real) / `mock` (no-GPU) backends, no content logging |
| `miner.py` | decrypts in RAM → infers → re-encrypts → zeroizes |
| `client.py` | ephemeral key per job, encrypts, submits, decrypts |
| `coordinator.py` | discovery + routing + Mode B selection (anti-collusion) |
| `mpc.py` | Mode B: additive 3-party secret sharing |
| `hardening.py` | locked/zeroized memory + self-attestation |
| `chain_bridge.py` | inference ↔ on-chain settlement bridge |

## Limits (prototype)

The OS hardening (mlock/VirtualLock, egress-less sandbox, binary attestation, no-swap, no-core-dump) is **specified** and will ship with the **production miner client**. Here we demonstrate the **cryptographic chain + the detection model**, not the OS isolation. Honest reminder: Mode A protects against the public / chain / network and **deters** the miner; the cryptographic guarantee against a root miner is **Mode B (MPC)**.
