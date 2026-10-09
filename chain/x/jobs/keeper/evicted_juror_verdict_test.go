package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A JUROR THAT VOTED AND WAS THEN EVICTED HAS ITS VERDICT ERASED FROM THE TALLY.
//
// `auditSlashDecision` walks the anchored verdicts and skips any whose miner record is gone
// (`antievasion.go`, "a registered miner is required"). That is a defensible rule — the tally weighs
// stake, and a departed miner carries none — but it has a consequence nothing had written down: the
// count of voters can fall BELOW the quorum floor because of an eviction unrelated to the audit, and
// a primary the committee judged guilty then walks away unslashed.
//
// ⛔ AND THE EVICTION IS AUTOMATIC. `pruneInactiveMiners` runs in EndBlock and removes the miner record
// of a juror that has been inactive too long. No attacker is needed: a juror that votes and then goes
// quiet for the prune window erases its own vote. Measured before writing this file: `Miner.Remove`
// appeared in ZERO test files, so no case had ever built this state.
//
// The pair below is the whole point — the same committee, the same verdicts, one registry difference.

func ejSetup(t *testing.T, f *fixture, jobID string) (types.Params, sdk.AccAddress) {
	t.Helper()
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "ejP", aeStake)
	for _, j := range []string{"ejJ1", "ejJ2", "ejJ3", "ejJ4"} {
		aeReg(f, t, j, 1)
	}
	aeAnchor(f, t, jobID, "ejJ1", "ejJ2", "ejJ3", "ejJ4")
	for _, j := range []string{"ejJ1", "ejJ2", "ejJ3", "ejJ4"} {
		aeVerdict(f, t, jobID, j, "0") // unanimous "invalid": the committee says the primary cheated
	}

	clientAcc := sdk.AccAddress([]byte("client_evicted_juror"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		JobId: jobID, State: "open+paid+optimistic+disputed", MinerId: "ejP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), jobID)))
	return p, clientAcc
}

// (a) THE CONTROL: four jurors present, four "invalid" verdicts, the floor is met, the primary is
// slashed. Without this half the case below would prove nothing — a bench that only shows the absence
// of a slash cannot tell "the eviction changed the outcome" from "this fixture never slashed".
func TestFourPresentJurorsReachTheFloorAndSlash(t *testing.T) {
	f := initFixture(t)
	p, _ := ejSetup(t, f, "jEJa")

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jEJa")
	require.NoError(t, err)
	require.Contains(t, job.State, "clawed", "four voters meet the floor: the verdict concludes")
	mP, err := f.keeper.Miner.Get(f.ctx, "ejP")
	require.NoError(t, err)
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "the guilty primary loses 80 %")
}

// (b) THE SAME COMMITTEE, THE SAME VERDICTS, ONE JUROR EVICTED: the tally drops to three, the floor is
// no longer met, and the primary keeps everything. The verdict did not change — the registry did.
func TestAnEvictedJurorErasesItsVerdictAndSparesTheSlash(t *testing.T) {
	f := initFixture(t)
	_, _ = ejSetup(t, f, "jEJb")

	// The state `pruneInactiveMiners` produces: the miner record is gone, the anchored seat and the
	// verdict remain. Removing the record directly is what the EndBlocker does, minus the window setup.
	require.NoError(t, f.keeper.Miner.Remove(f.ctx, "ejJ4"))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jEJb")
	require.NoError(t, err)
	require.NotContains(t, job.State, "clawed",
		"three counted voters are below the floor: the audit cannot conclude, so nothing is taken")
	require.Empty(t, job.SlashRecords, "and no slash is recorded either")

	mP, err := f.keeper.Miner.Get(f.ctx, "ejP")
	require.NoError(t, err)
	require.Equal(t, aeStake, mP.Stake,
		"the primary the committee called guilty keeps its whole stake because one juror left the registry")

	// The verdict itself is untouched: what changed is who is allowed to be counted. Asserting this
	// separates "the vote disappeared" from "the vote no longer counts" — only the second is true.
	has, err := f.keeper.Commit.Has(f.ctx, "jEJb__verdict__ejJ4")
	require.NoError(t, err)
	require.True(t, has, "the anchored verdict is still there; it is the TALLY that drops it")
}

// ⛔ AND THE ACCUSED MUST NOT SIT ON ITS OWN JURY — a guard that NOTHING was testing.
//
// `auditSlashDecision` skips the primary's own verdict ("exclude the primary, the sole origin under
// k=1"). Measured by mutation: removing that line leaves the ENTIRE keeper suite green. Nothing
// forbade the accused from voting on its own guilt.
//
// The consequence is not a rounding detail. The tally weighs STAKE, and a primary carries far more of
// it than the jurors drawn to judge it: its single "valid" vote outweighs a unanimous jury, and the
// slash disappears. This case is the same committee as (a) plus one extra verdict — the primary's.
func TestThePrimaryCannotVoteOnItsOwnGuilt(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "ejP", aeStake)
	for _, j := range []string{"ejJ1", "ejJ2", "ejJ3", "ejJ4"} {
		aeReg(f, t, j, 1)
	}
	// ⛔ THE PRIMARY IS ANCHORED AMONG THE SEATS. This is the only state where the exclusion line does
	// any work: a primary that is NOT in the committee is already dropped by the `allowed` test one line
	// above, so a case built without this anchoring passes whether the guard exists or not — measured,
	// and it is why the first version of this case proved nothing.
	aeAnchor(f, t, "jEJc", "ejP", "ejJ1", "ejJ2", "ejJ3", "ejJ4")
	for _, j := range []string{"ejJ1", "ejJ2", "ejJ3", "ejJ4"} {
		aeVerdict(f, t, "jEJc", j, "0")
	}
	// The accused anchors its own verdict, claiming the work was valid — and it carries far more stake
	// than the whole jury, so counting it would flip the stake majority.
	aeVerdict(f, t, "jEJc", "ejP", "1")

	clientAcc := sdk.AccAddress([]byte("client_primary_juror"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jEJc", types.Job{
		JobId: "jEJc", State: "open+paid+optimistic+disputed", MinerId: "ejP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jEJc")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jEJc")
	require.NoError(t, err)
	require.Contains(t, job.State, "clawed",
		"the primary's own vote must not be counted: the jury's unanimous verdict stands")
	mP, err := f.keeper.Miner.Get(f.ctx, "ejP")
	require.NoError(t, err)
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake,
		"and the slash lands exactly as it does without that self-serving verdict")
}
