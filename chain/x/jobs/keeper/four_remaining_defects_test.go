package keeper_test

import (
	"fmt"
	"testing"

	"github.com/stretchr/testify/require"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Four defects with one shape: a guard whose condition is a number that cannot express the case it is
// asked about. `initH > 0` cannot say "height zero is a real import"; `DisputeHeight != 0` cannot say
// "disputed at this very block"; the beacon freeze asked nothing about where its seed came from; and
// the expiry walk had no number bounding it. Each case below drives the shipped path, and each has its
// twin, the half that must keep working.

// (1) The two ADR-037 anchors at import height zero.
//
// `dendrad export --for-zero-height` is the documented way to restart a chain, and the SDK leaves the
// header height at 0 during InitChain whenever `InitialHeight` is not > 1. The old `initH > 0` guards
// were therefore false for exactly that import: nobody was re-anchored, and `jobPoolFreezeHeight` then
// errored for every imported job, deferring every draw forever with no error anyone watches.
func TestGenesisArmsTheAnchorsAtHeightZero(t *testing.T) {
	f := initFixture(t)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(0) // what the SDK gives for --for-zero-height

	gen := types.DefaultGenesis()
	m := registeredMiner(t, 0x21, gvStakeHi)
	gen.MinerMap = []types.Miner{m}
	gen.JobMap = []types.Job{{JobId: "j0", State: "open", Fee: 1000}}
	require.NoError(t, f.keeper.InitGenesis(ctx, *gen))

	_, mErr := f.keeper.MinerRegisteredHeight.Get(ctx, m.MinerId)
	require.NoError(t, mErr,
		"an import at height 0 must date its miners: an undated miner is excluded from every frozen pool")
	_, jErr := f.keeper.JobPoolFreezeHeight.Get(ctx, "j0")
	require.NoError(t, jErr,
		"an import at height 0 must anchor its jobs: without it jobPoolFreezeHeight errors and the draw is deferred forever")

	// The property that matters, beyond the presence of two keys: the imported miner is actually
	// drawable on the imported job. `registered == freeze == 0` holds, which is the point.
	require.NoError(t, f.keeper.Beacon.Set(ctx, "j0", types.Beacon{JobId: "j0", Seed: "s"}))
	set, aErr := f.keeper.AssignedCommitteeForTest(ctx, "j0", 3)
	require.NoError(t, aErr, "the draw must succeed on a job imported at height 0")
	require.True(t, set[m.MinerId], "the imported population must stay eligible on an imported job")
}

// The one-block sybil window of a normal restart, which (1) exposed and did not close.
//
// On a normal restart `initial_height = N+1`, the SDK puts the InitChain header at N+1, and N+1 is
// also the first block CometBFT finalises. Anchoring both ADR-037 values at `initH` therefore tied with
// a `create-miner` landing in that block, and since `minerInFrozenPool` is `reg <= freeze`, the
// newcomer entered the frozen pool of every imported job. The exported genesis publishes `beaconMap`,
// so the seeds are known in advance: one ground address, one transaction, one block.
//
// The case asserts the order, not a number: an imported miner stays in, a first-block newcomer stays
// out. A test that computed `initH - 1` next to the code would agree with any future change to the
// rule, including a wrong one.
func TestGenesisFirstBlockNewcomerStaysOutOfTheImportedPools(t *testing.T) {
	f := initFixture(t)
	const importH = int64(100) // normal restart: the InitChain header is the first finalised block
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(importH)

	gen := types.DefaultGenesis()
	imported := registeredMiner(t, 0x31, gvStakeHi)
	gen.MinerMap = []types.Miner{imported}
	gen.JobMap = []types.Job{{JobId: "jimp", State: "open", Fee: 1000}}
	require.NoError(t, f.keeper.InitGenesis(ctx, *gen))

	// The newcomer is registered by the shipped message, on purpose.
	// The invariant `reg(newcomer) > freeze(imported job)` rests on two files, `frozenPoolAnchor` and
	// the dating in `msg_server_miner.go`, so a case that writes the date by hand can only see the
	// first. Changing the dating line to `frozenPoolAnchor(BlockHeight())` (the "one anchor
	// everywhere" refactor this shape invites) reopens the sybil window entirely, while a test that
	// stipulates the date stays green. So the newcomer registers through the shipped message.
	// The address comes from the fixture's own codec: `registeredMiner` encodes a `dendra` prefix,
	// which the message server refuses; that is fine for a genesis import, not for the live path.
	srv := keeper.NewMsgServerImpl(f.keeper)
	newAddr, aErr2 := f.addressCodec.BytesToString([]byte("newcomerAddr________________"))
	require.NoError(t, aErr2)
	newcomerID := deriveIDFor(t, f, newAddr)
	_, cErr := srv.CreateMiner(ctx, &types.MsgCreateMiner{
		Creator: newAddr, MinerId: newcomerID, Operator: newAddr, Stake: gvStakeHi,
	})
	require.NoError(t, cErr, "the newcomer must register through the shipped path, or the dating code is never exercised")

	freeze, fErr := f.keeper.JobPoolFreezeHeight.Get(ctx, "jimp")
	require.NoError(t, fErr)
	regImported, iErr := f.keeper.MinerRegisteredHeight.Get(ctx, imported.MinerId)
	require.NoError(t, iErr)
	regNewcomer, nErr := f.keeper.MinerRegisteredHeight.Get(ctx, newcomerID)
	require.NoError(t, nErr)

	require.LessOrEqual(t, regImported, freeze,
		"the imported population must stay in its own pools -- they really were there before")
	require.Greater(t, regNewcomer, freeze,
		"a miner registered in the first finalised block must stay out of every imported pool: that tie is the sybil window ADR-037 exists to close")

	// And the end-to-end property, beyond the two numbers: the draw itself.
	require.NoError(t, f.keeper.Beacon.Set(ctx, "jimp", types.Beacon{JobId: "jimp", Seed: "seed-now-public"}))
	set, aErr := f.keeper.AssignedCommitteeForTest(ctx, "jimp", 3)
	require.NoError(t, aErr)
	require.True(t, set[imported.MinerId], "the imported miner must remain drawable on an imported job")
	require.False(t, set[newcomerID], "the newcomer must never be drawn on an imported job")
}

// The live equality at OpenJob, the same defect seen from the other side.
//
// With `committee_reveal_delay = 0`, a configuration `Params.Validate` accepts, the beacon seed is
// fixed in the opening block. A proposer reading the pending MsgOpenJob therefore knows the committee
// before it proposes, and one `create-miner` of its own in that same block would be dated exactly
// at the freeze; under a `reg <= freeze` rule a tie is enough to be in. It could then grind an
// id offline until the draw puts it first, on the very job it has just entered.
//
// Both miners register through the shipped message and the job opens through the shipped message: an
// invariant that spans `frozenPoolAnchor` and the dating in `msg_server_miner.go` cannot be guarded by
// a case that writes either of them by hand.
func TestOpeningFreezesThePreviousBlockPool(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, cErr := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, cErr)
	const h = int64(40)

	// Already serving the network, one block before the job opens.
	earlyAddr, e1 := f.addressCodec.BytesToString([]byte("earlyMinerAddr______________"))
	require.NoError(t, e1)
	earlyID := deriveIDFor(t, f, earlyAddr)
	_, err := srv.CreateMiner(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h-1),
		&types.MsgCreateMiner{Creator: earlyAddr, MinerId: earlyID, Operator: earlyAddr, Stake: gvStakeHi})
	require.NoError(t, err)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)

	// The proposer's own miner, inserted into the opening block.
	lateAddr, e2 := f.addressCodec.BytesToString([]byte("lateMinerAddr_______________"))
	require.NoError(t, e2)
	lateID := deriveIDFor(t, f, lateAddr)
	_, err = srv.CreateMiner(ctx, &types.MsgCreateMiner{
		Creator: lateAddr, MinerId: lateID, Operator: lateAddr, Stake: gvStakeHi})
	require.NoError(t, err)

	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jlive", Fee: 30})
	require.NoError(t, err)

	freeze, fErr := f.keeper.JobPoolFreezeHeight.Get(ctx, "jlive")
	require.NoError(t, fErr)
	regEarly, rE := f.keeper.MinerRegisteredHeight.Get(ctx, earlyID)
	require.NoError(t, rE)
	regLate, rL := f.keeper.MinerRegisteredHeight.Get(ctx, lateID)
	require.NoError(t, rL)

	require.LessOrEqual(t, regEarly, freeze,
		"a miner already serving before the opening block must be in that job's pool")
	require.Greater(t, regLate, freeze,
		"a miner appearing in the opening block must stay out: the tie is what let a proposer seat itself")

	// The end-to-end property, beyond the two numbers: the draw itself, on a seed made public.
	require.NoError(t, f.keeper.Beacon.Set(ctx, "jlive", types.Beacon{JobId: "jlive", Seed: "seed-now-public"}))
	set, aErr := f.keeper.AssignedCommitteeForTest(ctx, "jlive", 3)
	require.NoError(t, aErr)
	require.True(t, set[earlyID], "the miner that was already there must remain drawable")
	require.False(t, set[lateID], "the miner inserted in the opening block must never be drawn on it")
}

