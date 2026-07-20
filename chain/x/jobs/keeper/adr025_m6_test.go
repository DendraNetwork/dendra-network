package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/types"
)

// M6a (ADR-025) — ÉCHANTILLONNAGE ADAPTATIF : un gros job voit son taux d'audit boosté par sa valeur.
// base=1 (≈0 %) + audit_adaptive=1 (ref) -> effective = 1 + fee/1 -> saturé à 10000 pour une grosse fee
// -> audité de façon déterministe. (Dormant si audit_adaptive=0 : couvert par le test M3.)
func TestADR025AdaptiveSampling(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1
	p.AuditAdaptive = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jBig", types.Job{JobId: "jBig", State: "open+paid+optimistic", MinerId: "m1", Fee: 50000}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jBig")))

	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err := f.keeper.Job.Get(ctx6, "jBig")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "gros job -> audit adaptatif saturé à 100 % -> audité")
}

// M6b (ADR-025) — PROBATION anti-Sybil : un mineur neuf (compteur <= N) est audité à 100 % même si la base
// est quasi nulle. base=1 + audit_probation_jobs=5 + compteur=1 -> effective=10000 -> audité déterministe.
func TestADR025Probation(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1
	p.AuditProbationJobs = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, f.keeper.MinerOptimisticCount.Set(f.ctx, "mNew", uint64(1))) // mineur neuf : 1er job
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jNew", types.Job{JobId: "jNew", State: "open+paid+optimistic", MinerId: "mNew", Fee: 10}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jNew")))

	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err := f.keeper.Job.Get(ctx6, "jNew")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "mineur en probation -> audité à 100 %")
}

// LIVENESS (ADR-025) — un audit ouvert mais jamais ré-adjugé est AUTO-VINDIQUÉ à l'échéance (innocence par
// défaut), libérant le job de l'état +disputed. Échéance posée directement -> EndBlock -> +resolved.
func TestADR025AuditResolveTimeout(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jStuck", types.Job{JobId: "jStuck", State: "open+paid+optimistic+disputed", MinerId: "m1"}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jStuck")))

	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err := f.keeper.Job.Get(ctx6, "jStuck")
	require.NoError(t, err)
	require.Contains(t, job.State, "resolved", "audit non honoré -> auto-vindiqué (liveness)")
	has, err := f.keeper.PendingAuditResolve.Has(ctx6, collections.Join(int64(6), "jStuck"))
	require.NoError(t, err)
	require.False(t, has, "échéance consommée")
}
