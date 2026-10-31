# Dendra — Integrator Quickstart (machine API)

> **Who this is for.** An agent, an oracle, or a backend that needs inference whose **response is verifiable on-chain** (randomly drawn committee, real cost, and a dishonest miner gets *slashed*). Dendra serves the **chat-completions API** (`/v1/chat/completions`): you change `base_url` and additionally receive a **proof block** named `dendra`.
>
> **Status: public testnet, `dendra-testnet`.** Utility token, no financial promise.
>
> **What follows describes the protocol. Whether a given stage has run is something you read from the
> chain, not from this page**, so this page states none and gives you the query instead. Counts and
> "has never happened" claims go stale silently; a command does not.
>
> `audit_state` is `"pending"` in every response, by construction: the gateway writes that field as a
> constant and never reads it back (see the note in section 2). To learn what actually happened to a
> job, read the chain, never the response.
>
> **Three stages gate a verdict, in order.** A job must be *opened*; the audit lottery must *draw* a
> committee for it; that committee must *conclude*. Each has its own condition, and none of them is a
> miner count:
> - **Opening** is refused when too few miners are eligible at that block:
>   `chain/x/jobs/keeper/pool_freeze.go::enoughEligibleMinersToVerify` needs `audit_min_quorum` + 1,
>   because the jury excludes the miner under audit.
> - **Drawing** is gated on the seed: under `committee_seed_source=1` no committee is drawn while
>   `latest_contributors` stays under `committee_min_vrf_contributors`; the jobs are deferred and
>   their fees stay held (`x/jobs/keeper/audit_sampling.go` logs each deferral, with the line
>   `no decentralized VRF seed at this height -> NO audit draw`). Only a bonded validator anchoring a
>   VRF key raises that reading.
> - **Concluding** needs `audit_min_quorum` jurors to vote, and voting is gated on hardware: a
>   registered miner that never runs `--judge` casts nothing.
>
> Read all three rather than assuming which one is closed:
> ```
> curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/job
> curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/params
> curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/committee_seed_health
> ```
>
> Write the `vindicated` / `rejected` branches of section 3 whatever those reads return: they are what
> the code reports once a committee concludes, and an integrator that omits them acts on an unaudited
> answer as if it were a confirmed one.

## 1. Calling the chat-completions API

No custom SDK. Any chat-completions client works; only `base_url` changes. The Python examples below use the `openai` client package (`pip install openai`).

The public `base_url` is `https://testnet-api.dendranetwork.com/demo/v1`, and it needs **no key**. The gateway serves a single model advertised as `dendra-network`: the `model` field is accepted but **not used for routing**.

> **`/v1` is not the open path.** `https://testnet-api.dendranetwork.com/v1/models` answers `401 unauthorized` and key issuance is not open. Build against `/demo/v1`.
>
> **A call can fail because the chain refuses it while the server is up.** From the client, a chain refusal and a dead server look the same, but they call for opposite debugging. The chain refuses to open a job at all while fewer than `audit_min_quorum + 1` miners are eligible at that block, because a job whose jury could never reach quorum would freeze its fee forever. Read **both** numbers rather than either one alone: `curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/miner` (`pagination.total`, the registered count) against `curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/params` (`audit_min_quorum`, then add one yourself). A page cannot tell you the current value, which is why both queries are given. While the registered count stays at or below `audit_min_quorum`, nothing opens.
>
> **Keep a client timeout of about ten minutes.** One consumer GPU serves each answer, and the gateway replies only after it has waited for the answer (up to `DENDRA_TIMEOUT` seconds, 240 by default, `gateway.py::TIMEOUT`) and then for the miner's commit on chain before it settles (up to 180 seconds more, `client.py::settle_when_ready`). A stock 30 s timeout then fails every call, as a *client* timeout the gateway never sees, so no log on either side will tell you why.

