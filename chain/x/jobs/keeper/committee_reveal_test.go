package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// Révélation différée du comité (H6, anti-grinding). Régime piloté par le param committee_reveal_delay.

// delay==0 (défaut) : la graine est figée DÈS l'open -> comportement historique, commit immédiat OK.
func TestCommitteeRevealDelayOff(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "job0", Fee: 10})
	require.NoError(t, err)

	b, err := f.keeper.Beacon.Get(f.ctx, "job0")
	require.NoError(t, err)
	require.NotEmpty(t, b.Seed, "delay==0 -> graine figée à l'open")
}

// delay>0 : graine VIDE à l'open, commit REFUSÉ ; l'EndBlocker à H+delay fige la graine (depuis
// l'AppHash) et le commit passe alors.
func TestCommitteeRevealDelayDeferred(t *testing.T) {
	f := initFixture(t)

	// activer la révélation différée (delay=2)
	p := types.DefaultParams()
	p.CommitteeRevealDelay = 2
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	// open au bloc 10
	ctx10 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(10)
	_, err = srv.OpenJob(ctx10, &types.MsgOpenJob{Creator: creator, JobId: "jobD", Fee: 10})
	require.NoError(t, err)

	// graine vide (pending) + révélation planifiée à H+delay=12
	b, err := f.keeper.Beacon.Get(ctx10, "jobD")
	require.NoError(t, err)
	require.Empty(t, b.Seed, "delay>0 -> graine non figée à l'open")
	has, err := f.keeper.PendingReveal.Has(ctx10, collections.Join(int64(12), "jobD"))
	require.NoError(t, err)
	require.True(t, has, "révélation planifiée à H+delay=12")

	// un mineur dont l'opérateur = creator
	_, err = srv.CreateMiner(ctx10, &types.MsgCreateMiner{Creator: creator, MinerId: "m1", Operator: creator, Stake: 1000})
	require.NoError(t, err)

	// commit AVANT révélation -> refusé (le comité n'est pas encore figé)
	_, err = srv.CreateCommit(ctx10, &types.MsgCreateCommit{Creator: creator, JobId: "jobD__m1", ResultCommit: "r"})
	require.Error(t, err, "commit refusé tant que le comité n'est pas révélé")

	// EndBlock à H=11 : rien n'est dû -> graine toujours vide
	ctx11 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11).WithHeaderInfo(header.Info{Height: 11, AppHash: []byte("apphash-11")})
	require.NoError(t, f.keeper.EndBlock(ctx11))
	b, _ = f.keeper.Beacon.Get(ctx11, "jobD")
	require.Empty(t, b.Seed, "pas encore révélé à H=11")

	// EndBlock à H=12 : révélation -> graine figée (non vide), entrée en attente consommée
	ctx12 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(12).WithHeaderInfo(header.Info{Height: 12, AppHash: []byte("apphash-12")})
	require.NoError(t, f.keeper.EndBlock(ctx12))
	b, err = f.keeper.Beacon.Get(ctx12, "jobD")
	require.NoError(t, err)
	require.NotEmpty(t, b.Seed, "révélé à H=12")
	has, _ = f.keeper.PendingReveal.Has(ctx12, collections.Join(int64(12), "jobD"))
	require.False(t, has, "entrée en attente consommée")

	// commit désormais accepté
	_, err = srv.CreateCommit(ctx12, &types.MsgCreateCommit{Creator: creator, JobId: "jobD__m1", ResultCommit: "r"})
	require.NoError(t, err, "commit accepté après révélation")
}
