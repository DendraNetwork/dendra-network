package main

import (
	"strings"
	"testing"
	"time"
)

// Whole passes: the lock, the unreadable counter across a success, a
// resolution whose delivery fails, and the dates of a changed alert.

func TestRunStopsQuietlyWhenTheLockIsHeld(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	unlock, err := lockDir(f.dir)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	f.status = func() (int, string) {
		t.Error("the checks ran while another pass held the lock")
		return 500, ""
	}
	if c := f.run(); c != 0 {
		t.Errorf("a pass that finds the lock held must exit 0, got %d", c)
	}
	if len(f.messages()) != 0 {
		t.Errorf("a pass that found the lock held posted: %q", f.messages())
	}
}

func TestUnreadableCountResetsAfterASuccess(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 500, "" }
	for i := 0; i < 2; i++ {
		f.advance(5 * time.Minute)
		f.run()
	}
	if len(f.messages()) != 0 {
		t.Fatalf("two failed passes must stay quiet: %q", f.messages())
	}
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[]}` }
	f.advance(5 * time.Minute)
	f.run()
	f.status = func() (int, string) { return 500, "" }
	for i := 0; i < 2; i++ {
		f.advance(5 * time.Minute)
		f.run()
	}
	if len(f.messages()) != 0 {
		t.Fatalf("the count must start again after the success: %q", f.messages())
	}
	f.advance(5 * time.Minute)
	f.run()
	m := f.messages()
	if len(m) != 1 || !strings.Contains(m[0], "firing grading-unreadable") {
		t.Fatalf("three failed passes in a row must fire again: %q", m)
	}
}

func TestFailedDeliveryKeepsTheRecordUntilItIsDelivered(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	f.run()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[]}` }
	f.hookCode = 500
	f.advance(5 * time.Minute)
	if c := f.run(); c != 1 {
		t.Fatalf("a failed resolution must exit 1, got %d", c)
	}
	if len(f.messages()) != 1 {
		t.Fatalf("the failed resolution posted: %q", f.messages())
	}
	st, _, err := loadState(f.dir, f.clock)
	if err != nil || len(st.Alerts) != 1 {
		t.Fatalf("the record must stay until the message goes out: %v %+v", err, st.Alerts)
	}
	f.hookCode = 204
	f.advance(5 * time.Minute)
	if c := f.run(); c != 0 {
		t.Fatalf("exit %d", c)
	}
	m := f.messages()
	if len(m) != 2 || !strings.Contains(m[1], "resolved grading-ungraded") {
		t.Fatalf("the resolution must go out on the next pass: %q", m)
	}
	st, _, _ = loadState(f.dir, f.clock)
	if len(st.Alerts) != 0 {
		t.Errorf("the record must go once the message is delivered: %+v", st.Alerts)
	}
}

func TestChangedAlertKeepsItsSince(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	since := f.clock.Unix()
	f.run()
	f.advance(5 * time.Minute)
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3,4]}` }
	f.run()
	st, _, err := loadState(f.dir, f.clock)
	if err != nil {
		t.Fatal(err)
	}
	rec, ok := st.Alerts["grading-ungraded"]
	if !ok {
		t.Fatalf("the alert must still be on record: %+v", st.Alerts)
	}
	if rec.Since != since || rec.LastSent != f.clock.Unix() || rec.Fingerprint != "days=3,4" {
		t.Errorf("a change must keep the start and update the rest: %+v", rec)
	}
}