// The same property at height zero, which is what the clamp protects and what nothing else tests.
//
// `initH - 1` on a uint64 anchor at initH = 0 wraps to 2^64-1. Both anchors would take that value, so
// `reg <= freeze` would still hold for the imported population and the obvious assertion would stay
// green, while every newcomer would also satisfy it. The pool would be open to everyone, with no
// error anywhere; only the newcomer side shows it.
func TestGenesisZeroAnchorAdmitsNoNewcomer(t *testing.T) {
	f := initFixture(t)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(0)

	gen := types.DefaultGenesis()
	imported := registeredMiner(t, 0x51, gvStakeHi)
	gen.MinerMap = []types.Miner{imported}
	gen.JobMap = []types.Job{{JobId: "jz", State: "open", Fee: 1000}}
	require.NoError(t, f.keeper.InitGenesis(ctx, *gen))

	gvMiner(t, f, "newcomer-block-1", gvStakeHi, 1) // the first block after a height-zero import

	freeze, fErr := f.keeper.JobPoolFreezeHeight.Get(ctx, "jz")
	require.NoError(t, fErr)
	require.Equal(t, uint64(0), freeze, "the clamp must keep 0, not wrap to 2^64-1")

	regImp, iErr := f.keeper.MinerRegisteredHeight.Get(ctx, imported.MinerId)
	require.NoError(t, iErr)
	require.LessOrEqual(t, regImp, freeze, "the imported population stays in its own pools")

	regNew, nErr := f.keeper.MinerRegisteredHeight.Get(ctx, "newcomer-block-1")
	require.NoError(t, nErr)
	require.Greater(t, regNew, freeze,
		"a miner registered after a height-zero import must stay out: an underflowed anchor would let everyone in")
}

