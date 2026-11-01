package types_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// An armed optimistic mode requires its guards armed.
//
// Optimistic mode (`verification_mode=1`) replaces k=3 redundancy with one execution plus a sampled
// audit. What peer comparison guarantees under k=3 rests, in this mode, on three things: disputing costs
// something, the audit committee cannot be chosen, and the slash bites. The first two need a check:
// without one, a genesis could arm optimistic mode with a zero dispute bond and a committee
// seed the job creator computes in advance.
//
// The defect has the shape of the zero value: these fields at 0 cannot be told apart from a choice,
// so "not set" read as "deliberately disarmed". These cases make them required.
//
// The bounds are dormant in mode 0 and change nothing on a redundant chain. The final witness checks
// that the mode that did not need hardening was left alone.

// mode1Coherent returns the smallest parameter set that arms optimistic mode with nothing left at zero.
func mode1Coherent() types.Params {
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.DisputeBond = 1000
	p.CommitteeRevealDelay = 1
	return p
}

func TestMode1CoherentIsAccepted(t *testing.T) {
	require.NoError(t, mode1Coherent().Validate(),
		"the reference set must pass, otherwise the cases below would go red for the wrong reason")
}

// (1) Disputing must not be free.
func TestMode1RequiresADisputeBond(t *testing.T) {
	p := mode1Coherent()
	p.DisputeBond = 0
	require.Error(t, p.Validate(),
		"optimistic mode with dispute_bond=0 was accepted: disputes are active (dispute_window>0) and "+
			"the anti-grief forfeiture forfeits nothing, so disputing every settled job costs only gas")

	// Dormancy: in redundant mode a zero bond stays a valid choice.
	p.VerificationMode = 0
	require.NoError(t, p.Validate(),
		"the bound must not bite in redundant mode, where peer comparison carries the verification")
}

// (2) The committee seed must not be readable before the block that uses it.
//
// The two settings answer two different attackers, so they are not interchangeable. A decentralised
// seed stops a single actor from picking the value; only a deferred reveal stops the proposer from
// seeing it in time to act, and the proposer composes the very block the seed would be fixed in.
func TestMode1RequiresAnUnpredictableSeed(t *testing.T) {
	bare := mode1Coherent()
	bare.CommitteeRevealDelay = 0
	bare.CommitteeSeedSource = 0
	require.Error(t, bare.Validate(),
		"optimistic mode with a zero reveal delay and no decentralised source: the seed is fixed at "+
			"open time on `height:time`, which the job creator observes, so it reopens until it draws "+
			"the committee it wants — and in optimistic mode that committee is the only verification")

	// The deferred reveal is the one that is required.
	byReveal := bare
	byReveal.CommitteeRevealDelay = 1
	require.NoError(t, byReveal.Validate(), "a deferred reveal must suffice")

	// The decentralised source does not substitute for it. With `delay = 0` the seed is fixed inside
	// the opening block: the proposer reads the pending job, assembles the vote extensions the seed is
	// built from, and knows the committee before it commits to proposing. Accepting this combination
	// let a proposer pick the committee of the job it was about to be paid for.
	byVrf := bare
	byVrf.CommitteeSeedSource = 1
	require.Error(t, byVrf.Validate(),
		"a decentralised seed with a zero delay must be refused: not controlling the value is not the "+
			"same as not seeing it before acting, and the proposer composes the block that fixes it")

	// And both together are the configuration the network actually runs.
	both := bare
	both.CommitteeSeedSource = 1
	both.CommitteeRevealDelay = 1
	require.NoError(t, both.Validate(), "decentralised source and deferred reveal must be accepted")

	// Dormancy: in redundant mode neither is required.
	bare.VerificationMode = 0
	require.NoError(t, bare.Validate())
}

// (3) A contributor floor with no source to guard is an inert guard.
//
// `committee_min_vrf_contributors` is only evaluated on the decentralised seed path. Posted without
// `committee_seed_source=1`, it is never read: governance believes it has required a
// decentralisation floor that does not exist. The contradiction does not depend on the verification
// mode.
func TestVrfFloorRequiresTheVrfSource(t *testing.T) {
	for _, mode := range []uint64{0, 1} {
		p := mode1Coherent()
		p.VerificationMode = mode
		p.CommitteeMinVrfContributors = 3
		p.CommitteeSeedSource = 0
		require.Error(t, p.Validate(),
			"mode %d: a VRF contributor floor posted without a decentralised VRF seed — the floor is "+
				"never evaluated", mode)

		p.CommitteeSeedSource = 1
		require.NoError(t, p.Validate(), "mode %d: floor consistent with its source", mode)
	}
}

// (4) Dormancy witness: the defaults (redundant mode) stay valid and none of these bounds applies.
// Without this witness, the redundant mode could have been hardened unnoticed.
func TestMode1CrosschecksDoNotTouchTheDefaults(t *testing.T) {
	p := types.DefaultParams()
	require.Equal(t, uint64(0), p.VerificationMode, "the default remains redundant mode")
	require.Equal(t, uint64(0), p.DisputeBond)
	require.Equal(t, uint64(0), p.CommitteeRevealDelay)
	require.Equal(t, uint64(0), p.CommitteeSeedSource)
	require.NoError(t, p.Validate(),
		"the default parameters are refused: a bound of optimistic mode has spilled onto redundant mode, "+
			"and a fresh chain would no longer start")
}