```python
from openai import OpenAI
client = OpenAI(base_url="https://testnet-api.dendranetwork.com/demo/v1",
                api_key="unused", timeout=600.0)

r = client.chat.completions.create(
    model="dendra-network",
    messages=[{"role": "user", "content": "Capital of Australia? Answer in one word."}],
)
print(r.choices[0].message.content)      # "Canberra"
print(r.usage)                            # prompt_tokens=14 completion_tokens=2 total_tokens=16
```

curl equivalent, copy-pasteable as-is:

```bash
curl -s --max-time 600 https://testnet-api.dendranetwork.com/demo/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"dendra-network","messages":[{"role":"user","content":"Capital of Australia? Answer in one word."}]}'
```

## 2. What Dendra adds: the `dendra` block (the verifiability API)

Every response carries a `dendra` object (non-streaming: at the root; SSE: in the **last** chunk). This is what an agent/oracle consumes to **decide whether to trust** the response.

```jsonc
{
  "choices": [ ... ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15 },
  "dendra": {
    "job_id": "job_...",              // the on-chain anchor for this inference
    "miner_id": "dm1...",             // bech32, derived from the operator address, never chosen
    "committee": ["dm1...", "dm1..."],// the drawn committee (VRF)
    "beacon": "…",                    // anti-grinding seed of the draw
    "settled": true,                  // did the chain accept the settlement? true / false / null (not known)
    "audit_state": "pending",         // always "pending" here: the VRF audit lands after settlement
    "cost_udndr": 650,                // real cost (1 DNDR = 1e6 udndr)
    "escrow_udndr": 650,
    "verify": {                       // how to cross-check, without trusting the gateway
      "proof_endpoint": "https://testnet-proof.dendranetwork.com/proof",
      "query": "dendrad query jobs get-job job_..."
    }
  }
}
```

Key fields:
- **`settled`** says whether the chain accepted this job's settlement, read from the settlement's own
  answer: `true`, `false`, or `null` when the gateway could not tell. With `false` the job stayed open: no
  audit will follow, the miner is not paid, and the client's escrow comes back when the job expires
  unserved. Treat the answer as unverified.
- **`audit_state`** is **always `"pending"`** in the response: the sampled (VRF) audit lands *after* settlement. The verified state is read **afterwards** on The Proof (`recent_audits[].state`) or via `verify.query`.
- **`verify`**: an object with `proof_endpoint` (The Proof URL, read-only, no secret) and `query` (the `dendrad` command to cross-check `job_id` directly on-chain). Read `verify.proof_endpoint` from the response; it may be a **relative path**, so resolve it against The Proof's public origin `https://testnet-proof.dendranetwork.com` before fetching (see §3). The raw service port is **not** reachable from outside: resolve against the host above, never against a port.
- **`cost_udndr` / `usage`**: real cost and tokens, client-verifiable.

Rollback (debug): `DENDRA_EXPOSE_META=0` removes the block on the gateway side.

## 3. Verifying a response by script (the oracle pattern)

The audit is asynchronous: at response time `audit_state="pending"`. Only a sample of jobs is ever drawn for audit, and a drawn job gets its outcome later. Read the job back from the chain by its `job_id` and classify its `state`. The state is a list of markers joined with `+`, so test them as whole words in a set, never as substrings (`noquorum` contains `quorum`).

```python
import json
import urllib.request

CHAIN = "https://testnet-api.dendranetwork.com/rest/dendra/jobs/v1"


def audit_outcome(state):
    """How the chain says an audit ended. Markers are whole words: noquorum is not quorum."""
    marks = set(state.split("+"))
    if "clawed" in marks:
        return "convicted"       # a jury convicted the miner and the client was refunded: do not act on the answer
    if "resolved" not in marks:
        return "no_verdict_yet"  # not drawn, waiting for a jury, or still open: most jobs stay here
    if "unwound" in marks:
        return "refunded"        # no jury ruled and the fee went back to the client: nothing was verified
    if {"vindicated", "quorum"} <= marks:
        return "upheld"          # a jury at the bar upheld the answer: that jury's verdict, not a proof
    return "unclear"             # a re-hearing, a governance vote, a challenge no jury ruled on, or an older state: read the job


meta = resp.model_extra["dendra"]    # the client package keeps extra fields here
job = json.load(urllib.request.urlopen(CHAIN + "/job/" + meta["job_id"]))["job"]
outcome = audit_outcome(job["state"])
```