// The twin: an imported miner must stay eligible on work opened after the restart. Shifting the
// anchor down could have excluded the whole imported population from new jobs instead.
func TestGenesisImportedMinerStaysEligibleOnAFreshJob(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	const importH = int64(100)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(importH)

	gen := types.DefaultGenesis()
	imported := registeredMiner(t, 0x41, gvStakeHi)
	gen.MinerMap = []types.Miner{imported}
	require.NoError(t, f.keeper.InitGenesis(ctx, *gen))

	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jnew", Fee: 30})
	require.NoError(t, err)

	freeze, fErr := f.keeper.JobPoolFreezeHeight.Get(ctx, "jnew")
	require.NoError(t, fErr)
	regImported, iErr := f.keeper.MinerRegisteredHeight.Get(ctx, imported.MinerId)
	require.NoError(t, iErr)
	require.LessOrEqual(t, regImported, freeze,
		"an imported miner must stay eligible on a job opened after the restart, or the migration kills the whole population")
}

// (2) A dispute opened at the export block.
//
// `DisputeHeight` travels as an age, so a dispute opened at the export block encodes as 0, the same 0
// that means "never disputed". The freshest dispute, the only one whose re-commit window still had
// its full length, was left at 0 on import and its window vanished entirely.
func TestGenesisDisputeOpenAtTheExportBlockSurvives(t *testing.T) {
	f := initFixture(t)
	H := int64(43755)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(H)
	require.NoError(t, f.keeper.Job.Set(ctx, "jd",
		types.Job{JobId: "jd", State: "open+paid+disputed", Fee: 1000, DisputeHeight: H}))

	exported, eErr := f.keeper.ExportGenesis(ctx)
	require.NoError(t, eErr)
	var age int64 = -1
	for _, j := range exported.JobMap {
		if j.JobId == "jd" {
			age = j.DisputeHeight
		}
	}
	require.Equal(t, int64(0), age,
		"premise: the encoded age is zero here — that ambiguity is the whole defect, not an accident of this test")

	f2 := initFixture(t)
	H2 := int64(7)
	ctx2 := sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(H2)
	require.NoError(t, f2.keeper.InitGenesis(ctx2, *exported))

	got, gErr := f2.keeper.Job.Get(ctx2, "jd")
	require.NoError(t, gErr)
	require.Equal(t, H2, got.DisputeHeight,
		"a dispute opened at the export block must be re-anchored on the import height, not left at 0 — at 0 the adjudication guard stops blocking and the fresh committee loses its whole re-commit window")
}

