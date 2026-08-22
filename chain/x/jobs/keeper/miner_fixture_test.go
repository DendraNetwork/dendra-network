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

// seedJurorPool registers `n` miners that the pool an OpenJob freezes at the current height WILL
// contain, so that a fixture whose subject is not the jury can still open a job.
//
// Why it exists. `enoughEligibleMinersToVerify` demands `effectiveSlashFloor(p) + 1` eligible miners in
// the frozen pool -- five at the default parameters, because a zero `audit_min_quorum` selects the
// fallback floor of four and the primary is excluded from its own audit. Before that floor was derived
// from the function the draw consults, admission abstained at zero and these fixtures opened jobs into
// pools that could never supply a jury. They were testing expiry, reveal delay or duplicate ids in a
// world the chain now refuses to create.
//
// ⚠️ THE STAKE IS ZERO, AND THAT IS A MEASUREMENT ABOUT THE GUARD, NOT A CONVENIENCE. A miner with no
// stake satisfies BOTH predicates the admission floor counts -- pool membership and juror eligibility --
// while carrying no weight in the draw, so it fills the pool without ever being selected. That is what
// lets a fixture whose subject is not the jury keep the committee it built. Seeded at 1000 instead, the
// filler WON seats and five tests that had nothing to do with the jury started failing on who was drawn.
// The same property says something about the chain: the floor counts identities, not capacity, so five
// worthless registrations satisfy it. That predates this helper -- it was already true at a governed
// quorum -- but if eligibility ever gains a stake floor, this helper stops working, loudly, and whoever
// sees it should read this paragraph rather than raise the number.
// The seeds start high on purpose: fixtures that build their own committee use small ones, and a
// collision would silently replace one of THEIR miners instead of adding to the pool.
func seedJurorPool(t *testing.T, f *fixture, n int) []types.Miner {
	t.Helper()
	out := make([]types.Miner, 0, n)
	for i := 0; i < n; i++ {
		m := registeredMiner(t, byte(200+i), 0)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, m.MinerId, m))
		// Membership in a frozen pool is NOT a field of the miner: `minerInFrozenPool` reads
		// `MinerRegisteredHeight` and EXCLUDES a miner that has none, logging it as a security event.
		// A fixture that writes only the record is invisible to the pool -- measured, 0 eligible.
		require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, m.MinerId, 0))
		out = append(out, m)
	}
	return out
}
