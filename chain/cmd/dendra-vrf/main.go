// Command dendra-vrf produces and verifies ECVRF proofs on the miner side. It reuses the chain package
// itself (github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf), so a proof produced here verifies
// on-chain (same ECVRF-EDWARDS25519-SHA512-ELL2 suite) and the miner needs no ECVRF implementation of
// its own. The miner runs `keygen` once (keeps sk, anchors pk through create-miner), then
// `prove <sk> <challenge>` at each availability challenge (the proof travels in
// MsgProveAvailability.vrf_proof).
//
// usage:
//
//	dendra-vrf keygen                          -> "<sk_hex>\t<pk_hex>"  (sk is secret, pk is anchored)
//	dendra-vrf prove  <sk_hex> <alpha>         -> "<proof_hex>"          (alpha = the current challenge)
//	dendra-vrf pubkey <sk_hex>                 -> "<pk_hex>"
//	dendra-vrf verify <pk_hex> <alpha> <pi_hex>-> "VALID <beta_hex>" | "INVALID"
package main

import (
	"encoding/hex"
	"fmt"
	"os"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

func usage() {
	fmt.Fprintln(os.Stderr, "usage: dendra-vrf keygen | prove <sk_hex> <alpha> | pubkey <sk_hex> | verify <pk_hex> <alpha> <proof_hex>")
	fmt.Fprintln(os.Stderr, "       DENDRA_VRF_SK=<sk_hex> supplies the secret instead, and keeps it out of argv:")
	fmt.Fprintln(os.Stderr, "       DENDRA_VRF_SK=... dendra-vrf prove <alpha> | DENDRA_VRF_SK=... dendra-vrf pubkey")
	os.Exit(2)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "dendra-vrf:", err)
	os.Exit(1)
}

func mustHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		fail(fmt.Errorf("invalid hex: %w", err))
	}
	return b
}

// secretAndArgs returns the secret key and the positionals that follow it.
//
// A secret passed on the command line is readable by any local process: argv shows up in `ps` and in
// /proc/<pid>/cmdline, which are world-readable. DENDRA_VRF_SK is therefore read first, and when it
// is set the key never reaches argv.
//
// The positional form still works on purpose: operator scripts pass the key that way, and breaking
// them would trade a disclosure nobody can currently exploit for a failure that stops anchoring
// outright. When the variable is set the key is not on the command line, so the remaining positionals
// shift left by one; callers read them from the returned slice, not by index. `want` is how many
// positionals must follow the secret. The arity is checked before the key is decoded: otherwise a
// call that forgot the key would decode its first positional as one, and the operator would get
// "invalid hex" about a value that was never a key, or, worse, an argument that happens to be valid
// hex of the right length would be accepted as the secret.
func secretAndArgs(pos, want int) ([]byte, []string) {
	if v := os.Getenv("DENDRA_VRF_SK"); v != "" {
		if len(os.Args) != pos+want {
			usage()
		}
		return mustHex(v), os.Args[pos:]
	}
	if len(os.Args) != pos+1+want {
		usage()
	}
	return mustHex(os.Args[pos]), os.Args[pos+1:]
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "keygen":
		pk, sk, err := vrf.GenerateKey(nil)
		if err != nil {
			fail(err)
		}
		fmt.Printf("%s\t%s\n", hex.EncodeToString(sk), hex.EncodeToString(pk))
	case "prove":
		sk, rest := secretAndArgs(2, 1)
		pi, err := vrf.Prove(sk, []byte(rest[0]))
		if err != nil {
			fail(err)
		}
		fmt.Println(hex.EncodeToString(pi))
	case "pubkey":
		sk, _ := secretAndArgs(2, 0)
		if len(sk) != vrf.PrivateKeySize {
			fail(fmt.Errorf("invalid sk (%d bytes, expected %d)", len(sk), vrf.PrivateKeySize))
		}
		fmt.Println(hex.EncodeToString(sk[32:])) // Ed25519: sk = seed||pub
	case "verify":
		if len(os.Args) != 5 {
			usage()
		}
		ok, beta := vrf.Verify(mustHex(os.Args[2]), []byte(os.Args[3]), mustHex(os.Args[4]))
		if !ok {
			fmt.Println("INVALID")
			os.Exit(1)
		}
		fmt.Printf("VALID %s\n", hex.EncodeToString(beta))
	default:
		usage()
	}
}