// The twin: a job that was never disputed must stay at zero. A fix that re-anchored everything
// would give an untouched job a dispute deadline out of nowhere.
func TestGenesisJobNeverDisputedStaysAtZero(t *testing.T) {
	f := initFixture(t)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(900)
	require.NoError(t, f.keeper.Job.Set(ctx, "jn", types.Job{JobId: "jn", State: "open", Fee: 10}))

	exported, eErr := f.keeper.ExportGenesis(ctx)
	require.NoError(t, eErr)

	f2 := initFixture(t)
	ctx2 := sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(3)
	require.NoError(t, f2.keeper.InitGenesis(ctx2, *exported))
	got, gErr := f2.keeper.Job.Get(ctx2, "jn")
	require.NoError(t, gErr)
	require.Equal(t, int64(0), got.DisputeHeight, "a job that was never disputed must not acquire a deadline")
}

// The export side, which only matters after two round trips.
//
// On a single export the value test and the marker test agree, because a dispute is always opened at
// a height >= 1, so `DisputeHeight != 0` is true for every disputed job in state. The export change
// looked like a fix and caught nothing: a mutation reverting it stayed green, which is how this was
// found.
//
// It matters as soon as an import lands at height 0, the case fixed above. A dispute opened at the
// export block imports as `0 - 0 = 0`: the state then holds a job that is disputed and whose
// DisputeHeight is zero, which the value test cannot see. Fifty blocks later the second export would
// skip it and write 0 again, and the next import would re-anchor a fifty-block-old dispute as if it
// had just been opened. The defect flips direction: the first version cancelled a fresh window, this
// one resurrects an expired one, and adjudication stays blocked on a job whose committee has long
// since had its chance.
func TestGenesisDisputeSurvivesTwoRoundTrips(t *testing.T) {
	f1 := initFixture(t)
	H1 := int64(100)
	c1 := sdk.UnwrapSDKContext(f1.ctx).WithBlockHeight(H1)
	require.NoError(t, f1.keeper.Job.Set(c1, "jd",
		types.Job{JobId: "jd", State: "open+paid+disputed", Fee: 1, DisputeHeight: H1}))
	g1, e1 := f1.keeper.ExportGenesis(c1)
	require.NoError(t, e1)

	// Import at height 0: what `--for-zero-height` produces, and now a real import.
	f2 := initFixture(t)
	c2 := sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(0)
	require.NoError(t, f2.keeper.InitGenesis(c2, *g1))
	j2, gErr := f2.keeper.Job.Get(c2, "jd")
	require.NoError(t, gErr)
	require.Equal(t, int64(0), j2.DisputeHeight,
		"premise: after an import at height 0 the dispute is anchored at zero — the state the value test cannot read")
	require.Contains(t, j2.State, "disputed", "premise: the marker is still there, and it is the only thing that still says so")

	// The chain runs on, then exports again.
	g2, e2 := f2.keeper.ExportGenesis(sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(50))
	require.NoError(t, e2)
	var age int64 = -1
	for _, j := range g2.JobMap {
		if j.JobId == "jd" {
			age = j.DisputeHeight
		}
	}
	require.Equal(t, int64(50), age,
		"a dispute anchored at 0 must export with its 50 elapsed blocks — a test on the value skips it and exports it as brand new, resurrecting a window that had expired")
}

