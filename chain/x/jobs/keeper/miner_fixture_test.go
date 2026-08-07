package keeper_test

import (
	"testing"

	"github.com/cosmos/cosmos-sdk/types/bech32"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// registeredMiner builds a genesis miner entry that the chain could ACTUALLY have produced.
//
// Fixtures used to write `{MinerId: "m1"}` — no creator, a chosen name. Since ADR-039 that is a state
// no code path can reach: `CreateMiner` derives the identifier from the signer and stores that signer
// as `Creator`, and `UpdateMiner` refuses a different one, so every stored miner satisfies
// `MinerId == DeriveMinerID(bytes(Creator))`. A fixture that violates it is not a stricter test, it is
// a test of a state the machine cannot be in — and once the genesis door started enforcing the same
// rule as the message door, those fixtures were the only thing that could not import.
//
// The seed makes each fixture distinct and deterministic. The returned identifier is opaque by
// construction (a hash), which is the point: an operator's chosen name is exactly what must not
// appear in the registry.
func registeredMiner(t *testing.T, seed byte, stake uint64) types.Miner {
	t.Helper()
	raw := make([]byte, 20)
	for i := range raw {
		raw[i] = seed + byte(i)
	}
	addr, err := bech32.ConvertAndEncode("dendra", raw)
	require.NoError(t, err)
	id, err := types.DeriveMinerID(raw)
	require.NoError(t, err)
	return types.Miner{MinerId: id, Creator: addr, Operator: addr, Stake: stake}
}
