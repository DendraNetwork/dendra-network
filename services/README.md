# Reference stack (Mode A): confidential inference

This folder is the reference implementation of Mode A confidential inference. The litepaper describes the design and the two-tier confidentiality model. On consumer GPUs the privacy guarantee is hardened deterrence, not a cryptographic one. The cryptographic guarantee against a miner with root access is Mode B (MPC), which is still a research spike. A datacenter TEE tier is on the roadmap, but it is not implemented, it cannot be selected, and nothing should rely on it: there is no hardware root of trust anywhere in the current code.

The flow: the client encrypts (ephemeral X25519 and AES-GCM); the miner decrypts in RAM, runs the inference, encrypts the answer and zeroizes its buffers; the client decrypts. The ledger only ever sees commitments (hashes). A leak is traced through a canary, which identifies the responsible miner so that it can be slashed.

## Who runs what

This folder holds both halves of the network. The half a file belongs to decides who is affected when it breaks.

| Component | Runs on | |
|-----------|---------|--|
| `miner.py`, the judge and reveal workers | the operator's machine | started inside the miner container by `deploy/join.sh` |
| `capacity_sign.py` | the operator's machine | signs the hardware report with the miner's own key |
| `client.py`, `cli.py` | whoever calls the network | |
| `relay.py`, `capacity_server.py`, `faucet.py`, `gateway.py` | the network operator's host | reached over HTTP; you never run these to join |
| `modea/` | both sides | the shared protocol |

`modea/` is where the two sides meet, and a mismatch there is easy to miss. A signature is produced on one side and verified on the other, so the canonical message, the key encoding and the address derivation must agree exactly. Each half can be correct on its own while the pair does not work, and nothing fails loudly, because the verifying side answers "I cannot accept this" instead of raising.

A change in `modea/`, or in either side of a pair that meets there, therefore needs a test that exercises the pair: one that signs with the operator's code and verifies with the network's. A test that re-implements either side only proves that the test agrees with itself.

## Install and test

```bash
cd services            # this folder
pip install -r requirements.txt
pytest -q tests/       # full suite: crypto, MPC, coordinator, network, persistence, hardening, chain bridge
```

The logic of the earlier standalone demos lives in the `modea/` package, is covered by `tests/`, and runs for real through the deployment kits.

## Running the real stack

- All in one (Docker): `docker compose up -d` from the repository root starts the chain, Ollama, the relay, the miners and the gateway.
- Joining a public network: `deploy/join.sh` (miner, validator or judge; see `deploy/README.md`).
- Components in this folder: `gateway.py` (chat-completions gateway, `/v1/chat/completions`), `miner.py` (persistent miner), `relay.py` (sealed message bus), `client.py` (client engine), `cli.py` (command line), `exporter.py` (Prometheus metrics).

## Building blocks (`modea/`)

Mode B, MPC (`modea/mpc.py`, `tests/test_mpc.py`): additive three-party secret sharing over a finite field. The client splits its input into three random shares, one per miner. Each miner computes `W @ share` locally without seeing the input, and the client reconstructs `y = W·x`. No single miner, and no pair of miners, can reconstruct the input. This is the linear backbone; the non-linear layers (softmax, GeLU) are the expensive part.

Distributed demonstrator (`modea/agent.py`, `tests/test_net.py`, `tests/test_persistence.py`): miners register and send heartbeats, so liveness is real. Confidential jobs are assigned and then settled, with commitments in a persistent ledger and a reputation score. The ledger holds only hashes and survives restarts, and a miner that stops sending heartbeats leaves the healthy set.

Coordinator (`modea/coordinator.py`, `tests/test_coordinator.py`): miner discovery, routing for Mode A, and the Mode B selection of three miners in the same region (low round-trip time) run by distinct operators, to resist collusion. It only ever sees metadata, never content.

Hardening (`modea/hardening.py`, `tests/test_hardening.py`): locked and zeroized memory (`mlock`, `VirtualLock`), core dumps disabled, and a software self-attestation of the binary.

Networked mode (`modea/server.py`, `modea/net_client.py`): the miner is an HTTP server and the client connects over the network. The test checks that the bytes sent on the wire are opaque: the secret never appears in the request.

Inference and chain bridge (`modea/chain_bridge.py`, `tests/test_chain_bridge.py`): confidential inference, on-chain payment, the anti-Sybil subsidy, a leak and its slash, with a confidentiality self-check.

## What the tests prove

1. No plaintext reaches the ledger: nothing is stored unencrypted on chain.
2. The ciphertext does not contain the plaintext.
3. Every job uses its own ephemeral key (forward secrecy).
4. A canary identifies the miner behind a leak, as objective evidence.
5. Commitments are verifiable 256-bit hashes.

## Modules (`modea/`)

| File | Role |
|---|---|
| `crypto.py` | ephemeral X25519 key exchange (ECDH), HKDF key derivation, AES-GCM encryption, zeroization |
| `ledger.py` | simulated on-chain registry that stores hashes only |
| `canary.py` | traceable markers and leak detection |
| `inference.py` | `ollama` (real) and `mock` (no GPU) backends, no content logging |
| `miner.py` | decrypts in RAM, runs the inference, encrypts the answer, zeroizes |
| `client.py` | ephemeral key per job: encrypts, submits, decrypts |
| `coordinator.py` | discovery, routing and the Mode B selection (collusion resistance) |
| `mpc.py` | Mode B: additive three-party secret sharing |
| `hardening.py` | locked and zeroized memory, self-attestation |
| `chain_bridge.py` | bridge between inference and on-chain settlement |

## Limits of this prototype

The full operating-system hardening (mlock or VirtualLock, a sandbox without network egress, binary attestation, no swap, no core dumps) is specified for the production miner client. The miner applies process-level confinement at start (non-dumpable process, no core dumps, no-new-privs, optional mlockall); the kit does not run the network-less sandbox or the egress firewall (`modea_confine.sh`, `modea_egress.sh`). This folder demonstrates the cryptographic chain and the detection model, not the OS isolation. Mode A protects against the public, the chain and the network, and deters the miner; the cryptographic guarantee against a miner with root access is Mode B (MPC).
