package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// LOSSLESS EXPORT/IMPORT.
//
// `GenesisState` used to stop at field 6 while the module held 23 collections: an export/import lost
// 17 of them SILENTLY — including `HeldFee`/`HeldBurn` (the coins survive in the module account, but
// nothing records whom they belong to) and the four deadline queues (the affected jobs then NEVER
// resolve, so their escrow stays locked without any error being raised).
//
// The test writes state into EVERY carried collection, exports, re-imports into a FRESH keeper, and
// compares. A field forgotten in `ExportGenesis` OR in `InitGenesis` makes it fail. Without such a
// test a collection can be added to the keeper and never wired to genesis, with nothing to signal it:
// the omission produces no error, only silent loss.
func TestGenesisRoundTripLosesNothing(t *testing.T) {
	f := initFixture(t)

	// --- source state, one entry per carried collection ---------------------------------------------
	// The miner carries the identifier its creator derives (ADR-039). The ancillary keys below keep
	// their short literals on purpose: they belong to OTHER collections, and this test measures
	// transport, not registry coherence.
	m1 := registeredMiner(t, 0x21, 42)
	require.NoError(t, gvSetMiner(f, f.ctx, m1.MinerId, m1))
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", State: "open", Fee: 7}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 99}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__m1", types.Commit{JobId: "j1", ResultCommit: "1,0"}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "j1", types.Beacon{JobId: "j1", Seed: "s"}))

	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "j1", 500))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "j1", 25))
	require.NoError(t, f.keeper.PendingReveal.Set(f.ctx, collections.Join(int64(11), "j1")))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(12), "j1")))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(13), "j1")))
	require.NoError(t, f.keeper.PendingAppealResolve.Set(f.ctx, collections.Join(int64(14), "j1")))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "j1", "m1,m2,m3"))
	require.NoError(t, f.keeper.ValidatorVrfPubkey.Set(f.ctx, "val1", "abcdef"))
	require.NoError(t, f.keeper.MinerOptimisticCount.Set(f.ctx, "m1", 3))
	require.NoError(t, f.keeper.AvailFailCount.Set(f.ctx, "m1", 2))
	require.NoError(t, f.keeper.AvailFailWindowStart.Set(f.ctx, "m1", 100))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "challenge-x"))
	// ⛔ TWO SETS THIS BENCH DID NOT POPULATE, SO NOTHING EVER EXECUTED THEM.
	// `genesis_coverage_test.go` classifies them as CARRIED — by INSPECTION. The measurement said
	// otherwise: in the coverage profile, the bodies of both export loops in `genesis.go` (the walks
	// over AuditSelected and PendingHeldRelease) were at count ZERO. A classification is an assertion;
	// only an execution turns it into a guarantee. And neither is a cache: AuditSelected carries a
	// SELECTION (which audits have already been drawn — the lottery's anti-replay) and
	// PendingHeldRelease a DEBT (held fees still to release). Losing them across a migration would
	// replay lotteries and erase debts, without a single error.
	require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "j1"))
	require.NoError(t, f.keeper.PendingHeldRelease.Set(f.ctx, "j1"))

	// EXPORT AT ONE HEIGHT, IMPORT AT ANOTHER. Without that gap the test would be BLIND to the property
	// it claims to check: with export and import heights both 0, `remaining = deadline - 0` then
	// `0 + remaining` lands back on the original value, and a transport in ABSOLUTE heights would pass
	// identically. That is the kind of reassuring green that proves nothing.
	const exportH, importH = int64(10), int64(5000)
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(exportH)

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)

	// --- re-import into a PRISTINE keeper, MUCH further along the chain ----------------------------
	g := initFixture(t)
	g.ctx = sdk.UnwrapSDKContext(g.ctx).WithBlockHeight(importH)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported))

	// BOTH SETS, READ BACK AFTER IMPORT. They carry no value: MEMBERSHIP is the information, so `Has`
	// is what decides, and a `false` here would be a replayable lottery and an erased debt.
	for nom, has := range map[string]func() (bool, error){
		"AuditSelected":      func() (bool, error) { return g.keeper.AuditSelected.Has(g.ctx, "j1") },
		"PendingHeldRelease": func() (bool, error) { return g.keeper.PendingHeldRelease.Has(g.ctx, "j1") },
	} {
		ok, hErr := has()
		require.NoError(t, hErr)
		require.True(t, ok, "%s must survive the round trip: it carries a selection or a debt, not a cache", nom)
	}

	// VALUE — the case that produced orphan funds.
	hf, err := g.keeper.HeldFee.Get(g.ctx, "j1")
	require.NoError(t, err, "HeldFee must survive: without it the held coins have no payee")
	require.Equal(t, uint64(500), hf)
	hb, err := g.keeper.HeldBurn.Get(g.ctx, "j1")
	require.NoError(t, err)
	require.Equal(t, uint64(25), hb)

	// DEADLINES — losing them freezes the jobs forever, with no error. Carrying them as ABSOLUTE
	// heights amounts to the same thing: the EndBlocker matches the EXACT height, so an appointment at
	// 13 on a chain restarted at 5000 would never be reached. What is checked is the RE-ANCHORING:
	// `importH + remaining`, where `remaining = deadline - exportH`.
	for name, pair := range map[string]collections.Pair[int64, string]{
		"PendingReveal":        collections.Join(importH+(11-exportH), "j1"),
		"PendingAudit":         collections.Join(importH+(12-exportH), "j1"),
		"PendingAuditResolve":  collections.Join(importH+(13-exportH), "j1"),
		"PendingAppealResolve": collections.Join(importH+(14-exportH), "j1"),
	} {
		var has bool
		switch name {
		case "PendingReveal":
			has, err = g.keeper.PendingReveal.Has(g.ctx, pair)
		case "PendingAudit":
			has, err = g.keeper.PendingAudit.Has(g.ctx, pair)
		case "PendingAuditResolve":
			has, err = g.keeper.PendingAuditResolve.Has(g.ctx, pair)
		case "PendingAppealResolve":
			has, err = g.keeper.PendingAppealResolve.Has(g.ctx, pair)
		}
		require.NoError(t, err)
		require.True(t, has, "%s: appointment not re-anchored on the restart height -> it will never fire (the walk matches the EXACT height) and the escrow stays locked", name)
	}
	// And the NEGATIVE proof: the old absolute key must no longer exist, otherwise an entry would have
	// been added instead of moved — and the dead entry would stay in the store forever.
	staleHas, err := g.keeper.PendingAuditResolve.Has(g.ctx, collections.Join(int64(13), "j1"))
	require.NoError(t, err)
	require.False(t, staleHas, "the deadline must NOT stay at its original height (it lies behind the restart)")

	// AUTHORIZATION + anti-abuse.
	ac, err := g.keeper.AuditCommittee.Get(g.ctx, "j1")
	require.NoError(t, err, "anchored committee lost -> every in-flight dispute becomes inert")
	require.Equal(t, "m1,m2,m3", ac)
	vk, err := g.keeper.ValidatorVrfPubkey.Get(g.ctx, "val1")
	require.NoError(t, err, "VRF key lost -> anti-grinding INACTIVE until re-anchoring")
	require.Equal(t, "abcdef", vk)
	moc, err := g.keeper.MinerOptimisticCount.Get(g.ctx, "m1")
	require.NoError(t, err, "Sybil probation lost -> a known miner starts over from zero")
	require.Equal(t, uint64(3), moc)
	afc, err := g.keeper.AvailFailCount.Get(g.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(2), afc)
	afw, err := g.keeper.AvailFailWindowStart.Get(g.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(100), afw)
	chal, err := g.keeper.AvailChallenge.Get(g.ctx)
	require.NoError(t, err)
	require.Equal(t, "challenge-x", chal)

	// The original state survives too (no regression on the 6 historical fields).
	m, err := g.keeper.Miner.Get(g.ctx, m1.MinerId)
	require.NoError(t, err)
	require.Equal(t, uint64(42), m.Stake)
	p, err := g.keeper.Pools.Get(g.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(99), p.Treasury)
}

