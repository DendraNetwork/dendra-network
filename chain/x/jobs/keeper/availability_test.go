package keeper_test

import (
	"encoding/hex"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// Availability. avail_epoch_blocks=0 (the default) turns everything OFF and ProveAvailability is
// refused: the end-to-end flow is untouched until governance enables availability.
func TestAvailabilityOff(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("operatorA___________________"))
	require.NoError(t, err)
	idM1 := deriveIDFor(t, f, op)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: idM1, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.ProveAvailability(f.ctx, &types.MsgProveAvailability{Creator: op, MinerId: idM1, Challenge: "x"})
	require.Error(t, err, "availability OFF -> refused")
}

// avail_epoch_blocks>0: the challenge is rolled from the AppHash at the epoch boundary, fresh proofs
// are recorded, and the AvailPool is paid out WEIGHTED BY BOND at the following boundary.
func TestAvailabilityChallengeAndPayout(t *testing.T) {
	f := initFixture(t)
	// Enable: 4-block epoch, 50 % of the AvailPool paid out per epoch.
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailPayoutBps = 5000
	p.AvailRequireDemand = false // this test isolates the BOND-weighted payout split; demand is covered by TestAvailabilityRequireDemand
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.emission.avail = 1_000_000 // simulated AvailPool, known amount

	srv := keeper.NewMsgServerImpl(f.keeper)
	opA, err := f.addressCodec.BytesToString([]byte("operatorA___________________"))
	require.NoError(t, err)
	opB, err := f.addressCodec.BytesToString([]byte("operatorB___________________"))
	require.NoError(t, err)
	idA := deriveIDFor(t, f, opA)
	idB := deriveIDFor(t, f, opB)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: idA, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opB, Operator: opB, MinerId: idB, Stake: 3000})
	require.NoError(t, err)

	// Epoch boundary h=4: rolls the challenge for epoch 1 (epoch 0 is empty, so nothing is paid).
	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("apphash-epoch1")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	require.NotEmpty(t, chal, "challenge rolled at the epoch boundary")

	// Wrong challenge -> refused.
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("apphash-epoch1")})
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opA, MinerId: idA, Challenge: "MAUVAIS"})
	require.Error(t, err, "incorrect challenge -> refused (anti pre-computation)")

	// Correct challenge -> both miners prove their presence for epoch 1.
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opA, MinerId: idA, Challenge: chal})
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opB, MinerId: idB, Challenge: chal})
	require.NoError(t, err)
	has, err := f.keeper.Available.Has(ctx5, collections.Join(int64(1), idA))
	require.NoError(t, err)
	require.True(t, has, "presence recorded for epoch 1")

	// Epoch boundary h=8: pays out epoch 1, weighted by bond (1000 against 3000; budget = 50 % of 1,000,000).
	ctx8 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(8).WithHeaderInfo(header.Info{Height: 8, AppHash: []byte("apphash-epoch2")})
	require.NoError(t, f.keeper.EndBlock(ctx8))

	// budget=500_000; total bond=4000; m1 = 500000*1000/4000 = 125000; m2 = 375000
	require.Equal(t, uint64(125_000), f.emission.availPaid[opA], "m1 share (bond 1000/4000)")
	require.Equal(t, uint64(375_000), f.emission.availPaid[opB], "m2 share (bond 3000/4000)")
	require.Equal(t, uint64(500_000), f.emission.avail, "AvailPool debited by the budget paid out")

	// Epoch 1 presences are purged after the payout.
	has, err = f.keeper.Available.Has(ctx8, collections.Join(int64(1), idA))
	require.NoError(t, err)
	require.False(t, has, "epoch 1 purged after payout")
}

