# Tokenomics model — Dendra ($DNDR)

Executable model of the **v5** economics: a **fixed supply of 10,000,000 DNDR, zero mint**. Rewards are **released from a pre-allocated Reserve** (33% of genesis) — never minted. The script projects supply, remaining Reserve, scarcity, miner income, fee revenue, treasury and cumulative burn over a configurable horizon.

> **The year axis is a modelling convention, not a chain property.** The chain counts **epochs** (`epoch_blocks` blocks), and the wall-clock length of a block is not fixed by the consensus. Every duration this model prints in years assumes a block interval; treat those outputs as scenarios, and never restate them as network figures without naming the interval assumed. Figures below are **model outputs, not commitments**.

```bash
python3 tokenomics_v5.py      # 10-year projection table
python3 -m pytest tests/      # tests
```

## Parameters (all in `V5Params`, tunable)

- **Fixed supply: 10,000,000 DNDR** — zero inflation, zero mint (no protocol minting; the standard `x/mint` module is removed from the chain binary).
- **Genesis allocation:** community 34% / reserve 33% / treasury 27% / team 5% / faucet float 1% — as shipped in `chain/config.optimistic.yml`.
- **Emission = release of the Reserve**, a decreasing share of what *remains* (never minted), applied **once per epoch** and split across three flows: **work** (per-job, demand-gated ≤ 1.5× non-recoverable demand) / **availability** / **security**. The rate is the on-chain `reserve_release_bps`; the model's default of 2200 bps is the binary's compiled default, **not** the launch genesis, which ships 2 bps per 86,400-block epoch.
- **Soft burn:** 5% of fees are burned, so supply can only move downwards. How far depends on real usage — the model's figure is a scenario output, not a target.
- **Per-job:** the protocol takes 15% of a job (the miner keeps 85%).

Everything is illustrative and governable. The price effect assumes real external demand, which is not guaranteed; the model is a design tool, not a forecast.
