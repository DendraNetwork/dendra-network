package main

import (
	"bytes"
	"os"
	"testing"
	"time"
)

// The status command when all is well and when the state is damaged, the
// listing of the alerts on record, and the refusals of the test command.

func TestStatusHealthyExitsZero(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	var out bytes.Buffer
	f.app.out = &out
	if c := f.app.cmdStatus(); c != 0 {
		t.Errorf("a healthy host must exit 0, got %d", c)
	}
	contains(t, "status", out.String(), "webhook: ready")
	contains(t, "status", out.String(), "ok")
}

func TestStatusReportsStateProblems(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	var out bytes.Buffer
	f.app.out = &out
	os.WriteFile(statePath(f.dir), []byte("{bad"), 0o600)
	if c := f.app.cmdStatus(); c != 1 {
		t.Errorf("a state that does not parse must exit 1, got %d", c)
	}
	contains(t, "status", out.String(), "does not parse")
	if _, err := os.Stat(statePath(f.dir)); err != nil {
		t.Errorf("status must leave the damaged file in place: %v", err)
	}
	out.Reset()
	os.Remove(statePath(f.dir))
	os.Mkdir(statePath(f.dir), 0o700)
	if c := f.app.cmdStatus(); c != 1 {
		t.Errorf("a state that cannot be read must exit 1, got %d", c)
	}
	contains(t, "status", out.String(), "state: not readable")
}

func TestStatusListsPostedAlerts(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	st := newState()
	st.Alerts["grading-ungraded"] = alertRecord{
		Check: "grading", Fingerprint: "days=3",
		Since: f.clock.Add(-2 * time.Hour).Unix(), LastSent: f.clock.Add(-1 * time.Hour).Unix(),
	}
	if err := saveState(f.dir, st); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	f.app.out = &out
	f.app.cmdStatus()
	contains(t, "status", out.String(), "posted   grading-ungraded")
	contains(t, "status", out.String(), "2026-10-24 04:00")
	contains(t, "status", out.String(), "2026-10-24 05:00")
}

func TestTestCommandRefusals(t *testing.T) {
	f := newFixture(t)
	os.Remove(webhookPath(f.dir))
	if c := f.app.cmdTest(); c != 2 {
		t.Errorf("no webhook: want exit 2, got %d", c)
	}
	if f.reached != 0 {
		t.Error("the webhook was reached without the file")
	}
	os.WriteFile(webhookPath(f.dir), []byte(f.hook.URL+"/hook\n"), 0o600)
	f.hookCode = 500
	if c := f.app.cmdTest(); c != 1 {
		t.Errorf("a failed delivery: want exit 1, got %d", c)
	}
}