// ECVRF — when a miner has anchored a vrf_pubkey, availability must be proven by a VRF PROOF over the
// challenge rather than by echoing it: missing -> refused, wrong -> refused, valid -> accepted. Miners
// without a vrf_pubkey keep the echo, for backward compatibility.
func TestAvailabilityVrfProof(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	opV, err := f.addressCodec.BytesToString([]byte("operatorVrf_________________"))
	require.NoError(t, err)
	opL, err := f.addressCodec.BytesToString([]byte("operatorLegacy______________"))
	require.NoError(t, err)

	// VRF miner: generate an ECVRF key pair and anchor the public key.
	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	idV := deriveIDFor(t, f, opV)
	idL := deriveIDFor(t, f, opL)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opV, Operator: opV, MinerId: idV, Stake: 1000, VrfPubkey: hex.EncodeToString(pk)})
	require.NoError(t, err)
	// Legacy miner: no vrf_pubkey.
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opL, Operator: opL, MinerId: idL, Stake: 1000})
	require.NoError(t, err)

	// Roll the challenge at the epoch boundary.
	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("apphash-epoch1")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	require.NotEmpty(t, chal)

	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("apphash-epoch1")})

	// VRF miner WITHOUT a proof -> refused.
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: idV, Challenge: chal})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// VRF miner with a VALID proof over a DIFFERENT challenge -> refused (not replayable).
	wrongPi, err := vrf.Prove(sk, []byte("autre-defi"))
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: idV, Challenge: chal, VrfProof: hex.EncodeToString(wrongPi)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// VRF miner with a valid proof over the CORRECT challenge -> accepted, presence recorded.
	pi, err := vrf.Prove(sk, []byte(chal))
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: idV, Challenge: chal, VrfProof: hex.EncodeToString(pi)})
	require.NoError(t, err)
	has, err := f.keeper.Available.Has(ctx5, collections.Join(int64(1), idV))
	require.NoError(t, err)
	require.True(t, has)

	// Legacy miner (no vrf_pubkey): a plain echo is still accepted.
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opL, MinerId: idL, Challenge: chal})
	require.NoError(t, err)
}

// Anti-farming: with avail_require_demand=true (the default), a miner that is PRESENT but served NO
// demand — a farmer with no GPU — receives NOTHING; only a miner that served demand is paid.
func TestAvailabilityRequireDemand(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams() // AvailRequireDemand is true by default
	p.AvailEpochBlocks = 4
	p.AvailPayoutBps = 5000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.emission.avail = 1_000_000

	srv := keeper.NewMsgServerImpl(f.keeper)
	opWork, err := f.addressCodec.BytesToString([]byte("operatorWork________________"))
	require.NoError(t, err)
	opIdle, err := f.addressCodec.BytesToString([]byte("operatorIdle________________"))
	require.NoError(t, err)
	idW := deriveIDFor(t, f, opWork)
	idI := deriveIDFor(t, f, opIdle)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opWork, Operator: opWork, MinerId: idW, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opIdle, Operator: opIdle, MinerId: idI, Stake: 1000})
	require.NoError(t, err)
	// mw served demand; mi did not.
	mw, err := f.keeper.Miner.Get(f.ctx, idW)
	require.NoError(t, err)
	mw.Demand = 10
	require.NoError(t, gvSetMiner(f, f.ctx, idW, mw))

	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("h1")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("h1")})
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opWork, MinerId: idW, Challenge: chal})
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opIdle, MinerId: idI, Challenge: chal})
	require.NoError(t, err)
	// Epoch 1 payout.
	ctx8 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(8).WithHeaderInfo(header.Info{Height: 8, AppHash: []byte("h2")})
	require.NoError(t, f.keeper.EndBlock(ctx8))
	require.Greater(t, f.emission.availPaid[opWork], uint64(0), "miner with demand -> paid")
	require.Equal(t, uint64(0), f.emission.availPaid[opIdle], "farmer without demand -> NOT paid (anti-farming)")
}

