# dendra-vrf

`dendra-vrf` produces and verifies ECVRF proofs on the miner side. It reuses the chain's own VRF
package (`github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf`), so a proof produced here
verifies on-chain (the same ECVRF-EDWARDS25519-SHA512-ELL2 suite) and the miner ships no ECVRF
implementation of its own.

The miner runs `keygen` once: it keeps the secret key and anchors the public key through
`create-miner`. At each availability challenge it runs `prove`; the proof travels on-chain in
`MsgProveAvailability.vrf_proof`.

## Build

```bash
cd chain && go build ./cmd/dendra-vrf
```

## Sub-commands

```
dendra-vrf keygen                            -> "<sk_hex>\t<pk_hex>"  (sk is secret, pk is anchored)
dendra-vrf prove  <sk_hex> <alpha>           -> "<proof_hex>"          (alpha = the current challenge)
dendra-vrf pubkey <sk_hex>                   -> "<pk_hex>"
dendra-vrf verify <pk_hex> <alpha> <pi_hex>  -> "VALID <beta_hex>" | "INVALID"
```

`verify` prints `VALID` followed by the beta hash when the proof checks, and `INVALID` with exit
code 1 when it does not.

## The secret key and `DENDRA_VRF_SK`

A secret passed on the command line is readable by any local process: argv shows up in `ps` and
in `/proc/<pid>/cmdline`, which are world-readable. `DENDRA_VRF_SK` supplies the secret instead,
and keeps it out of argv:

```
DENDRA_VRF_SK=<sk_hex> dendra-vrf prove <alpha>
DENDRA_VRF_SK=<sk_hex> dendra-vrf pubkey
```

When the variable is set, the key never reaches the command line and the remaining positionals
shift left by one. The positional form still works, for callers that pass the key as an argument; there the key is
visible in the process list of the machine, so the published scripts pass it through
`DENDRA_VRF_SK`. The arity is checked before the key is decoded, so a call that forgot the key
fails on its argument count rather than on a value that was never a key.

## Exit codes

- 0: the command succeeded.
- 1: the operation failed (bad hex, bad key length, an invalid proof).
- 2: usage.
