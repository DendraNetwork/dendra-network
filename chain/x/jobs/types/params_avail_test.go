package types_test

import (
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-022 — slashable availability (VRF-sampled challenge). These tests FREEZE the dormancy and the
// consistency bounds that apply at activation. While the avail_* params are 0, Validate() is STRICTLY
// unchanged.

// armedAvail returns DefaultParams with the availability machinery COMPLETE and consistent (slash armed).
func armedAvail() types.Params {
	p := types.DefaultParams()
	p.AvailEpochBlocks = 300   // challenge epoch
	p.AvailDeadlineBlocks = 50 // response deadline
	p.AvailThresholdBps = 7000 // cosine threshold (anything lower is aberrant)
	p.AvailSlashBps = 500      // 5 % of the stake
	p.AvailSlashMax = 1000000  // absolute cap (udndr)
	p.AvailFailWindow = 10     // sliding window
	p.AvailFailK = 3           // 3 failures out of 10 before a slash (anti-false-positive)
	return p
}

// TestAvail_DormantByDefault — as shipped, slashable availability is OFF and Validate is unchanged.
func TestAvail_DormantByDefault(t *testing.T) {
	p := types.DefaultParams()
	if p.AvailSlashBps != 0 || p.AvailFailK != 0 || p.AvailDeadlineBlocks != 0 ||
		p.AvailThresholdBps != 0 || p.AvailFailWindow != 0 || p.AvailSlashMax != 0 {
		t.Fatalf("ADR-022 must be DORMANT by default (every avail_* param at 0)")
	}
	if err := p.Validate(); err != nil {
		t.Fatalf("DefaultParams (availability dormant) must validate: %v", err)
	}
}

// TestAvail_ArmedComplete — the complete and consistent machinery validates.
func TestAvail_ArmedComplete(t *testing.T) {
	if err := armedAvail().Validate(); err != nil {
		t.Fatalf("a COMPLETE armed availability configuration must validate: %v", err)
	}
}

// TestAvail_ArmedIncompleteRejected — an ARMED slash without its machinery is REJECTED: it would be
// either inert or dangerous.
func TestAvail_ArmedIncompleteRejected(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(p *types.Params)
	}{
		{"no_epoch", func(p *types.Params) { p.AvailEpochBlocks = 0 }},
		{"no_deadline", func(p *types.Params) { p.AvailDeadlineBlocks = 0 }},
		{"no_fail_k", func(p *types.Params) { p.AvailFailK = 0 }},
		{"no_fail_window", func(p *types.Params) { p.AvailFailWindow = 0 }},
	}
	for _, c := range cases {
		p := armedAvail()
		c.mutate(&p)
		if err := p.Validate(); err == nil {
			t.Fatalf("[%s] an armed availability slash without its machinery MUST be rejected (inert or false-positive prone)", c.name)
		}
	}
}

// TestAvail_WindowGEFailK — the window must be able to contain the failure quorum.
func TestAvail_WindowGEFailK(t *testing.T) {
	p := armedAvail()
	p.AvailFailWindow = 2
	p.AvailFailK = 3 // 3 failures required within a window of 2 is unreachable
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_fail_window < avail_fail_k must be rejected (unreachable quorum)")
	}
}

// TestAvail_WindowLE64 — the sliding window is a 64-bit bitmask, so an armed W > 64 is REJECTED;
// silently clamping it would falsify the calibration. While dormant (slash=0), W > 64 is tolerated.
func TestAvail_WindowLE64(t *testing.T) {
	p := armedAvail()
	p.AvailFailWindow = 65
	if err := p.Validate(); err == nil {
		t.Fatalf("an armed avail_fail_window > 64 must be rejected (sliding bitmask capacity)")
	}
	p = armedAvail()
	p.AvailFailWindow = 64 // the exact bound is accepted
	if err := p.Validate(); err != nil {
		t.Fatalf("avail_fail_window = 64 must validate: %v", err)
	}
	p = types.DefaultParams() // dormant: no constraint bites
	p.AvailFailWindow = 300
	if err := p.Validate(); err != nil {
		t.Fatalf("W>64 while DORMANT (slash=0) must not be rejected: %v", err)
	}
}

// TestAvail_BpsBounds — basis points stay bounded by 10000.
func TestAvail_BpsBounds(t *testing.T) {
	p := armedAvail()
	p.AvailThresholdBps = 10001
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_threshold_bps > 10000 must be rejected")
	}
	p = armedAvail()
	p.AvailSlashBps = 10001
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_slash_bps > 10000 must be rejected")
	}
}
