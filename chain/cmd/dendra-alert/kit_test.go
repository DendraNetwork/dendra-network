package main

import (
	"context"
	"strings"
	"testing"
)

// The shape of the kit floor, the edge of the lead window, and the rows of the
// capacity table.

func TestKitFloorFormatBoundaries(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	passed := "DENDRA_KIT_DEADLINE=2026-10-23T06:00:00Z\n" // before the fake clock
	for _, g := range []string{"1", "9", "10", "123456789"} {
		f.writeEnv("DENDRA_KIT_MIN=" + g + "\n" + passed)
		if r := f.app.checkKit(ctx); r.Err != nil || len(r.Findings) != 0 || !strings.Contains(r.Note, "has passed") {
			t.Errorf("floor %q must be accepted: %+v", g, r)
		}
	}
	for _, b := range []string{"0", "03", "-1", "+3", "3.5", "3x", "three", "1234567890", ""} {
		f.writeEnv("DENDRA_KIT_MIN=" + b + "\n" + passed)
		r := f.app.checkKit(ctx)
		if len(r.Findings) != 1 || r.Findings[0].Key != "kit-config" {
			t.Errorf("floor %q must fire kit-config: %+v", b, r)
		}
	}
}

func TestKitLeadWindowBoundary(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	reached := false
	f.capacity = func() (int, string) {
		reached = true
		return 200, `{"verified":{"live_nodes":0,"kit_versions":{}}}`
	}
	f.writeEnv("DENDRA_KIT_MIN=3\nDENDRA_KIT_DEADLINE=2026-10-27T06:00:01Z\n") // one second outside the 72 h window
	r := f.app.checkKit(ctx)
	if reached || r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("one second outside the window must leave the registry alone: %+v", r)
	}
	if !strings.Contains(r.Note, "72 h away") {
		t.Errorf("the note must say how far the deadline is: %q", r.Note)
	}
	f.writeEnv("DENDRA_KIT_MIN=3\nDENDRA_KIT_DEADLINE=2026-10-27T06:00:00Z\n") // exactly the lead hours
	if r := f.app.checkKit(ctx); !reached || r.Err != nil {
		t.Errorf("the check must start at exactly the lead hours: %+v", r)
	}
}

func TestKitTableValidation(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	f.writeEnv("DENDRA_KIT_MIN=3\nDENDRA_KIT_DEADLINE=2026-10-27T06:00:00Z\n") // inside the window
	for name, body := range map[string]string{
		"negative version":  `{"verified":{"live_nodes":1,"kit_versions":{"-1":1}}}`,
		"fractional count":  `{"verified":{"live_nodes":1,"kit_versions":{"3":1.5}}}`,
		"count is a string": `{"verified":{"live_nodes":1,"kit_versions":{"3":"4"}}}`,
	} {
		body := body
		f.capacity = func() (int, string) { return 200, body }
		if r := f.app.checkKit(ctx); r.Err == nil || len(r.Findings) != 0 {
			t.Errorf("%s must leave the registry unreadable: %+v", name, r)
		}
	}
	f.capacity = func() (int, string) {
		return 200, `{"verified":{"live_nodes":2,"kit_versions":{"2":0,"3":2}}}`
	}
	if r := f.app.checkKit(ctx); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("a version below the floor with zero nodes must stay quiet: %+v", r)
	}
}
