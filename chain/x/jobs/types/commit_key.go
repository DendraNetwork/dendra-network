package types

import (
	"strconv"
	"strings"
)

// The shape of a commit key, decided in one place.
//
// Two sites derive the job id from a commit key (`msg_server_commit.go`, `commitProvesAssignedWork`),
// and they must do it the same way. Taking `key[:LastIndex("__")]` as is, without trimming the
// `__verdict` and `__redo` roles, is harmless while the derivation only feeds a beacon lookup.
// Once it decides a refusal, the two answers must agree: a verdict key would look up
// `Job["<job>__verdict"]`, which never exists, and every verdict would be rejected.
//
// A fifth shape carries no job. `e<N>__reveals__<minerId>` is the epoch marker anchored by the reveal
// worker, armed by default. The chain has no name for that namespace (it exists only on the services
// side), so it had to be learned from the producer. Classing it as an ordinary commit would refuse it
// at every epoch, on every judge node, through the guard meant to protect the audit layer.
//
// Routing does not use `msg.Kind`. That field is supplied by the sender and no on-chain decision reads
// it (its only non-generated use is a write, in `msg_server_commit.go`). Exempting on
// `kind == "revealmark"` would hand out the whole bypass: the key says what it is, never the caller.

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
// The type is not called `CommitKey`: that name is already the collections prefix in `key_commit.go`,
// and redeclaring it does not compile.
type ParsedCommitKey struct {
	Kind    CommitKeyKind
	JobId   string // empty unless JobScoped()
	MinerId string
	Epoch   uint64 // meaningful only for CommitKeyEpochMarker
}

// JobScoped reports whether this key names a job that must exist.
//
// There are three answers, which is why this is not a single boolean over the parse:
//   - `ok == false` from ParseCommitKey: the key is refused, it is not a shape this chain serves;
//   - `ok == true && !JobScoped()`: the key is legitimate and names no job (epoch marker);
//   - `ok == true && JobScoped()`: the key names a job, and that job must exist.
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
// serves; the caller must then refuse, never guess.
func ParseCommitKey(key string) (ParsedCommitKey, bool) {
	idx := strings.LastIndex(key, commitKeySep)
	if idx < 0 {
		return ParsedCommitKey{}, false
	}
	base, minerId := key[:idx], key[idx+2:]
	if base == "" || minerId == "" {
		return ParsedCommitKey{}, false
	}

	// The jobless family is recognised first, and the order matters. `e<N>__reveals__<mid>` carries
	// no job id at all: no suffix trim can recover one, and `Job.Has("e<N>__reveals")` is always
	// false. Recognising it after the trims would class it as a primary commit and refuse every
	// epoch marker on every judge node.
	if ep, ok := parseEpochMarkerBase(base); ok {
		return ParsedCommitKey{Kind: CommitKeyEpochMarker, Epoch: ep, MinerId: minerId}, true
	}

	// The order of the two trims matters: `__verdict` first, `__redo` second, which brings
	// `<job>__redo__verdict__<mid>` back to the bare job id. Inverting them leaves a `<job>__redo`
	// residue and reopens the defect. It is the order `commitProvesAssignedWork` already applied,
	// and that function now calls this one instead of keeping a second copy.
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

	// The residue must be a bare job id, and this check does not rely on the opening handler.
	// `msg_server_open_job.go` forbids `__` inside a job id, but the genesis path writes `k.Job.Set`
	// directly and `types.GenesisState.Validate()` is never on the InitChain route, so an imported
	// job may carry the sequence. Without this check `<realJob>__x1__<me>` would also pass: an
	// unbounded number of permanent keys inside the `<jobId>__` range the settlement handlers walk.
	if base == "" || strings.Contains(base, commitKeySep) {
		return ParsedCommitKey{}, false
	}
	return ParsedCommitKey{Kind: kind, JobId: base, MinerId: minerId}, true
}

// parseEpochMarkerBase reads `e<N>` out of `e<N>__reveals`, in canonical form only.
//
// `e007` and `e7` would otherwise name the same epoch under two distinct keys: two permanent free
// writes for one fact, which is what the caller is trying to bound. So the decimal form is rendered
// again and compared, and only the canonical spelling is a marker.
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
