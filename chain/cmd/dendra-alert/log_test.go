package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The log line, and the rotation of alert.log at 1 MiB.

func TestLogfWritesOneLineToOutAndFile(t *testing.T) {
	f := newFixture(t)
	var out bytes.Buffer
	f.app.out = &out
	f.app.logf("test: %s", "hello")
	if !strings.HasPrefix(out.String(), "2026-10-24T06:00:00Z test: hello\n") {
		t.Errorf("the line must carry the UTC timestamp of the clock: %q", out.String())
	}
	b, err := os.ReadFile(filepath.Join(f.dir, "alert.log"))
	if err != nil || string(b) != out.String() {
		t.Errorf("the same line must reach alert.log: %q %v", b, err)
	}
}

func TestLogRotationBoundary(t *testing.T) {
	f := newFixture(t)
	var out bytes.Buffer
	f.app.out = &out
	p := filepath.Join(f.dir, "alert.log")
	os.WriteFile(p, bytes.Repeat([]byte("a"), 1<<20), 0o600)
	f.app.logf("first")
	if _, err := os.Stat(p + ".1"); err == nil {
		t.Error("a log of exactly 1 MiB must stay in place")
	}
	f.app.logf("second")
	b, err := os.ReadFile(p + ".1")
	if err != nil || !strings.Contains(string(b), "first") {
		t.Errorf("the old log must move aside once it passes 1 MiB: %q %v", b, err)
	}
	b, err = os.ReadFile(p)
	if err != nil || !strings.Contains(string(b), "second") || strings.Contains(string(b), "first") {
		t.Errorf("the new log must hold only the new line: %q %v", b, err)
	}
}
