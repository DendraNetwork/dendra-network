package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// RunEpoch reads the GOVERNABLE on-chain parameters (emission rate and epoch_blocks) instead of
// hard-coded constants. The fallback to the defaults is covered by the neighbouring emission tests.
func TestRunEpochGovernableParams(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 1_000_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 0))
	f.bank.supply = 10_000_000_000000 // demand 0 -> work flow gated to 0

	// GOVERNED PARAMS: release 10% (instead of 22%), avail 30%, one epoch every 50 blocks.
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(1000, 5000, 3000, 15000, 50)))

	// height 40 < governed epoch_blocks (50) -> nothing yet
	ctx40 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(40)
	require.NoError(t, f.keeper.RunEpoch(ctx40))
	r0, err := f.keeper.Reserve.Get(ctx40)
	require.NoError(t, err)
	require.Equal(t, reserve, r0, "governed epoch_blocks (50) honored: nothing happens at height 40")

	// height 60 > 50 -> epoch triggered at the governed RATE (10%)
	ctx60 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(60)
	require.NoError(t, f.keeper.RunEpoch(ctx60))
	r1, err := f.keeper.Reserve.Get(ctx60)
	require.NoError(t, err)
	// release 10% = 100k udndr; work gated to 0; avail 30% = 30k; security = remainder = 20k; released = 50k
	require.Equal(t, reserve-50_000_000000, r1, "governed emission rate (10%) applied")
}
