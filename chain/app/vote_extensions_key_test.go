package app

import (
	"crypto/rand"
	"encoding/hex"
	"strings"
	"testing"

	"cosmossdk.io/log"
	abci "github.com/cometbft/cometbft/abci/types"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// ═══ A VRF KEY INSTALLED AFTER THE PROCESS STARTED MUST STILL BE USED ════════════════════════════
//
// The key used to be read exactly once, in New(). A node that was already running when its operator
// installed the key therefore kept a nil key and kept returning an EMPTY vote extension at every
// height — while voting normally, staying in the set and never being jailed. Nothing said so: the
// chain's aggregation can only report `no_extension=1`, never why, and the one boot line that
// explained it had long rotated out of the log buffer. The network ran on the LEGACY seed, with
// anti-grinding inactive, for as long as that lasted.
//
// These cases hold BOTH halves of the fix: the node recovers on its own, and the diagnostic is still
// findable hours later. They drive `newExtendVoteHandler` — the handler CometBFT actually calls —
// rather than the holder alone, because the defect lived in the wiring: a test that only exercised
// the retry logic would have stayed green through the entire incident.

// ── helpers ─────────────────────────────────────────────────────────────────────────────────────

type logLine struct {
	msg string
	kv  []any
}

// spyLogger captures what is SAID, not what is computed. A message built and never logged is exactly
// the failure mode this file exists to catch.
type spyLogger struct {
	infos  []logLine
	errors []logLine
}

func (s *spyLogger) Info(msg string, kv ...any) { s.infos = append(s.infos, logLine{msg, kv}) }
func (s *spyLogger) Warn(string, ...any)        {}
func (s *spyLogger) Debug(string, ...any)       {}
func (s *spyLogger) Error(msg string, kv ...any) {
	s.errors = append(s.errors, logLine{msg, kv})
}
func (s *spyLogger) With(...any) log.Logger { return s }
func (s *spyLogger) Impl() any              { return s }

// carries reports whether the needle appears in the message or in any of its string values.
func (l logLine) carries(t *testing.T, needle string) bool {
	t.Helper()
	if strings.Contains(l.msg, needle) {
		return true
	}
	for _, v := range l.kv {
		if s, ok := v.(string); ok && strings.Contains(s, needle) {
			return true
		}
	}
	return false
}

func ctxWithLogger(l log.Logger) sdk.Context {
	return sdk.NewContext(nil, cmtproto.Header{}, false, l)
}

func fixedAlpha(sdk.Context, int64, []byte) []byte { return []byte("test-alpha") }

func testKey(t *testing.T) ed25519.PrivateKey {
	t.Helper()
	_, sk, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	require.Len(t, sk, ed25519.PrivateKeySize)
	return sk
}

// keySource stands in for the file on disk, and COUNTS its reads: "the disk is not touched again" is
// a property of the fix, not a detail.
type keySource struct {
	reads  int
	sk     ed25519.PrivateKey
	reason string
}

func (s *keySource) load() (ed25519.PrivateKey, string) {
	s.reads++
	return s.sk, s.reason
}

const reasonUnset = "DENDRA_VRF_KEY_FILE is not set (a node without a VRF key is legitimate)"

// drive runs the handler over a range of heights and returns the extensions produced.
func drive(t *testing.T, h func(sdk.Context, *abci.RequestExtendVote) (*abci.ResponseExtendVote, error),
	ctx sdk.Context, from, to int64) [][]byte {
	t.Helper()
	var out [][]byte
	for ht := from; ht <= to; ht++ {
		resp, err := h(ctx, &abci.RequestExtendVote{Height: ht, Hash: []byte("block")})
		require.NoError(t, err, "ExtendVote must never fail a vote over a VRF problem")
		out = append(out, resp.VoteExtension)
	}
	return out
}

// ── (1) the healthy case pays nothing ───────────────────────────────────────────────────────────

func TestAKeyLoadedAtBootNeverRereadsTheFile(t *testing.T) {
	src := &keySource{sk: testKey(t)}
	keys := newVrfKeyHolder(src.load)
	require.Equal(t, 1, src.reads, "the key is read once at boot")
	require.True(t, keys.ready())

	spy := &spyLogger{}
	exts := drive(t, newExtendVoteHandler(keys, fixedAlpha), ctxWithLogger(spy), 1, 500)

	require.Equal(t, 1, src.reads,
		"a node that booted WITH its key must never touch the disk again: the retry is a failure path, "+
			"not a per-block cost")
	for i, e := range exts {
		require.Len(t, e, vrf.ProofSize, "height %d produced no proof", i+1)
	}
	require.Empty(t, spy.errors, "a healthy node must say nothing about its key")
}

// ── (2) THE INCIDENT, REPLAYED ──────────────────────────────────────────────────────────────────

func TestAKeyInstalledAfterBootIsPickedUpWithoutARestart(t *testing.T) {
	src := &keySource{reason: reasonUnset}
	keys := newVrfKeyHolder(src.load)
	require.False(t, keys.ready(), "the node boots mute, which is the legitimate starting state")

	spy := &spyLogger{}
	h := newExtendVoteHandler(keys, fixedAlpha)
	ctx := ctxWithLogger(spy)

	// Before: the extension is empty and the reason is SAID.
	before := drive(t, h, ctx, 1, 99)
	for i, e := range before {
		require.Empty(t, e, "height %d must carry an empty extension while the node has no key", i+1)
	}
	require.Len(t, spy.errors, 1, "the reason is said once per retry window, not once per block")
	require.True(t, spy.errors[0].carries(t, "DENDRA_VRF_KEY_FILE"),
		"the refusal must name WHY, not just that the node is mute")

	// The operator installs the key on a node that IS RUNNING. No restart.
	expected := testKey(t)
	src.sk, src.reason = expected, ""

	// Recovery is bounded by the window, not immediate: the last attempt was at height 1, so the next
	// one falls at 101. The expectation is EXACT rather than "somewhere around there" — a cadence that
	// drifts must turn this bench red instead of slipping through.
	after := drive(t, h, ctx, 100, 140)
	require.Empty(t, after[0], "height 100 is still inside the window opened at height 1")
	require.Len(t, after[1], vrf.ProofSize,
		"THE DEFECT: a key installed on a running node was never picked up, and the validator stayed "+
			"silently mute for as long as the process lived. It must be taken up at the first retry "+
			"after the key appears — height 101 here — without a restart")
	for i, e := range after[1:] {
		require.Len(t, e, vrf.ProofSize, "height %d stopped contributing again", 101+int64(i))
	}
	require.Equal(t, expected, keys.key())

	require.Len(t, spy.infos, 1, "the recovery is announced exactly once")
	require.True(t, spy.infos[0].carries(t, "no restart was needed"))
	require.True(t, spy.infos[0].carries(t, hexPub(expected)),
		"the recovery line must print the PUBLIC key, because that is the value the operator has to "+
			"compare with the one anchored on-chain")
}

// ── (3) the diagnostic does not scroll out of the log ───────────────────────────────────────────

func TestTheDiagnosticIsRestatedOftenEnoughToStayFindable(t *testing.T) {
	src := &keySource{reason: reasonUnset}
	keys := newVrfKeyHolder(src.load)
	spy := &spyLogger{}

	drive(t, newExtendVoteHandler(keys, fixedAlpha), ctxWithLogger(spy), 1, 1000)

	require.Equal(t, 10, len(spy.errors),
		"1000 blocks at one restatement per %d must say it 10 times: once is a diagnostic that has "+
			"scrolled away by the time anyone looks, every block is a flood that gets filtered out — "+
			"two different ways of being silent", vrfKeyRetryBlocks)
	require.Equal(t, 10, src.reads-1, "one disk read per retry window, no more")
	require.True(t, spy.errors[len(spy.errors)-1].carries(t, "DENDRA_VRF_KEY_FILE"),
		"the LAST line must still carry the reason: an operator reads the tail of the log, not its head")
}

// ── (4) a held key is never replaced in flight ──────────────────────────────────────────────────

func TestAHeldKeyIsNeverReplacedByAChangingFile(t *testing.T) {
	first := testKey(t)
	src := &keySource{sk: first}
	keys := newVrfKeyHolder(src.load)

	src.sk = testKey(t) // rotation attempted under the running process
	drive(t, newExtendVoteHandler(keys, fixedAlpha), ctxWithLogger(&spyLogger{}), 1, 300)

	require.Equal(t, first, keys.key(),
		"swapping a LIVE signing key because a file changed under the process would be a worse defect "+
			"than the one being closed: the only transition allowed is nothing -> key")
	require.Equal(t, 1, src.reads)
}

// ── (5) dormancy: the first height can arrive long after boot ───────────────────────────────────

func TestTheFirstHeightRetriesImmediately(t *testing.T) {
	// Vote extensions stay INERT until vote_extensions_enable_height is reached, so ExtendVote can be
	// called for the first time hours after New(). Making the first retry wait a window would leave the
	// node mute for no reason at exactly the moment the feature arms itself.
	src := &keySource{reason: reasonUnset}
	keys := newVrfKeyHolder(src.load)
	src.sk, src.reason = testKey(t), ""

	exts := drive(t, newExtendVoteHandler(keys, fixedAlpha), ctxWithLogger(&spyLogger{}), 5000, 5000)

	require.Len(t, exts[0], vrf.ProofSize,
		"the first height the handler ever sees must retry, whatever that height is")
	require.Equal(t, 2, src.reads)
}

func hexPub(sk ed25519.PrivateKey) string {
	return hex.EncodeToString(sk.Public().(ed25519.PublicKey))
}

// ═══ A COUNT IS NOT AN IDENTIFICATION ═══════════════════════════════════════════════════════════
//
// The aggregation diagnostic emitted `no_extension=1` and nothing else, while holding the offending
// validator's address in hand. On a two-validator network that reads as "one of you", and the operator
// on the other side has no way to tell which — they asked for exactly this after two days of looking.
// Two comments in vote_extensions.go already claimed the line recorded WHICH validator; it did not.

func TestTheMuteRosterNamesTheValidatorAndTheReason(t *testing.T) {
	a := []byte{0x9f, 0x79, 0x96, 0x72}
	got := formatMuteRoster([]vrfMute{{a, "no_extension"}}, vrfMuteRosterMax)
	require.Equal(t, "9F799672:no_extension", got,
		"the address must be UPPERCASE hex — the exact form /validators prints, so an operator can "+
			"match it against their own without converting anything")
}

func TestAnEmptyRosterSaysNothing(t *testing.T) {
	require.Equal(t, "", formatMuteRoster(nil, vrfMuteRosterMax),
		"a healthy commit must add no noise to the line")
}

func TestTheRosterANNOUNCESItsTruncation(t *testing.T) {
	ms := make([]vrfMute, 8)
	for i := range ms {
		ms[i] = vrfMute{[]byte{byte(i)}, "no_extension"}
	}
	got := formatMuteRoster(ms, 3)
	require.Equal(t, 4, len(strings.Fields(got)), "three names plus the marker")
	require.True(t, strings.HasSuffix(got, "+5-more"),
		"a roster that dropped names SILENTLY would let an operator conclude their validator is fine "+
			"because it is not listed — the opposite of what this line exists to say")
}

// ═══ A NIL PRECOMMIT IS NOT A BROKEN VRF KEY ════════════════════════════════════════════════════
//
// A vote extension only rides on a precommit FOR A BLOCK. The aggregation loop did not read
// BlockIdFlag, so a validator that precommitted nil — nothing wrong with its key, it simply did not
// get the proposal in time — was filed under `no_extension`, which reads as "your key is not
// producing a proof". Measured on the live chain: flag 3 on 7 of 8 consecutive blocks, a seed
// contribution rate matching that share exactly, and a VRF key loaded and anchored throughout.

func TestANilPrecommitIsNamedAsSuchAndNotAsAMissingExtension(t *testing.T) {
	require.Equal(t, "precommit_nil", voteFlagReason(cmtproto.BlockIDFlagNil),
		"filing this under no_extension sends the operator to inspect a healthy key")
	require.Equal(t, "vote_absent", voteFlagReason(cmtproto.BlockIDFlagAbsent),
		"absent and nil are different faults: only one of them is a missed block for x/slashing")
	require.Equal(t, "", voteFlagReason(cmtproto.BlockIDFlagCommit),
		"a validator that did precommit for the block must fall through to the extension checks")
}

func TestAnUnknownFlagIsNamedRatherThanTreatedAsHealthy(t *testing.T) {
	require.Equal(t, "flag_unknown", voteFlagReason(cmtproto.BlockIDFlag(99)),
		"a flag nobody anticipated must not silently take the contributing path")
}

// ── the ORDER of the tests, which is where the defect actually lived ─────────────────────────────
//
// The two cases above drive the `voteFlagReason` table. They would stay GREEN if the call were removed
// from the tally loop, or simply placed AFTER the size check — which is precisely the mistake being
// closed. The table is correct either way; the fault is in where it is called from. So these cases
// drive `tallyVotes`, the caller.

func vote(addr []byte, power int64, flag cmtproto.BlockIDFlag, ext []byte) abci.ExtendedVoteInfo {
	return abci.ExtendedVoteInfo{
		Validator:     abci.Validator{Address: addr, Power: power},
		BlockIdFlag:   flag,
		VoteExtension: ext,
	}
}

func TestANilVoteIsFiledUnderItsFlagAndNotUnderAMissingExtension(t *testing.T) {
	alpha := []byte("alpha-de-test")
	sk := testKey(t)
	pi, err := vrf.Prove(sk, alpha)
	require.NoError(t, err)
	pub := sk.Public().(ed25519.PublicKey)

	tal := tallyVotes([]abci.ExtendedVoteInfo{
		vote([]byte{0xaa}, 1000000, cmtproto.BlockIDFlagCommit, pi),
		vote([]byte{0xbb}, 8, cmtproto.BlockIDFlagNil, nil),
	}, alpha, func([]byte) ed25519.PublicKey { return pub })

	require.Len(t, tal.betas, 1, "the validator that did commit must still contribute")
	require.Equal(t, 1, tal.notCommit)
	require.Equal(t, 0, tal.noExt,
		"THE DEFECT, AND IT IS AN ORDERING ONE: with the extension size read before the flag, a nil "+
			"precommit lands in no_extension — which reads as a broken VRF key and sends the operator "+
			"to inspect one that is perfectly healthy")
	require.Equal(t, "BB:precommit_nil", formatMuteRoster(tal.mute, vrfMuteRosterMax),
		"and the line must NAME the validator, not merely count it")
	require.Equal(t, int64(1000008), tal.totalPower,
		"a nil vote still weighs in the denominator: the power share is a floor on participation, so it "+
			"must not rise as the network degrades")
	require.Equal(t, int64(1000000), tal.contribPower)
}

func TestACommittedVoteWithAMalformedExtensionIsStillNoExtension(t *testing.T) {
	tal := tallyVotes([]abci.ExtendedVoteInfo{
		vote([]byte{0xcc}, 5, cmtproto.BlockIDFlagCommit, []byte("far too short")),
	}, []byte("alpha"), func([]byte) ed25519.PublicKey { return nil })

	require.Equal(t, 1, tal.noExt, "moving the blindness elsewhere is not removing it")
	require.Equal(t, 0, tal.notCommit)
	require.Equal(t, "CC:no_extension", formatMuteRoster(tal.mute, vrfMuteRosterMax))
}

func TestACommittedProofWithNoAnchoredKeyKeepsItsOwnReason(t *testing.T) {
	alpha := []byte("alpha")
	pi, err := vrf.Prove(testKey(t), alpha)
	require.NoError(t, err)

	tal := tallyVotes([]abci.ExtendedVoteInfo{
		vote([]byte{0xdd}, 5, cmtproto.BlockIDFlagCommit, pi),
	}, alpha, func([]byte) ed25519.PublicKey { return nil })

	require.Equal(t, 1, tal.noPk,
		"an unanchored key and a nil precommit send the operator to two different places, and the line "+
			"must keep them apart")
	require.Equal(t, "DD:vrf_key_not_anchored", formatMuteRoster(tal.mute, vrfMuteRosterMax))
}

func TestTheRosterCarriesEachReasonSeparately(t *testing.T) {
	got := formatMuteRoster([]vrfMute{
		{[]byte{0xaa}, "no_extension"},
		{[]byte{0xbb}, "vrf_key_not_anchored"},
		{[]byte{0xcc}, "invalid_proof"},
	}, vrfMuteRosterMax)
	require.Equal(t, "AA:no_extension BB:vrf_key_not_anchored CC:invalid_proof", got,
		"the three headings are not interchangeable: one is a missing key, one an unanchored one, "+
			"one a forged or stale proof, and they send the operator to three different places")
}
