"""Bridge between Mode A and the state machine (Dendra L1).

Two COMPLEMENTARY layers advance in lockstep around an inference job:
  - the Ledger (commitments, ADR-011) proves CONFIDENTIALITY: it contains only hashes, never content;
  - the Chain (state machine, ADR-018) carries the ECONOMICS: escrow, miner payout (part of it
    vesting), NON-RECOVERABLE demand (ADR-017), gated subsidy, slash on leak.

The confidential inference loop (client encrypts -> miner infers in locked memory -> result
re-encrypted) remains the Mode A one: the bridge only OPENS the escrow before, SETTLES after, and
writes nothing but COMMITMENTS.
"""
from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

# The reference state machine lives in a sibling `chain-modules` package and is reached through
# sys.path rather than copied here, so the bridge and the machine it settles against cannot drift.
# That package is an OPTIONAL dependency: it belongs to the full development tree and is absent from
# a distribution that ships the services alone. Its absence must therefore DISABLE this bridge and
# nothing else. Importing it unconditionally is what makes the difference: a hard import turns a
# missing optional dependency into an unimportable module, and pytest reports that as a COLLECTION
# error that aborts the entire service suite rather than as one unavailable feature.
_CHAIN_PKG = Path(__file__).resolve().parents[2] / "chain-modules"
if _CHAIN_PKG.is_dir() and str(_CHAIN_PKG) not in sys.path:
    sys.path.insert(0, str(_CHAIN_PKG))

try:
    from chain import Chain                  # noqa: E402  (the on-chain state machine)
except ImportError as _exc:                  # the sibling package is not part of this distribution
    Chain = None                             # type: ignore[assignment,misc]
    CHAIN_IMPORT_ERROR: ImportError | None = _exc
else:
    CHAIN_IMPORT_ERROR = None

#: True when the reference state machine is importable, i.e. when this bridge can build its own chain.
CHAIN_AVAILABLE = Chain is not None

_DISABLED = (
    "modea.chain_bridge is DISABLED: the reference state machine package `chain` was not found "
    "(expected in {path}). It is part of the full development tree and is not shipped with the "
    "services alone. Nothing else in this package is affected -- only OnChainInference is "
    "unavailable, and it still works if an already-built state machine is passed explicitly as "
    "OnChainInference(chain=...)."
)

if not CHAIN_AVAILABLE:
    # Say it out loud rather than degrade silently: an operator who imports this module must learn
    # that on-chain settlement is off, and why, without having to read the source.
    warnings.warn(_DISABLED.format(path=_CHAIN_PKG), RuntimeWarning, stacklevel=2)

from . import canary as canary_mod
from .client import Client
from .ledger import Ledger
from .miner import Miner


@dataclass
class JobOutcome:
    job_id: str
    plaintext: str            # CLIENT side only (never on-chain)
    result_commit: str        # on-chain commitment to the encrypted result
    miner_liquid: int         # the miner's cumulative liquid on-chain balance
    miner_locked: int         # the vesting share (anti-dump)
    canary_token: str = ""    # watermark, when the job carries one — used to demonstrate a leak


class OnChainInference:
    """Orchestration: on-chain escrow -> confidential inference -> settlement -> commitment."""

    def __init__(self, chain: Chain | None = None, ledger: Ledger | None = None):
        if chain is None:
            if not CHAIN_AVAILABLE:
                raise RuntimeError(_DISABLED.format(path=_CHAIN_PKG)) from CHAIN_IMPORT_ERROR
            chain = Chain()
        self.chain = chain
        self.ledger = ledger if ledger is not None else Ledger()

    # ------------------------------------------------------------------ onboarding
    def onboard_miner(self, miner: Miner, *, operator: str, region: str,
                      stake: int, funder: str) -> None:
        """On-chain bond for the miner (it keeps its Mode A identity key for encryption)."""
        self.chain.register_miner(miner.miner_id, operator=operator, region=region,
                                  stake=stake, funder=funder)

    # ------------------------------------------------------------------ un job
    def run_job(self, *, job_id: str, client: Client, client_addr: str, miner: Miner,
                prompt: str, fee: int, with_canary: bool = False) -> JobOutcome:
        if not self.chain.is_active(miner.miner_id):
            raise ValueError(f"miner {miner.miner_id} is not bonded/active on-chain")

        # 1) (optional) canary: a unique watermark, committed on-chain, embedded in the prompt
        cana = canary_mod.make_canary() if with_canary else None
        prompt_eff = canary_mod.embed(prompt, cana) if cana else prompt
        canary_commit = cana.commit if cana else ""

        # 2) the client encrypts (EPHEMERAL key) -> commitment to the encrypted request
        sub, key = client.submit(job_id, miner.pub, prompt_eff)

        # 3) on-chain ESCROW: open_job sees ONLY commitments (ADR-011)
        self.chain.open_job(job_id, client=client_addr, miner_id=miner.miner_id, mode="A",
                            fee=fee, client_commit=sub.client_commit, canary_commit=canary_commit)

        # 4) CONFIDENTIAL inference (locked memory, no content ever logged)
        res = miner.handle_job(job_id, sub.client_eph_pk, sub.sealed_prompt)

        # 5) on-chain SETTLEMENT: settle_job pays the miner (with vesting) and records the demand
        self.chain.settle_job(job_id, result_commit=res.result_commit)

        # 6) commitment ledger (proof of confidentiality: no content)
        self.ledger.record(job_id, miner.miner_id, sub.client_commit,
                           res.result_commit, canary_commit)

        # 7) the client opens the result and strips the watermark, if any
        plaintext = canary_mod.strip(client.open_result(job_id, key, res.sealed_result))

        return JobOutcome(
            job_id=job_id, plaintext=plaintext, result_commit=res.result_commit,
            miner_liquid=self.chain.balances[miner.miner_id],
            miner_locked=self.chain.locked_balance(miner.miner_id),
            canary_token=(cana.token if cana else ""),
        )

    # ------------------------------------------------------------------ economics
    def claim_subsidy(self, miner_id: str, epoch: int) -> int:
        """Work subsidy, gated by non-recoverable demand (ADR-017), after the epoch closes."""
        return self.chain.claim_work_subsidy(miner_id, epoch)

    def report_leak(self, leaked_text: str, reporter: str) -> dict:
        """A canary watermark reappears -> objective proof -> the miner is slashed on-chain (ADR-012)."""
        m = canary_mod._MARKER_RE.search(leaked_text)
        if not m:
            raise ValueError("no canary watermark in the leaked text")
        token = m.group(1)
        return self.chain.report_leak(leaked_text=leaked_text, canary_token=token, reporter=reporter)

    # ------------------------------------------------------------------ confidentiality audit
    def privacy_dump(self) -> str:
        """Concatenates everything that is on-chain (jobs + ledger) to prove the absence of content."""
        import json
        jobs = {jid: {k: j[k] for k in ("client", "miner_id", "mode", "fee", "client_commit",
                                        "canary_commit", "result_commit", "state")}
                for jid, j in self.chain.jobs.items()}
        return json.dumps({"jobs": jobs, "ledger": self.ledger.dump_public()}, ensure_ascii=False)
