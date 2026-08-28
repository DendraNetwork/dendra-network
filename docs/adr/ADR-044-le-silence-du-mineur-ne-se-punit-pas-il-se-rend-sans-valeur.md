# ADR-044 — A mute miner cannot be punished on this chain; the payoff can be removed instead

**Status:** **GESTURE 2 SHIPPED, DORMANT (2026-08-22, consensus epoch 13). GESTURE 1 NOT ENACTED.**
The owner arbitrated on 2026-08-22 and the mandatory order was respected: the Go landed first.

`audit_unwind_blocks` exists, defaults to **0**, and at 0 nothing is read, written or decided — the
decision returns before its first store access, which `audit_unwind_dormant_test.go` drives directly
rather than through its call sites, because a refusal and a call that never happened look identical
from outside. Nine cases, mutation-proven 3/3: disarming the zero guard, the fail-closed on the date,
or the overflow guard each reddens exactly the case that holds it. Wired at BOTH deferral sites — the
small-jury deferral (inside the function, so a future caller inherits it) and the `default` arm of
`resolveDisputedAudit`, ahead of the appeal branch, since an appeal is one more postponement.

Two things the original text did not settle, both derived from the module's own rules rather than
decided here:
- **The dispute bond is RETURNED.** `resolveDisputedAudit` already states the rule — *return when the
  disputer is right or when no instance remains* — and an unwind is exactly "no instance remains".
  Escrowing it on a job now resolved would strand it; confiscating would sanction a disputer no
  committee ever found wrong.
- **`Validate` keeps the age ≥ `audit_resolve_timeout`.** Below it the unwind fires before the audit
  has had its own scheduled retry, which would REPLACE the deferral instead of bounding it.

**The epoch moved even though dormant is byte-identical.** proto3 omits the zero, so the stored Params
serialise unchanged and today's behaviour is untouched. The bump is owed to the day governance ARMS the
parameter: an older binary would ignore field 41 and keep deferring while a newer one unwinds, and an
epoch declares *same state machine*. `chain/app/upgrades.go` carries a second named plan
(`v3-audit-unwind`) so the switch can happen without destroying the chain.

Gesture 1 (`audit_sample_bps = 10000`) stays **unenacted** and must not be enacted alone: it is exactly
the lock described below.

> **Staying mute is profitable, and the profit is a parameter.** A primary is paid optimistically, and
> an audit only opens on a draw. Expected value of silence ≈ `(1 − a) × minerNet` where `a` is
> `audit_sample_bps`. The live chain serves **`a = 5000`** (`/dendra/jobs/v1/params`), which reproduces the calibration figure of **+1912 udndr per job** at
> `q = 0`.

## What was measured, first hand

| Fact | Where |
|---|---|
| `auditDraw` returns a value in `[0,10000)` | `audit_sampling.go` — `% 10000`, pure function |
| `effectiveAuditBps` can only **raise** the rate (probation, adaptive), never lower it | `audit_sampling.go` |
| The only branch that finalises a payment with **zero** verification is the `else` of the draw | `audit_sampling.go:233-236` — *"Not audited means the optimistic payment is FINAL"* |
| `silence_slash_bps` has exactly one reader, `clawbackPayment`, which has **no production caller** | `antievasion.go:295` and its bench T4 |
| The live chain nevertheless serves **`silence_slash_bps = 2000`** | `/dendra/jobs/v1/params` |
| `config.optimistic.yml` announces `audit_sample_bps: "10000"` — *"100 % SAMPLED"* | `config.optimistic.yml:145` |
| The genesis actually in use carries `5000`, written by a deployment tool outside the chain source | `/dendra/jobs/v1/params` vs the file above |
| **Re-measured against the live public network**: `audit_sample_bps=5000`, `silence_slash_bps=2000`, `audit_min_quorum=4`, `verification_mode=1`, `enforce_model_registry=false` — every row above still holds | direct `GET /dendra/jobs/v1/params` |
| The file is **never** the source for this parameter: `docker/entrypoint-chain.sh:168` sets `ASB="${DENDRA_AUDIT_SAMPLE_BPS:-5000}"` and rewrites `.app_state.jobs.params.audit_sample_bps` at every container start | `entrypoint-chain.sh:168` and `:343` |
| Of the **5** divergences between the served jobs params and the file, **4 are deliberate**: `dispute_bond=50000` and the two ADR-022 availability params dormant at `0` are the entrypoint's own defaults, and `audit_resolve_timeout=240` is set by `deploy/launch/.env.public.example:29` with a check behind it (`launch_env_check.sh:95`) | measured path-aware, module `jobs` only |

Two of those rows deserve to be read together. **The chain publicly advertises a 20 % silence penalty
that no code path can apply**: `query jobs params` shows it, and its only consumer is unreachable. A
governed parameter that is displayed but inert is worse than a missing one — it answers the question
"is silence punished here?" with a number instead of with "no".

And the declared sampling rate does not match the served one. That is a **desynchronisation, not an
arbitration**: nobody decided 50 %, a second writer simply won.

