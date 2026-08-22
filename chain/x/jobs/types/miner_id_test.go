package types_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// FROZEN VECTORS — the expected strings below were produced by an INDEPENDENT implementation
// (a Python BIP-173 bech32 encoder, itself first checked against the BIP-173 reference vector
// `bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4`), not by running DeriveMinerID and pasting its
// output. That distinction is the entire point of this test.
//
// A test that calls the function and compares against the function — or against a second copy of the
// same formula written in Go — is green for every possible implementation, including a wrong one. It
// would not have caught a swapped hrp, a bech32m checksum constant, a truncation at 8 or 16 bytes, or
// a domain prefix appended instead of prepended. Each of those changes every string below.
//
// These identifiers are CONSENSUS: they are the storage keys of the miner registry. Changing the hrp,
// the domain string, the payload length, or the hash makes every existing miner unaddressable. This
// table is therefore a refusal, not a description — if it goes red, the answer is a chain reset and a
// version bump on MinerIDDomain, never an edit of the expected column.
func TestDeriveMinerIDFrozenVectors(t *testing.T) {
	cases := []struct {
		nom  string
		addr []byte
		want string
	}{
		{
			nom:  "20 zero bytes",
			addr: make([]byte, 20),
			want: "dm1enc90u2ammtjjp3f07rll3",
		},
		{
			nom:  "20 incrementing bytes 0x00..0x13",
			addr: []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19},
			want: "dm1xrltuf5purwu7p8gtp9p5k",
		},
		{
			nom: "32 bytes of 0xff",
			addr: []byte{
				0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
				0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
				0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
				0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
			},
			want: "dm1g7ldhw2zzyzkme2hd06qhx",
		},
		{
			// The 28-byte padded address the keeper fixtures build. Kept here so the vector table and
			// the keeper suite exercise the same input.
			nom:  "keeper fixture address signerAddr__________________",
			addr: []byte("signerAddr__________________"),
			want: "dm1jxkgmdveesv0vpq6cd573m",
		},
		{
			nom:  "single byte 0x01",
			addr: []byte{0x01},
			want: "dm1ly0h85ay9l6h4tepqqtx7x",
		},
	}
	for _, c := range cases {
		t.Run(c.nom, func(t *testing.T) {
			got, err := types.DeriveMinerID(c.addr)
			require.NoError(t, err)
			require.Equal(t, c.want, got,
				"CONSENSUS-BREAKING: the miner id derivation moved. Every registered miner keyed under the old id becomes unreachable. Do NOT update this expected value without a chain reset and a bump of types.MinerIDDomain.")
		})
	}
}

// The constants are frozen too, and separately from the vectors.
//
// The vectors above already fail if any of these moves, but they fail as an opaque string mismatch.
// Naming the three quantities makes the failure say WHICH knob was turned, and puts the version
// discipline (`/v1`) in front of whoever turns it.
func TestMinerIDConstantsAreFrozen(t *testing.T) {
	require.Equal(t, "dm", types.MinerIDHRP, "the miner id hrp is consensus: it prefixes every registry key")
	require.Equal(t, "dendra/miner-id/v1", types.MinerIDDomain, "changing the derivation REQUIRES bumping this version string, so old and new ids can never be silently equal")
	require.Equal(t, 10, types.MinerIDPayloadLen, "the payload length is consensus: it changes every identifier")
}

// An empty address derives NOTHING. It must not derive a well-formed id for "nobody".
func TestDeriveMinerIDRefusesEmptyAddress(t *testing.T) {
	got, err := types.DeriveMinerID(nil)
	require.Error(t, err)
	require.Empty(t, got)

	got, err = types.DeriveMinerID([]byte{})
	require.Error(t, err)
	require.Empty(t, got)
}

// Two distinct addresses must not share an identifier, and one address must always give the same one.
// This is the weak property — the vectors carry the strong one — but it is the property the
// CreateMiner guard states out loud, so it is asserted rather than assumed.
func TestDeriveMinerIDIsDeterministicAndInjectiveOnSamples(t *testing.T) {
	a := []byte("addressAlpha________________")
	b := []byte("addressBeta_________________")

	idA1, err := types.DeriveMinerID(a)
	require.NoError(t, err)
	idA2, err := types.DeriveMinerID(a)
	require.NoError(t, err)
	require.Equal(t, idA1, idA2, "derivation must be pure: same address, same identifier")

	idB, err := types.DeriveMinerID(b)
	require.NoError(t, err)
	require.NotEqual(t, idA1, idB, "distinct addresses must not collide")

	// A one-byte difference at the END of the address must change the identifier. If the domain
	// prefix were appended rather than prepended, or the hash truncated before the address was
	// consumed, this is where it would show.
	c := append(append([]byte{}, a...), 'x')
	idC, err := types.DeriveMinerID(c)
	require.NoError(t, err)
	require.NotEqual(t, idA1, idC)
}
