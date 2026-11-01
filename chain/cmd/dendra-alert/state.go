package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// alertState is what one pass leaves for the next: the last block height seen and when it last moved,
// how many passes in a row each check could not read, and the alerts that are firing.
type alertState struct {
	Blocks  blockState             `json:"blocks"`
	Unknown map[string]int         `json:"unknown"`
	Alerts  map[string]alertRecord `json:"alerts"`
}

type blockState struct {
	Height  int64 `json:"height"`
	MovedAt int64 `json:"moved_at"`
	Seen    bool  `json:"seen"`
}

type alertRecord struct {
	Check       string `json:"check"`
	Fingerprint string `json:"fingerprint"`
	Since       int64  `json:"since"`
	LastSent    int64  `json:"last_sent"`
}

func newState() alertState {
	return alertState{Unknown: map[string]int{}, Alerts: map[string]alertRecord{}}
}

func statePath(dir string) string { return filepath.Join(dir, "state.json") }

// loadState reads state.json. A missing file is the first pass. A file that does not parse is set
// aside under a new name and the pass starts from an empty state, so that a damaged file cannot stop
// every later alert; the second result says so.
func loadState(dir string, now time.Time) (alertState, string, error) {
	p := statePath(dir)
	b, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return newState(), "", nil
	}
	if err != nil {
		return newState(), "", fmt.Errorf("cannot read %s: %w", p, err)
	}
	st := newState()
	if err := decodeState(b, &st); err != nil {
		aside := fmt.Sprintf("%s.bad-%d", p, now.Unix())
		if rerr := os.Rename(p, aside); rerr != nil {
			return newState(), "", fmt.Errorf("%s does not parse (%v) and cannot be set aside: %w", p, err, rerr)
		}
		return newState(), fmt.Sprintf("%s did not parse (%v); set aside as %s", p, err, filepath.Base(aside)), nil
	}
	return st, "", nil
}

// decodeState parses state.json strictly: an unknown field is an error, and the maps are never nil.
func decodeState(b []byte, st *alertState) error {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(st); err != nil {
		return err
	}
	if st.Unknown == nil {
		st.Unknown = map[string]int{}
	}
	if st.Alerts == nil {
		st.Alerts = map[string]alertRecord{}
	}
	return nil
}

// saveState writes the file in one rename, mode 600.
func saveState(dir string, st alertState) error {
	b, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	p := statePath(dir)
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, p); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}
