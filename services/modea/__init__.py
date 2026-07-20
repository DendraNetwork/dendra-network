"""Prototype Mode A — inference confidentielle (DEAI L1).

Implemente le flux du Mode A (ADR-011) avec les controles de securite de MODE-A-SECURITE :
chiffrement bout-en-bout a cles ephemeres, rien en clair on-chain (ledger = commitments),
canaries de detection de fuite (ADR-012), traitement ephemere + zeroisation cote mineur.

NB : prototype de reference (pas durci au niveau OS : mlock/sandbox/attestation sont decrits
dans MODE-A-SECURITE et seront ajoutes au client mineur de production).
"""
__all__ = ["crypto", "ledger", "canary", "inference", "miner", "client"]