// (3) The beacon freeze on a seed the proposer could have chosen.
//
// A proposer that also runs a miner can compute both candidate seeds before proposing (the
// vote-extension aggregate it holds, and the fallback it already knows) and omit the envelope when the
// fallback gives it the primary seat. Omitting costs nothing. The audit draw already refused this; the
// work draw, a few lines above it, did not, and the work draw decides who gets paid.
func TestBeaconFreezeIsDeferredWhileTheSeedIsStillSelectable(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1 // rewarded mode: the seed must be decentralized
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	h := int64(50)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).
		WithHeaderInfo(header.Info{Height: h, AppHash: []byte("apphash-50")})
	require.NoError(t, f.keeper.Beacon.Set(ctx, "jb", types.Beacon{JobId: "jb", Seed: ""}))
	require.NoError(t, f.keeper.PendingReveal.Set(ctx, collections.Join(h, "jb")))

	require.NoError(t, f.keeper.EndBlock(ctx))

	b, bErr := f.keeper.Beacon.Get(ctx, "jb")
	require.NoError(t, bErr)
	require.Equal(t, "", b.Seed,
		"with no decentralized seed the committee must not be frozen: the proposer would be choosing it")

	// The appointment is not lost: refusing without deferring would never assign this job at all.
	// 20 = auditDeferStride (unexported), the same constant the audit draw defers by.
	has, hErr := f.keeper.PendingReveal.Has(ctx, collections.Join(h+20, "jb"))
	require.NoError(t, hErr)
	require.True(t, has, "the deferral must keep the appointment, otherwise the job is never assigned at all")
	old, oErr := f.keeper.PendingReveal.Has(ctx, collections.Join(h, "jb"))
	require.NoError(t, oErr)
	require.False(t, old, "the appointment must move, not be duplicated at every block")
}

// The twin: in legacy mode (committee_seed_source != 1) the fallback is the designed behaviour and
// the freeze must still happen. A fix that deferred unconditionally would stop every assignment on
// a network that never enabled the decentralized seed.
func TestBeaconFreezeHappensInLegacyMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.CommitteeSeedSource = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	h := int64(51)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).
		WithHeaderInfo(header.Info{Height: h, AppHash: []byte("apphash-51")})
	require.NoError(t, f.keeper.Beacon.Set(ctx, "jl", types.Beacon{JobId: "jl", Seed: ""}))
	require.NoError(t, f.keeper.PendingReveal.Set(ctx, collections.Join(h, "jl")))

	require.NoError(t, f.keeper.EndBlock(ctx))

	b, bErr := f.keeper.Beacon.Get(ctx, "jl")
	require.NoError(t, bErr)
	require.NotEqual(t, "", b.Seed, "in legacy mode the fallback seed is the design: the freeze must happen")
}