What each outcome means:

- `convicted`: a jury convicted the miner and the client was refunded. Do not act on the answer.
- `upheld`: a jury at the bar upheld the answer. It is that jury's verdict, reached by a judge model; it is not a recomputation.
- `refunded`: the audit closed with no verdict, because no jury could sit or none ruled in time, and the fee went back to the client. Nothing was verified, so a closed audit is not strong trust: treat the answer as unverified.
- `unclear`: the job closed another way (a re-hearing, a governance vote, or a challenge no jury ruled on), or its state is older than these markers. Read the job and treat the answer as unverified.
- `no_verdict_yet`: no audit has concluded on this job. Most jobs stay here for good, since only a sample is drawn, so this is the baseline with no audit behind it. If the job was drawn, read it again later.

The public audit feed lists only the latest audits (`recent_audits` of `verify.proof_endpoint`), so a job that is missing from it says nothing: read the job, as above.

The economic model that makes this credible: the committee is drawn at random (VRF, non-grindable), a sample of jobs is audited by a jury of other miners running the judge model the chain names, and a miner the jury convicts loses stake (hard slash). Cheating is *-EV*.

## 4. Limits

- **Consumer-grade confidentiality is hardened deterrence** (confinement plus slashing), **not** a cryptographic guarantee. Software attestation exists but is **not enforced by default** (`DENDRA_ATTEST_REQUIRE=0`), so do not count it as a control. A datacenter tier backed by a hardware TEE is **on the roadmap, not implemented, and there is no hardware root of trust today**: do not design against it. Content never touches the chain (hashes and verdicts only).
- **Slashing is armed in the settlement path.** Whether a miner has been slashed on the public network is a reading of the chain, not of this page; a verdict needs a jury that concludes (see the top of this page). `-EV` describes the mechanism and claims no observed history.
- **Throughput** is bounded by miner inference, not by the chain; this is a public testnet with variable capacity.
- **Streaming**: the `dendra` block arrives in the **last** SSE chunk (the job settles after generation).
- A minimal legality filter runs upstream. It refuses manifestly illegal content only: adult, rude or controversial content passes.

## 5. Endpoints

All integrator calls go to `https://testnet-api.dendranetwork.com/demo/v1`:

| Path | Role |
|---|---|
| `POST /demo/v1/chat/completions` | inference (chat-completions API) + `dendra` block — **no key** |
| `GET /demo/v1/models` | served models (returns `dendra-network`) |
| `GET /health` | gateway status |

Verifiability lives on its own host, read-only and without a secret:
- `GET https://testnet-proof.dendranetwork.com/proof`: The Proof.

**What does not answer**, so you do not build against it: `/v1/*` and `/points` require a key and return `401`; the raw service ports `:8090` and `:8091` are not exposed publicly.

## 6. Paying with your own wallet: signed requests on the relay

The gateway opens every job with its own key. A client that holds DNDR can skip it: it opens the job on
chain itself, pays the fee into escrow, and hands the sealed prompt to the miners through the relay,
signed with the key that paid. No relay token and no gateway key are involved.

What you need, all read from `https://testnet-api.dendranetwork.com/network-info.txt`, a list of `KEY=value`
lines: `CHAIN_ID`, `DENDRA_NODE` (the node your `dendrad` talks to) and `DENDRA_RELAY` (the relay). You also
need a `dendrad` key with a balance that covers the fee. The commands below read the three values and the
price from that file, so there is nothing to fill in:

```bash
INFO=$(curl -fsS https://testnet-api.dendranetwork.com/network-info.txt)
CHAIN_ID=$(printf '%s\n' "$INFO" | sed -n 's/^CHAIN_ID=//p')
export DENDRA_NODE=$(printf '%s\n' "$INFO" | sed -n 's/^DENDRA_NODE=//p')
export DENDRA_RELAY=$(printf '%s\n' "$INFO" | sed -n 's/^DENDRA_RELAY=//p')
PRICE=$(printf '%s\n' "$INFO" | sed -n 's/^REQUEST_PRICE_UDNDR=//p')
```

**The price.** The relay accepts a signed request when the job is still open on chain and escrowed at
least the relay's request price, in udndr: one price per request, whatever the model. A fee above the price
is accepted. A relay with no price refuses every signed request with `PRICE_UNSET`; a request then passes
only with the relay's token, which the gateway holds. `network-info.txt` carries the price under
`REQUEST_PRICE_UDNDR` only when the relay applies it: `deploy/testnet/publish_network.sh` reads the price
from the running relay and refuses to publish any other. No `REQUEST_PRICE_UDNDR` line means no signed
path. A deposit
refused on the price says so in the `why` field of its `401`, with both amounts (`UNDERPAID`).

**With the reference client** (`services/cli.py`), which opens the job, seals the prompt to
the on-chain key of each member of the committee the chain's verification mode calls for
(`client.py::_submit_committee_k`; `--k` overrides it), deposits it, waits for the answer and
settles:

```bash
export DENDRA_SIGN_KEY=mykey          # the key that pays: it must be the one passed to --client
python3 cli.py submit "Capital of Australia? Answer in one word." --client mykey --fee "$PRICE"
```

`DENDRA_SIGN_KEY` and `--client` name the same key on purpose: the relay attributes the request to the
address that signed it, and accepts it only when that address is the client the chain records for the job.

**By hand**, the order is the same:

1. Choose a job id (letters, digits and underscores, no `__`) and open the job with
   `JOB=job_$(date +%s); dendrad tx jobs open-job "$JOB" "$PRICE" --from mykey --chain-id "$CHAIN_ID" --node "$DENDRA_NODE"`.
2. Read the committee: `dendrad query jobs assigned-committee "$JOB" --node "$DENDRA_NODE"`. Its `members`
   are the miner ids; the next two steps run once per member, with that id in `MID`.
3. Seal the prompt to the member's anchored encryption key (`/dendra/jobs/v1/miner`, field `enc_pubkey`) and
   send it with `POST $DENDRA_RELAY/req/${JOB}__$MID`, with the signature headers that
   `services/modea/relay_signature.py::headers` builds. `X-Dendra-Miner` must be the member's id.
4. Read the sealed answers: `GET $DENDRA_RELAY/res/${JOB}__$MID`, open to anyone, since only you
   hold the key that decrypts them.
5. Settle the job from the same key.

**What the relay checks, in this order**, and the code it answers with when a check fails:

| Check | Refusal |
|---|---|
| the miner the key names is the miner the header names | `KEY_MISMATCH` |
| the signature verifies over this exact body | `INVALID_SIGNATURE`, `MALFORMED` |
| the signing address is the client the chain records for the job | `FOREIGN_CLIENT`, `UNKNOWN_JOB` |
| the relay has a request price | `PRICE_UNSET` |
| the job can be read on chain | `FEE_UNREADABLE`, `JOB_STATE_UNREADABLE` |
| the job's state is `open`: a job paid, settled, expired or refunded takes no request | `JOB_NOT_OPEN` |
| the job's `fee` is at least that price | `UNDERPAID` |
| `<mid>` is a member of the committee the chain assigned to the job | `UNASSIGNED_MINER`, `COMMITTEE_UNREADABLE` |
| the miner is attested, when the relay requires it | `NOT_ATTESTED` |
| the deposit was not seen before | `REPLAY`, `HEIGHT_REGRESSION` |

The checks before the last one record nothing: a deposit refused on one of them can be sent again
unchanged once the cause is gone. A relay that does not read signatures answers `401` with `why` starting
`off`. Every deposit key is write-once: a second, different request for the same `<job_id>__<mid>` is
refused `409`.

See also: `deploy/join.sh` (run a miner/judge).
