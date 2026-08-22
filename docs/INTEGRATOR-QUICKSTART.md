# Dendra — Integrator Quickstart (machine API)

> **Who this is for.** An agent, an oracle/AVS, or a backend that needs inference whose **response is verifiable on-chain** (randomly drawn committee, real cost, and a dishonest miner gets *slashed*). Dendra is **drop-in OpenAI**: you change `base_url` and additionally receive a **proof block** named `dendra`.
>
> **Status: research network / devnet.** Utility token, no financial promise.
>
> **What follows describes the protocol, not the running network.** On the public endpoint **no job has
> ever been verified on-chain**. Committees are drawn, but a verdict needs `audit_min_quorum` jurors to
> vote and the jury is every registered miner *except* the one under audit — a single registered miner
> leaves it empty. So `audit_state` is `"pending"` on every response today, and the `vindicated` /
> `rejected` branches of §3 have never fired. Write them anyway: they are what the code will report
> once the miner count clears the floor, and an integrator that omits them acts on an unaudited answer
> as if it were a confirmed one.

## 1. Calling — it is the OpenAI API

No custom SDK. Any OpenAI client works; only `base_url` changes.

The public `base_url` is `https://api.dendranetwork.com/demo/v1`, and it needs **no key**. The gateway serves a single model advertised as `dendra-network`: the `model` field is accepted but **not used for routing**.

> **`/v1` is not the open path.** `https://api.dendranetwork.com/v1/models` answers `401 unauthorized` and key issuance is not open. Build against `/demo/v1`.
>
> **A call fails here by an ON-CHAIN REFUSAL, not for want of a server** — and the two look identical from the client, while they send you to opposite debugs. The chain refuses to open a job at all while fewer than `audit_min_quorum + 1` miners are eligible at that block, because a job whose jury could never reach quorum would freeze its fee forever. Read **both** numbers rather than either one alone: `curl -s http://api.dendranetwork.com:1317/dendra/jobs/v1/miner` (`pagination.total`, the registered count) against `curl -s http://api.dendranetwork.com:1317/dendra/jobs/v1/params` (`audit_min_quorum`, then add one yourself). A page cannot tell you the current value — that is why both queries are above. While the registered count stays at or below `audit_min_quorum`, nothing opens.
>
> **Set your client timeout above 300 s** for when one is serving. A single answer can take **over 200 seconds** — one consumer GPU per answer. A stock 30 s timeout then fails every call, and it fails as a *client* timeout — the gateway never sees it, so no log on either side will tell you why.

```python
from openai import OpenAI
client = OpenAI(base_url="https://api.dendranetwork.com/demo/v1",
                api_key="unused", timeout=300.0)

r = client.chat.completions.create(
    model="dendra",
    messages=[{"role": "user", "content": "Capital of Australia? Answer in one word."}],
)
print(r.choices[0].message.content)      # "Canberra"
print(r.usage)                            # prompt_tokens=14 completion_tokens=2 total_tokens=16
```

curl equivalent, copy-pasteable as-is:

```bash
curl -s --max-time 300 https://api.dendranetwork.com/demo/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"dendra","messages":[{"role":"user","content":"Capital of Australia? Answer in one word."}]}'
```

## 2. What Dendra adds — the `dendra` block (the verifiability API)

Every response carries a `dendra` object (non-streaming: at the root; SSE: in the **last** chunk). This is what an agent/oracle consumes to **decide whether to trust** the response.

```jsonc
{
  "choices": [ ... ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15 },
  "dendra": {
    "job_id": "job_...",              // the on-chain anchor for this inference
    "miner_id": "dm1...",             // bech32, DERIVED from the operator address (ADR-039) — never chosen
    "committee": ["dm1...", "dm1..."],// the drawn committee (VRF)
    "beacon": "…",                    // anti-grinding seed of the draw
    "audit_state": "pending",         // ALWAYS "pending" here: the VRF audit lands AFTER settlement
    "cost_udndr": 650,                // real cost (1 DNDR = 1e6 udndr)
    "escrow_udndr": 650,
    "verify": {                       // how to cross-check, without trusting the gateway
      "proof_endpoint": "https://proof.dendranetwork.com/proof",
      "query": "dendrad query jobs get-job job_..."
    }
  }
}
```

