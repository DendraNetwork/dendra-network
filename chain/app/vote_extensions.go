package app

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"os"
	"strconv"
	"strings"
	"sync"

	abci "github.com/cometbft/cometbft/abci/types"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	"github.com/cosmos/cosmos-sdk/baseapp"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/mempool"

	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// Decentralized VRF over ABCI++ vote extensions.
//
// At every height each validator contributes a verifiable VRF output, produced with the VRF key it
// anchored on-chain through MsgRegisterValidatorVrfKey. The proposer aggregates those outputs into a
// single unpredictable, unforgeable seed (see AggregateBeacons) that no single actor controls. This
// file wires production (ExtendVote), verification (VerifyVoteExtension), and the aggregation and
// injection path (PrepareProposal / ProcessProposal / PreBlocker).
//
// Dormant by default: CometBFT invokes these handlers only once
// consensus_params.abci.vote_extensions_enable_height is set (>0) and reached. Until then this wiring
// is inert and the running chain is unaffected.

// vrfVoteDomain separates the vote-extension alpha domain from every other VRF use on this chain.
var vrfVoteDomain = []byte("dendra/vrf/ve/v1")

// vrfVoteAlphaBase returns the deterministic, public part of alpha: domain || height (8 bytes, big-endian).
func vrfVoteAlphaBase(height int64) []byte {
	var h [8]byte
	binary.BigEndian.PutUint64(h[:], uint64(height))
	return append(append([]byte{}, vrfVoteDomain...), h[:]...)
}

// vrfVoteAlphaWithHash returns alpha = domain || height || block hash. The hash of the block being
// voted on is unpredictable before that block is proposed, so an adversary gets about zero blocks of
// lookahead instead of the one block that seed chaining alone would leave. The hash comes from the
// ABCI request (RequestExtendVote.Hash / RequestVerifyVoteExtension.Hash) and is bound to the
// precommit BlockID, so producer and verifier derive the same alpha for the same vote and liveness is
// not at risk. When the hash is absent (bootstrap), it falls back to seed chaining
// (DecentralizedSeed[height-1]), then to domain || height: it degrades and never blocks.
func (app *App) vrfVoteAlphaWithHash(ctx sdk.Context, height int64, hash []byte) []byte {
	a := vrfVoteAlphaBase(height)
	if len(hash) > 0 {
		return append(a, hash...)
	}
	if height > 1 {
		if prev, ok := app.JobsKeeper.GetDecentralizedSeed(ctx, height-1); ok && len(prev) > 0 {
			return append(a, prev...)
		}
	}
	return a
}

// vrfVoteAlphaForAggregation returns the deterministic alpha the PreBlocker uses to re-verify, at
// aggregation time, the extensions produced at extHeight. It reads back the hash of block extHeight
// that the PreBlocker stored at that height (the finalized block hash), so it reproduces exactly what
// ExtendVote and VerifyVoteExtension signed. When the hash is absent (first active block) it uses the
// same fallback as the production side, so every node aggregates the same value.
func (app *App) vrfVoteAlphaForAggregation(ctx sdk.Context, extHeight int64) []byte {
	if h, ok := app.JobsKeeper.GetBlockHash(ctx, extHeight); ok && len(h) > 0 {
		return app.vrfVoteAlphaWithHash(ctx, extHeight, h)
	}
	return app.vrfVoteAlphaWithHash(ctx, extHeight, nil)
}

