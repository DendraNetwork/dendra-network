package types

import (
	"crypto/sha256"
	"fmt"

	"github.com/cosmos/cosmos-sdk/types/bech32"
)

// MinerIDHRP is the bech32 human-readable part of a derived miner identifier.
//
// It differs from the account prefix on purpose. A miner id and an account address are not
// interchangeable, and reusing the account hrp would let one be pasted where the other is expected
// with the checksum still validating; the failure would surface as a "miner not found" far from the
// paste. A different hrp makes the confusion fail at the decoder.
const MinerIDHRP = "dm"

// MinerIDDomain separates this hash from every other sha256 taken over the same address bytes.
//
// Without a domain prefix, `sha256(addr)` computed anywhere else in the protocol (a commit key, a
// seed, a future index) would collide with a miner id by construction. The prefix has a fixed length
// (18 bytes), so `domain || addr` admits exactly one parse and no address can be crafted to shift the
// boundary.
//
// The `/v1` matters: changing the derivation later must change this string, so that old and new
// identifiers cannot be silently equal for some inputs.
const MinerIDDomain = "dendra/miner-id/v1"

// MinerIDPayloadLen is the number of leading sha256 bytes carried by the identifier.
//
// 10 bytes = 80 bits. The relevant resistance is to second preimages, not birthday collisions: the
// attacker needs an id that collides with an existing registered miner in order to squat its identity,
// and must hold the private key of the address that produces it. 2^80 targeted work per victim, for
// the right to reuse an id whose bond and payee journal it still does not control, is not a path
// anyone takes. The 25-character result stays readable in a log line, which is what an operator
// copies.
const MinerIDPayloadLen = 10

// DeriveMinerID computes the canonical miner identifier for an account address.
//
// It is a pure function of the address bytes: no clock, no height, no chain state, no randomness.
// The CreateMiner guard rests on that: anyone can recompute the expected id offline from an address
// alone, so an operator can verify a refusal without querying anything.
//
// The identifier is derived rather than chosen because a self-chosen id is a free-text field written
// by the party being identified: it can impersonate another operator, carry a hostname, or be
// registered again under a different key after an eviction. Derivation removes the choice: one
// address, one identifier, forever, and anyone can check the binding.
//
// addrBytes are the raw account bytes (what the address codec's StringToBytes returns), never the
// bech32 string. Hashing the string would make the id depend on the account prefix, so the same key
// would derive a different miner id on a chain whose prefix changed.
func DeriveMinerID(addrBytes []byte) (string, error) {
	// An empty address is not an address. Deriving from it would return a well-formed identifier for
	// "nobody", a string that passes every downstream check while binding to no key. Fail closed:
	// the caller validates the signer first, so this is defence in depth and not a reachable path.
	if len(addrBytes) == 0 {
		return "", fmt.Errorf("cannot derive a miner id from an empty address")
	}
	h := sha256.New()
	h.Write([]byte(MinerIDDomain))
	h.Write(addrBytes)
	sum := h.Sum(nil)
	return bech32.ConvertAndEncode(MinerIDHRP, sum[:MinerIDPayloadLen])
}

// MinerIDForAccount derives the identifier for a bech32 account address, decoding it first.
//
// It keeps the rule to exactly one implementation. Callers that hold a bech32 string (genesis
// validation, the simulation's genesis generator, anything reading a config) would otherwise each
// write their own decode-then-derive, and the day one of them decoded differently the chain and its
// checkers would disagree about who a miner is.
//
// It uses bech32.DecodeAndConvert rather than an address codec because it runs during genesis
// validation, where no codec is wired, and must not depend on the SDK's global prefix configuration.
func MinerIDForAccount(addr string) (string, error) {
	_, addrBytes, err := bech32.DecodeAndConvert(addr)
	if err != nil {
		return "", fmt.Errorf("creator %q is not a decodable bech32 address: %w", addr, err)
	}
	return DeriveMinerID(addrBytes)
}

// ValidateMinerIdentity checks that a registry entry carries the identifier its creator derives.
//
// The registry has two write paths and the rule must cover both. `CreateMiner` enforces derivation
// on the message path, while `InitGenesis` writes `MinerMap` straight to the store, and a check of
// duplicate keys alone lets any identifier through. A genesis (hand-written, or exported from a
// chain older than this rule) could otherwise plant `m-pc-01`, a hostname, or any chosen string,
// permanently and world-readable: the impersonation, leakage and re-entry the message guard exists
// to close.
//
// Adding it was cheap only because the launched genesis carries an empty MinerMap, so no existing
// artifact becomes invalid; on a chain that had already shipped miners under free-text ids it would
// be a migration with real holders behind it.
//
// It also enforces "one address, one miner". Two entries sharing a creator derive the same identifier,
// so the existing duplicate-index check refuses them, a property the message path gets from the
// store's uniqueness and the genesis path had no way to obtain.
//
// A missing creator is refused. proto3 omits an empty `creator`, so an entry that never had one cannot
// be told apart from one whose field was dropped, and on a security check the missing value must be an
// error; otherwise omitting the field would be the cheapest way past this guard.
func ValidateMinerIdentity(minerID, creator string) error {
	if creator == "" {
		return fmt.Errorf("miner %q carries no creator: its identifier cannot be checked against the address it must be derived from", minerID)
	}
	want, err := MinerIDForAccount(creator)
	if err != nil {
		return fmt.Errorf("miner %q: %w", minerID, err)
	}
	if minerID != want {
		return fmt.Errorf("miner %q is not derived from its creator %q (expected %q): a genesis may not register an identifier that CreateMiner would refuse", minerID, creator, want)
	}
	return nil
}
