"""Mode A prototype — confidential inference on the L1.

Implements the Mode A flow (ADR-011) with the security controls described in
the Mode A security note (development repository):
end-to-end encryption with ephemeral keys, nothing unencrypted on chain (the ledger holds commitments only),
canaries that detect leaks (ADR-012), and ephemeral processing with zeroisation on the miner side.

This is a reference prototype and is not OS-hardened: mlock, sandboxing and attestation are described in
the Mode A security note (development repository) and belong to the production miner client.
"""
__all__ = ["crypto", "ledger", "canary", "inference", "miner", "client"]