// loadNodeVrfKey loads the node's private VRF key from the file named by DENDRA_VRF_KEY_FILE (hex of a
// 64-byte Ed25519 key, as produced by `dendra-vrf keygen`). When the key is missing, unreadable or
// invalid it returns nil: the node then contributes no VRF proof, and its empty vote extension stays
// valid and does not affect liveness.
//
// Every failure returns a reason along with nil. An unset variable, an unreadable file, a key of the
// wrong shape and a node that has no key on purpose all produce the same empty vote extension, and
// the chain's diagnostic can name which validator is mute but not why. The reason is the only place
// the cause survives.
func loadNodeVrfKey() (ed25519.PrivateKey, string) {
	path := strings.TrimSpace(os.Getenv("DENDRA_VRF_KEY_FILE"))
	if path == "" {
		return nil, "DENDRA_VRF_KEY_FILE is not set (a node without a VRF key is legitimate: it just never contributes to the seed)"
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, "DENDRA_VRF_KEY_FILE=" + path + " could not be read: " + err.Error()
	}
	b, err := hex.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil {
		return nil, "DENDRA_VRF_KEY_FILE=" + path + " is not valid hex: " + err.Error()
	}
	if len(b) != ed25519.PrivateKeySize {
		return nil, "DENDRA_VRF_KEY_FILE=" + path + " decodes to the wrong size (a VRF key is " +
			strconv.Itoa(ed25519.PrivateKeySize) + " bytes, this one is " + strconv.Itoa(len(b)) + ")"
	}
	return ed25519.PrivateKey(b), ""
}

// vrfKeyRetryBlocks is how often, in blocks, a validator that has no key tries to load one again.
// That is about 8 minutes at the block interval this chain currently observes; it sets the operator's
// feedback loop and the cadence at which the diagnostic below is repeated. It is not a consensus
// quantity: it changes when a node reads a file, never what nodes agree on.
const vrfKeyRetryBlocks = 100

// vrfKeyHolder carries the node's VRF key and, while it has none, re-reads the key file periodically.
//
// If the key were read only once, in New(), before the baseapp is sealed, an operator who installs the
// key or sets DENDRA_VRF_KEY_FILE on a node that is already running would change nothing: the process
// would keep its nil key and return an empty extension at every height. Nothing would show it, since
// the node votes normally and is never jailed, and the aggregation can only report `no_extension=1`.
//
// Two properties are deliberate:
//   - A key, once held, is never replaced. The only transition is from no key to a key. Silently
//     swapping a live signing key because a file changed under the process would be worse than the
//     defect this closes; rotation requires a restart.
//   - The retry costs nothing on a healthy node: `ready()` returns before any file access, so a node
//     that booted with its key never touches the disk again.
//
// The read is deliberately synchronous, inside ExtendVote: the key file sits in the node's config
// directory, on the same filesystem as the database this process writes at every height. A
// filesystem that could hang this 129-byte read has already stopped the node, so an asynchronous read
// would buy no liveness and would add a race.
//
// CometBFT serialises calls on the consensus connection; the lock keeps this type from silently
// depending on that.
type vrfKeyHolder struct {
	mu      sync.Mutex
	sk      ed25519.PrivateKey
	reason  string
	nextTry int64
	load    func() (ed25519.PrivateKey, string)
}

func newVrfKeyHolder(load func() (ed25519.PrivateKey, string)) *vrfKeyHolder {
	sk, reason := load()
	return &vrfKeyHolder{sk: sk, reason: reason, load: load}
}

// ready reports whether this node can sign a VRF proof at all.
func (h *vrfKeyHolder) ready() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.sk) == ed25519.PrivateKeySize
}

// key returns the private key to sign with, or nil.
func (h *vrfKeyHolder) key() ed25519.PrivateKey {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.sk
}

// why returns why this node has no key, or "" when it has one.
func (h *vrfKeyHolder) why() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.reason
}

// at is called once per height while the node has no key. It returns (acquired, say):
//   - acquired=true: a key just appeared, and `say` is its public key, to compare with the anchored one
//   - acquired=false: `say` is the reason the node is still mute, or "" when it is not yet time to retry
//
// The reason is repeated on a cadence because the boot line is emitted once and rotates out of the
// log buffer: on a node that has been up for a day, the sentence that explains the silence is gone.
func (h *vrfKeyHolder) at(height int64) (bool, string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.sk) == ed25519.PrivateKeySize || height < h.nextTry {
		return false, ""
	}
	h.nextTry = height + vrfKeyRetryBlocks
	sk, reason := h.load()
	if len(sk) == ed25519.PrivateKeySize {
		h.sk, h.reason = sk, ""
		return true, hex.EncodeToString(sk.Public().(ed25519.PublicKey))
	}
	h.reason = reason
	return false, reason
}

