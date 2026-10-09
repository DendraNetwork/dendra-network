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

// A miner bond is a REAL escrowed deposit, not a counter. Checked end to end: (1) insufficient funds
// -> create refused; (2) create -> real escrow, with coins leaving the signer and landing in the
// module account; (3) slash -> takes a REAL amount off the refundable balance; (4) exit (delete) ->
// refunds the REMAINING bond, while the slashed part stays in the module account.
func TestMinerBondEscrowGO13(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	creator, err := f.addressCodec.BytesToString([]byte("go13minerSigner_____________"))
	require.NoError(t, err)
	creatorBz, err := f.addressCodec.StringToBytes(creator)
	require.NoError(t, err)
	creatorAddr := sdk.AccAddress(creatorBz)

	coin := func(n int64) sdk.Coins { return sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(n))) }

	// (1) Insufficient funds (500 < stake 1000) -> create REFUSED: the bond is not a mere counter.
	f.bank.setBalance(creatorAddr, coin(500))
	idG0 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: idG0, Operator: creator, Stake: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrInsufficientFunds)
	has, err := f.keeper.Miner.Has(f.ctx, idG0)
	require.NoError(t, err)
	require.False(t, has) // no miner is created without an effective bond

	// (2) Sufficient funds -> create performs a REAL ESCROW: -1000 for the signer, +1000 in the module account.
	f.bank.setBalance(creatorAddr, coin(10_000))
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: idG0, Operator: creator, Stake: 1000})
	require.NoError(t, err)
	require.Equal(t, coin(9_000).String(), f.bank.SpendableCoins(f.ctx, creatorAddr).String())
	require.Equal(t, coin(1_000).String(), f.bank.mod[types.ModuleName].String())

	// (3a) An UNAUTHORIZED caller cannot slash: a real bond must not be griefable.
	_, err = srv.SlashMiner(f.ctx, &types.MsgSlashMiner{Creator: creator, MinerId: idG0})
	require.Error(t, err)
	mUntouched, err := f.keeper.Miner.Get(f.ctx, idG0)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), mUntouched.Stake) // stake intact after the unauthorized attempt

	// (3b) Slash by the AUTHORITY (governance): 1000 -> 200 refundable (slash_leak_bps=8000 -> 80 %).
	_, err = srv.SlashMiner(f.ctx, &types.MsgSlashMiner{Creator: f.authority, MinerId: idG0})
	require.NoError(t, err)
	m, err := f.keeper.Miner.Get(f.ctx, idG0)
	require.NoError(t, err)
	require.Equal(t, uint64(200), m.Stake)

	// (4) Exit -> refunds the REMAINING bond (200); the slashed part (800) stays in the module account.
	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: idG0})
	require.NoError(t, err)
	require.Equal(t, coin(9_200).String(), f.bank.SpendableCoins(f.ctx, creatorAddr).String()) // 9000 + 200
	require.Equal(t, coin(800).String(), f.bank.mod[types.ModuleName].String())                // 1000 - 200
}