// ADR-022 — in INCENTIVIZED mode (verification_mode=1), availability REQUIRES a VRF: a LEGACY miner
// (no vrf_pubkey) can no longer prove presence by echo, which ends AvailPool farming without a GPU. A
// VRF miner with a valid proof passes and collects the pool; the legacy miner is never paid. In legacy
// mode (0) the echo stays accepted (see TestAvailabilityVrfProof), so the end-to-end flow does not
// regress.
func TestADR022AvailRequiresVrfInOptimistic(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailPayoutBps = 5000
	p.AvailRequireDemand = false // isolate the VRF gate, without the demand filter
	p.VerificationMode = 1       // INCENTIVIZED mode -> VRF mandatory for availability (ADR-022)
	// verification_mode=1 carries consistency preconditions (see Params.Validate); set them to stay valid.
	p.AuditSampleBps = 1000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.emission.avail = 1_000_000

	srv := keeper.NewMsgServerImpl(f.keeper)
	opV, err := f.addressCodec.BytesToString([]byte("operatorVrf022______________"))
	require.NoError(t, err)
	opL, err := f.addressCodec.BytesToString([]byte("operatorLegacy022___________"))
	require.NoError(t, err)

	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	idV := deriveIDFor(t, f, opV)
	idL := deriveIDFor(t, f, opL)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opV, Operator: opV, MinerId: idV, Stake: 1000, VrfPubkey: hex.EncodeToString(pk)})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opL, Operator: opL, MinerId: idL, Stake: 1000})
	require.NoError(t, err)

	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("apphash-022")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	require.NotEmpty(t, chal)

	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("apphash-022")})

	// LEGACY (no vrf_pubkey) in incentivized mode -> REFUSED: the GPU-free farmable echo is over.
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opL, MinerId: idL, Challenge: chal})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "ADR-022: an echo without a VRF is refused in incentivized mode")

	// VRF with a VALID proof over the correct challenge -> accepted, presence recorded.
	pi, err := vrf.Prove(sk, []byte(chal))
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: idV, Challenge: chal, VrfProof: hex.EncodeToString(pi)})
	require.NoError(t, err)
	has, err := f.keeper.Available.Has(ctx5, collections.Join(int64(1), idV))
	require.NoError(t, err)
	require.True(t, has, "VRF presence recorded (epoch 1)")

	// Epoch 1 payout: only the VRF miner is present, so it takes the whole budget; the legacy farmer gets 0.
	ctx8 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(8).WithHeaderInfo(header.Info{Height: 8, AppHash: []byte("apphash-022-b")})
	require.NoError(t, f.keeper.EndBlock(ctx8))
	require.Greater(t, f.emission.availPaid[opV], uint64(0), "VRF miner -> paid")
	require.Equal(t, uint64(0), f.emission.availPaid[opL], "legacy farmer (no VRF) -> never paid in incentivized mode")
}

// ADR-022 — SLASHABLE LIVENESS: a bonded miner that is ABSENT (has not proven its availability)
// accumulates failures; at least avail_fail_k within the window triggers a bounded SLASH and a BURN. A
// PRESENT miner is never touched, and an ISOLATED failure (< k) does not slash, which is the
// anti-false-positive property. All of it is DORMANT while avail_slash_bps == 0.
func TestADR022AvailSlashLiveness(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2 // answer within 2 blocks of the challenge
	p.AvailSlashBps = 2000    // 20 % of the stake
	p.AvailSlashMax = 0       // no cap
	p.AvailFailWindow = 10
	p.AvailFailK = 2     // 2 failures per window before a slash (an isolated one does not slash)
	p.AvailPayoutBps = 0 // isolate the slash: no payout
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	opP, err := f.addressCodec.BytesToString([]byte("operatorPresent_____________"))
	require.NoError(t, err)
	opA, err := f.addressCodec.BytesToString([]byte("operatorAbsent______________"))
	require.NoError(t, err)
	idP := deriveIDFor(t, f, opP)
	idA := deriveIDFor(t, f, opA)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opP, Operator: opP, MinerId: idP, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: idA, Stake: 1000})
	require.NoError(t, err)

	proveEpoch := func(h int64, op, id string) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
		chal, err := f.keeper.AvailChallenge.Get(ctxH)
		require.NoError(t, err)
		_, err = srv.ProveAvailability(ctxH, &types.MsgProveAvailability{Creator: op, MinerId: id, Challenge: chal})
		require.NoError(t, err)
	}
	endBlock := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}

	endBlock(4)             // rolls the epoch 1 challenge (epoch 0 is exempt: bootstrap)
	proveEpoch(5, opP, idP) // mp present in epoch 1; ma ABSENT

	endBlock(8) // slash check for epoch 1 -> failure #1 for ma (< 2, no slash)
	ma, err := f.keeper.Miner.Get(f.ctx, idA)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), ma.Stake, "1 ISOLATED failure -> NO slash (anti false positive)")
	proveEpoch(9, opP, idP) // mp present in epoch 2; ma ABSENT

	endBlock(12) // slash check for epoch 2 -> failure #2 for ma (>= k) -> 20 % SLASH
	ma, err = f.keeper.Miner.Get(f.ctx, idA)
	require.NoError(t, err)
	require.Equal(t, uint64(800), ma.Stake, "2 failures within the window -> 20 % slash (1000 -> 800)")
	mp, err := f.keeper.Miner.Get(f.ctx, idP)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), mp.Stake, "a PRESENT miner is never slashed")
	require.Equal(t, uint64(200), f.bank.burned.AmountOf("udndr").Uint64(), "the availability slash is BURNED: deflationary, with no beneficiary")
}

