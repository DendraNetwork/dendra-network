package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// The strict reader of state.json, the roundtrip with saveState, and a state
// file that cannot be read at all.

func TestDecodeStateRejectsUnknownFields(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(statePath(dir), []byte(`{"blocks":{"height":1,"moved_at":2,"seen":true},"unknown":{},"alerts":{},"extra":1}`), 0o600)
	st, note, err := loadState(dir, time.Unix(1000, 0))
	if err != nil || note == "" || len(st.Alerts) != 0 {
		t.Fatalf("an unknown field must be treated as a damaged file: %v %q", err, note)
	}
	m, _ := filepath.Glob(statePath(dir) + ".bad-*")
	if len(m) != 1 {
		t.Errorf("the damaged file must be set aside: %v", m)
	}
}

func TestStateRoundtripKeepsRecordsAndModes(t *testing.T) {
	dir := t.TempDir()
	st := newState()
	st.Blocks = blockState{Height: 42, MovedAt: 1000, Seen: true}
	st.Unknown["grading"] = 2
	st.Alerts["grading-ungraded"] = alertRecord{Check: "grading", Fingerprint: "days=3", Since: 5, LastSent: 6}
	if err := saveState(dir, st); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(statePath(dir) + ".tmp"); err == nil {
		t.Error("the temporary file was left behind")
	}
	got, note, err := loadState(dir, time.Unix(1, 0))
	if err != nil || note != "" {
		t.Fatalf("a saved state must load back: %v %q", err, note)
	}
	if got.Blocks.Height != 42 || !got.Blocks.Seen || got.Blocks.MovedAt != 1000 ||
		got.Unknown["grading"] != 2 || got.Alerts["grading-ungraded"].Fingerprint != "days=3" ||
		got.Alerts["grading-ungraded"].Since != 5 {
		t.Errorf("the roundtrip lost data: %+v", got)
	}
	if fi, err := os.Stat(statePath(dir)); err != nil || fi.Mode().Perm() != 0o600 {
		t.Errorf("state.json must be readable by its owner alone: %v %v", fi, err)
	}
}

func TestLoadStateUnreadableIsAnError(t *testing.T) {
	dir := t.TempDir()
	os.Mkdir(statePath(dir), 0o700)
	st, note, err := loadState(dir, time.Unix(1, 0))
	if err == nil || note != "" || len(st.Alerts) != 0 {
		t.Errorf("a state.json that cannot be read must stop the pass: %v %q", err, note)
	}
}