// newExtendVoteHandler builds the ExtendVote handler. It is separate from setupVoteExtensions so the
// recovery path can be tested without a sealed baseapp: the defect it closes was in the wiring, and a
// test that only drove the holder would prove nothing about what CometBFT calls.
func newExtendVoteHandler(
	keys *vrfKeyHolder,
	alpha func(sdk.Context, int64, []byte) []byte,
) func(sdk.Context, *abci.RequestExtendVote) (*abci.ResponseExtendVote, error) {
	empty := func() (*abci.ResponseExtendVote, error) {
		return &abci.ResponseExtendVote{VoteExtension: []byte{}}, nil
	}
	proveErrs := 0
	return func(ctx sdk.Context, req *abci.RequestExtendVote) (*abci.ResponseExtendVote, error) {
		if !keys.ready() {
			if acquired, say := keys.at(req.Height); say != "" {
				if acquired {
					ctx.Logger().Info("VRF: node key loaded after boot, this node now contributes to the "+
						"committee seed (no restart was needed)", "height", req.Height, "pubkey", say,
						"check", "it must equal the key anchored by register-validator-vrf-key")
				} else {
					ctx.Logger().Error("VRF: no node key -> this node contributes nothing to the committee "+
						"seed (its vote extension stays empty; consensus and liveness are unaffected). "+
						"Install the key and it is picked up within "+strconv.Itoa(vrfKeyRetryBlocks)+
						" blocks, without a restart", "height", req.Height, "reason", say)
				}
			}
			if !keys.ready() {
				return empty()
			}
		}
		pi, err := vrf.Prove(keys.key(), alpha(ctx, req.Height, req.Hash))
		if err != nil {
			// Throttled: this runs every block, and a log that floods gets filtered out. Log output enters
			// no state hash, so this has no consensus effect.
			proveErrs++
			if proveErrs == 1 || proveErrs%100 == 0 {
				ctx.Logger().Error("VRF: the key loaded but proving failed -> empty extension, no contribution",
					"height", req.Height, "occurrences", proveErrs, "err", err)
			}
			return empty()
		}
		return &abci.ResponseExtendVote{VoteExtension: pi}, nil
	}
}

