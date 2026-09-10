"""Mode A prototype — confidential inference on the L1.

Implements the Mode A flow (ADR-011) with the security controls described in MODE-A-SECURITE:
end-to-end encryption with ephemeral keys, nothing in clear on-chain (the ledger holds commitments only),
canaries that detect leaks (ADR-012), and ephemeral processing with zeroisation on the miner side.

This is a reference prototype and is not OS-hardened: mlock, sandboxing and attestation are described in
MODE-A-SECURITE and belong to the production miner client.
"""
__all__ = ["crypto", "ledger", "canary", "inference", "miner", "client"]