// (5) The defect the fix above introduced, found in review before production.
//
// Deferring by a fixed stride makes every batch of the same residue class land on the same height and
// merge. The reveal walk had no cap, so the merged batch grew without bound (measured 50, 100, 150,
// 200, 250 entries in a single EndBlock), the unbounded pass the expiry cap exists to prevent. A fix
// that changes when work happens can break a bound that held only because of the old timing.
func TestDeferredFreezeStaysBoundedDespiteCoalescing(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1 // the seed must be decentralized, so everything gets deferred
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	const total = 250
	const cap = 200 // revealMaxPerBlock (unexported)
	h := int64(1000)
	for i := 0; i < total; i++ {
		id := fmt.Sprintf("jc%03d", i)
		require.NoError(t, f.keeper.Beacon.Set(f.ctx, id, types.Beacon{JobId: id}))
		require.NoError(t, f.keeper.PendingReveal.Set(f.ctx, collections.Join(h, id)))
	}

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).
		WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
	require.NoError(t, f.keeper.EndBlock(ctx))

	aH, atDeferred, elsewhere := countReveal(t, f, h, h+20)
	require.Equal(t, cap, atDeferred,
		"the deferral must move at most the cap: a merged batch is exactly what grows without bound")
	require.Equal(t, total-cap, aH,
		"what does not fit must stay where it is — dropping it would never assign those jobs at all")
	require.Equal(t, 0, elsewhere, "no appointment may land anywhere else")
}

// The backlog drains, which the cap alone would have lost: an entry left at a past height must be
// picked up, never stranded.
func TestDeferredFreezeDrainsTheRemainder(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	h := int64(2000)
	for i := 0; i < 210; i++ {
		id := fmt.Sprintf("jd%03d", i)
		require.NoError(t, f.keeper.Beacon.Set(f.ctx, id, types.Beacon{JobId: id}))
		require.NoError(t, f.keeper.PendingReveal.Set(f.ctx, collections.Join(h, id)))
	}
	base := sdk.UnwrapSDKContext(f.ctx)
	require.NoError(t, f.keeper.EndBlock(base.WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("a")})))
	aH, _, _ := countReveal(t, f, h, h+20)
	require.Equal(t, 10, aH, "premise: 10 entries are left at height h")

	require.NoError(t, f.keeper.EndBlock(base.WithBlockHeight(h+1).WithHeaderInfo(header.Info{Height: h + 1, AppHash: []byte("b")})))
	atH2, _, _ := countReveal(t, f, h, h+21)
	require.Equal(t, 0, atH2, "an appointment left at a past height must be picked up, never stranded")
}

// An orphaned appointment is consumed. The normal path removes the key even with no beacon; the
// deferral loop did not, so the entry was rescheduled forever and swelled every merged batch.
func TestDeferredFreezeConsumesAnOrphanedAppointment(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	h := int64(300)
	require.NoError(t, f.keeper.PendingReveal.Set(f.ctx, collections.Join(h, "orphan"))) // no beacon at all
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("a")})
	require.NoError(t, f.keeper.EndBlock(ctx))

	n := 0
	require.NoError(t, f.keeper.PendingReveal.Walk(f.ctx, nil,
		func(collections.Pair[int64, string]) (bool, error) { n++; return false, nil }))
	require.Equal(t, 0, n, "an appointment whose beacon is gone must be consumed, not deferred for ever")
}

// A fee-less job books no appointment. `expireUnservedJob` returns at once when `Fee == 0`, and this
// chain has neither a per-block gas limit nor a minimum gas price, so without this guard filling the
// collection would cost nothing.
func TestOpeningWithoutAFeeSchedulesNoExpiry(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "free", Fee: 0})
	require.NoError(t, err, "premise: a zero-fee job is accepted by the chain — that is the whole point")
	require.Equal(t, 0, countExpiry(t, f), "a job with no escrow must not book an appointment that can never do anything")

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "paid", Fee: 500})
	require.NoError(t, err)
	require.Equal(t, 1, countExpiry(t, f), "and a job with an escrow must still book one")
}