// setupVoteExtensions wires the ABCI++ ExtendVote / VerifyVoteExtension handlers. It is called from
// New() before app.Load(), that is, before the baseapp is sealed. It is inert while vote extensions
// are off.
func (app *App) setupVoteExtensions() {
	keys := newVrfKeyHolder(loadNodeVrfKey) // may hold nil: a node without a VRF key sends an empty extension

	// Reported at boot, where an operator looks, and repeated on a cadence by the handler when there is
	// no key, because this line alone rotates out of the log. A node with no key says so and why; a node
	// with one prints the public key, which is what has to match the anchored value.
	if sk := keys.key(); len(sk) == ed25519.PrivateKeySize {
		app.Logger().Info("VRF: node key loaded, this node will contribute to the committee seed",
			"pubkey", hex.EncodeToString(sk.Public().(ed25519.PublicKey)),
			"check", "it must equal the key anchored by register-validator-vrf-key")
	} else {
		app.Logger().Error("VRF: no node key -> this node contributes nothing to the committee seed "+
			"(its vote extension stays empty; consensus and liveness are unaffected)", "reason", keys.why())
	}

	// ExtendVote: the node signs alpha(height) with its VRF key and carries the 80-byte ECVRF proof as
	// the extension. A VRF problem never fails a vote; the handler returns an empty extension instead.
	// Every failing branch produces the same empty extension, so the logs are the only thing that tells
	// them apart, and that tells them apart from a node with no key on purpose.
	app.SetExtendVoteHandler(newExtendVoteHandler(keys, app.vrfVoteAlphaWithHash))

	// VerifyVoteExtension is deliberately lenient, so this path cannot hurt liveness. Empty: accept (no
	// contribution). Wrong size: reject. Otherwise, if the validator's VRF key is anchored on-chain,
	// verify the proof and reject a forged one. Key not anchored: accept, since aggregation only keeps
	// proofs verified against an anchored key.
	app.SetVerifyVoteExtensionHandler(func(ctx sdk.Context, req *abci.RequestVerifyVoteExtension) (*abci.ResponseVerifyVoteExtension, error) {
		ext := req.VoteExtension
		if len(ext) == 0 {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_ACCEPT}, nil
		}
		if len(ext) != vrf.ProofSize {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_REJECT}, nil
		}
		pk := app.validatorVrfPubkey(ctx, req.ValidatorAddress)
		if pk == nil {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_ACCEPT}, nil
		}
		if ok, _ := vrf.Verify(pk, app.vrfVoteAlphaWithHash(ctx, req.Height, req.Hash), ext); !ok {
			return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_REJECT}, nil
		}
		return &abci.ResponseVerifyVoteExtension{Status: abci.ResponseVerifyVoteExtension_ACCEPT}, nil
	})

	// Decentralized seed aggregation (PrepareProposal / ProcessProposal / PreBlocker).
	// The default handlers are delegated to while vote extensions are inactive, so dormant means unchanged
	// behaviour: mempool selection plus the stock re-verification of proposed txs.
	defaultPH := baseapp.NewDefaultProposalHandler(mempool.NoOpMempool{}, app)
	defaultPrepare := defaultPH.PrepareProposalHandler()
	defaultProcess := defaultPH.ProcessProposalHandler()

	// PrepareProposal: the proposer injects the extended commit (the vote extensions of H-1) at the head
	// of the block, after validating it (signatures and 2/3 of voting power, via ValidateVoteExtensions).
	app.SetPrepareProposal(func(ctx sdk.Context, req *abci.RequestPrepareProposal) (*abci.ResponsePrepareProposal, error) {
		if !voteExtensionsActive(ctx, req.Height) {
			return defaultPrepare(ctx, req)
		}
		if err := baseapp.ValidateVoteExtensions(ctx, app.StakingKeeper, req.Height, ctx.ChainID(), req.LocalLastCommit); err != nil {
			// On a real network (strict signature checking) a failure here would silently fall back to
			// the default handler and leave the decentralized VRF inactive, so it is logged. On SDK 0.53
			// the function ignores height and chainID (it reads them from ctx); they are passed anyway
			// for accuracy and forward compatibility.
			ctx.Logger().Error("vote extensions: ValidateVoteExtensions failed -> falling back (decentralized VRF seed inactive for this block)", "height", req.Height, "err", err)
			return defaultPrepare(ctx, req)
		}
		bz, err := req.LocalLastCommit.Marshal()
		if err != nil {
			return defaultPrepare(ctx, req)
		}
		inj := append(append([]byte{}, vrfInjectPrefix...), bz...)
		return &abci.ResponsePrepareProposal{Txs: prependWithinBudget(inj, req.Txs, req.MaxTxBytes)}, nil
	})

	// ProcessProposal: when an injection is present, validate it (well-formed extended commit and
	// signatures), then submit the rest of the block to the default handler. A rejection blocks the
	// proposal, which is the safe direction.
	app.SetProcessProposal(newProcessProposalHandler(defaultProcess,
		func(ctx sdk.Context, ec abci.ExtendedCommitInfo, height int64) error {
			return baseapp.ValidateVoteExtensions(ctx, app.StakingKeeper, height, ctx.ChainID(), ec)
		}))

	// PreBlocker: consume the injection, re-aggregate the seed deterministically and store it on-chain,
	// then run the modules' PreBlock (which keeps upgrade logic and the like). When inactive or without
	// an injection, only PreBlock runs.
	app.SetPreBlocker(func(ctx sdk.Context, req *abci.RequestFinalizeBlock) (*sdk.ResponsePreBlock, error) {
		if voteExtensionsRecording(ctx, req.Height) {
			// Remember the hash of this block: the next block's aggregation reads it back to re-verify
			// the extensions of this height deterministically (alpha is bound to the block). Dormant
			// means no write at all.
			if err := app.JobsKeeper.SetBlockHash(ctx, req.Height, req.Hash); err != nil {
				ctx.Logger().Error("vote extensions: set block hash", "err", err)
			}
			// Windowed pruning keeps the store bounded: drop BlockHash, DecentralizedSeed, contributor
			// count and contributor power far behind the head. Alpha only reads h and h-1, so
			// vrfStatePruneWindow=1000 is a very wide margin. No-op before H=1000.
			if old := req.Height - vrfStatePruneWindow; old > 0 {
				_ = app.JobsKeeper.DeleteBlockHash(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeed(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeedContributors(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeedContributorPower(ctx, old)
			}
			// Consumption has its own gate, one height narrower than the write gate around it (see
			// `voteExtensionsRecording`). At H == enable_height nothing validates the injected envelope,
			// so aggregating it would let the proposer of that block choose the seed;
			// preBlockerMayAggregate explains why.
			if preBlockerMayAggregate(ctx, req.Height, req.Txs) {
				var ec abci.ExtendedCommitInfo
				if err := ec.Unmarshal(req.Txs[0][len(vrfInjectPrefix):]); err == nil {
					if seed, n, powerBps, ok := app.aggregateSeed(ctx, ec, req.Height-1); ok {
						if err := app.JobsKeeper.SetDecentralizedSeed(ctx, req.Height, seed); err != nil {
							ctx.Logger().Error("vote extensions: set decentralized seed", "err", err)
						} else {
							ctx.Logger().Info("vote extensions: decentralized VRF seed", "height", req.Height, "contributors", n, "contributor_power_bps", powerBps, "seed", hex.EncodeToString(seed))
							// Store the number of contributors: committeeBaseSeed reads it to enforce the
							// committee_min_vrf_contributors floor, which keeps a silent fallback to a centralized
							// seed from going unnoticed.
							if err := app.JobsKeeper.SetDecentralizedSeedContributors(ctx, req.Height, uint64(n)); err != nil {
								ctx.Logger().Error("vote extensions: set decentralized seed contributors", "err", err)
							}
							// Store the contributors' share of power (bps of the commit's total power), which feeds
							// the dynamic ⌈2N/3⌉ floor that committeeBaseSeed applies in power. A floor on cardinality
							// alone would be defeated by dust sybils. Same write site as the seed.
							if err := app.JobsKeeper.SetDecentralizedSeedContributorPower(ctx, req.Height, powerBps); err != nil {
								ctx.Logger().Error("vote extensions: set decentralized seed contributor power", "err", err)
							}
						}
					}
				}
			}
		}
		return app.ModuleManager.PreBlock(ctx)
	})
}

// newProcessProposalHandler composes the validation of the vote-extension injection with the handler
// `next`, which applies while vote extensions are dormant.
//
// Invariant: both branches submit the block to the same transaction checks.
// The envelope injected at the head holds the marshaled extended commit rather than a transaction. A
// branch that accepted as soon as the injection validated would exempt the rest of the block from the
// checks `next` applies (decoding, verification, the proposal gas cap), and that exemption would open
// at the moment `vote_extensions_enable_height` is reached, when the decentralized VRF is activated.
// The envelope is therefore stripped and `req.Txs[1:]` delegated, so block validation does not depend
// on the state of vote extensions.
//
// `next` is called on a copy: mutating `req` in place would hand the ABCI caller back a request with
// its envelope missing.
func newProcessProposalHandler(
	next sdk.ProcessProposalHandler,
	validateExtensions func(sdk.Context, abci.ExtendedCommitInfo, int64) error,
) sdk.ProcessProposalHandler {
	return func(ctx sdk.Context, req *abci.RequestProcessProposal) (*abci.ResponseProcessProposal, error) {
		if !voteExtensionsActive(ctx, req.Height) || len(req.Txs) == 0 || !bytes.HasPrefix(req.Txs[0], vrfInjectPrefix) {
			return next(ctx, req)
		}
		var ec abci.ExtendedCommitInfo
		if err := ec.Unmarshal(req.Txs[0][len(vrfInjectPrefix):]); err != nil {
			return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_REJECT}, nil
		}
		if err := validateExtensions(ctx, ec, req.Height); err != nil {
			ctx.Logger().Error("vote extensions: ProcessProposal rejected the injection (ValidateVoteExtensions)", "height", req.Height, "err", err)
			return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_REJECT}, nil
		}
		rest := *req
		rest.Txs = req.Txs[1:]
		return next(ctx, &rest)
	}
}