⚠️ **And the writer is named, which changes what "fixing" means.** `docker/entrypoint-chain.sh`
rewrites the jobs params with `jq` at **every container start** (`:343`), taking the rate from its own
default `ASB="${DENDRA_AUDIT_SAMPLE_BPS:-5000}"` (`:168`). So `config.optimistic.yml` is not merely
out of date on this line — it is **never read** for it. Editing the file would change nothing; the
decision lives in the entrypoint's default and in the deployment environment.
⛔ **What this is NOT, measured before claiming it.** A path-aware comparison of the 20 comparable
`jobs` params shows **5** divergences, and **4 are deliberate**: `dispute_bond=50000` is the same
entrypoint default, the two ADR-022 availability params sit at `0` because dormancy at zero is the
documented design (`x/jobs/types/params.go:75`), and `audit_resolve_timeout=240` is set on purpose by
`deploy/launch/.env.public.example:29` — with a guard that checks it (`launch_env_check.sh:95`).
*Only `audit_sample_bps` is a desynchronisation. A first pass here counted six divergences and then
five; the sixth was `epoch_blocks`, which exists in BOTH the jobs and the emission modules — comparing
a YAML key without its path compares two different parameters that happen to share a name.*

## ⛔ The obvious fix locks the network

Reading the row above, the reflex is to set `audit_sample_bps = 10000` and be done. **Do not**, not in
the current network state, and the reason is in the code rather than in a judgement call:

At `a = 10000` every settled job enters the audit path. There, if the drawable jury is smaller than
`effectiveSlashFloor(p)` — `audit_min_quorum`, **4** on this chain — the audit **defers with no
sanction and the fee stays held** (`audit_sampling.go:194-203`). With **one** registered miner, and
the jury excluding the primary, that floor is unreachable **for every job, forever**. `HeldFee` then
never clears, so `hasOpenObligation` stays true and `DeleteMiner` refuses: **every miner's bond is
locked for good**, and the network cannot even be left.

The deferral is unbounded *on purpose* — `audit_jury_floor.go:43`: bounding it would make evasion
free. The consequence is that raising the rate converts an economic hole into a liveness trap.

## The decision

**Gesture 1 — no code.** `audit_sample_bps = 10000` by `MsgUpdateParams`. Since the draw is in
`[0,10000)`, `draw < 10000` is always true and the unverified-finalisation branch becomes
unreachable. `Validate` accepts `10000`.

**Gesture 2 — consensus-breaking Go, dormant by default.** A governed `audit_unwind_blocks`; `0`
keeps behaviour byte-identical. At the two deferral sites — jury below floor
(`audit_jury_floor.go::deferAuditForSmallJury`) and the `default` arm of `resolveDisputedAudit` — if the parameter is
armed and the debt is older than it, **unwind the trade instead of rescheduling**:
`refundRetainedToClient` (`antievasion.go:438`, an existing primitive that consumes `HeldFee` and its
twin `HeldSince`, returns the deferred burn, reverses `Demand` under the same
`job.Client != miner.Operator` guard used at credit time, and tolerates a `nil` miner), then persist
the miner, clear the committee, mark the job, emit an event. **No sanction**: stake intact, bond
intact, neither paid nor slashed. Fail-closed on the date: if `HeldSince` is unreadable, or absent
while `HeldFee` exists, **defer as today** — an unknown age is never a licence to unwind.

**⛔ The order is not stylistic.** Gesture 2 must ship first. Gesture 1 alone produces exactly the
lock described above; gesture 2 alone is inert until governance arms it. Together, silence yields
nothing rather than being free.

**Why this is not the release the code forbids.** `audit_jury_floor.go:43` — *"releasing for want of
an audit would make the dodge free"* — is about paying **the miner**. Refunding **the client** makes
the dodge worth **zero** instead of free, which is the only exit compatible with the principle
already written there.

## What was refuted, and why it matters

Three independent designs were produced and each was put through four adversarial lenses
(chain-breaking, consensus non-determinism, cheap circumvention, honest-operator punished). **All
three were refuted by all four.** They failed for one shared reason worth recording:

> Each conditioned the release of funds on the **absence of a bad signal**, when this chain has no
> **positive** signal to require. There is no `<jobId>__reveal__` branch: the decision rests on
> verdicts alone, and the reveal stays an off-chain exchange. So any such guard falls back on the
> jury — and a jury that never concludes turns the guard against the honest miner, whose bond it
> locks and whose exit it refuses.

That is why the surviving design adds **no** sanction and **no** per-miner counter, and touches the
payoff rather than the punishment.

## What stays open, and is not to be masked

- **Silence is still not punished.** Its gain is brought to zero; nothing costs the mute miner beyond
  gas. This is a deliberate ranking — impunity above an unjust slash, and an unjust slash above a
  halted chain — and it belongs in public text rather than in a footnote.
- **Jury capture is untouched.** A verdict is a signed commit with no proof of judgement, and jurors
  are drawn from every miner in the frozen pool. Enough identities registered at `min_stake` before
  the freeze buy a *vindication*, hence a fully "verified" payment.
- **Service griefing gets worse, by construction.** After an unwind the client is refunded but was
  never served, and a mute miner can burn jobs at the price of gas. No bound is proposed here,
  because confiscating the bond would re-create the unjust-slash case.
- **The root is untouched.** "Committed but never revealed" and "revealed something false" remain
  indistinguishable on-chain. Only an **anchored reveal fingerprint** would turn silence into a
  falsifiable fact, and that is a different change.
- **Cost is unmeasured.** What `a = 10000` does to `EndBlock` under the `auditMaxPerBlock` budget, on
  a chain with many open debts, has not been measured. It should be, before gesture 1.
