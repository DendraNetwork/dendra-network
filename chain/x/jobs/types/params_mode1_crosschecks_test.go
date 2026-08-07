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

// (2) THE COMMITTEE SEED MUST NOT BE PREDICTABLE — at least one of the two sources armed.
func TestMode1RequiresAnUnpredictableSeed(t *testing.T) {
	nu := mode1Coherent()
	nu.CommitteeRevealDelay = 0
	nu.CommitteeSeedSource = 0
	require.Error(t, nu.Validate(),
		"optimistic mode with BOTH sources of unpredictability at zero: the seed is fixed at open time "+
			"on `height:time`, which the job creator observes, so it reopens until it draws the committee "+
			"it wants — and in optimistic mode that committee IS the only verification")

	// Either one ALONE is enough: the guard is about the property, not about one mechanism.
	parRevelation := nu
	parRevelation.CommitteeRevealDelay = 1
	require.NoError(t, parRevelation.Validate(), "deferred reveal alone must suffice")

	parVrf := nu
	parVrf.CommitteeSeedSource = 1
	require.NoError(t, parVrf.Validate(), "a decentralised VRF seed alone must suffice")

	// Dormancy: in redundant mode neither is required.
	nu.VerificationMode = 0
	require.NoError(t, nu.Validate())
}

// (3) A CONTRIBUTOR FLOOR WITH NO SOURCE TO GUARD IS AN INERT GUARD.
//
// `committee_min_vrf_contributors` is only evaluated on the decentralised seed path. Posted without
// `committee_seed_source=1`, it is never read: governance believes it has required a decentralisation
// floor that does not exist. The contradiction does not depend on the verification mode.
func TestPlancherVrfExigeLaSourceVrf(t *testing.T) {
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
func TestLesCroisementsMode1NeTouchentPasLesDefauts(t *testing.T) {
	p := types.DefaultParams()
	require.Equal(t, uint64(0), p.VerificationMode, "the default remains redundant mode")
	require.Equal(t, uint64(0), p.DisputeBond)
	require.Equal(t, uint64(0), p.CommitteeRevealDelay)
	require.Equal(t, uint64(0), p.CommitteeSeedSource)
	require.NoError(t, p.Validate(),
		"the DEFAULT parameters are refused: a bound of optimistic mode has spilled onto redundant mode, "+
			"and a fresh chain would no longer start")
}
