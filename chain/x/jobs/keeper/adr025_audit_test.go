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

// M3 (ADR-025) — TIRAGE D'AUDIT en EndBlock. En mode optimiste, un job réglé k=1 est programmé pour audit
// au bloc SUIVANT ; à audit_sample_bps=10000 (100 %) le tirage est déterministe -> le job passe +disputed
// (dispute protocolaire que le comité frais tranchera en M4). DORMANT : en mode 0, settleOptimistic ne
// tourne pas -> aucune entrée PendingAudit (couvert par les tests existants, qui restent verts).
func TestADR025OptimisticAuditEndBlock(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 % -> tirage déterministe (toujours audité)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// Règlement optimiste au bloc 5 -> audit programmé au bloc 6.
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5)
	_, err = srv.CreateMiner(ctx5, &types.MsgCreateMiner{Creator: creator, MinerId: "m1", Operator: creator, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.OpenJob(ctx5, &types.MsgOpenJob{Creator: creator, JobId: "jobA", Fee: 30})
	require.NoError(t, err)
	_, err = srv.CreateCommit(ctx5, &types.MsgCreateCommit{Creator: creator, JobId: "jobA__m1", ResultCommit: "1,2,3"})
	require.NoError(t, err)
	_, err = srv.SettleSemantic(ctx5, &types.MsgSettleSemantic{Creator: creator, JobId: "jobA"})
	require.NoError(t, err)

	// Pas d'audit au moment du règlement ; entrée programmée à h+1=6.
	job, err := f.keeper.Job.Get(ctx5, "jobA")
	require.NoError(t, err)
	require.NotContains(t, job.State, "disputed", "pas d'audit au règlement")
	has, err := f.keeper.PendingAudit.Has(ctx5, collections.Join(int64(6), "jobA"))
	require.NoError(t, err)
	require.True(t, has, "audit programmé à h+1=6")

	// EndBlock au bloc 6 -> tirage 100 % -> dispute protocolaire ouverte + entrée consommée.
	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("apphash-6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err = f.keeper.Job.Get(ctx6, "jobA")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "audité -> +disputed")
	require.Equal(t, int64(6), job.DisputeHeight, "hauteur d'audit enregistrée")
	has, err = f.keeper.PendingAudit.Has(ctx6, collections.Join(int64(6), "jobA"))
	require.NoError(t, err)
	require.False(t, has, "entrée d'audit consommée (décision unique)")
}
