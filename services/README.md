# Reference stack (Mode A): confidential inference

This folder is the reference implementation of Mode A confidential inference. The litepaper describes the design and the two-tier confidentiality model. On consumer GPUs the privacy guarantee is hardened deterrence, not a cryptographic one. The cryptographic guarantee against a miner with root access is Mode B (MPC), which is still a research spike. A datacenter TEE tier is on the roadmap, but it is not implemented, it cannot be selected, and nothing should rely on it: there is no hardware root of trust anywhere in the current code.

The flow: the client encrypts (ephemeral X25519 and AES-GCM); the miner decrypts in RAM, runs the inference, encrypts the answer and zeroizes its buffers; the client decrypts. The ledger only ever sees commitments (hashes). A leak is traced through a canary, which identifies the responsible miner so that it can be slashed.

## Who runs what

This folder holds both halves of the network. The half a file belongs to decides who is affected when it breaks.

| Component | Runs on | |
|-----------|---------|--|
| `miner.py`, the judge and reveal workers | the operator's machine | started inside the miner container by `deploy/join.sh` |
| `capacity_sign.py` | the operator's machine | signs the hardware report with the miner's own key |
| `the_proof.py` | the network operator's host | the read-only feed the public site reads |
| `final_season_server.py` and the season modules | the programme's hosts | the public season service and its graders |
| `client.py`, `cli.py` | whoever calls the network | |
| `relay.py`, `capacity_server.py`, `faucet.py`, `gateway.py` | the network operator's host | reached over HTTP; you never run these to join |
| `modea/` | both sides | the shared protocol |

`modea/` is where the two sides meet, and a mismatch there is easy to miss. A signature is produced on one side and verified on the other, so the canonical message, the key encoding and the address derivation must agree exactly. Each half can be correct on its own while the pair does not work, and nothing fails loudly, because the verifying side answers "I cannot accept this" instead of raising.

A change in `modea/`, or in either side of a pair that meets there, therefore needs a test that exercises the pair: one that signs with the operator's code and verifies with the network's. A test that re-implements either side only proves that the test agrees with itself.

## Install and test

```bash
cd services    # this folder
pip install -r requirements.txt
pytest -q tests/       # full suite: crypto, MPC, coordinator, network, persistence, hardening, chain bridge
```

The logic of the earlier standalone demos lives in the `modea/` package, is covered by `tests/`, and runs for real through the deployment kits.

Many `test_*.py` files at this level are standalone scripts: their body runs at import time and ends in `sys.exit()`. `conftest.py` keeps a collection pass from importing them; the gate battery runs each one by one with `python3`.

## Running the real stack

- All in one (Docker): `docker compose up -d` from the repository root starts the chain and the services; the local inference engine and the local miners are an optional profile (`docker compose --profile local up -d`), since miners normally run on their own machines.
- Joining a public network: `deploy/join.sh` (miner, validator or judge; see `deploy/README.md`).

The files of this folder, service by service, follow.

## Gateway

- `gateway.py` — serves the chat-completions API (`/v1/models`, `/v1/chat/completions`); routes every chat request through the network (escrow, committee, inference, verdict, payment) and returns the answer in the chat-completions format.
- `content_filter.py` — the illegal-content filter at the gateway, on the cleartext, before encryption: it blocks only what is manifestly illegal; adult, rude, controversial or opinionated content passes.
- `gateway_fund.py` — funds the gateway subsidy account from the faucet at startup; the gateway signs from its own keyring.
- `test_gateway_failure_status.py` — a failed inference must not read as an answer.

## Relay

The untrusted message bus. It carries ciphertext and public keys, never cleartext.

- `relay.py` — the relay itself: publishes miner public keys, carries the sealed requests and answers.
- `relay_client.py` — its HTTP client, standard library only.
- `relay_store.py` — the relay's disk store: a sealed reveal must survive a restart.
- `modea/relay_canon.py` — the canonical message of a signed write: two different writes cannot produce the same signed bytes.
- `modea/relay_carrier.py` — the carrier of a signed write: a document under a dedicated chain-ID, unbroadcastable by construction.
- `modea/relay_signature.py` — the writer side: turns a deposit into the headers the relay verifies.
- `modea/relay_write.py` — the verification at the relay: the four building blocks composed in their order.
- `modea/relay_antireplay.py` — replay protection and freshness, two properties kept apart.
- `modea/registry_cache.py` — the miner registry cache: it reports a state and decides nothing.
- `modea/cosmos_addr.py` — derives an account address (`dendra1...`) from a public key in pure Python, so the relay can ask who writes.

