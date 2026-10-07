# Contributing to Dendra

Thanks for your interest. Dendra runs as a **public testnet**, and contributions are welcome.

What is publicly reachable is **`dendra-testnet`**, a chain started on a fresh genesis. The previous chain,
`dendra`, was abandoned without a snapshot — no balance, registration, job or history carried over — and
this chain can be reset the same way. It launched from a genesis with **one validator, run by the project on
one host**: at launch that validator holds all the voting power and orders and finalises every block, so at
launch consensus tolerates no fault — a validator holding a third or more of the power is a single point of
failure, and if it stops, the chain stops;
surviving the loss of any one validator takes at least four, each below a third (n >= 3f+1). At launch nothing about it is
decentralised or fault-tolerant; read the validator set at `curl -s http://testnet-api.dendranetwork.com:26657/validators`
(or `dendrad query staking validators`) rather than from this page. Do not take a count from this page, and do not
take a comparison either - read it:
`curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/miner` gives `pagination.total`, the registered
count — an upper bound, since only miners that proved availability in the current or previous window are
drawn for work or audit. The rule behind it: the jury excludes the miner under audit, so a verdict needs
`audit_min_quorum` drawn jurors voting, i.e. one more present, eligible miner than that quorum. The [`README`](README.md) states that with the queries that check it.

**The protocol does pay for work**, and the settlement path is armed from block 1 — miner, validator
and whistleblower each have a flow, sourced from client fees and the pre-allocated Reserve, never from
inflation (see [What the protocol pays](README.md#what-the-protocol-pays)). The genesis sets `hold_bps` to
10000, so the miner's net share and the protocol's cut are retained at settlement: a job the audit lottery
does not draw releases them at finality, a drawn job is paid on an upheld verdict, and a conviction slashes
the miner and refunds the client. The work subsidy is credited as a right at finality and claimed by the
miner (`MsgClaimSubsidy`).

**The Final Testnet Season is a separate reward programme**, specified in [ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md)
with its rules at [dendranetwork.com/final-season](https://dendranetwork.com/final-season/): it ends on
7 November 2026 at 23:59 UTC (the chain's own block times decide), has no registration, and pays weekly in
testnet `$DNDR` per miner identity by a formula published in advance. Each day
is ranked once final and published with its evidence, and anyone can recompute it with
`services/final_season_rank.py`. The season's rewards count as points: each testnet DNDR the season paid
becomes one mainnet DNDR, credited in the mainnet genesis, and mainnet launches when the season ends; faucet grants, transfers, on-chain job pay
and the work subsidy are not points. Keep the two apart: on-chain pay is computed by the keeper code from
parameters readable on a live node, the season's rewards by the published formula, and describing either as the other is wrong — as is describing the network as
offering nothing to earn. `$DNDR` is a utility token used inside the protocol; there is no token sale,
presale or ICO, testnet `$DNDR` is never sold, and the chain is resettable: a reset erases balances; the
season's published rankings are archived before any reset.

Two floors stand between a job and a concluded audit, they are independent, and **each needs a different
contribution**. The VRF contributor floor, `committee_min_vrf_contributors`, defers a draw on any block
carrying fewer validator VRF vote-extensions than it — anchored **validators** count toward it, never
miners. A job deferred that way unwinds like an audit that cannot conclude: after `audit_unwind_blocks`, its
fee and the protocol's cut return to the client. The genesis sets that floor to 1, and while `committee_min_vrf_contributors`
is 1, one validator's VRF output carrying at least two thirds of the committing power is enough to seed a draw, so **at launch the project's validator alone produces
the randomness of every assignment and audit draw**; raising it takes independent validators and a governance vote. The jury
floor is the second: an audit concludes only when `audit_min_quorum` drawn jurors vote and the jury excludes
the miner under audit, so it needs one more present, eligible miner than that quorum. That floor gates the
verdict, not the job: with `audit_unwind_blocks` above zero and `hold_bps` at 10000, a job opens below it
(`pool_freeze.go::enoughEligibleMinersToVerify`). Read that quorum, `audit_sample_bps`,
`audit_unwind_blocks`, `hold_bps` and `committee_min_vrf_contributors` from the chain
(`dendrad query jobs params -o json`, or `/dendra/jobs/v1/params`) rather than from any page: the first
sets how many must vote, the second what share of settled jobs the lottery draws, and a drawn job whose jury
does not conclude within `audit_unwind_blocks` returns its fee and the protocol's cut to the client: never
paid unverified, never held for ever.

Which side of either floor the network stands on is not written here, for the same reason no count is: read
it from the chain (`/dendra/jobs/v1/committee_seed_health` for the contributors, `/dendra/jobs/v1/miner` for
the miners). A page that names the blocking floor is wrong the day either one moves.

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

Dendra's communication stays honest: utility token, no financial promises, public testnet status, **no claim of superiority over frontier models** — the property offered is privacy plus on-chain verifiability, and it is stated without ranking anyone else — and the consumer tier is hardened **deterrence**, not a cryptographic guarantee. Please keep contributions (code comments, docs) in the same spirit.

The same rule applies to measurements: a claim states a **property**, not an isolated number. When the property does not hold everywhere, cite the measurement **and its perimeter** (which machines, which validator set, which run) rather than dropping the qualifier.

And it applies to tense. A sentence about the **protocol** ("a proven cheat is slashed") and a sentence about the **network** ("cheats are being slashed") are both allowed; writing the first and letting a reader take it for the second is not. When a doc asserts something about what is running, it should name the query that settles it.

**Understating is not a safe default.** Honesty here is symmetric: writing "there is nothing to earn" on the grounds that a network is small is as false as promising a yield, and it is false in a way that is easy to miss, because it sounds cautious. The rule is the same in both directions — describe what the code does, cite the file that does it, and let the reader check. If a claim about rewards cannot point at the code that computes them — a keeper file and a parameter readable from a live node for on-chain pay, the published Final Testnet Season formula for the programme — it does not belong in the docs.
