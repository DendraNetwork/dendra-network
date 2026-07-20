# Contributing to Dendra

Thanks for your interest. Dendra is **research / devnet** software — expect rough edges and resets.

## Where things live

- **Chain (Go, Cosmos SDK 0.53.6):** `chain/` — modules `x/jobs`, `x/emission`, `x/modelregistry`. This is the source of truth.
- **Off-chain reference stack (Python):** `services/` — gateway, miner, relay, client, judge glue, content filter, exporter, faucet.
- **Economic model:** `tokenomics/`.
- **Run kits:** `deploy/`, `docker/`.
- **Design rationale:** `docs/adr/` (read the ADRs before proposing protocol changes).

## Dev workflow

- Chain: `cd chain && go build ./... && go test ./...` (Go 1.25+).
- Python stack: `cd services && pip install -r requirements.txt` then run components per `deploy/`.
- Full local network: `docker compose up -d`.

## Pull requests

1. Open an issue first for anything non-trivial (protocol, tokenomics, security) so we can agree on direction.
2. Keep PRs focused. Include a test for each behavioral change (the chain favors deterministic table tests).
3. **Do not** weaken the sacred invariants without an ADR: fixed supply / zero mint, no plaintext on-chain, balanced escrow / settle-once, non-recoverable demand, "never the bond" on clawback, the anti-bubble `R` rule.
4. By contributing you agree your contribution is licensed under **Apache-2.0** (the project license).

## Security

Do **not** file security issues publicly — see [`SECURITY.md`](SECURITY.md).

## Honesty rule

Dendra's communication stays honest: utility token, no financial promises, devnet/research status, no "better than ChatGPT" claims, and the consumer tier is hardened **deterrence**, not a cryptographic guarantee. Please keep contributions (code comments, docs) in the same spirit.