Benches: `test_relay_access.py` (who may write, what may be destroyed), `test_relay_antireplay.py`, `test_relay_auth_message.py` (a refusal speaks for its route only), `test_relay_auth_message_mutation.py` (does the bench know how to say no), `test_relay_canon.py` (injectivity), `test_relay_carrier.py` (against a signature the binary really produced), `test_relay_concurrency.py` (atomic write-once, silent sockets), `test_relay_list_filter.py` (the filtered work queue and its ETag), `test_relay_store.py`, `test_relay_write.py` (the order of the checks), `test_silent_deposits.py` (a discarded result must not read as success), `test_registry_cache.py` (never-read is not stale), `test_cosmos_addr.py` (against an external truth, not against itself).

## The proof feed

- `the_proof.py` — the Proof feed: a read-only HTTP facade over the real on-chain state (audited jobs, slashes, VRF health), so the public site shows a feed nobody staged. No writes, no secrets, no content: counters and public metadata only.

## Capacity

- `capacity_server.py` — the network capacity registry: `POST /capacity` (a node posts its hardware report), `GET /capacity` (the aggregate and per-node list), `GET /metrics` (the same as metrics).
- `capacity_sign.py` — signs one capacity report and prints the headers that carry the proof; entering the verified block takes a signature by the operator of a staked miner.

## Faucet

- `faucet.py` — the native faucet: one address per POST, sends serialized by a lock, from the only key in the faucet's own keyring.

## The season programme

The Final Testnet Season, module by module.

- `final_season_server.py` — the public service served under `/final-season/v1/`: status, signed payout declarations, the evidence files, the rankings.
- `final_season_rules.py` — the season's rules in one place, as integers of udndr; every other piece of the programme imports these values.
- `final_season_generator.py` — the season's own requests: a fixed number a day, spread over the day, never counted per identity.
- `final_season_grader.py` — the model-graded check of the work answers, run on each grading host; each host has its own token, bound to a grader name.
- `final_season_grader_measure.py` — measures the work grade on real answers; writes nothing to the programme.
- `final_season_calc.py` — the calculation's pure core: facts in, ranking out; no network, no clock, no file.
- `final_season_chain.py` — the programme's chain reads: every read returns what it read or raises, so "no job settled" and "the node did not answer" never look alike.
- `final_season_evidence.py` — the evidence log: one append-only file per day, the record of what the chain does not hold.
- `final_season_facts.py` — turns the evidence log and the chain's records into the day's identity rows; pure, so a reader can rerun the day.
- `final_season_rank.py` — computes or recomputes a day's ranking; anyone can run it to check what was published.
- `final_season_payout.py` — the weekly payment from the payout account; plan only without `--yes`.
- `final_season_miner.py` — the miner's side: where its rewards go (`payout`, `payout-prepare`, `payout-submit`, `status`, `--lock`).
- `final_season_address.py` — which addresses can receive a payment; an address can be well formed and still unpayable.
- `points_indexer.py` — the Season 0 points indexer: dormant, writes nothing on-chain, points provisional and traced.

## The judge

- `judge_worker.py` — the committee worker: judges the audited sample and commits its verdict on-chain; run by an already-registered miner alongside the daemon.
- `modea/judge.py` — the judge's verdict on "do both answers carry the same fact": the primary's answer against the worker's own reference, committed as 1 or 0.
- `modea/judge_nli.py` — the entailment stage backed by an NLI classifier: it replaces the LLM judge on the same-fact question with a signal of a different kind.
- `modea/coherence_ppl.py` — coherence by perplexity: deterministic word-salad detection.
- `modea/coherence_pll.py` — coherence scored by masked pseudo-perplexity, a drop-in replacement for `modea/coherence_ppl.py`, selected by `DENDRA_PPL_BACKEND=pll`.
- `reveal_helpers.py` — the reveal to the anchored jury of an audited job.