// vrfInjectPrefix marks the single injection "tx" at the head of a block carrying the marshaled
// extended commit.
var vrfInjectPrefix = []byte("DENDRA_VE1")

// vrfStatePruneWindow is the number of blocks of BlockHash/DecentralizedSeed kept (a sliding window
// that bounds the store). The VRF alpha only reads h and h-1; 1000 is a wide margin, kept for
// observability and snapshots. The PreBlocker deletes the entry at req.Height-vrfStatePruneWindow.
const vrfStatePruneWindow = 1000

// voteExtensionsActive reports whether vote extensions are consumable at this height. The extensions of
// H-1 only exist once vote_extensions_enable_height is reached, so injecting at H requires
// H > enableHeight.
func voteExtensionsActive(ctx sdk.Context, height int64) bool {
	cp := ctx.ConsensusParams()
	if cp.Abci == nil || cp.Abci.VoteExtensionsEnableHeight <= 0 {
		return false
	}
	return height > cp.Abci.VoteExtensionsEnableHeight
}

// preBlockerMayAggregate reports whether this height may consume an injected envelope.
//
// It is a named function called by the PreBlocker, rather than inline code in the `SetPreBlocker`
// closure, so tests can reach it: while it lived in the closure, removing the `voteExtensionsActive`
// term left the whole module green.
//
// Of its three terms, `voteExtensionsActive` is the one that matters. It is the consumption predicate
// (`h > enable_height`), while the enclosing block is gated on the write predicate
// (`h >= enable_height`), one height wider so the block hash of E is recorded for the aggregation at
// E+1. At H == E, `newProcessProposalHandler` delegates the whole block, envelope included, to `next`,
// which is `NoOpProcessProposal` (the default handler is built with `mempool.NoOpMempool{}`, and in
// cosmos-sdk v0.53.6 `baseapp/abci_utils.go`, `NoOpProcessProposal` accepts unconditionally for a NoOp
// mempool). At that height nothing validates the envelope. Aggregating it would write the seed, the
// contributor count and the contributor power from unchecked data, and `aggregateSeed` derives the 2/3
// power floor from powers in that same envelope, so the floor would hold by construction. That seed
// freezes committees, so it decides who is paid.
//
// Refusing at E costs nothing: CometBFT delivers extensions only from E+1, so at E only a forged
// envelope can exist.
func preBlockerMayAggregate(ctx sdk.Context, height int64, txs [][]byte) bool {
	return voteExtensionsActive(ctx, height) &&
		len(txs) > 0 && bytes.HasPrefix(txs[0], vrfInjectPrefix)
}