// BACKWARD COMPATIBILITY. A genesis produced by an earlier binary carries NONE of the fields 7 and
// beyond. The import must then behave exactly as before, not fail on nil slices.
//
// WHAT THIS TOLERANCE DOES *NOT* COVER, stated because the two are easy to confuse: missing FIELDS
// are tolerated, a legacy IDENTIFIER is not. The case below pins that boundary, so the refusal is a
// decision on record rather than something a future operator discovers at a reset.
func TestGenesisImportToleratesLegacyFile(t *testing.T) {
	f := initFixture(t)
	legacy := types.DefaultGenesis() // fields 7+ all empty, as in an older export
	m1 := registeredMiner(t, 0x31, 1)
	legacy.MinerMap = []types.Miner{{MinerId: m1.MinerId, Creator: m1.Creator, Stake: 1}}
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *legacy))
	m, err := f.keeper.Miner.Get(f.ctx, m1.MinerId)
	require.NoError(t, err)
	require.Equal(t, uint64(1), m.Stake)
}

// A FREE-TEXT IDENTIFIER FROM AN OLDER CHAIN IS REFUSED, AND THAT IS THE POINT (ADR-039).
//
// Tolerating it would reopen precisely what the derived identifier closes: a hostname engraved in a
// public registry, an operator's name available to whoever writes the genesis, and an evicted
// identity returning under its old string. The refusal happens in `InitGenesis` rather than only in
// `Validate`, because `InitChain` calls the former and never the latter — a rule that lives only in
// `validate-genesis` is a rule an operator skips by not running the command.
func TestGenesisImportRefusesALegacyChosenIdentifier(t *testing.T) {
	f := initFixture(t)
	legacy := types.DefaultGenesis()
	legacy.MinerMap = []types.Miner{{MinerId: "m-pc-01", Stake: 1}}
	err := f.keeper.InitGenesis(f.ctx, *legacy)
	require.Error(t, err, "a genesis may not re-plant a chosen identifier through the import door")
	require.Contains(t, err.Error(), "m-pc-01", "the refusal must name the entry it rejects")
	_, gErr := f.keeper.Miner.Get(f.ctx, "m-pc-01")
	require.Error(t, gErr, "nothing must have been written before the refusal")
}

