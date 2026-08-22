package keeper_test

import (
	"fmt"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══════════════════════════════════════════════════════════════════════════════════════════════════
// ADR-042 — THE WORK DRAW MUST REFUSE THE DEAD, AND MUST NOT BE BUYABLE.
//
// These two properties are what a whole network died without. On the chain destroyed on 2026-08-13:
//   · one machine stopped on 08-11 and still drew primary on 51 jobs out of 51, because the WORK draw
//     had no liveness test at all — the JURY draw had one, and only the jury;
//   · that same miner held 5 000 000 against 50 000 for everyone else, and `lessByStakeScore` gave it
//     a score a hundred times smaller, so it also won 9 of the 9 draws where it had competitors.
//
// ⚠️ WHAT THESE CASES ARE FOR, AND WHAT THEY ARE NOT. They do not check that the code "runs". Each
// one is built so that exactly ONE of the two guards can make it pass: remove the vitality filter and
// the first pair goes red; remove the ceiling and the second pair goes red. A case that both guards
// could satisfy would be green for a reason nobody chose, and would stay green when one of them is
// deleted — which is precisely how four benches certified a nine-day outage earlier the same week.
// ═══════════════════════════════════════════════════════════════════════════════════════════════════

const (
	// The freshness window falls back to the compiled constant when the parameter is 0 (the default),
	// so "long ago" has to clear that constant, not a number invented here.
	abFreeze  = uint64(400_000)
	abAncient = uint64(1) // registered at height 1: 400 000 - 1 is far outside any freshness window
)

// abSetup plants a job whose pool freezes at `abFreeze`, and returns the fixture.
//
// The freeze height is set EXPLICITLY rather than through the shared helper: this file is about what
// the draw does at a given height, so the height has to be visible in the test that reads it.
func abSetup(t *testing.T, jobId string) *fixture {
	t.Helper()
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, jobId, abFreeze))
	require.NoError(t, f.keeper.Job.Set(f.ctx, jobId, types.Job{JobId: jobId, State: "open", Fee: 100000}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, jobId, types.Beacon{JobId: jobId, Seed: "s"}))
	return f
}

// abMiner registers a miner with an explicit registration height and stake.
func abMiner(t *testing.T, f *fixture, id string, registered, stake uint64) {
	t.Helper()
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, registered))
	require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: stake}))
}

// ---------------------------------------------------------------------------------------------------
// ① A MINER THAT WENT SILENT BEFORE THE JOB OPENED IS NOT DRAWN FOR IT.
//
// Everyone here carries the SAME stake, so the ceiling cannot be what separates them: only vitality
// can. `mDead` registered at height 1 and never committed; the others registered just before the
// freeze. If the filter is removed, `mDead` re-enters the pool and this case fails on the count.
// ---------------------------------------------------------------------------------------------------
func TestADR042_TheWorkDrawRefusesAMinerThatWasAlreadySilentWhenTheJobOpened(t *testing.T) {
	f := abSetup(t, "jDead")
	const same = uint64(50_000)
	abMiner(t, f, "mDead", abAncient, same)
	for i := 0; i < 3; i++ {
		abMiner(t, f, fmt.Sprintf("mLive%d", i), abFreeze-10, same)
	}

	set, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jDead", keeper.CommitteeSize)
	require.NoError(t, err)
	require.False(t, set["mDead"],
		"a miner that had produced nothing for the whole freshness window when the job opened must not be assignable")
	require.Len(t, set, 3, "the three live miners still fill the committee: the filter excludes, it does not starve")
}