// voteExtensionsRecording is the write gate, deliberately one height wider than the consumption gate.
//
// `voteExtensionsActive` answers whether a block may consume extensions, and CometBFT delivers them
// only from enable_height+1, so `>` is right there. The block hash, however, is read back one height
// after it is written, to rebuild the alpha that ExtendVote signed. At the enable height E, CometBFT
// already calls ExtendVote, which signs an alpha bound to the block hash of E. Gating the write on the
// consumption predicate skipped the one height whose hash the next block needs: at E+1 the
// aggregation could not reproduce that alpha, every honest proof was classified `invalid_proof`, no
// seed was written, and the audit draws of that height were deferred. That was deterministic on every
// node, so there was no fork; the cost was a false diagnosis at the moment an operator turns it on.
// One height, no more: writing at E costs one entry, and the pruning window reclaims it.
func voteExtensionsRecording(ctx sdk.Context, height int64) bool {
	cp := ctx.ConsensusParams()
	if cp.Abci == nil || cp.Abci.VoteExtensionsEnableHeight <= 0 {
		return false
	}
	return height >= cp.Abci.VoteExtensionsEnableHeight
}

// prependWithinBudget puts the injection first, then appends mempool txs for as long as MaxTxBytes allows.
func prependWithinBudget(inj []byte, txs [][]byte, maxBytes int64) [][]byte {
	out := [][]byte{inj}
	budget := maxBytes - int64(len(inj))
	for _, tx := range txs {
		budget -= int64(len(tx))
		if budget < 0 {
			break
		}
		out = append(out, tx)
	}
	return out
}

// vrfMuteRosterMax bounds the roster below. Past a handful of names a log line stops being read, and
// the counters next to it already carry the totals.
const vrfMuteRosterMax = 5