Benches: `test_judge_two_endpoints.py` (two inferences, two models, two endpoints), `test_prompt_commitment.py` (the question is committed before the answer is known), `test_prompt_commit_endtoend.py` (the commitment reproduces across processes, or it certifies nothing).

## The miner

- `miner.py` — the persistent miner: creates its keys, funds itself at the faucet, registers on chain with its own signature.
- `reveal_worker.py` — the primary's reveal, as a standalone process alongside the daemon.
- `reveal_marker.py` — the reveal marker, stage 1 of the two-stage silence test: a primary that did nothing becomes decidable without the relay.
- `miner_selftest.py` — the miner's self-test, run inside the container: a report for a person, or one JSON.
- `serve_guard.py` — no mining model on the CPU, measured where the model runs; the rule is decided by `deploy/hw_probe.sh --role`.
- `modea/keyring.py` — the miner's keyring: which backend, which passphrase, how `dendrad` is handed it; read from the state of the disk, never from a setting.
- `modea/miner_id.py` — the canonical miner identifier, client side: the chain derives it from the signer's address and refuses any other value.
- `modea/owner_tx.py` — the transactions an owner signs, prepared on the mining machine (owner mode: `deploy/join.sh --owner`).
- `modea/heartbeat.py` — the daemon's heartbeat file: what the loop last did, read by the container's healthcheck and the self-test.
- `modea/inference.py` — the inference backends: `OllamaBackend` (the local engine), `MockBackend` (deterministic, no GPU), `ChatCompletionsBackend` (any local engine serving the chat-completions API, chosen by `DENDRA_INFER_BACKEND`); no backend logs content.
- `modea/dendrad_argv.py` — builds a `dendrad` command line whose positional values cannot be read as options.
- `modea/job_registry.py` — the job cache: a deposit of kind `req` is bound to the job's paying client.
- `modea/miner.py` — the hardened miner: decrypts in locked memory, runs the inference, re-encrypts, wipes.
- `modea/hardening.py` — locked and zeroized buffers, core dumps disabled, the software self-attestation.
- `modea/confine.py` — process-level confinement of the whole miner process.
- `modea_attest.py` — prints the confinement attestation; honest about what a self-measurement can and cannot show.
- `modea_confine.sh` — runs the daemon under confinement, three layers from the most portable to the strongest.
- `modea_egress.sh` — the egress firewall for the miner: the relay, the chain and the inference engine, nothing else.

Benches: `test_identity_glue.sh` (the entrypoint waits for the identity the daemon resolved), `test_persistent_identity.py` (the identity rename is written down), `test_sign_key_follows_rename.py` (the signing identity follows the keyring rename), `test_reveal_marker.py` (a membership proof impossible to forge), `test_reveal_worker_marker.py` (the marker anchoring, with doubles for the relay and the chain).

## Client and CLI

- `client.py` — the client engine: submit a request, track it, settle, read network state; reused by the dashboard and the CLI.
- `cli.py` — drives the local network from the terminal (`state`, `submit`, `job`).
- `modea/client.py` — one ephemeral key per job; no state between jobs.
- `modea/net_client.py` — the network client: the bytes of the request are exposed, so the absence of plaintext is proven rather than asserted.

## Readiness and measurement

- `c3_gate.py` — the honest C3 readiness report; it never prints `GO`, because that call belongs to people.
- `c3_jury_forensics.py` — are the missing verdicts absent or late: two causes that call for opposite remedies.
- `c3_jury_model.py` — which lever closes open audits: adding jurors, or getting the seated jurors to vote.
- `c3_run_measure.py` — the measurement of the C3 load run, separated from traffic submission.

Benches: `test_c3_gate.py`, `test_c3_jury_forensics.py`, `test_c3_jury_model.py`, `test_c3_run_measure.py`.

## Observability

- `exporter.py` — exposes the network state (miners, stake, balances, pools, height, jobs) as Prometheus metrics on `/metrics`, refreshed on a loop; it queries the chain through `client.py`.
- `dendra_grafana_dashboard.json` — data file: the dashboard that reads those metrics.
- `test_relay_exporter.py` — the relay observability path end to end.

