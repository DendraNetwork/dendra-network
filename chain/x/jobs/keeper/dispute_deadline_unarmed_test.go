package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// "EVERY DISPUTE HAS A DEADLINE" HELD ONLY WHILE ONE PARAMETER WAS NON-ZERO, AND ONLY IN ONE MODE.
//
// `DisputeVerdict` books the appointment that guarantees an open dispute cannot hang for ever, and its
// own comment gives the reason: the re-adjudication pool is N-5, so "too small, or with those drawn
// staying silent, and the job would stay `+disputed` FOREVER — contradicting the claim in
// `audit_sampling.go` that no job remains +disputed indefinitely. […] every dispute has a deadline".
// The whole block sits inside `if p.AuditResolveTimeout > 0`.
//
// `Params.Validate` coupled the two parameters only under `verification_mode == 1`. In k=3 mode nothing
// did, and `DisputeVerdict` itself gates only on `dispute_window == 0`. So `dispute_window > 0` beside
// `audit_resolve_timeout = 0` was a legal set, one MsgUpdateParams away. MEASURED there, before the
// fix: the dispute was accepted, the bond left the disputer, the job was marked, NO appointment existed
// at any height, and a hundred thousand blocks later nothing had closed.
//
// The reason never depended on the mode, so the clause is now general (`params.go`), guarded by
// `dispute_window > 0` — the defaults are 0/0, so a dormant dispute system trips nothing.
//
// ⛔ WHERE THE INVARIANT NOW LIVES, AND WHY CASE (b) EXISTS. The handler's block is STILL conditional:
// nothing inside it forbids a zero timeout. The guarantee rests entirely on validation refusing that
// set at the door. Case (b) writes the unarmed parameters straight into the store, bypassing
// `Validate` as no message can, and shows what the handler alone still does — so that whoever ever
// relaxes the validation clause can see, in one place, exactly what it was holding up.
//
// ⚠️ AND THE FIX ITSELF LANDED IN THE WRONG SCOPE FIRST. The clause was inserted with the indentation
// of the outer level while the braces kept it inside the `verification_mode == 1` block. Go does not
// read indentation: build green, vet green, eight packages green, effect NIL. What revealed it was
// this file's premise assertion NOT failing when it should have. A test that asserts its own premise
// is what turns a silent no-op into a red.

func ddParams(window, timeout uint64) types.Params {
	p := types.DefaultParams() // verification_mode stays 0: the mode that had no coupling
	p.DisputeWindow = window
	p.DisputeBond = 4000
	p.AuditResolveTimeout = timeout
	return p
}

// ddSetup wires a settled job and a funded disputer. `valide` selects how the parameters reach the
// store: through `Validate` (what governance can post) or straight into it (what only a test can do).
func ddSetup(t *testing.T, jobID, disputerSeed string, p types.Params, valide bool) (*fixture, types.MsgServer, string) {
	t.Helper()
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	if valide {
		require.NoError(t, p.Validate(), "premise: this set must be one governance can actually post")
	}
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the dispute window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(disputerSeed)))
	require.NoError(t, err)
	dispBz, err := f.addressCodec.StringToBytes(disp)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000))))

	require.NoError(t, gvSetMiner(f, f.ctx, "mDD", types.Miner{MinerId: "mDD", Stake: adr033Big}))
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		JobId: jobID, State: "open+paid", MinerId: "mDD", Fee: 100000,
	}))
	return f, srv, disp
}

// ddAppointed reports whether ANY resolve appointment exists for this job, at any height. The walk is
// what proves it: reading one computed height would only say "not at the height I guessed".
func ddAppointed(t *testing.T, f *fixture, jobID string) bool {
	t.Helper()
	found := false
	require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil,
		func(key collections.Pair[int64, string]) (bool, error) {
			if key.K2() == jobID {
				found = true
				return true, nil
			}
			return false, nil
		}))
	return found
}

// (a) THE DOOR IS SHUT: a dispute system that can be opened without a deadline is no longer a set
// governance can post, in any mode. The refusal must also name the ordering, not merely the zero.
func TestOpeningDisputesWithoutADeadlineIsRefusedInEveryMode(t *testing.T) {
	err := ddParams(10, 0).Validate()
	require.Error(t, err, "dispute_window > 0 with no resolve timeout leaves an open dispute with no exit")
	require.Contains(t, err.Error(), "audit_resolve_timeout")

	// Not only zero: a timeout at or below the window resolves before adjudication can run. That was
	// already the mode-1 reason; it is now everyone's.
	require.Error(t, ddParams(10, 10).Validate(), "a timeout equal to the window is refused too")
	require.Error(t, ddParams(10, 3).Validate(), "and one below it")

	// ...and the dormant configuration stays valid, or the clause would stop the network instead of
	// protecting it. The defaults are 0/0.
	require.NoError(t, types.DefaultParams().Validate(), "a dormant dispute system trips nothing")
	require.NoError(t, ddParams(10, 11).Validate(), "armed above the window: legal, as before")
}

// (b) WHAT THE HANDLER ALONE STILL DOES, with the unarmed parameters written straight into the store —
// a state no message can now produce. The block is conditional, so the whole guarantee is the
// validation clause above. Whoever relaxes it will find this case waiting.
func TestTheHandlerItselfBooksNoDeadlineWhenTheTimeoutIsZero(t *testing.T) {
	f, srv, disp := ddSetup(t, "jDD0", "disputer_dd_zero_______", ddParams(10, 0), false)

	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jDD0"})
	require.NoError(t, err, "the handler gates only on dispute_window: it accepts")

	dispBz, err := f.addressCodec.StringToBytes(disp)
	require.NoError(t, err)
	require.Equal(t, int64(10000-4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"the bond really left the disputer: this is not a call that did nothing")

	job, err := f.keeper.Job.Get(f.ctx, "jDD0")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed")
	require.False(t, ddAppointed(t, f, "jDD0"),
		"NO resolve appointment at any height: the guarantee the handler pays for is simply absent")

	// Nobody re-commits, nobody judges — the state the block exists for. Drive the EndBlocker far past
	// any plausible deadline: the job is still open and the bond still gone.
	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 100_000
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, err = f.keeper.Job.Get(f.ctx, "jDD0")
	require.NoError(t, err)
	require.NotContains(t, job.State, "resolved",
		"a hundred thousand blocks later the dispute has still not closed by itself")
	require.Equal(t, int64(10000-4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"and the bond has not come back either")
}

// (c) THE CONTROL, through the door governance actually uses: armed above the window, the appointment
// exists and the EndBlocker closes the dispute with no juror at all. Without this half, "it did not
// resolve" in (b) could not be told apart from "this fixture never resolves anything".
func TestAnArmedDisputeClosesOnItsOwnWithNoJuror(t *testing.T) {
	f, srv, disp := ddSetup(t, "jDD5", "disputer_dd_armed______", ddParams(10, 15), true)

	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jDD5"})
	require.NoError(t, err)
	require.True(t, ddAppointed(t, f, "jDD5"), "armed, the handler books the appointment")

	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 15
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, err := f.keeper.Job.Get(f.ctx, "jDD5")
	require.NoError(t, err)
	require.Contains(t, job.State, "resolved",
		"the deadline decides with no juror at all -- exactly what case (b) loses")
}