// vrfMute names a vote that did not contribute to the seed, and why.
type vrfMute struct {
	addr []byte
	why  string
}

// voteFlagReason says why a vote cannot carry a VRF proof at all, or "" when the validator
// precommitted for the block and its extension is worth looking at.
//
// A vote extension only rides on a precommit for a block. A validator that precommits nil, for
// example because it did not get the proposal in time, sends no extension by construction, with
// nothing wrong on its side. Without this check the aggregation filed that vote under `no_extension`,
// which reads as a VRF key fault and sent operators to inspect a healthy key. Measured on this chain:
// a validator showed flag 3 (nil) on 7 of 8 consecutive blocks, its seed contribution rate matched
// that share, and its VRF key was loaded and anchored the whole time.
//
// Slashing does not see it either: `x/slashing/keeper/infractions.go` computes
// `missed := signed == comet.BlockIDFlagAbsent`, so a nil precommit is not a missed block. A validator
// can precommit nil indefinitely, never be jailed, keep its seat and its stake, and contribute nothing
// to the seed while every liveness signal stays green. This is the only place the difference shows.
func voteFlagReason(flag cmtproto.BlockIDFlag) string {
	switch flag {
	case cmtproto.BlockIDFlagCommit:
		return ""
	case cmtproto.BlockIDFlagNil:
		return "precommit_nil"
	case cmtproto.BlockIDFlagAbsent:
		return "vote_absent"
	default:
		return "flag_unknown"
	}
}

// formatMuteRoster renders "ADDR:why" pairs for the diagnostic line, in upper-case hex, the form the
// CometBFT /validators endpoint prints, so operators can match their own address without converting.
//
// Without it, the diagnostic would only count: the aggregation loop has `v.Validator.Address` in hand,
// and `no_extension=1` alone cannot tell an operator on a two-validator network whether their own
// validator is the mute one.
//
// The truncation is announced: a roster that silently dropped names would let an operator conclude
// that their validator is fine because it is not listed.
func formatMuteRoster(ms []vrfMute, max int) string {
	if len(ms) == 0 {
		return ""
	}
	n := len(ms)
	if n > max {
		n = max
	}
	parts := make([]string, 0, n+1)
	for _, m := range ms[:n] {
		parts = append(parts, strings.ToUpper(hex.EncodeToString(m.addr))+":"+m.why)
	}
	if len(ms) > n {
		parts = append(parts, "+"+strconv.Itoa(len(ms)-n)+"-more")
	}
	return strings.Join(parts, " ")
}

// seedTally is one pass over an extended commit: the valid betas, the two power sums, and a count and
// a name for every vote that did not contribute.
type seedTally struct {
	betas                            [][]byte
	contribPower, totalPower         int64
	notCommit, noExt, noPk, badProof int
	mute                             []vrfMute
}

// tallyVotes classifies every vote of an extended commit against alpha.
//
// It is a separate function so the order of its checks can be tested. What can go wrong is the order:
// reading the extension's size before the vote's flag files a nil precommit (which cannot carry an
// extension) under a VRF fault. A test of the `voteFlagReason` table alone stays green through that
// mistake, because the table is right either way.
//
// `totalPower` counts every vote, contributing or not: the power share is a floor on participation, so
// an absent validator must weigh in the denominator, or the share would rise as the network degrades.
func tallyVotes(votes []abci.ExtendedVoteInfo, alpha []byte, pubkeyOf func(addr []byte) ed25519.PublicKey) seedTally {
	var t seedTally
	for _, v := range votes {
		t.totalPower += v.Validator.Power
		// The flag comes first, because it changes what the next check means.
		if why := voteFlagReason(v.BlockIdFlag); why != "" {
			t.notCommit++
			t.mute = append(t.mute, vrfMute{v.Validator.Address, why})
			continue
		}
		if len(v.VoteExtension) != vrf.ProofSize {
			t.noExt++
			t.mute = append(t.mute, vrfMute{v.Validator.Address, "no_extension"})
			continue
		}
		pk := pubkeyOf(v.Validator.Address)
		if pk == nil {
			t.noPk++
			t.mute = append(t.mute, vrfMute{v.Validator.Address, "vrf_key_not_anchored"})
			continue
		}
		if ok, beta := vrf.Verify(pk, alpha, v.VoteExtension); ok {
			t.betas = append(t.betas, beta)
			t.contribPower += v.Validator.Power
		} else {
			t.badProof++
			t.mute = append(t.mute, vrfMute{v.Validator.Address, "invalid_proof"})
		}
	}
	return t
}

