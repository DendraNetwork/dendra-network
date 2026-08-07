// Command dendra-vrf is a small utility that produces and verifies ECVRF proofs on the MINER side,
// reusing EXACTLY the chain package (github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf): a proof
// produced here necessarily verifies on-chain (same ECVRF-EDWARDS25519-SHA512-ELL2 suite). It avoids
// reimplementing ECVRF in Python. The miner runs `keygen` once (keeps sk, anchors pk through
// create-miner), then `prove <sk> <challenge>` at each availability challenge (the proof travels in
// MsgProveAvailability.vrf_proof).
//
// usage:
//
//	dendra-vrf keygen                          -> "<sk_hex>\t<pk_hex>"  (sk SECRET, pk to be anchored)
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
		if len(os.Args) != 4 {
			usage()
		}
		pi, err := vrf.Prove(mustHex(os.Args[2]), []byte(os.Args[3]))
		if err != nil {
			fail(err)
		}
		fmt.Println(hex.EncodeToString(pi))
	case "pubkey":
		if len(os.Args) != 3 {
			usage()
		}
		sk := mustHex(os.Args[2])
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
