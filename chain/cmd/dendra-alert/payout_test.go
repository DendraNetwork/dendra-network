package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The payout files the check cannot read, and a pass that carries both payout
// findings at once.

func TestPayoutUnreadableFiles(t *testing.T) {
	f := newFixture(t)
	payoutDir := filepath.Join(f.home, "payout")
	alertPath := filepath.Join(payoutDir, "season-payout.alert")
	logPath := filepath.Join(payoutDir, "season-payout.log")
	os.MkdirAll(alertPath, 0o700)
	if r := f.app.checkPayout(); r.Err == nil || len(r.Findings) != 0 {
		t.Errorf("an alert file that cannot be read must stop the check: %+v", r)
	}
	os.Remove(alertPath)
	os.WriteFile(alertPath, []byte("2026-10-24T03:00:05Z season payout: exit 1: x\n"), 0o600)
	if err := os.Symlink(logPath, logPath); err == nil {
		if r := f.app.checkPayout(); r.Err == nil || len(r.Findings) != 0 {
			t.Errorf("a log that cannot be stat-ed must stop the check: %+v", r)
		}
		os.Remove(logPath)
	}
}

func TestPayoutPostsBothFindingsInKeyOrder(t *testing.T) {
	requireFiringKeysSorted(t)
	f := newFixture(t)
	f.healthy()
	os.Remove(filepath.Join(f.home, "payout", "season-payout.log"))
	os.WriteFile(filepath.Join(f.home, "payout", "season-payout.alert"),
		[]byte("2026-10-24T03:00:05Z season payout: exit 2: failed\nsecond line\n"), 0o600)
	f.run()
	m := f.messages()
	if len(m) != 2 {
		t.Fatalf("an alert file and a missing log are two findings: %q", m)
	}
	if !strings.Contains(m[0], "firing payout-alert") || !strings.Contains(m[1], "firing payout-silent") {
		t.Errorf("the findings must go out in key order: %q", m)
	}
	if strings.Contains(strings.Join(m, "\n"), "second line") {
		t.Errorf("only the first line of the alert file may be shown: %q", m)
	}
	contains(t, "text", m[0], "exit 2")
}
