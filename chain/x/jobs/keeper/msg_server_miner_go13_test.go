package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// TestMinerBondEscrowGO13 — GO-13 : le bond mineur est un DÉPÔT RÉEL séquestré (pas un compteur).
// On vérifie de bout en bout : (1) fonds insuffisants -> create refusé ; (2) create -> escrow réel
// (coins quittent le signataire, atterrissent au compte de module) ; (3) slash -> retire un montant
// RÉEL du remboursable ; (4) exit (delete) -> rembourse le bond RESTANT, le slashé reste au module.
func TestMinerBondEscrowGO13(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	creator, err := f.addressCodec.BytesToString([]byte("go13minerSigner_____________"))
	require.NoError(t, err)
	creatorBz, err := f.addressCodec.StringToBytes(creator)
	require.NoError(t, err)
	creatorAddr := sdk.AccAddress(creatorBz)

	coin := func(n int64) sdk.Coins { return sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(n))) }

	// (1) fonds insuffisants (500 < stake 1000) -> create REFUSÉ : le bond n'est pas un simple compteur.
	f.bank.setBalance(creatorAddr, coin(500))
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "g0", Operator: creator, Stake: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrInsufficientFunds)
	has, err := f.keeper.Miner.Has(f.ctx, "g0")
	require.NoError(t, err)
	require.False(t, has) // aucun mineur créé sans bond effectif

	// (2) fonds suffisants -> create ESCROW RÉEL : -1000 au signataire, +1000 au compte de module.
	f.bank.setBalance(creatorAddr, coin(10_000))
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "g0", Operator: creator, Stake: 1000})
	require.NoError(t, err)
	require.Equal(t, coin(9_000).String(), f.bank.SpendableCoins(f.ctx, creatorAddr).String())
	require.Equal(t, coin(1_000).String(), f.bank.mod[types.ModuleName].String())

	// (3a) durcissement : un NON-autorisé ne peut PAS slasher (bond réel non griefable).
	_, err = srv.SlashMiner(f.ctx, &types.MsgSlashMiner{Creator: creator, MinerId: "g0"})
	require.Error(t, err)
	mUntouched, err := f.keeper.Miner.Get(f.ctx, "g0")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), mUntouched.Stake) // stake intact après tentative non-autorisée

	// (3b) slash par l'AUTORITÉ (gov) : 1000 -> 200 remboursable (slash_leak_bps=8000 -> 80%).
	_, err = srv.SlashMiner(f.ctx, &types.MsgSlashMiner{Creator: f.authority, MinerId: "g0"})
	require.NoError(t, err)
	m, err := f.keeper.Miner.Get(f.ctx, "g0")
	require.NoError(t, err)
	require.Equal(t, uint64(200), m.Stake)

	// (4) exit -> rembourse le bond RESTANT (200) ; le slashé (800) reste au module (trésorerie).
	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "g0"})
	require.NoError(t, err)
	require.Equal(t, coin(9_200).String(), f.bank.SpendableCoins(f.ctx, creatorAddr).String()) // 9000 + 200
	require.Equal(t, coin(800).String(), f.bank.mod[types.ModuleName].String())                 // 1000 - 200
}
