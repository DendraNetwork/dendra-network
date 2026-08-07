package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// JUROR VITALITY IS NOT MINING — the exit guard, pinned by three cases.
//
// THE RULE. Clause (b) of `hasOpenObligation` (`miner_vitality.go`) must NOT hold a juror the protocol
// has ITSELF declared undrawable — that is, beyond `jurorFreshnessBlocks`, where `eligibleAsJuror`
// already refuses to summon it. Holding someone in an old committee while refusing to draw them into a
// fresh one is incoherent.
//
// WHAT WAS REJECTED, and why these tests lock it out: "release once the audit is past its deadline" is
// wrong. An audit past its deadline without quorum is PRECISELY the case of a primary whose audit could
// not conclude. A cheater able to stall its own audit would leave with its stake, and the slash would
// become avoidable by waiting. Hence case (3) below: it proves the AUDITED party does not move.
//
// THE CONTRACT OF THESE TESTS:
//   (1) the LIVE juror stays held         — the fix must not overreach
//   (2) the EXPIRED juror may leave       — the behaviour being introduced
//   (3) the AUDITED party never leaves, expired or not — the slash must stay unavoidable
// Without (1), one hole is merely traded for another.

const rgStake = uint64(1_000_000)

// rgSetup — a miner, its creator, and enough funds to refund its bond on exit.
func rgSetup(t *testing.T, f *fixture, id, seed string) (string, sdk.Context) {
	t.Helper()
	aeFundModule(f)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(seed)))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
		MinerId: id, Stake: rgStake, Operator: creator, Creator: creator,
	}))
	return creator, sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
}

// (1) THE LIVE JUROR STAYS HELD. A miner that anchored a commit recently is summonable
// (`eligibleAsJuror` draws it); its seat in an ONGOING audit therefore means something, and letting it
// desert would make the quorum unreachable — the original harm clause (b) exists to prevent.
func TestVitalite_JureVIVANT_SiegeantAuditEnCours_ResteRetenu(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mAliveJuror", "creator_juror_alive_")

	// LIVE: commit anchored 1,000 blocks ago, far inside jurorFreshnessBlocks (259,200).
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mAliveJuror", uint64(vitH-1000)))
	// Sits on the committee of an UNRESOLVED audit, and is NOT the primary of the job.
	require.NoError(t, gvSetJob(f, f.ctx, "jVivant", types.Job{
		JobId: "jVivant", MinerId: "unAutrePrimaire", State: "open+paid+optimistic+disputed",
	}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jVivant", "mAliveJuror,mAutre"))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mAliveJuror"})
	require.Error(t, err,
		"a SUMMONABLE juror sitting on an ongoing audit must stay held: letting it leave is the "+
			"desertion clause (b) exists to prevent")
	has, hErr := f.keeper.Miner.Has(f.ctx, "mAliveJuror")
	require.NoError(t, hErr)
	require.True(t, has, "it must still be in the registry")
}

// (2) THE EXPIRED JUROR MUST BE ABLE TO LEAVE.
//
// The failure mode: identities whose operator keys are lost still sit on unresolved audits. They will
// NEVER vote. Clause (b) holds them, the audit cannot conclude for want of their votes, and pruning
// goes through the SAME guard: neither voluntary exit nor automatic eviction. The seat is immortal
// precisely because it is dead, and it keeps the quorum bar high for the whole network.
func TestVitalite_JurePERIME_SiegeantAuditEnCours_DoitPouvoirSortir(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mStaleJuror", "creator_juror_stale_")

	// EXPIRED: last commit at block 1, current height 700,000 -> 699,999 > 259,200. `eligibleAsJuror`
	// refuses to summon it onto a FRESH committee; that is the whole incoherence being removed.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mStaleJuror", vitAncient))
	require.NoError(t, gvSetJob(f, f.ctx, "jPerime", types.Job{
		JobId: "jPerime", MinerId: "unAutrePrimaire", State: "open+paid+optimistic+disputed",
	}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jPerime", "mStaleJuror,mAutre"))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mStaleJuror"})
	require.NoError(t, err,
		"a juror the protocol already refuses to summon must no longer be held: keeping it serves "+
			"nothing and keeps the quorum bar high for the whole network")
	has, hErr := f.keeper.Miner.Has(f.ctx, "mStaleJuror")
	require.NoError(t, hErr)
	require.False(t, has, "it must have left the registry")
}

// (3) THE AUDITED PARTY NEVER LEAVES, EVEN EXPIRED. Clause (c) holds the PRIMARY of an unresolved
// disputed job, and the clause (b) behaviour must not touch it. This miner is expired exactly like the
// one in case (2) — only its ROLE differs. If this case ever stopped failing the exit, the slash would
// become avoidable by waiting.
func TestVitalite_ClauseC_PrimairePerimeDUnJobDisputeResteRetenu(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mStalePrimary", "creator_primary_stale__")

	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mStalePrimary", vitAncient))
	// It is the PRIMARY of the unresolved disputed job — the party being judged, not a juror.
	require.NoError(t, gvSetJob(f, f.ctx, "jAudite", types.Job{
		JobId: "jAudite", MinerId: "mStalePrimary", State: "open+paid+optimistic+disputed",
	}))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mStalePrimary"})
	require.Error(t, err,
		"the primary of an unresolved audit NEVER leaves, expired or not: otherwise lying goes back to "+
			"0-EV instead of -EV, and the slash is avoided by waiting")
	has, hErr := f.keeper.Miner.Has(f.ctx, "mStalePrimary")
	require.NoError(t, hErr)
	require.True(t, has)
}

// (4) THE RELEASE APPLIES TO BOTH EXIT PATHS — voluntary exit AND automatic pruning.
//
// The reason is decisive: an identity whose operator key is lost can never call `DeleteMiner`.
// Restricting the release to voluntary exit would only serve miners that still hold their key, i.e.
// exactly those that do not have the problem. Only pruning can remove an identity whose key is gone.
// `TestC1bis_ExpiredJurorSeatIsPrunable` pins that path; `TestC1_ActiveCommitteeSeatStillProtects`
// pins the complement with a FRESH juror.
//
// THE FORM OF THE JUSTIFICATION MATTERS AS MUCH AS ITS CONTENT. Writing "every prune candidate is
// expired BY CONSTRUCTION (604,800 > 259,200)" holds for the COMPILED defaults and is false in general:
// both windows are governable. The rule is therefore written on the PREDICATE (`eligibleAsJuror`
// evaluated), never on a presumed order — and `Params.Validate` bounds
// `miner_prune_blocks >= juror_freshness_blocks` so the reverse incoherence cannot be configured.
