"""Pont Mode A <-> state machine (Dendra L1).

Deux couches COMPLEMENTAIRES avancent en lockstep autour d'un job d'inference :
  - le **Ledger** (engagements, ADR-011) prouve la CONFIDENTIALITE : il ne contient que des
    hash, jamais de contenu ;
  - la **Chain** (state machine, ADR-018) porte l'ECONOMIE : escrow, paiement mineur 85 %
    (dont 30 % en vesting), demande NON-RECUPERABLE (ADR-017), subvention gatee, slash sur fuite.

La boucle d'inference confidentielle (client chiffre -> mineur infere en memoire verrouillee ->
resultat re-chiffre) reste celle du Mode A : le pont se contente d'**ouvrir l'escrow avant**,
de **regler apres**, et de n'ecrire que des **engagements**.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Pont vers le paquet `chain` (prototype/chain-modules), sans le copier.
_CHAIN_PKG = Path(__file__).resolve().parents[2] / "chain-modules"
if str(_CHAIN_PKG) not in sys.path:
    sys.path.insert(0, str(_CHAIN_PKG))

from chain import Chain                      # noqa: E402  (la state machine on-chain)

from . import canary as canary_mod
from .client import Client
from .ledger import Ledger
from .miner import Miner


@dataclass
class JobOutcome:
    job_id: str
    plaintext: str            # cote CLIENT uniquement (jamais on-chain)
    result_commit: str        # engagement on-chain du resultat chiffre
    miner_liquid: int         # solde liquide du mineur on-chain (cumule)
    miner_locked: int         # part en vesting (anti-dump)
    canary_token: str = ""    # marqueur (si job watermarke) — sert a demontrer une fuite


class OnChainInference:
    """Orchestration : escrow on-chain -> inference confidentielle -> reglement -> engagement."""

    def __init__(self, chain: Chain | None = None, ledger: Ledger | None = None):
        self.chain = chain if chain is not None else Chain()
        self.ledger = ledger if ledger is not None else Ledger()

    # ------------------------------------------------------------------ onboarding
    def onboard_miner(self, miner: Miner, *, operator: str, region: str,
                      stake: int, funder: str) -> None:
        """Bond on-chain du mineur (le mineur conserve sa cle d'identite Mode A pour le chiffrement)."""
        self.chain.register_miner(miner.miner_id, operator=operator, region=region,
                                  stake=stake, funder=funder)

    # ------------------------------------------------------------------ un job
    def run_job(self, *, job_id: str, client: Client, client_addr: str, miner: Miner,
                prompt: str, fee: int, with_canary: bool = False) -> JobOutcome:
        if not self.chain.is_active(miner.miner_id):
            raise ValueError(f"mineur {miner.miner_id} non bonde/inactif on-chain")

        # 1) (option) canari : marqueur unique, engage on-chain, insere dans le prompt
        cana = canary_mod.make_canary() if with_canary else None
        prompt_eff = canary_mod.embed(prompt, cana) if cana else prompt
        canary_commit = cana.commit if cana else ""

        # 2) le client chiffre (cle EPHEMERE) -> engagement de la requete chiffree
        sub, key = client.submit(job_id, miner.pub, prompt_eff)

        # 3) ESCROW on-chain : open_job ne voit QUE des engagements (ADR-011)
        self.chain.open_job(job_id, client=client_addr, miner_id=miner.miner_id, mode="A",
                            fee=fee, client_commit=sub.client_commit, canary_commit=canary_commit)

        # 4) inference CONFIDENTIELLE (memoire verrouillee, aucun log de contenu)
        res = miner.handle_job(job_id, sub.client_eph_pk, sub.sealed_prompt)

        # 5) REGLEMENT on-chain : settle_job paie le mineur (85 %/vesting) + enregistre la demande
        self.chain.settle_job(job_id, result_commit=res.result_commit)

        # 6) ledger d'engagements (preuve de confidentialite : aucun contenu)
        self.ledger.record(job_id, miner.miner_id, sub.client_commit,
                           res.result_commit, canary_commit)

        # 7) le client ouvre le resultat et retire le marqueur eventuel
        plaintext = canary_mod.strip(client.open_result(job_id, key, res.sealed_result))

        return JobOutcome(
            job_id=job_id, plaintext=plaintext, result_commit=res.result_commit,
            miner_liquid=self.chain.balances[miner.miner_id],
            miner_locked=self.chain.locked_balance(miner.miner_id),
            canary_token=(cana.token if cana else ""),
        )

    # ------------------------------------------------------------------ economie
    def claim_subsidy(self, miner_id: str, epoch: int) -> int:
        """Subvention 'travail' gatee par la demande non-recuperable (ADR-017), apres cloture d'epoque."""
        return self.chain.claim_work_subsidy(miner_id, epoch)

    def report_leak(self, leaked_text: str, reporter: str) -> dict:
        """Un marqueur canari reapparait -> preuve objective -> slash du mineur on-chain (ADR-012)."""
        m = canary_mod._MARKER_RE.search(leaked_text)
        if not m:
            raise ValueError("aucun marqueur canari dans le texte fuite")
        token = m.group(1)
        return self.chain.report_leak(leaked_text=leaked_text, canary_token=token, reporter=reporter)

    # ------------------------------------------------------------------ audit confidentialite
    def privacy_dump(self) -> str:
        """Concatene tout ce qui est 'on-chain' (jobs + ledger) -> sert a prouver l'absence de contenu."""
        import json
        jobs = {jid: {k: j[k] for k in ("client", "miner_id", "mode", "fee", "client_commit",
                                        "canary_commit", "result_commit", "state")}
                for jid, j in self.chain.jobs.items()}
        return json.dumps({"jobs": jobs, "ledger": self.ledger.dump_public()}, ensure_ascii=False)
