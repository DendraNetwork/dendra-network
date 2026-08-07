# Contributing to Dendra

Thanks for your interest. Dendra is **research / devnet** software — expect rough edges and resets.

What is publicly reachable is an **open devnet endpoint**: three validators and one miner. Two validators
and the miner are operated by the project; the third validator is an outside operator, bonding 9 DNDR
against the largest validator's 1 000 000, so the count includes an outsider while the voting power
does not. Inference is served and jobs settle. Audit committees are drawn, intermittently, and none
concludes — the jury excludes the miner under audit, so one miner leaves no eligible juror, and a verdict
needs `audit_min_quorum` (4) jurors voting, i.e. five registered miners. No job is verified on-chain. The [`README`](README.md) states that with the queries that check it.

**The protocol does pay for work**, and the settlement path is armed from block 1 — miner, validator
and whistleblower each have a flow, sourced from client fees and the pre-allocated Reserve, never from
inflation (see [What the protocol pays](README.md#what-the-protocol-pays)). Two things are true at once and the docs must carry both.
**Contribution is recorded**, because every job, settlement, verdict and slash is written into the block
that carries it — and if a mainnet is launched, the project intends that record to count among the
inputs to how an allocation is decided. **And nothing is promised**: no amount, no rate, no ratio, no
date, no eligibility, no reservation, no advantage for arriving early, and no guarantee that a mainnet
will exist at all. `services/points_indexer.py` computes a provisional view of that
record; it is a reading of public state, not a ledger of entitlements, and its formula can change or be
withdrawn. Its internal "Season 0" title is a legacy name being retired — it implies a schedule and a
successor, and neither exists. Do not turn either half into the other: describing this as a rewards campaign is wrong, and
describing the network as offering nothing to earn is wrong too. Testnet `$DNDR` still has no monetary value and the chain
is resettable.

The most useful contribution right now is a **second independent operator** — the VRF contributor floor
that currently defers every audit draw is lifted by the second anchored validator key, not by a patch,
and lifting it is also what releases the retained earnings of every miner — the seed check runs before
the per-job audit lottery, so below the floor nothing is released at all.

## Where things live

- **Chain (Go, Cosmos SDK 0.53.6):** `chain/` — modules `x/jobs`, `x/emission`, `x/modelregistry`. This is the source of truth.
- **Off-chain reference stack (Python):** `services/` — gateway, miner, relay, client, judge glue, content filter, exporter, faucet.
- **Economic model:** `tokenomics/`.
- **Run kits:** `deploy/`, `docker/`.
- **Design rationale:** [`docs/adr/`](docs/adr/README.md) — read the ADR covering the area before proposing a protocol change; [`docs/adr/README.md`](docs/adr/README.md) is the index.

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

Dendra's communication stays honest: utility token, no financial promises, devnet/research status, **no claim of superiority over frontier models** — the property offered is privacy plus on-chain verifiability, and it is stated without ranking anyone else — and the consumer tier is hardened **deterrence**, not a cryptographic guarantee. Please keep contributions (code comments, docs) in the same spirit.

The same rule applies to measurements: a claim states a **property**, not an isolated number. When the property does not yet hold everywhere, cite the measurement **and its perimeter** (which machines, which validator set, which run) rather than dropping the qualifier.

And it applies to tense. A sentence about the **protocol** ("a proven cheat is slashed") and a sentence about the **network** ("cheats are being slashed") are both allowed; writing the first and letting a reader take it for the second is not. When a doc asserts something about what is running, it should name the query that settles it.

**Understating is not a safe default.** Honesty here is symmetric: writing "there is nothing to earn" because no miner has joined yet is as false as promising a yield, and it is false in a way that is easy to miss, because it sounds cautious. The rule is the same in both directions — describe what the code does, cite the file that does it, and let the reader check. If a claim about rewards cannot point at a keeper file and a parameter readable from a live node, it does not belong in the docs.