// One escrow per job id. Nothing refused a re-open: `Job.Set` overwrites the record, so a client
// retrying "dup" with a different fee moved a second escrow into the module while `job.Fee` kept only
// the last one. Every exit computes from `job.Fee`, so the first escrow had no path out of the module
// at all: not stolen, unreachable.
func TestOpeningTheSameJobIdTwiceIsRefused(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "dup", Fee: 10})
	require.NoError(t, err)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "dup", Fee: 20})
	require.Error(t, err, "a second OpenJob on the same id must be refused: its escrow could never come back")
	require.Contains(t, err.Error(), "already exists")

	// And only one appointment was booked: two would refund the same `job.Fee` twice.
	require.Equal(t, 1, countExpiry(t, f), "a refused re-open must not leave a second appointment behind")

	job, gErr := f.keeper.Job.Get(f.ctx, "dup")
	require.NoError(t, gErr)
	require.Equal(t, uint64(10), job.Fee, "the first record must stand: overwriting it is what orphaned the first escrow")
}

// countReveal returns how many appointments sit at height `a`, at height `b`, and anywhere else.
func countReveal(t *testing.T, f *fixture, a, b int64) (int, int, int) {
	t.Helper()
	var na, nb, other int
	require.NoError(t, f.keeper.PendingReveal.Walk(f.ctx, nil,
		func(key collections.Pair[int64, string]) (bool, error) {
			switch key.K1() {
			case a:
				na++
			case b:
				nb++
			default:
				other++
			}
			return false, nil
		}))
	return na, nb, other
}

// (4) The expiry pass, bounded and without orphans.
//
// The EndBlocker has no gas. Every appointment is written by an OpenJob, so a block full of them lands
// that many refunds on one later height. A cap alone would have stranded the overflow (entries left
// at h are never looked at again once the pass moves on), losing the escrow the mechanism exists to
// return. The range covers every due height, so the cap is a rate.
func TestExpiryIsBoundedPerBlockAndLeavesNoOrphan(t *testing.T) {
	f := initFixture(t)
	const cap = 200 // jobExpiryMaxPerBlock (unexported)
	const n = 205

	h := int64(9)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)
	for i := 0; i < n; i++ {
		require.NoError(t, f.keeper.PendingJobExpiry.Set(ctx, collections.Join(h, fmt.Sprintf("jx%03d", i))))
	}

	require.NoError(t, f.keeper.EndBlock(ctx))
	require.Equal(t, n-cap, countExpiry(t, f),
		"the pass must stop at the cap: an unbounded one runs a job read plus a bank transfer per entry in a block nothing can interrupt")

	// The next block takes the backlog, which the cap alone would have lost.
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h+1)))
	require.Equal(t, 0, countExpiry(t, f),
		"an appointment left at a past height must be picked up, never stranded — stranding it loses the escrow this mechanism exists to return")
}

// The twin: an appointment in the future must not be touched. A range that swallowed everything would
// refund every open job at the next block.
func TestExpiryDoesNotTouchAFutureAppointment(t *testing.T) {
	f := initFixture(t)
	h := int64(4)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)
	require.NoError(t, f.keeper.PendingJobExpiry.Set(ctx, collections.Join(h+1, "jfuture")))

	require.NoError(t, f.keeper.EndBlock(ctx))
	require.Equal(t, 1, countExpiry(t, f), "an appointment due later must survive this block untouched")
}

func countExpiry(t *testing.T, f *fixture) int {
	t.Helper()
	n := 0
	require.NoError(t, f.keeper.PendingJobExpiry.Walk(f.ctx, nil,
		func(collections.Pair[int64, string]) (bool, error) { n++; return false, nil }))
	return n
}