// ---------------------------------------------------------------------------------------------------
// ② AND IT DOES NOT TURN VITALITY INTO AN ENTRY FILTER.
//
// A miner that registered recently and has never committed is INNOCENT, not inert: it must be drawn,
// or no newcomer could ever earn its first job. This is the twin that keeps case ① from being
// satisfied by a filter that simply refuses everyone.
// ---------------------------------------------------------------------------------------------------
func TestADR042_ABrandNewMinerIsStillDrawn(t *testing.T) {
	f := abSetup(t, "jNew")
	const same = uint64(50_000)
	abMiner(t, f, "mNewcomer", abFreeze, same) // registered at the freeze itself, zero commits
	abMiner(t, f, "mOther", abFreeze-10, same)

	set, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jNew", keeper.CommitteeSize)
	require.NoError(t, err)
	require.True(t, set["mNewcomer"], "vitality must never become an ENTRY filter: a newcomer has to be drawable")
}

// ---------------------------------------------------------------------------------------------------
// ③ OUTBIDDING THE FLOOR BUYS NOTHING.
//
// The reproduction of the real failure, at its real ratio: one miner at 100x `min_stake` against
// twelve at the floor. All thirteen are equally vital, so vitality cannot be what decides — only the
// ceiling can. Without it the rich miner takes slot 0 on essentially every seed; the assertion is
// therefore made over MANY seeds rather than one, because a single job could go either way by luck.
// ---------------------------------------------------------------------------------------------------
func TestADR042_AHundredfoldStakeNoLongerBuysEveryPrimarySlot(t *testing.T) {
	const (
		floor = uint64(50_000)
		rich  = floor * 100
		jobs  = 40
	)
	wins := 0
	for n := 0; n < jobs; n++ {
		jobId := fmt.Sprintf("jRich%02d", n)
		f := abSetup(t, jobId)
		abMiner(t, f, "mRich", abFreeze-10, rich)
		for i := 0; i < 12; i++ {
			abMiner(t, f, fmt.Sprintf("mPoor%02d", i), abFreeze-10, floor)
		}
		ordered, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, jobId, 1)
		require.NoError(t, err)
		require.Len(t, ordered, 1)
		if ordered[0] == "mRich" {
			wins++
		}
	}
	// Uncapped, a hundredfold stake takes slot 0 about 94 % of the time (the measured figure on the
	// destroyed chain). Capped at twice the floor it lands near 15 %. The bar is set at half the
	// uncapped rate: high enough that a real ceiling passes it comfortably, low enough that removing
	// the ceiling cannot.
	require.Less(t, wins, jobs/2,
		"a miner cannot buy the primary slot: it took %d of %d draws, which is the uncapped regime", wins, jobs)
	require.Greater(t, wins, 0,
		"the stake must still WEIGH something — a ceiling is not a deletion of stake weighting")
}

// ---------------------------------------------------------------------------------------------------
// ⑤ A JOB IS REFUSED WHEN IT COULD NEVER BE VERIFIED — AND ACCEPTED THE MOMENT IT COULD.
//
// ⛔ MEASURED ON THE RELAUNCH ITSELF, AT BLOCK ~30. The launcher runs an end-to-end check that opens a
// job; zero miners were registered, so that job froze an EMPTY pool: never assignable, never
// auditable, its escrow unreachable. Exactly the state 42 of the 51 jobs of the previous chain died
// in. The failure needs no attacker — only the ordinary order in which a network is brought up.
//
// The two halves are asserted together on purpose: a guard that only ever refuses would pass the
// first case and quietly break the network, which is the failure mode of every fail-closed check
// nobody exercised on the green path.
// ---------------------------------------------------------------------------------------------------
func TestADR042_AJobIsRefusedWhileNoOneCouldEverVerifyIt(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AuditMinQuorum = 4 // the jury excludes the primary, so this asks for 5 eligible miners
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	ms := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("adr042OpenJobClient_________"))
	require.NoError(t, err)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(abFreeze))

	// FOUR eligible miners: one short of the floor, which is the case that must be refused.
	for i := 0; i < 4; i++ {
		abMiner(t, f, fmt.Sprintf("mFew%d", i), abFreeze-10, p.MinStake)
	}
	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jTooFew", Fee: 30})
	require.Error(t, err, "a job whose pool freezes below quorum+1 can never reach a verdict: it must be refused, not accepted and abandoned")
	require.Contains(t, err.Error(), "eligible miner", "the refusal must name what is missing, or the client cannot act on it")
	_, gErr := f.keeper.JobPoolFreezeHeight.Get(f.ctx, "jTooFew")
	require.Error(t, gErr, "a refused job must leave NO frozen anchor behind: a half-created job is worse than none")

	// The fifth clears the floor, and the same call must now succeed.
	abMiner(t, f, "mFew4", abFreeze-10, p.MinStake)
	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jEnough", Fee: 30})
	require.NoError(t, err, "at quorum+1 eligible miners the job is verifiable: refusing here would halt the network instead of protecting it")
}

