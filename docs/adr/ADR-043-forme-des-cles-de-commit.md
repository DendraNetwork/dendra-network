# ADR-043 — A commit key says what it is, and one function decides it

**Status:** Accepted, implemented, **not deployed**. The change is consensus-breaking:
`docker/CONSENSUS_EPOCH` moves **5 → 6**, so it reaches a network only through a fresh genesis. The
chain running today is on epoch 5 and stays there.

> **`CreateCommit` never checked that the job existed.** No `k.Job.Get`, no `k.Job.Has`, and
> `grep "func .*ValidateBasic" chain/` returns **0** across the whole module. A **registered**
> miner therefore wrote a PERMANENT entry — `UpdateCommit` and `DeleteCommit` are neutralised, and
> overwriting is refused — under a job id no job had ever opened. Free, beyond gas: there is no custom
> ante-handler. And unbounded in size: roughly a mebibyte per transaction, the only ceiling being
> CometBFT's default `MaxTxBytes`.

## The decision

A commit key is decomposed by exactly one function, `types.ParseCommitKey`, which returns three
distinguishable outcomes. `CreateCommit` then requires that the job named by the key exists, refuses
a residue that is not a bare job id, and bounds the payload fields.

## What made this hard, and it was not the guard

The obvious form of the fix — `k.Job.Has(msg.JobId[:strings.LastIndex(msg.JobId, "__")])` — **cuts
the audit layer**. Three facts, each measured, stand between the obvious form and a correct one.

### 1. Two derivations already existed, and they disagreed

`msg_server_commit.go` took `key[:LastIndex("__")]` as-is. `commitProvesAssignedWork`
(`miner_vitality.go`) then trimmed the `__verdict` and `__redo` roles from it. While the derivation
only fed a beacon lookup, the gap had no visible effect. The moment it decides a **refusal**, the two
answers can no longer differ: a verdict key would look up `Job["<job>__verdict"]`, which is false for
ever, and every verdict would be rejected in consensus.

Removing the second copy had a side effect worth recording: the deferred-reveal guard now applies to
verdict keys. With the old `key[:idx]` it read `Beacon["<job>__verdict"]` — a key that never exists —
so it silently never applied to them.

### 2. A fifth key shape carries no job at all, and the chain has no name for it

| Shape | Job id to extract | Producer | Job required |
|---|---|---|---|
| `<jobId>__<minerId>` | `<jobId>` | miner daemon | yes |
| `<jobId>__verdict__<minerId>` | `<jobId>` | judge daemon | yes |
| `<jobId>__redo__<minerId>` | `<jobId>` | `msg_server_adjudicate.go`, `redo_committee.go` | yes |
| `<jobId>__redo__verdict__<minerId>` | `<jobId>` | — (made legal by the trim order) | yes |
| `e<N>__reveals__<minerId>` | **none** | reveal worker's epoch marker | **never** |

The last row is the one that matters. The epoch marker is anchored like any commit, is **armed by
default**, and names no job. `grep` for that namespace across `x/` outside tests returns **0** — the
chain has no name for it; it exists only on the services side. Nothing in the Go code could have
suggested it: the producer had to be read. A `Job.Has` requirement applied to it would refuse **every
epoch marker, at every epoch, on every judge node** — the audit layer cut by the guard meant to
protect it.

Hence `JobScoped()`, and **three answers rather than two**:

- `ok == false` — the key is not a shape this chain serves; refuse.
- `ok == true && !JobScoped()` — legitimate, and names no job. Accept without a job.
- `ok == true && JobScoped()` — names a job, and that job must exist.

Collapsing the last two refuses the marker; collapsing the first two accepts anything shaped like one.
`false` here never means "refuse".

### 3. The exemption must come from the KEY, never from the sender

Routing on `msg.Kind == "revealmark"` would have been the short path. `Kind` is supplied by the sender
and **no on-chain decision reads it** — its single non-generated use is a write. Exempting on it would
hand the whole bypass over for free: write the label, skip the guard. So the key alone decides, and a
bench case fails if that ever changes.

## Two things the existence check does not close

**Padding.** `<realJob>__x1__<me>` shelters behind a job id that does exist. Without a test on the
residue, a miner opens an unbounded number of permanent keys inside the `<jobId>__` range the
settlement handlers walk. That test does **not** lean on `msg_server_open_job.go` refusing `__` inside
a job id: the genesis path writes `k.Job.Set` raw, and `types.GenesisState.Validate()` is never on the
`InitChain` route, so an imported job may carry the sequence.

**Size.** The bounds are **derived, not chosen**: `embedMaxDim * len("-1048576") + (embedMaxDim - 1)`,
computed from what the chain already declares it can read back for a semantic commit
(`msg_server_verify_semantic.go`). Change `embedMaxDim` or `embedMaxMag` and the bound follows. `Kind`
is bounded rather than validated, because an enum there would be a rule nothing enforces while a bound
stops the field from being a place to park bytes.

## What was proven, and what was not

Derivation: 16 cases, **mutation 5/5** — inverted trim order, padding test removed, marker recognised
after the trims, canonical form dropped, `JobScoped` widened. Handler: 7 cases, **mutation 5/6**.
Whole module: build, vet, **8 packages green**.

⛔ **The sixth mutation is not caught, and the test file says so.** The fail-closed branch on a store
read that errors is exercised by no case: `collections.Map.Has` returns `(false, nil)` for a missing
key, and the neighbouring technique (`Params.Remove`) works only because a missing `Item` makes its
`Get` fail. Measured: removing that branch leaves the bench green. It is kept anyway —
`known, _ := k.Job.Has(...)` would silently authorise the day that read can fail, which is the shape
`params_fail_closed_test.go` exists to forbid elsewhere.

## Consequence for whoever adds a sixth shape

The table above is enforced by `x/jobs/types/commit_key.go` and its bench. A service that invents a new
key shape without declaring it there will have its commits **refused by the chain** — silently for the
developer, loudly for the operator. That is the intended failure mode: an undeclared shape is not a
shape this chain serves. Declare it in the parser, add its row to the bench, and say whether it names a
job.
