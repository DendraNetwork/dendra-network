package types

import (
	"crypto/sha256"
	"fmt"

	"github.com/cosmos/cosmos-sdk/types/bech32"
)

// MinerIDHRP is the bech32 human-readable part of a derived miner identifier.
//
// It is DISTINCT from the account prefix on purpose. A miner id and an account address are not
// interchangeable, and reusing the account hrp would let one be pasted where the other is expected
// with the checksum still validating — the failure would surface as a "miner not found" far from the
// paste. A different hrp makes the confusion fail at the decoder.
const MinerIDHRP = "dm"

// MinerIDDomain separates this hash from every other sha256 taken over the same address bytes.
//
// Without a domain prefix, `sha256(addr)` computed anywhere else in the protocol — a commit key, a
// seed, a future index — would collide with a miner id by construction rather than by accident. The
// prefix is FIXED-LENGTH (18 bytes), so `domain || addr` admits exactly one parse and no address can
// be crafted to shift the boundary.
//
// The `/v1` is load-bearing: changing the derivation later must change this string, so that old and
// new identifiers cannot be silently equal for some inputs.
const MinerIDDomain = "dendra/miner-id/v1"

// MinerIDPayloadLen is the number of leading sha256 bytes carried by the identifier.
//
// 10 bytes = 80 bits. Collision resistance here is SECOND-PREIMAGE, not birthday: the attacker does
// not need any two colliding ids, it needs one that collides with an EXISTING registered miner in
// order to squat its identity, and it must hold the private key of the address that produces it.
// 2^80 targeted work per victim, for the right to reuse an id whose bond and payee journal it still
// does not control, is not a path anyone takes. The 25-character result stays readable in a log line,
// which is what an operator actually copies.
const MinerIDPayloadLen = 10

// DeriveMinerID computes the canonical miner identifier for an account address.
//
// It is a PURE function of the address bytes: no clock, no height, no chain state, no randomness.
// That is the property the CreateMiner guard rests on — anyone can recompute the expected id offline
// from an address alone, so the chain refusing a miner id is verifiable by the operator without
// querying anything.
//
// The identifier is DERIVED rather than chosen because a self-chosen id is a free-text field written
// by the party being identified: it can be made to impersonate another operator, to carry a hostname,
// or to be re-registered under a different key after an eviction. Derivation removes the choice
// entirely — one address, one identifier, forever, and the binding is checkable by anyone.
//
// addrBytes are the RAW account bytes (what the address codec's StringToBytes returns), never the
// bech32 string. Hashing the string instead would make the id depend on the account prefix, so the
// same key would derive a different miner id on a chain whose prefix changed.
func DeriveMinerID(addrBytes []byte) (string, error) {
	// An empty address is not an address. Deriving from it would return a well-formed identifier for
	// "nobody" — a string that passes every downstream check while binding to no key. Fail closed:
	// the caller validates the signer first, so this is defence in depth, not a reachable path.
	if len(addrBytes) == 0 {
		return "", fmt.Errorf("cannot derive a miner id from an empty address")
	}
	h := sha256.New()
	h.Write([]byte(MinerIDDomain))
	h.Write(addrBytes)
	sum := h.Sum(nil)
	return bech32.ConvertAndEncode(MinerIDHRP, sum[:MinerIDPayloadLen])
}

// MinerIDForAccount derives the identifier for a bech32 ACCOUNT address, decoding it first.
//
// It exists so that the rule has exactly one implementation. Callers that hold a bech32 string —
// genesis validation, the simulation's genesis generator, anything reading a config — would otherwise
// each write their own decode-then-derive, and the day one of them decodes differently the chain and
// its checkers would disagree about who a miner is.
//
// bech32.DecodeAndConvert rather than an address codec: this runs during genesis validation, where no
// codec is wired, and it must not depend on the SDK's global prefix configuration having been set.
func MinerIDForAccount(addr string) (string, error) {
	_, addrBytes, err := bech32.DecodeAndConvert(addr)
	if err != nil {
		return "", fmt.Errorf("creator %q is not a decodable bech32 address: %w", addr, err)
	}
	return DeriveMinerID(addrBytes)
}

// ValidateMinerIdentity checks that a registry entry carries the identifier its creator derives.
//
// THE RULE HAD ONLY ONE DOOR, AND THE REGISTRY HAS TWO. `CreateMiner` enforced derivation on the
// message path while `InitGenesis` wrote `MinerMap` straight to the store, and `GenesisState.Validate`
// checked nothing but duplicate keys. So a genesis — hand-written, or exported from a chain that
// predates this rule — could re-plant `m-pc-01`, a hostname, or any chosen string, permanently and
// world-readable. That is the exact impersonation, leakage and re-entry the message guard exists to
// close: covering one of two doors is not a guarantee, it is a guarantee-shaped comment.
//
// This is cheap TODAY and could not be later. The launched genesis carries an EMPTY MinerMap, so no
// existing artifact becomes invalid by adding it; the same check on a chain that had already shipped
// miners under free-text ids would be a migration with real holders behind it.
//
// IT ALSO ENFORCES "ONE ADDRESS, ONE MINER" FOR FREE. Two entries sharing a creator derive the SAME
// identifier, so the duplicate-index check that already exists refuses them — a property the message
// path gets from the store's uniqueness and the genesis path had no way to obtain.
//
// ABSENCE IS A REFUSAL, NOT A PASS. proto3 omits an empty `creator`, so an entry that never had one
// is indistinguishable from one whose field was dropped. On a security criterion the missing value
// must be an error: were it tolerated, the cheapest way past this guard would be to omit the field.
func ValidateMinerIdentity(minerID, creator string) error {
	if creator == "" {
		return fmt.Errorf("miner %q carries no creator: its identifier cannot be checked against the address it must be derived from", minerID)
	}
	want, err := MinerIDForAccount(creator)
	if err != nil {
		return fmt.Errorf("miner %q: %w", minerID, err)
	}
	if minerID != want {
		return fmt.Errorf("miner %q is NOT derived from its creator %q (expected %q): a genesis may not register an identifier that CreateMiner would refuse (ADR-039)", minerID, creator, want)
	}
	return nil
}
