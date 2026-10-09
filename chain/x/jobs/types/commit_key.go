package types

import (
	"strconv"
	"strings"
)

// ── THE SHAPE OF A COMMIT KEY, DECIDED IN ONE PLACE ─────────────────────────────────────────────
//
// WHY THIS FILE EXISTS. Two sites derived the job id from a commit key, and not the same way:
// `msg_server_commit.go` took `key[:LastIndex("__")]` as-is, while `commitProvesAssignedWork` then
// trimmed the `__verdict` and `__redo` roles. As long as the derivation only fed a beacon lookup the
// gap had no visible consequence. The moment it decides a REFUSAL, the two answers can no longer
// disagree: a verdict key would look up `Job["<job>__verdict"]`, which is false FOR EVER, and every
// verdict would be rejected.
//
// ⛔ AND THERE IS A FIFTH SHAPE THAT CARRIES NO JOB. `e<N>__reveals__<minerId>` is the epoch marker
// anchored by the reveal worker, armed by default. The chain
// has NO name for that namespace — it exists only on the services side — so nothing here could have
// guessed it: the producer had to be read. Classing it as an ordinary commit would refuse it at
// every epoch, on every judge node, by the very guard that claims to protect the audit layer.
//
// ⛔ AND WE DO NOT ROUTE ON `msg.Kind`. That field is supplied by the sender and no on-chain decision
// reads it (its single non-generated use is a write, in `msg_server_commit.go`). Exempting on
// `kind == "revealmark"` would hand over the whole bypass for free: it is the KEY that must say what
// it is, never the caller.

// CommitKeyKind is the genre of a commit key, decided by the key itself and by nothing else.
type CommitKeyKind uint8

const (
	CommitKeyInvalid CommitKeyKind = iota
	CommitKeyPrimary
	CommitKeyVerdict
	CommitKeyRedo
	CommitKeyRedoVerdict
	CommitKeyEpochMarker
)

const (
	commitKeySep      = "__"
	commitRoleVerdict = "verdict"
	commitRoleRedo    = "redo"
	commitRoleReveals = "reveals"
	epochMarkerPrefix = "e"
	// Longest decimal form of a uint64. A longer run of digits cannot be an epoch, and refusing it
	// here keeps `strconv.ParseUint` from being the thing that decides.
	epochMaxDigits = 20
)

// ParsedCommitKey is the decomposition of a commit key.
//
// The type is NOT called `CommitKey`: that name is already the collections prefix in
// `key_commit.go`, and redeclaring it does not compile.
type ParsedCommitKey struct {
	Kind    CommitKeyKind
	JobId   string // empty unless JobScoped()
	MinerId string
	Epoch   uint64 // meaningful ONLY for CommitKeyEpochMarker
}

// JobScoped reports whether this key names a job that must exist.
//
// THREE ANSWERS, NOT TWO, and the third is why this is not a single boolean over the parse:
//   - `ok == false` from ParseCommitKey  -> the key is REFUSED, it is not a shape this chain serves;
//   - `ok == true && !JobScoped()`       -> the key is LEGITIMATE and names no job (epoch marker);
//   - `ok == true && JobScoped()`        -> the key names a job, and that job must exist.
//
// Collapsing the last two refuses the epoch marker; collapsing the first two accepts anything shaped
// like one. `false` here never means "refuse".
func (k ParsedCommitKey) JobScoped() bool {
	switch k.Kind {
	case CommitKeyPrimary, CommitKeyVerdict, CommitKeyRedo, CommitKeyRedoVerdict:
		return true
	}
	return false
}

// ParseCommitKey decomposes a commit key. `ok` is false when the key is not a shape this chain
// serves — the caller must then refuse, never guess.
func ParseCommitKey(key string) (ParsedCommitKey, bool) {
	idx := strings.LastIndex(key, commitKeySep)
	if idx < 0 {
		return ParsedCommitKey{}, false
	}
	base, minerId := key[:idx], key[idx+2:]
	if base == "" || minerId == "" {
		return ParsedCommitKey{}, false
	}

	// THE JOBLESS FAMILY IS RECOGNISED FIRST, and the order is load-bearing. `e<N>__reveals__<mid>`
	// carries no job id at all: no suffix trim can recover one, and `Job.Has("e<N>__reveals")` is
	// false for ever. Recognising it AFTER the trims would class it as a primary commit and refuse
	// every epoch marker on every judge node.
	if ep, ok := parseEpochMarkerBase(base); ok {
		return ParsedCommitKey{Kind: CommitKeyEpochMarker, Epoch: ep, MinerId: minerId}, true
	}

	// ⚠️ THE ORDER OF THE TWO TRIMS IS LOAD-BEARING — `__verdict` first, `__redo` second: that is what
	// brings `<job>__redo__verdict__<mid>` back to the bare job id. Inverting them leaves a
	// `<job>__redo` residue and reopens exactly the defect. It is the order
	// `commitProvesAssignedWork` already applied, and that function now calls this one instead of
	// keeping a second copy.
	kind := CommitKeyPrimary
	if s, found := strings.CutSuffix(base, commitKeySep+commitRoleVerdict); found {
		base, kind = s, CommitKeyVerdict
	}
	if s, found := strings.CutSuffix(base, commitKeySep+commitRoleRedo); found {
		if kind == CommitKeyVerdict {
			kind = CommitKeyRedoVerdict
		} else {
			kind = CommitKeyRedo
		}
		base = s
	}

	// THE RESIDUE MUST BE A BARE JOB ID, and this test does NOT lean on the opening handler.
	// `msg_server_open_job.go` does forbid `__` inside a job id, but the GENESIS path writes
	// `k.Job.Set` raw and `types.GenesisState.Validate()` is never on the InitChain route: an imported
	// job may carry the sequence. Above all, without this test `<realJob>__x1__<me>` would pass — an
	// unbounded number of permanent keys inside the `<jobId>__` range the settlement handlers walk.
	if base == "" || strings.Contains(base, commitKeySep) {
		return ParsedCommitKey{}, false
	}
	return ParsedCommitKey{Kind: kind, JobId: base, MinerId: minerId}, true
}

// parseEpochMarkerBase reads `e<N>` out of `e<N>__reveals`, in CANONICAL form only.
//
// `e007` and `e7` would otherwise name the same epoch under two distinct keys — two permanent free
// writes for one fact, which is the very thing the caller is trying to bound. So the decimal form is
// re-rendered and compared: only the canonical spelling is a marker.
func parseEpochMarkerBase(base string) (uint64, bool) {
	rest, found := strings.CutSuffix(base, commitKeySep+commitRoleReveals)
	if !found {
		return 0, false
	}
	digits, found := strings.CutPrefix(rest, epochMarkerPrefix)
	if !found || digits == "" || len(digits) > epochMaxDigits {
		return 0, false
	}
	ep, err := strconv.ParseUint(digits, 10, 64)
	if err != nil {
		return 0, false
	}
	if strconv.FormatUint(ep, 10) != digits {
		return 0, false
	}
	return ep, true
}