// DISPUTE DEADLINE: it is the REMAINING time that must survive the reset, not the height.
//
// `DisputeHeight` is an ABSOLUTE height of the previous chain. Exported as-is, the guard
// `BlockHeight() < DisputeHeight + DisputeWindow` (msg_server_adjudicate.go) stays true for the WHOLE
// height of the previous chain: adjudication becomes unreachable and the disputer's bond stays locked.
// The job is frozen permanently — and, like the silent loss the round trip above covers, without any
// error ever being raised.
//
// This test fails against a transport that keeps absolute heights: after an import at height 1 it
// would still read 1,000,000 there, a window pushed a million blocks out instead of the 50 remaining.
func TestGenesisDisputeDeadlineSurvivesReset(t *testing.T) {
	const (
		disputeH = int64(1_000_000) // dispute opened high in the previous chain
		exportH  = int64(1_000_050) // 50 blocks elapsed at export time
		importH  = int64(1)         // the new chain restarts from zero
		elapsed  = exportH - disputeH
	)

	f := initFixture(t)
	require.NoError(t, gvSetJob(f, f.ctx, "jd", types.Job{
		JobId: "jd", State: "settled+disputed", Disputer: "d1", DisputeHeight: disputeH,
	}))
	// A job that was NEVER disputed: the witness. It must stay at 0, with no spurious shift.
	require.NoError(t, gvSetJob(f, f.ctx, "jn", types.Job{JobId: "jn", State: "settled"}))

	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(exportH)
	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)

	g := initFixture(t)
	g.ctx = sdk.UnwrapSDKContext(g.ctx).WithBlockHeight(importH)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported))

	jd, err := g.keeper.Job.Get(g.ctx, "jd")
	require.NoError(t, err)

	// The invariant that matters: the time elapsed since the dispute opened is PRESERVED, so the time
	// left in the window is too — whatever height the new chain starts at.
	require.Equal(t, elapsed, importH-jd.DisputeHeight,
		"elapsed time not preserved: the dispute window no longer expires at the same relative moment")

	// NEGATIVE proof — without it the test cannot tell "re-anchored" from "unchanged".
	require.NotEqual(t, disputeH, jd.DisputeHeight,
		"absolute height of the previous chain re-imported as-is: adjudication unreachable")

	// And the adjudication guard must actually be able to reopen within a bounded delay.
	require.Less(t, jd.DisputeHeight, importH,
		"the deadline must be reachable from the init height, not pushed a million blocks out")

	jn, err := g.keeper.Job.Get(g.ctx, "jn")
	require.NoError(t, err)
	require.Zero(t, jn.DisputeHeight, "a job that was never disputed must not be given an invented deadline")
}
