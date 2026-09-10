package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-025 — OPTIMISTIC verification by sampling (params, k=1 settlement, and the Nash guard).
// Everything is DORMANT by default (verification_mode=0), so the mode-0 tests stay green unmodified:
// that is the non-regression proof for the redundant k=3 path.

// The new parameters default to 0 (dormant) and are bounded by Validate.
func TestADR025ParamsDefaultDormant(t *testing.T) {
	p := types.DefaultParams()
	require.Equal(t, uint64(0), p.VerificationMode, "default = redundant (dormant)")
	require.Equal(t, uint64(0), p.AuditSampleBps, "default = no audit")
	require.NoError(t, p.Validate())

	bad := types.DefaultParams()
	bad.VerificationMode = 2
	require.Error(t, bad.Validate(), "verification_mode outside {0,1} rejected")

	bad2 := types.DefaultParams()
	bad2.AuditSampleBps = 10001
	require.Error(t, bad2.Validate(), "audit_sample_bps > 10000 rejected")
}

// In optimistic mode, a fee that breaks the Nash inequality is REFUSED at open time; a small fee passes.
// In mode 0 the guard is inert, which the existing OpenJob tests cover.
func TestADR025NashGuardOpenJob(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams() // min_stake=1000, slash_leak_bps=8000
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // 10 %
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// Nash-safe iff  audit*slash*min_stake > (10000-audit)*10000*fee  =>  8e9 > 9e7*fee  =>  fee < ~89.
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "nashHigh", Fee: 100000})
	require.Error(t, err, "fee too high -> refused by the Nash guard")

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "nashOk", Fee: 10})
	require.NoError(t, err, "small fee -> accepted")
}

// In optimistic mode a job settles by paying a SINGLE primary miner (k=1) against its lone commit; the
// job is marked paid+optimistic, and a second settlement is rejected by the jobIsPaid replay guard.
func TestADR025OptimisticSettleK1(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// A single registered miner is therefore the k=1 primary (and the k=3 committee degenerates to it).
	idM1 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: idM1, Operator: creator, Stake: 1000})
	require.NoError(t, err)

	// Small fee (passes the Nash guard). delay=0 freezes the seed at open time, so an immediate commit is valid.
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "jobO", Fee: 30})
	require.NoError(t, err)

	// The primary anchors a commit (a valid integer vector).
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jobO__" + idM1, ResultCommit: "1,2,3"})
	require.NoError(t, err)

	// OPTIMISTIC k=1 settlement: pay the primary, mark the job.
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: creator, JobId: "jobO"})
	require.NoError(t, err, "optimistic k=1 settlement accepted")

	job, err := f.keeper.Job.Get(f.ctx, "jobO")
	require.NoError(t, err)
	require.Contains(t, job.State, "optimistic", "job marked optimistic")
	require.Contains(t, job.State, "paid", "job marked paid (replay guard)")
	require.Equal(t, idM1, job.MinerId, "primary recorded on the job")

	// Soft burn applied from escrow (fee=30, fee_burn_bps=500 -> 1 udndr burned).
	require.False(t, f.bank.burned.IsZero(), "soft burn applied at optimistic settlement")

	// Replay guard: a second settlement is refused.
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: creator, JobId: "jobO"})
	require.Error(t, err, "double settlement rejected")
}

// OPTIMISTIC settlement takes the protocol cut (split between validators, team and treasury) AND credits
// `Demand = treasury+team` when the client differs from the operator. Without that credit the emission
// subsidy never rewarded inference work and the 85/15 tokenomics was inconsistent. The anti-self-dealing
// rule is preserved. Slashing an audited primary reverses this Demand, which the ADR-028 tests cover.
func TestADR025OptimisticCutAndDemand(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	client, err := f.addressCodec.BytesToString([]byte("clientAAAA__________________"))
	require.NoError(t, err)

	p := types.DefaultParams() // protocol_fee_bps=1500, validator=5000, team=2000, fee_burn_bps=500
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	idM1 := deriveIDFor(t, f, op)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: idM1, Stake: 1000})
	require.NoError(t, err)

	// Client DISTINCT from the operator (anti-self-dealing); fee=80 passes the Nash guard (fee < ~89).
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "jCD", Fee: 80})
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "jCD__" + idM1, ResultCommit: "1,2,3"})
	require.NoError(t, err)
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: client, JobId: "jCD"})
	require.NoError(t, err)

	// fee=80 -> burn=4, cut=12 (validators=6, team=2, treasury=4), minerNet=64; Demand=treasury+team=6.
	m, err := f.keeper.Miner.Get(f.ctx, idM1)
	require.NoError(t, err)
	require.Equal(t, uint64(6), m.Demand, "Demand=treasury+team credited at optimistic settlement (client != operator)")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(6), pools.Validators, "cut -> validators")
	require.Equal(t, uint64(2), pools.Team, "cut -> team")
	require.Equal(t, uint64(4), pools.Treasury, "cut -> treasury")
}