Key fields:
- **`audit_state`** is **always `"pending"`** in the response: the sampled (VRF) audit lands *after* settlement. The verified state is read **afterwards** on The Proof (`recent_audits[].state`) or via `verify.query`.
- **`verify`** — object: `proof_endpoint` (The Proof URL, read-only, no secret) + `query` (the `dendrad` command to cross-check `job_id` directly on-chain). Read `verify.proof_endpoint` from the response; it may be a **relative path**, so resolve it against The Proof's public origin `https://proof.dendranetwork.com` before fetching (see §3). The raw service port is **not** reachable from outside — resolve against the host above, never against a port.
- **`cost_udndr` / `usage`** — real cost and tokens, client-verifiable.

Rollback (debug): `DENDRA_EXPOSE_META=0` removes the block on the gateway side.

## 3. Verifying a response by script (the oracle pattern)

The audit is asynchronous: at response time `audit_state="pending"`. For oracle use, you **re-read** The Proof a little later and look for the `job_id` in `recent_audits` (the recently audited jobs). Absent from `recent_audits` = not yet audited (VRF sampling only draws a fraction of jobs).

```python
resp = client.chat.completions.create(model="dendra", messages=[...])
meta = resp.model_extra["dendra"]           # openai-python exposes the extra under model_extra
import urllib.request, json
from urllib.parse import urljoin
# proof_endpoint may be relative; resolve it against The Proof's public origin
proof_url = urljoin("https://proof.dendranetwork.com", meta["verify"]["proof_endpoint"])
proof = json.load(urllib.request.urlopen(proof_url))
audit = next((a for a in proof["recent_audits"] if a["job_id"] == meta["job_id"]), None)
if audit is None:
    trust = "unaudited"                      # not drawn by the audit (majority of jobs) — baseline trust
elif "clawed" in audit["state"]:
    trust = "rejected"                       # the server was SLASHED → do NOT act on this response
elif "resolved" in audit["state"]:
    trust = "vindicated"                     # audited AND confirmed honest → strong trust
else:
    trust = "pending"                        # audit in progress → wait / re-read
```

The economic model that makes this credible: the committee is drawn at random (VRF, non-grindable), a sample of jobs is audited by a **heterogeneous committee of judges**, and a divergent server loses stake (hard slash). Cheating is *-EV*.

## 4. Limits (honest)

- **Consumer-grade confidentiality = hardened deterrence** (confinement + slashing), **not** a cryptographic guarantee. Software attestation exists but is **not enforced by default** (`DENDRA_ATTEST_REQUIRE=0`), so do not count it as a control. A datacenter tier backed by a hardware TEE is **roadmap — not implemented, and there is no hardware root of trust today**: do not design against it. Content never touches the chain (hashes/verdicts only).
- **Slashing is armed in the settlement path, and no miner has been slashed on the public network**, for the reason given at the top: a verdict needs a jury that does not exist yet. `-EV` describes the mechanism, not an observed history.
- **Throughput** bounded by miner inference, not by the chain; research network, variable capacity.
- **Streaming**: the `dendra` block arrives in the **last** SSE chunk (the job settles after generation).
- Minimal legality filter upstream (manifestly illegal content refused) — not a politeness filter.

## 5. Endpoints

All integrator calls go to `https://api.dendranetwork.com/demo/v1`:

| Path | Role |
|---|---|
| `POST /demo/v1/chat/completions` | inference (drop-in OpenAI) + `dendra` block — **no key** |
| `GET /demo/v1/models` | served models (returns `dendra-network`) |
| `GET /health` | gateway status |

Verifiability lives on its own host, read-only and without a secret:
- `GET https://proof.dendranetwork.com/proof` — The Proof.

**What does not answer**, so you do not build against it: `/v1/*` and `/points` require a key and return `401`; the raw service ports `:8090` and `:8091` are not exposed publicly.

See also: `deploy/join.sh` (run a miner/judge).
