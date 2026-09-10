package types_test

import (
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// TestInvariant8_SybilWashNegativeEV pins the Sybil-wash invariant.
//
// `Demand` (credited when client != operator) does NOT measure external traction (the oracle problem is
// unsolvable on-chain), so `r_settlement` is a PROXY. The only REAL risk is an emission DRAIN, which exists
// ONLY if Sybil self-dealing becomes +EV. At the SHIPPED parameters it is -EV: the operator loses cut+burn to
// unlock WorkGate x (team+treasury). This test FREEZES that -EV: if a governance increase of work_gate_bps OR
// a shift of the cut -> team/treasury split broke the inequality, the wash would become a real drain, and this
// test MUST fail.
func TestInvariant8_SybilWashNegativeEV(t *testing.T) {
	p := types.DefaultParams()
	wg := p.WorkGateBps // 15000 = the per-miner subsidy gate that the wash exploits (msg_server_claim_subsidy.go:39)

	if !p.WashSubsidyNegativeEV(wg) {
		t.Fatalf("INVARIANT #8 BROKEN: Sybil self-dealing is +EV at the shipped parameters "+
			"(ProtocolFee=%d ValidatorReward=%d FeeBurn=%d WorkGate=%d) -> an emission DRAIN is possible",
			p.ProtocolFeeBps, p.ValidatorRewardBps, p.FeeBurnBps, wg)
	}

	// Sanity check on the audited order of magnitude: a subsidy of 1125 bps of the fee < a cost of 2000 bps,
	// margin ~1.78x. team+treasury = ProtocolFee x (10000 - ValidatorReward)/10000 = 1500 x 5000/10000 = 750 bps;
	// subsidy = WorkGate/10000 x 750 = 1.5 x 750 = 1125 bps; cost = ProtocolFee + FeeBurn = 2000 bps.
	if p.ProtocolFeeBps == 1500 && p.ValidatorRewardBps == 5000 && p.FeeBurnBps == 500 && wg == 15000 {
		tt := p.ProtocolFeeBps * (10000 - p.ValidatorRewardBps) / 10000 // 750
		sub := wg * tt / 10000                                          // 1125 (multiply BEFORE dividing)
		cost := p.ProtocolFeeBps + p.FeeBurnBps                         // 2000
		if tt != 750 || cost != 2000 || sub != 1125 || sub >= cost {
			t.Fatalf("sanity broken: team+treasury=%d sub=%d cost=%d (expected 750 / 1125 / 2000)", tt, sub, cost)
		}
	}
}

// TestInvariant8_GuardDiscriminates — the helper really DISCRIMINATES: a high enough work_gate_bps makes the
// wash +EV and the guard MUST return false. Tipping point: subsidy >= cost <=> WorkGate/10000 x 750 >= 2000
// <=> WorkGate >= 26666.67, so 26667 is +EV (false) and 26666 stays -EV (true).
func TestInvariant8_GuardDiscriminates(t *testing.T) {
	p := types.DefaultParams()
	if p.WashSubsidyNegativeEV(26667) {
		t.Fatalf("the guard did not bite: WorkGate=26667 should make the wash +EV (therefore NOT -EV)")
	}
	if !p.WashSubsidyNegativeEV(26666) {
		t.Fatalf("false positive from the guard: WorkGate=26666 should stay -EV")
	}
}

// TestInvariant8_GenesisRejectsPlusEV — the -EV guard does not live ONLY in MsgUpdateParams.
// GenesisState.Validate() (hence `validate-genesis`) also refuses a genesis whose parameters would make the
// wash +EV, so a Params.Set at genesis load cannot bypass the invariant.
func TestInvariant8_GenesisRejectsPlusEV(t *testing.T) {
	gs := types.DefaultGenesis()
	if err := gs.Validate(); err != nil {
		t.Fatalf("the default genesis (-EV) was wrongly rejected: %v", err)
	}
	gs.Params.WorkGateBps = 26667 // makes the wash +EV at the default split
	if err := gs.Validate(); err == nil {
		t.Fatal("a +EV genesis (work_gate=26667) MUST be rejected by invariant #8")
	}
}

// TestValidate_Mode1FloorsDissuasion — in ARMED optimistic mode, governance cannot ZERO OUT the deterrent
// (slash_leak_bps=0 => a free slash => cheating is +EV, which invariant #8 does not cover since it guards the
// SUBSIDY). Lower bounds ACTIVE in mode-1 only: slash_leak_bps >= 2500, audit_sample_bps >= 500. In dormant
// mode-0 the floors are INACTIVE.
func TestValidate_Mode1FloorsDissuasion(t *testing.T) {
	p := types.DefaultParams() // mode-0: the floors do not apply
	if err := p.Validate(); err != nil {
		t.Fatalf("defaults (mode-0) wrongly rejected: %v", err)
	}
	// Arm optimistic mode properly: timeout STRICTLY greater than the dispute window, a non-zero dispute
	// bond, and an unpredictable committee seed source.
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.DisputeBond = 1000
	p.CommitteeRevealDelay = 1
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-1 at the shipped values (slash_leak=8000) wrongly rejected: %v", err)
	}
	p.SlashLeakBps = 0
	if err := p.Validate(); err == nil {
		t.Fatal("mode-1 + slash_leak_bps=0 MUST be rejected (deterrent zeroed out by governance)")
	}
	p.SlashLeakBps = 2499
	if err := p.Validate(); err == nil {
		t.Fatal("mode-1 + slash_leak_bps=2499 MUST be rejected (floor is 2500)")
	}
	p.SlashLeakBps = 2500
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-1 + slash_leak_bps=2500 (exact floor) wrongly rejected: %v", err)
	}
	p.AuditSampleBps = 499
	if err := p.Validate(); err == nil {
		t.Fatal("mode-1 + audit_sample_bps=499 MUST be rejected (floor is 500)")
	}
	p.AuditSampleBps = 500
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-1 + audit_sample_bps=500 wrongly rejected: %v", err)
	}
	// Back to mode-0: dormancy is STRICTLY intact (slash_leak=0 becomes valid again).
	p.VerificationMode = 0
	p.SlashLeakBps = 0
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-0 + slash_leak=0 must stay VALID (dormant): %v", err)
	}
}
