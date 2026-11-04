package types_test

import (
	"strings"
	"testing"

	"github.com/cosmos/cosmos-sdk/types/bech32"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The registry has two write paths, and the derived-identity check first guarded only one.
//
// `CreateMiner` refuses a chosen identifier. `InitGenesis` wrote `MinerMap` straight to the store and
// `GenesisState.Validate` looked only for duplicate keys, so a genesis could register `m-pc-01`, a
// hostname, or any string the message path would have refused, permanently and world-readable: the
// impersonation, leakage and re-entry the message guard exists to close.
//
// These cases exercise the rule in both directions: a guard only ever seen accepting has never been
// seen refusing.
//
// The launched genesis carries an empty MinerMap (measured on the live chain), which is why the rule
// could be added without invalidating any shipped artifact, and why it had to be added before the
// first operators registered under identifiers of their own choosing.

// accountAddress builds a valid account address from raw bytes without depending on the SDK's global
// prefix configuration, which is not initialised inside a `types` package test.
func accountAddress(t *testing.T, raw []byte) string {
	t.Helper()
	a, err := bech32.ConvertAndEncode("dendra", raw)
	require.NoError(t, err)
	return a
}

// expectedID returns the identifier the chain will require for this address.
func expectedID(t *testing.T, raw []byte) string {
	t.Helper()
	id, err := types.DeriveMinerID(raw)
	require.NoError(t, err)
	return id
}

// minerEntry builds a registry entry the chain could actually have produced, for fixtures elsewhere
// in this package. The same seed gives the same entry, which keeps the duplication case a duplication
// case instead of an identity failure in disguise.
func minerEntry(t *testing.T, seed byte) types.Miner {
	t.Helper()
	raw := make([]byte, 20)
	for i := range raw {
		raw[i] = seed + byte(i)
	}
	return types.Miner{MinerId: expectedID(t, raw), Creator: accountAddress(t, raw)}
}

func TestGenesisRefusesAChosenMinerIdentifier(t *testing.T) {
	raw := []byte{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a,
		0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x11, 0x12, 0x13, 0x14}
	addr := accountAddress(t, raw)
	good := expectedID(t, raw)

	cases := []struct {
		name     string
		minerID  string
		creator  string
		accepted bool
		wants    string // fragment the refusal must carry
	}{
		{
			name:    "known-good: the identifier is the one its creator derives",
			minerID: good, creator: addr, accepted: true,
		},
		{
			// The case that motivates the rest: the hostname the message path refuses,
			// brought back through the genesis path.
			name:    "known-bad: a hostname planted by the genesis",
			minerID: "m-pc-01", creator: addr,
			wants: "not derived from its creator",
		},
		{
			// The zero rule: proto3 omits an empty string, so "never had a creator" and "field
			// dropped" arrive identical. On a security check absence must be an error, otherwise the
			// cheapest way past this guard is to leave the field out.
			name:    "known-bad: no creator at all — absence is not compliance",
			minerID: good, creator: "",
			wants: "carries no creator",
		},
		{
			name:    "known-bad: undecodable creator",
			minerID: good, creator: "dendra1thisisnotanaddress",
			wants: "not a decodable bech32 address",
		},
		{
			// An identifier derived from another address is well formed, passes the bech32 decoder,
			// and does not belong to the account carrying it: that is impersonation, not a typo.
			name:    "known-bad: an identifier derived from a different address",
			minerID: expectedID(t, append([]byte{0xff}, raw[1:]...)), creator: addr,
			wants: "not derived from its creator",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := types.ValidateMinerIdentity(c.minerID, c.creator)
			if c.accepted {
				require.NoError(t, err, "a correctly derived identifier must be accepted")
				return
			}
			require.Error(t, err, "this identifier must be refused")
			require.Contains(t, err.Error(), c.wants,
				"the refusal must say why: a generic message forces the operator to guess")
		})
	}
}

// The refusal quotes the expected identifier. A message that only says "wrong id" makes the operator
// reimplement the derivation to find out what to write, when the value is already computed at the
// moment of refusal. The message path already makes the same promise.
func TestTheRefusalQuotesTheExpectedIdentifier(t *testing.T) {
	raw := []byte{0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f, 0x30, 0x31, 0x32, 0x33,
		0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d}
	err := types.ValidateMinerIdentity("m-pc-01", accountAddress(t, raw))
	require.Error(t, err)
	require.Contains(t, err.Error(), expectedID(t, raw),
		"the message must carry the identifier to use, not merely report the mismatch")
	require.True(t, strings.Contains(err.Error(), "CreateMiner would refuse"),
		"the message must name the rule it applies, so it can be found again")
}

// The rule has to live in GenesisState.Validate as well as in the isolated function, since Validate is
// what `ValidateGenesis` calls.
func TestValidateGenesisCarriesTheRule(t *testing.T) {
	raw := []byte{0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
		0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x4f, 0x50, 0x51, 0x52, 0x53}
	addr := accountAddress(t, raw)

	// The genesis actually launched has no miners. It must stay valid: that is what made this rule
	// free to add, and it shows no shipped artifact was invalidated.
	empty := types.DefaultGenesis()
	require.Empty(t, empty.MinerMap)
	require.NoError(t, empty.Validate(), "the launched genesis (empty MinerMap) must remain valid")

	good := types.DefaultGenesis()
	good.MinerMap = []types.Miner{{MinerId: expectedID(t, raw), Creator: addr, Region: "eu", Stake: 1}}
	require.NoError(t, good.Validate())

	bad := types.DefaultGenesis()
	bad.MinerMap = []types.Miner{{MinerId: "m-pc-01", Creator: addr, Region: "eu", Stake: 1}}
	require.Error(t, bad.Validate(), "ValidateGenesis must refuse a chosen identifier")

	// A property that comes for free, "one address, one miner": two entries sharing a creator derive
	// the same identifier, so the duplicate check that already existed refuses them. The message path
	// got this property from the store's uniqueness; the genesis path had no way to obtain it.
	twice := types.DefaultGenesis()
	id := expectedID(t, raw)
	twice.MinerMap = []types.Miner{
		{MinerId: id, Creator: addr, Region: "eu", Stake: 1},
		{MinerId: id, Creator: addr, Region: "us", Stake: 2},
	}
	require.Error(t, twice.Validate(), "one address cannot carry two miners")
}
