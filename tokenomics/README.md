# Tokenomics model — Dendra ($DNDR)

Executable model of the **v5** economics: a **fixed supply of 10,000,000 DNDR, zero mint**. Rewards are **released from a pre-allocated Reserve** (33% of genesis) — never minted. The script projects supply, remaining Reserve, scarcity, miner income, fee revenue, treasury and cumulative burn over 10 years.

```bash
python3 tokenomics_v5.py      # 10-year projection table
python3 -m pytest tests/      # tests
```

## Parameters (all in `V5Params`, tunable)

- **Fixed supply: 10,000,000 DNDR** — zero inflation, zero mint (no protocol minting; the standard `x/mint` module is removed from the chain binary).
- **Genesis allocation:** community 35% / reserve 33% / treasury 27% / team 5%.
- **Emission = release of the Reserve**, ~22%/yr of the *remaining* Reserve (decreasing, never minted), split across three flows: **work** (per-job, demand-gated ≤ 1.5× non-recoverable demand) / **availability** / **security**.
- **Soft burn:** 5% of fees are burned → mildly deflationary (supply trends toward ~8.1M at 10 years).
- **Per-job:** the protocol takes 15% of a job (the miner keeps 85%).

Everything is illustrative and governable. The price effect assumes real external demand, which is not guaranteed; the model is a design tool, not a forecast.
