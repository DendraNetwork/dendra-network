package types_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══ AN ARMED OPTIMISTIC MODE REQUIRES ITS GUARDS ARMED ══════════════════════════════════════════
//
// What was documented and not required. Optimistic mode (`verification_mode=1`) replaces k=3
// redundancy with ONE execution plus a sampled audit. Everything that guaranteed correctness through
// peer comparison now rests on three things: that disputing costs something, that the audit
// committee cannot be chosen, and that the slash bites. The first two were constrained NOWHERE — a
// genesis could arm optimistic mode with a zero dispute bond and a committee seed the job creator
// computes in advance.
//
// The defect has the shape of the zero value: these fields at 0 are indistinguishable from a choice,
// and "not set" read as "deliberately disarmed". These cases make them REQUIRED.
//
// The bounds are DORMANT in mode 0: they change nothing on a redundant chain. The final witness
// checks that — without it, the mode that did not need hardening could have been hardened.

// mode1Coherent — the smallest parameter set that arms optimistic mode with nothing left at zero.
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

func TestMode1CoherentEstAccepte(t *testing.T) {
	require.NoError(t, mode1Coherent().Validate(),
		"the reference set must pass, otherwise the cases below would go red for the wrong reason")
}

// (1) DISPUTING MUST NOT BE FREE.
func TestMode1ExigeUnBondDeDispute(t *testing.T) {
	p := mode1Coherent()
	p.DisputeBond = 0
	require.Error(t, p.Validate(),
		"optimistic mode with dispute_bond=0 was accepted: disputes are ACTIVE (dispute_window>0) and "+
			"the anti-grief forfeiture forfeits nothing, so disputing every settled job costs only gas")

	// Dormancy: in redundant mode a zero bond stays a valid choice.
	p.VerificationMode = 0
	require.NoError(t, p.Validate(),
		"the bound must not bite in redundant mode, where peer comparison carries the verification")
}

// (2) THE COMMITTEE SEED MUST NOT BE READABLE BEFORE THE BLOCK THAT USES IT.
//
// The two knobs answer two different attackers, so they are not interchangeable. A decentralised
// seed stops a single actor from PICKING the value; only a deferred reveal stops the PROPOSER from
// SEEING it in time to act — and the proposer composes the very block the seed would be fixed in.
func TestMode1RequiresAnUnpredictableSeed(t *testing.T) {
	nu := mode1Coherent()
	nu.CommitteeRevealDelay = 0
	nu.CommitteeSeedSource = 0
	require.Error(t, nu.Validate(),
		"optimistic mode with a zero reveal delay and no decentralised source: the seed is fixed at "+
			"open time on `height:time`, which the job creator observes, so it reopens until it draws "+
			"the committee it wants — and in optimistic mode that committee IS the only verification")

	// THE DEFERRED REVEAL IS THE ONE THAT IS REQUIRED.
	parRevelation := nu
	parRevelation.CommitteeRevealDelay = 1
	require.NoError(t, parRevelation.Validate(), "a deferred reveal must suffice")

	// AND THE DECENTRALISED SOURCE DOES NOT SUBSTITUTE FOR IT. With `delay = 0` the seed is fixed
	// INSIDE the opening block: the proposer reads the pending job, assembles the vote extensions the
	// seed is built from, and knows the committee before it commits to proposing. Accepting this
	// combination is what let a proposer pick the committee of the job it was about to be paid for.
	parVrf := nu
	parVrf.CommitteeSeedSource = 1
	require.Error(t, parVrf.Validate(),
		"a decentralised seed with a ZERO delay must be REFUSED: not controlling the value is not the "+
			"same as not seeing it before acting, and the proposer composes the block that fixes it")

	// And both together are the configuration the network actually runs.
	lesDeux := nu
	lesDeux.CommitteeSeedSource = 1
	lesDeux.CommitteeRevealDelay = 1
	require.NoError(t, lesDeux.Validate(), "decentralised source AND deferred reveal must be accepted")

	// Dormancy: in redundant mode neither is required.
	nu.VerificationMode = 0
	require.NoError(t, nu.Validate())
}

// (3) A CONTRIBUTOR FLOOR WITH NO SOURCE TO GUARD IS AN INERT GUARD.
//
// `committee_min_vrf_contributors` is only evaluated on the decentralised seed path. Posted without
// `committee_seed_source=1`, it is never read: governance believes it has required a decentralisation
// floor that does not exist. The contradiction does not depend on the verification mode.
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

// (4) DORMANCY WITNESS — the defaults (redundant mode) stay valid and none of these bounds bites.
// Without this witness, the redundant mode could have been hardened unnoticed.
func TestMode1CrosschecksDoNotTouchTheDefaults(t *testing.T) {
	p := types.DefaultParams()
	require.Equal(t, uint64(0), p.VerificationMode, "the default remains redundant mode")
	require.Equal(t, uint64(0), p.DisputeBond)
	require.Equal(t, uint64(0), p.CommitteeRevealDelay)
	require.Equal(t, uint64(0), p.CommitteeSeedSource)
	require.NoError(t, p.Validate(),
		"the DEFAULT parameters are refused: a bound of optimistic mode has spilled onto redundant mode, "+
			"and a fresh chain would no longer start")
}