## Building blocks (`modea/`)

Mode B, MPC (`modea/mpc.py`, `tests/test_mpc.py`): additive three-party secret sharing over a finite field. The client splits its input into three random shares, one per miner. Each miner computes `W @ share` locally without seeing the input, and the client reconstructs `y = W·x`. No single miner, and no pair of miners, can reconstruct the input. This is the linear backbone; the non-linear layers (softmax, GeLU) are the expensive part.

Distributed demonstrator (`modea/agent.py`, `tests/test_net.py`, `tests/test_persistence.py`): miners register and send heartbeats, so liveness is real. Confidential jobs are assigned and then settled, with commitments in a persistent ledger and a reputation score. The ledger holds only hashes and survives restarts, and a miner that stops sending heartbeats leaves the healthy set.

Coordinator (`modea/coordinator.py`, `tests/test_coordinator.py`): miner discovery, routing for Mode A, and the Mode B selection of three miners in the same region (low round-trip time) run by distinct operators, to resist collusion. It only ever sees metadata, never content.

Hardening (`modea/hardening.py`, `tests/test_hardening.py`): locked and zeroized memory (`mlock`, `VirtualLock`), core dumps disabled, and a software self-attestation of the binary.

Networked mode (`modea/server.py`, `modea/net_client.py`): the miner is an HTTP server and the client connects over the network. The test checks that the bytes sent on the wire are opaque: the secret never appears in the request.

Inference and chain bridge (`modea/chain_bridge.py`, `tests/test_chain_bridge.py`): confidential inference, on-chain payment, the anti-Sybil subsidy, a leak and its slash, with a confidentiality self-check.

## Shared modules (`modea/`)

| File | Role |
|---|---|
| `modea/__init__.py` | the package doc: the Mode A flow and its security controls |
| `modea/crypto.py` | ephemeral X25519 keys, ECDH plus HKDF-SHA256, AES-256-GCM, zeroization |
| `modea/ledger.py` | the simulated on-chain registry, hashes only; a stand-in, never a source of truth |
| `modea/canary.py` | leak-detection canaries: a marker that proves which miner served the job |
| `modea/agent.py` | miner registration plus the heartbeat loop towards the coordinator |
| `modea/coordinator.py` | discovery, routing and the Mode B selection of three miners; sees metadata only |
| `modea/mpc.py` | Mode B: additive three-party secret sharing over a finite field |
| `modea/server.py` | the HTTP miner server of the demonstrator; `/mpc_linear` is not the production path |
| `modea/chain_bridge.py` | the bridge between Mode A and the state machine: commitments and economics in lockstep |
| `modea/chain_id.py` | the chain ID is read, never written; one definition for the whole stack |
| `modea/exposure.py` | the reading of public-exposure flags and listen addresses, one definition |
| `modea/kit_floor.py` | the kit floor (`DENDRA_KIT_MIN`, `DENDRA_KIT_DEADLINE`) read in one place by the relay, the faucet and the season rules |
| `modea/kit_version.py` | the kit version, from the file the kit ships (`docker/KIT_VERSION`) |

`requirements.txt` holds the dependencies of this folder.

## What the tests prove

1. No plaintext reaches the ledger: nothing is stored unencrypted on chain.
2. The ciphertext does not contain the plaintext.
3. Every job uses its own ephemeral key (forward secrecy).
4. A canary identifies the miner behind a leak, as objective evidence.
5. Commitments are verifiable 256-bit hashes.

## Limits of this prototype

The full operating-system hardening (mlock or VirtualLock, a sandbox without network egress, binary attestation, no swap, no core dumps) is specified for the production miner client. The miner applies process-level confinement at start (non-dumpable process, no core dumps, no-new-privs, optional mlockall); the kit does not run the network-less sandbox or the egress firewall (`modea_confine.sh`, `modea_egress.sh`). This folder demonstrates the cryptographic chain and the detection model, not the OS isolation. Mode A protects against the public, the chain and the network, and deters the miner; the cryptographic guarantee against a miner with root access is Mode B (MPC).