// aggregateSeed recomputes the decentralized seed from an extended commit: for each vote it resolves
// the validator's anchored VRF key, verifies the proof against alpha(extHeight), and aggregates the
// valid betas. It is deterministic: AggregateBeacons sorts, and the input is the injected commit,
// identical on every node. It returns (seed, contributorCount, powerShareBps, ok), with ok=false when
// no beta is valid.
// powerShareBps = (sum of the power of the votes carrying a valid VRF proof) / (sum of the power of all
// votes in the commit) x 10000. Absent or proofless validators count in the denominator, which is the
// conservative direction. This power-weighted measure backs the ⌈2N/3⌉ floor, since a floor on
// cardinality alone could be griefed with dust sybils.
func (app *App) aggregateSeed(ctx sdk.Context, ec abci.ExtendedCommitInfo, extHeight int64) ([]byte, int, uint64, bool) {
	alpha := app.vrfVoteAlphaForAggregation(ctx, extHeight)
	t := tallyVotes(ec.Votes, alpha, func(a []byte) ed25519.PublicKey {
		return app.validatorVrfPubkey(ctx, a)
	})
	betas, contribPower, totalPower := t.betas, t.contribPower, t.totalPower
	// Observability (ADR-034). Without this line a stuck `contributors=1` cannot be diagnosed: each
	// non-contributing vote is skipped silently, and no query exposes ValidatorVrfPubkey, so operators
	// cannot read back their own anchoring. It records why contributors are missing (the vote never
	// committed, no VRF extension, no anchored VRF key for that validator, or an invalid proof),
	// throttled so it cannot flood. Log output enters no state hash, so this is safe here.
	if (t.noExt+t.noPk+t.badProof+t.notCommit) > 0 && extHeight%20 == 0 {
		ctx.Logger().Info("VRF: votes not contributing to the decentralized seed (diagnostic)",
			"height", extHeight, "votes_total", len(ec.Votes), "contributing", len(betas),
			"not_committed", t.notCommit,
			"no_extension", t.noExt, "vrf_key_not_anchored", t.noPk, "invalid_proof", t.badProof,
			"mute", formatMuteRoster(t.mute, vrfMuteRosterMax))
	}
	if len(betas) == 0 {
		return nil, 0, 0, false
	}
	seed, err := vrf.AggregateBeacons(betas)
	if err != nil {
		return nil, 0, 0, false
	}
	var powerBps uint64
	if totalPower > 0 && contribPower >= 0 {
		powerBps = uint64(contribPower) * 10000 / uint64(totalPower)
	}
	return seed, len(betas), powerBps, true
}

// validatorVrfPubkey resolves a validator's anchored VRF public key from its consensus address (as
// supplied by ABCI): consAddr -> validator (staking) -> operator account -> anchored key. It returns
// nil when the key cannot be found, is not anchored, or is invalid.
func (app *App) validatorVrfPubkey(ctx sdk.Context, consAddr []byte) ed25519.PublicKey {
	validator, err := app.StakingKeeper.GetValidatorByConsAddr(ctx, sdk.ConsAddress(consAddr))
	if err != nil {
		return nil
	}
	valBz, err := sdk.ValAddressFromBech32(validator.OperatorAddress)
	if err != nil {
		return nil
	}
	// A validator's VRF key is anchored under its operator account (same bytes as the valoper address).
	accStr := sdk.AccAddress(valBz).String()
	hexpk, err := app.JobsKeeper.ValidatorVrfPubkey.Get(ctx, accStr)
	if err != nil {
		return nil
	}
	b, err := hex.DecodeString(hexpk)
	if err != nil || len(b) != ed25519.PublicKeySize {
		return nil
	}
	return ed25519.PublicKey(b)
}
