package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A GATE NOBODY HAD EVER CROSSED, AND A SETTLEMENT BODY NOBODY HAD EVER RUN.
//
// `SettleJob` is the v1 raw settlement, kept only because removing an RPC needs a proto regeneration.
// It is reserved to the governance authority. Measured with `go tool cover`: 35 of its 45 statements
// were at count 0 — the refusal was exercised, the gate was never PASSED, so everything below it (the
// five Pools counters, the ADR-017 demand credit and the `+settled` marker) had never executed.
//
// The distinction matters more than the ratio: a handler whose refusal is tested and whose success is
// not looks guarded, and the part that moves value is the untested half.

func TestSettleJobIsReservedToTheAuthority(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, _ := rgSetup(t, f, "mSJ", "creator_settlejob_refus_")

	require.NoError(t, gvSetJob(f, f.ctx, "jSJ", types.Job{JobId: "jSJ", MinerId: "mSJ", Fee: 10000}))
	// Pools is read the way the HANDLER reads it: on a fresh store the item does not exist yet, and
	// absent is zero. Asserting `require.NoError` here would have made this case fail on its own
	// scaffolding rather than on the guard it exists to prove.
	sjPools := func() types.Pools {
		p, e := f.keeper.Pools.Get(f.ctx)
		if e != nil {
			return types.Pools{}
		}
		return p
	}
	before := sjPools()

	_, err := srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: creator, JobId: "jSJ"})
	require.Error(t, err, "a raw settlement by anyone would let a caller move an escrow with no verification")
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	require.Equal(t, before, sjPools(), "the refusal must leave every pool untouched")
	job, err := f.keeper.Job.Get(f.ctx, "jSJ")
	require.NoError(t, err)
	require.NotContains(t, job.State, "+settled")
}

// The other half: the authority DOES settle, and the five pools split the fee exactly. Every figure is
// derived from the parameters the handler reads — retyping them would pin a configuration, not a rule.
func TestSettleJobByTheAuthoritySplitsTheFeeAcrossThePools(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, _ = rgSetup(t, f, "mSJ2", "creator_settlejob_ok_")

	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	// A client distinct from the miner's operator, so the ADR-017 demand credit applies (the
	// self-dealing exception has its own case below).
	client := ssAddr20(t, f, "client-sj2")
	require.NoError(t, gvSetJob(f, f.ctx, "jSJ2", types.Job{
		JobId: "jSJ2", MinerId: "mSJ2", Fee: 10000, Client: client,
	}))

	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	cut := uint64(10000) * p.ProtocolFeeBps / 10000
	minerGross := uint64(10000) - cut
	minerLocked := minerGross * p.MinerVestBps / 10000
	minerLiquid := minerGross - minerLocked
	validators := cut * p.ValidatorRewardBps / 10000
	team := cut * p.TeamFeeBps / 10000
	treasury := cut - validators - team
	require.Positive(t, treasury, "with treasury at 0 the demand assertion below would prove nothing")

	before, err := f.keeper.Miner.Get(f.ctx, "mSJ2")
	require.NoError(t, err)

	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: authority, JobId: "jSJ2"})
	require.NoError(t, err, "the authority is the one caller this vestige serves")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, minerLiquid, pools.MinerPaid)
	require.Equal(t, minerLocked, pools.MinerLocked)
	require.Equal(t, validators, pools.Validators)
	require.Equal(t, team, pools.Team)
	require.Equal(t, treasury, pools.Treasury)
	// The split must be EXACT: a rounding leak here would silently mint or lose escrow at every job.
	require.Equal(t, uint64(10000),
		pools.MinerPaid+pools.MinerLocked+pools.Validators+pools.Team+pools.Treasury,
		"the five pools must account for the whole fee, to the unit")

	after, err := f.keeper.Miner.Get(f.ctx, "mSJ2")
	require.NoError(t, err)
	require.Equal(t, before.Demand+treasury+team, after.Demand,
		"ADR-017: the non-recoverable demand is treasury + team")

	job, err := f.keeper.Job.Get(f.ctx, "jSJ2")
	require.NoError(t, err)
	require.Contains(t, job.State, "+settled", "the marker is what locks the escrow against the other settlement paths")
}

// SELF-DEALING: a client paying its own miner must not manufacture demand. Without this case the
// exception is one `if` that no test crosses, and demand is what gates the subsidy.
func TestSettleJobDoesNotCreditDemandOnSelfDealing(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, _ = rgSetup(t, f, "mSJ3", "creator_settlejob_self_")

	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	miner, err := f.keeper.Miner.Get(f.ctx, "mSJ3")
	require.NoError(t, err)
	// The client IS the miner's operator: paying yourself must not buy subsidy eligibility.
	require.NoError(t, gvSetJob(f, f.ctx, "jSJ3", types.Job{
		JobId: "jSJ3", MinerId: "mSJ3", Fee: 10000, Client: miner.Operator,
	}))

	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: authority, JobId: "jSJ3"})
	require.NoError(t, err)

	after, err := f.keeper.Miner.Get(f.ctx, "mSJ3")
	require.NoError(t, err)
	require.Equal(t, miner.Demand, after.Demand,
		"self-dealing must credit NO demand: otherwise a miner funds its own subsidy with its own money")
	// ... and the pools still move, so the case cannot pass by the settlement simply not happening.
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Positive(t, pools.Treasury, "the settlement itself must still have taken place")
}