// ⛔ THE FIFTH MINER MUST BE IN THE POOL, NOT MERELY ALIVE — the tie this guard got wrong for a day.
//
// The pool freezes at `frozenPoolAnchor(h)` = h-1, so a miner registered in the OPENING block is not
// in it. The guard used to count vitality at `h`, where that newcomer counts, and the job was born
// with quorum+1 announced and quorum drawable — permanently unverifiable, which is the single failure
// this refusal exists to prevent. Passing the anchor alone does not fix it either: vitality is
// `height <= reg || height-reg <= window`, so the newcomer is vital AT the anchor too. Only the pool's
// own predicate (`reg <= freeze`) separates them, which is why the guard now applies BOTH.
//
// The block is composed by a proposer that can read the pending MsgOpenJob, so "registered in the same
// block" is its choice — the reason the anchor sits strictly below `h` in the first place.
func TestADR042_AMinerRegisteredInTheOpeningBlockStillDoesNotCount(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AuditMinQuorum = 4 // jury excludes the primary -> 5 miners must be IN THE FROZEN POOL
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	ms := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("adr042TieOpenJobClient______"))
	require.NoError(t, err)
	h := int64(abFreeze)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)

	// Four miners already in the pool, plus one that registers in the very block that opens the job.
	for i := 0; i < 4; i++ {
		abMiner(t, f, fmt.Sprintf("mTie%d", i), abFreeze-10, p.MinStake)
	}
	abMiner(t, f, "mTieNewcomer", uint64(h), p.MinStake) // registered AT h: alive, but outside the pool

	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jTie", Fee: 30})
	require.Error(t, err,
		"five miners exist, but only four are in the pool this job freezes: accepting would create a job no jury can ever reach")
	require.Contains(t, err.Error(), "eligible miner")
	_, gErr := f.keeper.JobPoolFreezeHeight.Get(f.ctx, "jTie")
	require.Error(t, gErr, "a refused job must leave no frozen anchor behind")

	// AND THE OTHER HALF: one block later the newcomer IS in the pool, and the same job opens. Without
	// this, a guard that refused everything would pass the case above and stop the network instead.
	ctxNext := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h + 1)
	_, err = ms.OpenJob(ctxNext, &types.MsgOpenJob{Creator: client, JobId: "jTieNext", Fee: 30})
	require.NoError(t, err,
		"at h+1 the newcomer is inside the frozen pool: the cost of the anchor is one block of latency, not a refusal")
	fz, fErr := f.keeper.JobPoolFreezeHeight.Get(f.ctx, "jTieNext")
	require.NoError(t, fErr)
	require.Equal(t, uint64(h), fz, "the pool of a job opened at h+1 closes on h -- the block that carried the newcomer")
}

