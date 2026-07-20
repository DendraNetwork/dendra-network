// Commande dendra-vrf — petit utilitaire pour produire/vérifier des preuves ECVRF côté MINEUR, en
// réutilisant EXACTEMENT le paquet de la chaîne (dendra/x/jobs/vrf) : la preuve produite ici se vérifie
// donc forcément on-chain (même suite ECVRF-EDWARDS25519-SHA512-ELL2). Évite d'implémenter l'ECVRF en
// Python. Le mineur : `keygen` une fois (garde sk, ancre pk via create-miner) ; `prove <sk> <defi>` à
// chaque défi de disponibilité (la preuve part dans MsgProveAvailability.vrf_proof).
//
// usage :
//   dendra-vrf keygen                          -> "<sk_hex>\t<pk_hex>"  (sk SECRET, pk à ancrer)
//   dendra-vrf prove  <sk_hex> <alpha>         -> "<proof_hex>"          (alpha = le défi courant)
//   dendra-vrf pubkey <sk_hex>                 -> "<pk_hex>"
//   dendra-vrf verify <pk_hex> <alpha> <pi_hex>-> "VALID <beta_hex>" | "INVALID"
package main

import (
	"encoding/hex"
	"fmt"
	"os"

	"dendra/x/jobs/vrf"
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
		fail(fmt.Errorf("hex invalide: %w", err))
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
			fail(fmt.Errorf("sk invalide (%d octets, attendu %d)", len(sk), vrf.PrivateKeySize))
		}
		fmt.Println(hex.EncodeToString(sk[32:])) // Ed25519 : sk = seed||pub
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