// SLIDING WINDOW — ANTI BOUNDARY GAMING: under a tumbling window, two absences straddling a window
// reset (say epochs 4 and 5 with W=4 started at epoch 1) did NOT slash, because the reset erased the
// first one, so a chronic offender could place k-1 absences before every boundary forever. The sliding
// window slashes on at least k absences in ANY span of W epochs.
func TestADR022AvailSlashSlidingNoBoundaryGaming(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2
	p.AvailSlashBps = 2000 // 20 %
	p.AvailSlashMax = 0
	p.AvailFailWindow = 4
	p.AvailFailK = 2
	p.AvailPayoutBps = 0
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("operatorGaming______________"))
	require.NoError(t, err)
	idG := deriveIDFor(t, f, op)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: idG, Stake: 1000})
	require.NoError(t, err)

	prove := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
		chal, err := f.keeper.AvailChallenge.Get(ctxH)
		require.NoError(t, err)
		_, err = srv.ProveAvailability(ctxH, &types.MsgProveAvailability{Creator: op, MinerId: idG, Challenge: chal})
		require.NoError(t, err)
	}
	endBlock := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}

	endBlock(4) // rolls the challenge (epoch 0 is exempt)
	// Present in epochs 1-3: a tumbling window would have anchored at epoch 1 and RESET before epoch 5.
	prove(5)
	endBlock(8)
	prove(9)
	endBlock(12)
	prove(13)
	endBlock(16)
	// ABSENT in epochs 4 and 5, straddling the former tumbling boundary -> the sliding window MUST slash.
	endBlock(20) // epoch 4 processed: 1 isolated absence -> no slash
	m, err := f.keeper.Miner.Get(f.ctx, idG)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), m.Stake, "1 isolated absence -> no slash")
	endBlock(24) // epoch 5 processed: 2 absences within a span of 4 -> SLASH (a tumbling window missed this case)
	m, err = f.keeper.Miner.Get(f.ctx, idG)
	require.NoError(t, err)
	require.Equal(t, uint64(800), m.Stake, "2 absences within a span of W -> 20%% slash (anti boundary gaming)")
	require.Equal(t, uint64(200), f.bank.burned.AmountOf("udndr").Uint64(), "availability slash BURNED")
}

// SLIDING WINDOW — EXPIRY: absences SPACED further apart than W epochs NEVER add up, because old
// failures slide out of the window, so an honest miner with rare outages accumulates no perpetual debt.
func TestADR022AvailSlashSlidingExpiry(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2
	p.AvailSlashBps = 2000
	p.AvailFailWindow = 2 // short window: 2 epochs
	p.AvailFailK = 2
	p.AvailPayoutBps = 0
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("operatorExpiry______________"))
	require.NoError(t, err)
	idE := deriveIDFor(t, f, op)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: idE, Stake: 1000})
	require.NoError(t, err)

	prove := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
		chal, err := f.keeper.AvailChallenge.Get(ctxH)
		require.NoError(t, err)
		_, err = srv.ProveAvailability(ctxH, &types.MsgProveAvailability{Creator: op, MinerId: idE, Challenge: chal})
		require.NoError(t, err)
	}
	endBlock := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}

	endBlock(4) // challenge rolled
	endBlock(8) // epoch 1: ABSENT (1 of 2, no slash)
	prove(9)
	endBlock(12) // epoch 2: present
	prove(13)
	endBlock(16) // epoch 3: present (the epoch 1 absence has slid out of the W=2 window)
	endBlock(20) // epoch 4: ABSENT — never 2 absences within a span of 2 -> NO slash
	m, err := f.keeper.Miner.Get(f.ctx, idE)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), m.Stake, "absences spaced further apart than W expire, and never slash")
	require.True(t, f.bank.burned.IsZero(), "no burn")
}

// ADR-022 — DORMANCY: avail_slash_bps == 0 (the default) means no availability slash at all, even when
// a bonded miner never proves its presence. The honest availability flow is unchanged.
func TestADR022AvailSlashDormant(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams() // avail_slash_bps=0 -> slashable liveness OFF
	p.AvailEpochBlocks = 4
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	opA, err := f.addressCodec.BytesToString([]byte("operatorDormant_____________"))
	require.NoError(t, err)
	idA := deriveIDFor(t, f, opA)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: idA, Stake: 1000})
	require.NoError(t, err)

	for _, h := range []int64{4, 8, 12, 16} { // 4 epochs without ever proving
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}
	ma, err := f.keeper.Miner.Get(f.ctx, idA)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), ma.Stake, "slashable availability OFF (default) -> stake intact despite the absence")
	require.True(t, f.bank.burned.IsZero(), "no burn while dormant")
}