// ⛔ AND THE OTHER HALF OF THE CONJUNCTION: BEING IN THE POOL IS NOT BEING ABLE TO JUDGE.
//
// This case exists because removing the vitality half of the guard left the ENTIRE jobs package green:
// measured, the "drop the vitality check" mutant produced 0 red. A half of a conjunction that no case
// defends is a half-dead guard, and it dies without a single test moving — the same shape as a guard
// whose command line is broken while its regex still passes.
//
// Here five miners are all INSIDE the frozen pool (`reg <= anchor`), but two registered at height 1 and
// never anchored a commit: the draw filters them out on freshness, so the real jury is three. Counting
// membership alone would announce a verifiable job and freeze an unverifiable pool — the same lie as
// the tie above, arrived at from the opposite side.
func TestADR042_APoolMemberThatIsStaleDoesNotCountEither(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AuditMinQuorum = 4 // -> 5 miners must be in the pool AND able to sit on the jury
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	ms := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("adr042StaleOpenJobClient____"))
	require.NoError(t, err)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(abFreeze))

	for i := 0; i < 3; i++ { // three fresh
		abMiner(t, f, fmt.Sprintf("mStaleFresh%d", i), abFreeze-10, p.MinStake)
	}
	for i := 0; i < 2; i++ { // two registered at height 1, no commit ever: in the pool, outside the window
		abMiner(t, f, fmt.Sprintf("mStaleOld%d", i), abAncient, p.MinStake)
	}

	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jStale", Fee: 30})
	require.Error(t, err,
		"five miners sit in the pool but two can never be drawn: the jury would be three, below quorum, for ever")
	require.Contains(t, err.Error(), "eligible miner")

	// The other half: two fresh replacements clear the floor and the same call succeeds.
	for i := 0; i < 2; i++ {
		abMiner(t, f, fmt.Sprintf("mStaleFix%d", i), abFreeze-10, p.MinStake)
	}
	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jStaleOk", Fee: 30})
	require.NoError(t, err, "five drawable miners is the floor: refusing here would stop the network, not protect it")
}

// ---------------------------------------------------------------------------------------------------
// ④ AND THE CEILING IS SCALED BY `min_stake`, NOT BY WHATEVER THE POOREST MINER CHOSE.
//
// THIS CASE EXISTS BECAUSE THE FIRST IMPLEMENTATION GOT IT WRONG. It derived the ceiling from the
// smallest stake PRESENT in the pool, which lets anyone stake dust and collapse every honest weight
// down to twice that dust — an adversary setting the scale of the guard meant to stop him. Here a
// sybil sits at stake 1, far below `min_stake`: the honest miners must keep their full weight, and
// the sybil must stay where the comparator puts it.
// ---------------------------------------------------------------------------------------------------
// ⚠️ THIS CASE WAS FIRST WRITTEN ON A SINGLE JOB, AND IT PROVED NOTHING. With four candidates for
// three seats, the sybil is left out most of the time whatever the ceiling is worth — so the case
// stayed green when the faulty scale was put back, and only the mutation run said so. Counting seats
// over many seeds is what separates "relegated because its stake is dust" from "left out by luck".
func TestADR042_ADustStakeCannotFlattenTheDraw(t *testing.T) {
	const jobs = 40
	p := types.DefaultParams()
	seats := 0
	for n := 0; n < jobs; n++ {
		jobId := fmt.Sprintf("jDust%02d", n)
		f := abSetup(t, jobId)
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		abMiner(t, f, "mSybil", abFreeze-10, 1) // one unit: far under min_stake
		for i := 0; i < 3; i++ {
			abMiner(t, f, fmt.Sprintf("mHonest%d", i), abFreeze-10, p.MinStake*2)
		}
		set, err := f.keeper.AssignedCommitteeForTest(f.ctx, jobId, keeper.CommitteeSize)
		require.NoError(t, err)
		require.Len(t, set, 3)
		if set["mSybil"] {
			seats++
		}
	}
	// With the ceiling scaled by `min_stake`, the honest miners keep 2000 against the sybil's 1 and it
	// is relegated on essentially every seed. With the ceiling scaled by the pool's own floor, that
	// same 1 becomes the scale: every honest weight collapses to 2, the gap falls to twofold, and the
	// sybil starts taking seats. The bar is set where those two regimes cannot both fit.
	require.LessOrEqual(t, seats, jobs/10,
		"a dust stake took %d of %d committees: the ceiling is being scaled by the poorest miner, which an adversary chooses", seats, jobs)
}
