package types_test

import (
	"strings"
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The table of key shapes, proven before the derivation is allowed to decide a refusal.
//
// Every row comes from a real producer: the first four are built by the chain
// (`msg_server_adjudicate.go`, `redo_committee.go`) and by the miner and judge daemons; the fifth by
// the reveal worker's epoch marker. The fifth is why this file exists: it carries no job id, and
// treating it as an ordinary commit would refuse it at every epoch, on every judge node.
// (Service files are named by role here on purpose: their file names are rewritten when the mirror is
// generated, so a name written into a comment would make the published copy differ from its source.)
func TestParseCommitKey_LegitimateShapes(t *testing.T) {
	const mid = "dm1enc90u2ammtjjp3f07rll3" // real length of a miner id (bech32, no "_")

	cases := []struct {
		name   string
		key    string
		kind   types.CommitKeyKind
		jobId  string
		scoped bool
		epoch  uint64
	}{
		{"primary work commit", "job42__" + mid, types.CommitKeyPrimary, "job42", true, 0},
		{"juror verdict", "job42__verdict__" + mid, types.CommitKeyVerdict, "job42", true, 0},
		{"redo", "job42__redo__" + mid, types.CommitKeyRedo, "job42", true, 0},
		{"redo verdict", "job42__redo__verdict__" + mid, types.CommitKeyRedoVerdict, "job42", true, 0},
		{"epoch marker (no job)", "e7__reveals__" + mid, types.CommitKeyEpochMarker, "", false, 7},
		{"epoch marker zero", "e0__reveals__" + mid, types.CommitKeyEpochMarker, "", false, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p, ok := types.ParseCommitKey(c.key)
			if !ok {
				t.Fatalf("legitimate key refused: %q", c.key)
			}
			if p.Kind != c.kind {
				t.Fatalf("kind = %v, want %v (key %q)", p.Kind, c.kind, c.key)
			}
			if p.JobId != c.jobId {
				t.Fatalf("jobId = %q, want %q", p.JobId, c.jobId)
			}
			if p.MinerId != mid {
				t.Fatalf("minerId = %q, want %q", p.MinerId, mid)
			}
			if p.JobScoped() != c.scoped {
				t.Fatalf("JobScoped() = %v, want %v", p.JobScoped(), c.scoped)
			}
			if p.Epoch != c.epoch {
				t.Fatalf("Epoch = %d, want %d", p.Epoch, c.epoch)
			}
		})
	}
}

// What the derivation must refuse. Padding is the case that matters: without the check a registered
// miner opens an unbounded number of permanent keys inside the `<jobId>__` range the settlement
// handlers walk, sheltering behind a job id that does exist.
func TestParseCommitKey_Refusals(t *testing.T) {
	const mid = "dm1enc90u2ammtjjp3f07rll3"

	cases := []struct {
		name string
		key  string
	}{
		{"no separator", "job42"},
		{"leading separator (empty base)", "__" + mid},
		{"empty miner id", "job42__"},
		{"padding: a segment slipped in the middle", "job42__x1__" + mid},
		{"longer padding", "job42__a__b__" + mid},
		{"non-canonical marker (e007)", "e007__reveals__" + mid},
		{"marker with no digits", "e__reveals__" + mid},
		{"marker without the e prefix", "7__reveals__" + mid},
		{"marker overflowing uint64", "e18446744073709551616__reveals__" + mid},
		{"absurd digit run", "e" + strings.Repeat("9", 21) + "__reveals__" + mid},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if p, ok := types.ParseCommitKey(c.key); ok {
				t.Fatalf("key accepted that must be refused: %q -> %+v", c.key, p)
			}
		})
	}
}

// The order of the two trims: `__verdict` must come off before `__redo`, otherwise
// `<job>__redo__verdict__<mid>` leaves the residue `<job>__redo`, which still holds the separator and
// is refused, so a redo verdict would be rejected in consensus. This case keeps the inversion from
// passing unnoticed.
func TestParseCommitKey_TrimOrder(t *testing.T) {
	p, ok := types.ParseCommitKey("job42__redo__verdict__dm1enc90u2ammtjjp3f07rll3")
	if !ok {
		t.Fatal("redo verdict refused: the trim order is inverted")
	}
	if p.JobId != "job42" {
		t.Fatalf("jobId = %q, want \"job42\" — the residue was not brought back to the bare job", p.JobId)
	}
	if p.Kind != types.CommitKeyRedoVerdict {
		t.Fatalf("kind = %v, want CommitKeyRedoVerdict", p.Kind)
	}
}

// Three answers, not two. A parse refusal and a legitimate job-less marker are different things;
// merging them either refuses the marker or accepts anything.
func TestParseCommitKey_ThreeAnswers(t *testing.T) {
	if _, ok := types.ParseCommitKey("no-separator-here"); ok {
		t.Fatal("a parse refusal must return ok=false")
	}
	m, ok := types.ParseCommitKey("e3__reveals__dm1enc90u2ammtjjp3f07rll3")
	if !ok {
		t.Fatal("the epoch marker must be accepted")
	}
	if m.JobScoped() {
		t.Fatal("the epoch marker must require no job")
	}
	j, ok := types.ParseCommitKey("job42__dm1enc90u2ammtjjp3f07rll3")
	if !ok || !j.JobScoped() {
		t.Fatal("a work commit must require its job")
	}
}
