package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// deriveIDFor returns the miner id the chain will REQUIRE for a given signer address.
//
// Every fixture that registers a miner goes through this helper rather than through a literal string.
// A literal id in a test is exactly the thing the guard exists to refuse, so a suite still full of
// them would either be red forever or be testing a chain that no longer exists.
func deriveIDFor(t *testing.T, f *fixture, creator string) string {
	t.Helper()
	bz, err := f.addressCodec.StringToBytes(creator)
	require.NoError(t, err)
	id, err := types.DeriveMinerID(bz)
	require.NoError(t, err)
	return id
}

// THE GUARD, PROVEN IN BOTH DIRECTIONS.
//
// A guard only ever seen accepting is not known to refuse anything. The two halves below are the same
// message with one field changed: the derived id registers, every chosen id is refused.
func TestADR039MinerIDMustBeDerived(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("adr039Signer________________"))
	require.NoError(t, err)
	want := deriveIDFor(t, f, creator)

	// RED — a chosen identifier is refused, whatever it looks like.
	for _, chosen := range []string{
		"m-pc-01",             // a hostname, the leak the derivation closes
		"m1",                  // the old convention
		"",                    // empty is not a free pass
		"dm1notarealchecksum", // bech32-SHAPED but not the derived value
		want + "x",            // one character off the right answer
	} {
		_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{
			Creator: creator, MinerId: chosen, Operator: creator, Stake: 1000,
		})
		require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest, "chosen id %q must be refused", chosen)
		require.Contains(t, err.Error(), want, "the refusal must quote the expected id, so the operator does not have to reimplement the derivation to obey it")
	}

	// GREEN — the derived identifier registers.
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{
		Creator: creator, MinerId: want, Operator: creator, Stake: 1000,
	})
	require.NoError(t, err)
	got, err := f.keeper.Miner.Get(f.ctx, want)
	require.NoError(t, err)
	require.Equal(t, creator, got.Creator)
}

// ONE ADDRESS REGISTERS ONE MINER. This is a CONSEQUENCE of the derivation, not a separate rule, and
// it is asserted here because it is the behaviour change an operator will actually hit: running two
// GPUs now requires two accounts. It used to be possible to register any number of miners from a
// single key by picking a new string each time — which is also what made a slashed identity cheap to
// abandon and re-create.
func TestADR039OneAddressRegistersOneMiner(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("adr039SoloSigner____________"))
	require.NoError(t, err)
	id := deriveIDFor(t, f, creator)

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: id, Operator: creator, Stake: 1000})
	require.NoError(t, err)

	// The second attempt cannot even name a different miner: the id is not the caller's to choose.
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: id, Operator: creator, Stake: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	require.Contains(t, err.Error(), "index already set")

	// A DIFFERENT address gets a DIFFERENT miner: the restriction is per-key, not global.
	other, err := f.addressCodec.BytesToString([]byte("adr039OtherSigner___________"))
	require.NoError(t, err)
	otherID := deriveIDFor(t, f, other)
	require.NotEqual(t, id, otherID)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: other, MinerId: otherID, Operator: other, Stake: 1000})
	require.NoError(t, err)
}

// THE GUARD FIRES BEFORE THE BOND. The comment in CreateMiner claims this ordering; a claim about
// ordering that no test observes is a comment, not a property.
//
// The balance is read after a REFUSED create. If the identity check ever drifted below
// `SendCoinsFromAccountToModule`, the escrow would have run before the refusal and this assertion is
// where it shows — independently of whether the surrounding transaction happens to revert.
func TestADR039IdentityGuardPrecedesTheBond(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("adr039BondOrder_____________"))
	require.NoError(t, err)
	creatorBz, err := f.addressCodec.StringToBytes(creator)
	require.NoError(t, err)
	creatorAddr := sdk.AccAddress(creatorBz)

	start := sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10_000)))
	f.bank.setBalance(creatorAddr, start)

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{
		Creator: creator, MinerId: "m-pc-01", Operator: creator, Stake: 1000,
	})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	require.Equal(t, start.String(), f.bank.SpendableCoins(f.ctx, creatorAddr).String(),
		"the identity refusal moved funds: the guard has drifted below the bond escrow")
	require.True(t, f.bank.mod[types.ModuleName].IsZero(), "nothing must be escrowed by a refused registration")
}
