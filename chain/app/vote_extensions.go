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

// ============================================================================
// DECENTRALIZED VRF over ABCI++ vote extensions.
//
// At every height each validator contributes a verifiable VRF OUTPUT, produced with the VRF key it
// anchored on-chain through MsgRegisterValidatorVrfKey. The proposer aggregates those outputs into a
// single unpredictable, unforgeable seed (see AggregateBeacons): no single actor controls it. This
// file wires both halves — PRODUCTION (ExtendVote), VERIFICATION (VerifyVoteExtension), and the
// aggregation/injection path (PrepareProposal / ProcessProposal / PreBlocker).
//
// DORMANT by default: CometBFT invokes these handlers ONLY once consensus_params.abci.
// vote_extensions_enable_height is set (>0) and reached. Until then this wiring is strictly inert and
// the running chain is unaffected.
// ============================================================================

// vrfVoteDomain separates the vote-extension alpha domain from every other VRF use on this chain.
var vrfVoteDomain = []byte("dendra/vrf/ve/v1")

// vrfVoteAlphaBase: the deterministic, public part of alpha = domain ‖ height (8 bytes, big-endian).
func vrfVoteAlphaBase(height int64) []byte {
	var h [8]byte
	binary.BigEndian.PutUint64(h[:], uint64(height))
	return append(append([]byte{}, vrfVoteDomain...), h[:]...)
}

// vrfVoteAlphaWithHash — alpha = domain‖height‖BLOCK-HASH. The hash of the block being voted on is
// unpredictable BEFORE that block is proposed, so an adversary gets ~0 blocks of lookahead instead of
// the ~1 block that seed chaining alone would leave. The hash comes from the ABCI request
// (RequestExtendVote.Hash / RequestVerifyVoteExtension.Hash) and is bound to the precommit BlockID, so
// producer and verifier derive the SAME alpha for the same vote — no liveness risk. When the hash is
// absent (bootstrap), fall back to seed CHAINING (DecentralizedSeed[height-1]), then to domain‖height:
// degrades cleanly, never blocks.
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

// vrfVoteAlphaForAggregation — the DETERMINISTIC alpha used by the PreBlocker to re-verify, at
// aggregation time, the extensions produced at extHeight. It reads back the hash of block extHeight
// that the PreBlocker stored at that height (the finalized block hash), so it reproduces EXACTLY what
// ExtendVote/VerifyVoteExtension signed. Hash absent (first active block) -> same fallback as the
// production side, so every node aggregates the same value.
func (app *App) vrfVoteAlphaForAggregation(ctx sdk.Context, extHeight int64) []byte {
	if h, ok := app.JobsKeeper.GetBlockHash(ctx, extHeight); ok && len(h) > 0 {
		return app.vrfVoteAlphaWithHash(ctx, extHeight, h)
	}
	return app.vrfVoteAlphaWithHash(ctx, extHeight, nil)
}

// loadNodeVrfKey loads the node's PRIVATE VRF key from the file named by DENDRA_VRF_KEY_FILE (hex of a
// 64-byte Ed25519 key, e.g. produced by dendra-vrf keygen). Missing, unreadable or invalid -> nil: the
// node then contributes NO VRF proof (its vote extension is empty, which stays valid and does not
// affect liveness).
//
// ⚠️ THE REASON STRING IS PART OF THE CONTRACT, NOT A CONVENIENCE. Every failure mode here returns nil
// AND a reason, because a bare nil makes them all look alike: an unset variable, an unreadable file and a
// key of the wrong shape each produce the same empty vote extension in perfect silence — and so does a
// node that legitimately has no key. The chain's own diagnostic now names WHICH validator is mute and
// under which of three headings, but it cannot say why that validator has no key: an unset variable and
// an unreadable file both land on `no_extension`, indistinguishably. So the cause has to be carried out
// of here or it is lost. A silent nil in a security path is indistinguishable from a healthy one.
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

// vrfKeyRetryBlocks — how often a validator that has NO key tries to load one again, in blocks.
// ~8 minutes at the interval this chain currently observes, which is the operator's feedback loop and
// also the cadence at which the diagnostic below is restated. Not a consensus quantity: it changes
// when a node reads a file, never what any node agrees on.
const vrfKeyRetryBlocks = 100

// vrfKeyHolder carries the node's VRF key and, WHILE IT HAS NONE, re-reads the file periodically.
//
// ⚠️ WHY THIS TYPE EXISTS, AND IT IS NOT A CONVENIENCE. The key used to be read exactly once, in
// New(), before the baseapp was sealed. An operator who installed the key or set DENDRA_VRF_KEY_FILE
// on a node that was ALREADY RUNNING therefore changed nothing: the process kept its nil key and kept
// returning an empty extension at every height, for as long as it stayed up. The failure is invisible
// from inside — the node votes normally, stays in the set, is never jailed — and invisible from
// outside too, because the chain's aggregation can only report `no_extension=1`, never WHY. An
// external operator lost days to it while the network ran on the LEGACY seed with anti-grinding
// inactive. Ordering a file against a process start is not a mistake an operator should pay for twice.
//
// TWO PROPERTIES ARE DELIBERATE:
//   - A key, once held, is NEVER replaced. The only transition is nothing -> key, which is
//     unambiguously an improvement. Silently swapping a live signing key because a file changed under
//     the process would be a worse defect than the one being closed; rotation requires a restart.
//   - The retry costs NOTHING on a healthy node: `ready()` short-circuits before any file access, so a
//     node that booted with its key never touches the disk again.
//
// The read is synchronous, inside ExtendVote, and that is deliberate rather than overlooked: the key
// file sits in the node's config directory, on the same filesystem as the database this process writes
// at every height. A filesystem that could hang this 129-byte read has already stopped the node, so
// making it asynchronous would buy no liveness and would add a race for nothing.
//
// The lock is not there because ABCI is concurrent — CometBFT serialises calls on the consensus
// connection — but so that this type does not silently depend on that remaining true.
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
//   - acquired=true  -> a key just appeared; `say` is its public key, to be compared with the anchored one
//   - acquired=false -> `say` is the reason the node is still mute, or "" when it is not yet time to retry
//
// Restating the reason on a cadence is the SECOND half of the fix: the boot line is emitted once and
// rotates out of the log buffer, so on a node that has been up for a day the single sentence that
// explains the silence is simply gone. A diagnostic that scrolls away is a diagnostic that does not
// exist at the moment it is needed.
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

// newExtendVoteHandler builds the ExtendVote handler. Extracted from setupVoteExtensions so the
// recovery path can be exercised without a sealed baseapp: the defect it closes lived in the WIRING,
// and a test that only drove the holder would have proved nothing about what CometBFT actually calls.
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
					ctx.Logger().Info("VRF: node key loaded AFTER boot, this node now contributes to the "+
						"committee seed (no restart was needed)", "height", req.Height, "pubkey", say,
						"check", "it must equal the key anchored by register-validator-vrf-key")
				} else {
					ctx.Logger().Error("VRF: NO node key -> this node contributes NOTHING to the committee "+
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
			// Throttled: this runs every block, and a log that floods gets filtered out, which is a
			// second way of being silent. Non-consensus — log output enters no state hash.
			proveErrs++
			if proveErrs == 1 || proveErrs%100 == 0 {
				ctx.Logger().Error("VRF: the key loaded but PROVING failed -> empty extension, no contribution",
					"height", req.Height, "occurrences", proveErrs, "err", err)
			}
			return empty()
		}
		return &abci.ResponseExtendVote{VoteExtension: pi}, nil
	}
}

// setupVoteExtensions wires the ABCI++ ExtendVote / VerifyVoteExtension handlers. Called from New()
// BEFORE app.Load(), i.e. before the baseapp is sealed. Inert while vote extensions are off.
func (app *App) setupVoteExtensions() {
	keys := newVrfKeyHolder(loadNodeVrfKey) // may hold nil (node without a VRF key -> empty extension)

	// SAID AT BOOT, WHERE AN OPERATOR LOOKS — and, when the answer is "no key", said again on a cadence
	// by the handler, because this line alone rotates out of the log. A node with no key says so and
	// why; a node with one prints the public key, which is exactly what has to match the anchored value.
	if sk := keys.key(); len(sk) == ed25519.PrivateKeySize {
		app.Logger().Info("VRF: node key loaded, this node WILL contribute to the committee seed",
			"pubkey", hex.EncodeToString(sk.Public().(ed25519.PublicKey)),
			"check", "it must equal the key anchored by register-validator-vrf-key")
	} else {
		app.Logger().Error("VRF: NO node key -> this node contributes NOTHING to the committee seed "+
			"(its vote extension stays empty; consensus and liveness are unaffected)", "reason", keys.why())
	}

	// ExtendVote: the node signs alpha(height) with ITS VRF key -> an 80-byte ECVRF proof carried as
	// the extension. Never fail a vote over a VRF problem: return an empty extension instead.
	// ⚠️ Every failing branch produces the SAME empty extension, so the logs above and below are the
	// only thing that tells them apart — and from "no key on purpose".
	app.SetExtendVoteHandler(newExtendVoteHandler(keys, app.vrfVoteAlphaWithHash))

	// VerifyVoteExtension is deliberately LENIENT, to keep liveness out of reach of this path. Empty ->
	// ACCEPT (no contribution); wrong size -> REJECT; otherwise, if the validator's VRF key is anchored
	// on-chain, verify the proof and REJECT a forged one. Key not anchored -> ACCEPT: aggregation only
	// ever keeps proofs verified against an anchored key, so accepting here costs nothing.
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

	// ---- Decentralized seed aggregation (PrepareProposal / ProcessProposal / PreBlocker) ----
	// DEFAULT handlers to delegate to while vote extensions are INACTIVE, so that "dormant" means
	// STRICTLY unchanged behaviour: mempool selection plus the stock re-verification of proposed txs.
	defaultPH := baseapp.NewDefaultProposalHandler(mempool.NoOpMempool{}, app)
	defaultPrepare := defaultPH.PrepareProposalHandler()
	defaultProcess := defaultPH.ProcessProposalHandler()

	// PrepareProposal: the proposer INJECTS the extended commit (the vote extensions of H-1) at the head
	// of the block, after validating it (signatures + 2/3 of voting power via ValidateVoteExtensions).
	app.SetPrepareProposal(func(ctx sdk.Context, req *abci.RequestPrepareProposal) (*abci.ResponsePrepareProposal, error) {
		if !voteExtensionsActive(ctx, req.Height) {
			return defaultPrepare(ctx, req)
		}
		if err := baseapp.ValidateVoteExtensions(ctx, app.StakingKeeper, req.Height, ctx.ChainID(), req.LocalLastCommit); err != nil {
			// On a real network (strict signature checking) a failure here would SILENTLY fall back to
			// the default handler, leaving the decentralized VRF inactive with nothing to show for it.
			// Log the failure: observability is the actual fix. Note on SDK 0.53: height and chainID are
			// ignored by the function (it re-reads them from ctx); they are passed anyway for accuracy
			// and forward compatibility.
			ctx.Logger().Error("vote extensions: ValidateVoteExtensions failed -> falling back (decentralized VRF seed INACTIVE for this block)", "height", req.Height, "err", err)
			return defaultPrepare(ctx, req)
		}
		bz, err := req.LocalLastCommit.Marshal()
		if err != nil {
			return defaultPrepare(ctx, req)
		}
		inj := append(append([]byte{}, vrfInjectPrefix...), bz...)
		return &abci.ResponsePrepareProposal{Txs: prependWithinBudget(inj, req.Txs, req.MaxTxBytes)}, nil
	})

	// ProcessProposal: when an injection is present, VALIDATE it (well-formed extended commit +
	// signatures) and then submit the REST of the block to the default handler. REJECT blocks the
	// proposal, which is the safe direction.
	app.SetProcessProposal(newProcessProposalHandler(defaultProcess,
		func(ctx sdk.Context, ec abci.ExtendedCommitInfo, height int64) error {
			return baseapp.ValidateVoteExtensions(ctx, app.StakingKeeper, height, ctx.ChainID(), ec)
		}))

	// PreBlocker: consume the injection -> deterministically re-aggregate the seed and STORE it on-chain,
	// THEN run the modules' PreBlock (which preserves upgrade logic and the like). Inactive, or no
	// injection -> PreBlock only.
	app.SetPreBlocker(func(ctx sdk.Context, req *abci.RequestFinalizeBlock) (*sdk.ResponsePreBlock, error) {
		if voteExtensionsRecording(ctx, req.Height) {
			// Remember the hash of THIS block: the next block's aggregation reads it back to
			// deterministically re-verify the extensions of this height (alpha is bound to the block).
			// Dormant => no write at all.
			if err := app.JobsKeeper.SetBlockHash(ctx, req.Height, req.Hash); err != nil {
				ctx.Logger().Error("vote extensions: set block hash", "err", err)
			}
			// Windowed pruning (KV anti-bloat): drop BlockHash, DecentralizedSeed, contributor count and
			// contributor power far behind the head. Alpha only ever reads h and h-1, so
			// vrfStatePruneWindow=1000 is a very wide margin that still bounds the store on a long-lived
			// network. No-op before H=1000.
			if old := req.Height - vrfStatePruneWindow; old > 0 {
				_ = app.JobsKeeper.DeleteBlockHash(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeed(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeedContributors(ctx, old)
				_ = app.JobsKeeper.DeleteDecentralizedSeedContributorPower(ctx, old)
			}
			// ⛔ CONSUMPTION IS GATED ON THE *CONSUMPTION* PREDICATE, NOT ON THE WRITE ONE.
			// The two gates differ by exactly one height, on purpose (see `voteExtensionsRecording`), and
			// that width is right for the block hash. It is wrong for the envelope: at H == enable_height
			// `voteExtensionsActive` is FALSE, so `newProcessProposalHandler` delegates the whole block —
			// envelope included — to `next`, and `next` is `NoOpProcessProposal` (the default handler is
			// built with `mempool.NoOpMempool{}`, and cosmos-sdk v0.53.6 `baseapp/abci_utils.go` (`NoOpProcessProposal`)
			// returns an unconditional ACCEPT for a NoOp mempool). Neither `ValidateVoteExtensions` nor
			// the LastCommit cross-check runs at that height.
			//
			// Aggregating there would therefore write `DecentralizedSeed`, the contributor COUNT and the
			// contributor POWER from an `ExtendedCommitInfo` nobody verified — and `aggregateSeed`
			// computes the BFT 2/3 power floor as a ratio of powers taken from that same envelope, so the
			// floor is satisfied by construction. The seed that freezes committees, i.e. that decides who
			// gets paid, would be chosen by the proposer of that one block.
			//
			// The handler's own INVARIANT names this moment: "that exemption would open at the PRECISE
			// moment `vote_extensions_enable_height` is reached". It was closed on the branch that
			// validates, and left open on the branch that skips validation entirely.
			//
			// Skipping the aggregation at E costs nothing: CometBFT only delivers extensions from E+1, so
			// a well-formed envelope cannot exist at E anyway — only a forged one can.
			if preBlockerMayAggregate(ctx, req.Height, req.Txs) {
				var ec abci.ExtendedCommitInfo
				if err := ec.Unmarshal(req.Txs[0][len(vrfInjectPrefix):]); err == nil {
					if seed, n, powerBps, ok := app.aggregateSeed(ctx, ec, req.Height-1); ok {
						if err := app.JobsKeeper.SetDecentralizedSeed(ctx, req.Height, seed); err != nil {
							ctx.Logger().Error("vote extensions: set decentralized seed", "err", err)
						} else {
							ctx.Logger().Info("vote extensions: decentralized VRF seed", "height", req.Height, "contributors", n, "contributor_power_bps", powerBps, "seed", hex.EncodeToString(seed))
							// Store the NUMBER of contributors: committeeBaseSeed reads it to enforce the
							// committee_min_vrf_contributors floor, which is what keeps a silent regression
							// to a centralized seed from going unnoticed.
							if err := app.JobsKeeper.SetDecentralizedSeedContributors(ctx, req.Height, uint64(n)); err != nil {
								ctx.Logger().Error("vote extensions: set decentralized seed contributors", "err", err)
							}
							// Store the contributors' share of POWER (bps of the commit's total power), which
							// feeds committeeBaseSeed's dynamic ⌈2N/3⌉ floor expressed IN POWER. A floor on
							// cardinality alone would be defeated by dust sybils. Same write site as the seed.
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
// `next`, which is the one that applies while vote extensions are dormant.
//
// INVARIANT: BOTH BRANCHES SUBMIT THE BLOCK TO THE SAME TRANSACTION CHECKS.
// The envelope injected at the head is NOT a transaction: it carries the marshaled extended commit. A
// branch answering ACCEPT as soon as the injection validated would exempt the entire rest of the block
// from the checks `next` applies (decoding, verification, the proposal gas cap) — and that exemption
// would open at the PRECISE moment `vote_extensions_enable_height` is reached, i.e. when the
// decentralized VRF is activated. So the envelope is stripped and `req.Txs[1:]` is delegated: block
// validation no longer depends on the state of vote extensions.
//
// `next` is called on a COPY: mutating `req` in place would hand the ABCI caller back a request with
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
		reste := *req
		reste.Txs = req.Txs[1:]
		return next(ctx, &reste)
	}
}

// vrfInjectPrefix marks the single injection "tx" at the head of a block carrying the marshaled
// extended commit.
var vrfInjectPrefix = []byte("DENDRA_VE1")

// vrfStatePruneWindow — number of blocks of BlockHash/DecentralizedSeed kept (sliding anti-bloat
// window). The VRF alpha only reads h and h-1; 1000 is a very wide margin, kept for observability and
// snapshots, that still bounds the store on a long-lived network. The PreBlocker deletes the entry at
// req.Height-vrfStatePruneWindow.
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

// preBlockerMayAggregate — MAY THIS HEIGHT CONSUME AN INJECTED ENVELOPE?
//
// Named, and CALLED by the PreBlocker, rather than written inline: the condition it carries was
// invisible to every test while it lived in a closure inside `SetPreBlocker`. Measured — removing the
// `voteExtensionsActive` term left the ENTIRE module green. A guard no bench can invoke is a guard that
// can die without a single case moving.
//
// The three terms, and why the first one is the one that matters. `voteExtensionsActive` is the
// CONSUMPTION predicate (`h > enable_height`); the enclosing block is gated on the WRITE predicate
// (`h >= enable_height`), which is deliberately one height wider so the block hash of E is recorded for
// the aggregation at E+1. At H == E exactly, `newProcessProposalHandler` delegates the whole block —
// envelope included — to `next`, and `next` is `NoOpProcessProposal` (the default handler is built with
// `mempool.NoOpMempool{}`; cosmos-sdk v0.53.6 `baseapp/abci_utils.go` (`NoOpProcessProposal`) returns an unconditional
// ACCEPT for a NoOp mempool). So at that one height nothing validates the envelope, and aggregating it
// would write the seed, the contributor count and the contributor power from data nobody checked —
// while `aggregateSeed` derives the BFT 2/3 power floor as a ratio of powers taken from that same
// envelope, so the floor is met by construction. That seed freezes committees, which is to say it
// decides who is paid.
//
// Refusing at E costs nothing: CometBFT only delivers extensions from E+1, so a well-formed envelope
// cannot exist there — only a forged one can.
func preBlockerMayAggregate(ctx sdk.Context, height int64, txs [][]byte) bool {
	return voteExtensionsActive(ctx, height) &&
		len(txs) > 0 && bytes.HasPrefix(txs[0], vrfInjectPrefix)
}

// voteExtensionsRecording — THE WRITE GATE, DELIBERATELY ONE HEIGHT WIDER THAN THE CONSUMPTION GATE.
//
// `voteExtensionsActive` answers "may this block CONSUME extensions", and CometBFT only delivers them
// from enable_height+1, so `>` is correct there. But the block hash is not consumed at the height it
// is written: it is read back ONE HEIGHT LATER to rebuild the alpha that ExtendVote signed. At the
// enable height E itself, CometBFT already calls ExtendVote -- and it signs an alpha bound to the
// block hash of E. Gating the WRITE on the consumption predicate therefore skipped exactly the one
// height whose hash the next block needs: at E+1 the aggregation could not reproduce that alpha, so
// every honest proof was classified `invalid_proof`, no seed was written, and the audit draws of that
// height were deferred. Deterministic on every node, so not a fork -- but a false diagnosis served at
// the precise moment an operator turns the feature on, which is what the rest of this file exists to
// prevent.
// One height, no more: writing at E costs one entry and the pruning window already reclaims it.
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

// vrfMuteRosterMax bounds the roster below. A log line is not a report: past a handful of names it
// stops being read, and the counters next to it already carry the totals.
const vrfMuteRosterMax = 5

// vrfMute names a vote that did not contribute to the seed, and why.
type vrfMute struct {
	addr []byte
	why  string
}

// voteFlagReason says why a vote cannot carry a VRF proof at all, or "" when the validator did
// precommit for the block and its extension is worth looking at.
//
// ⛔ WHY THIS EXISTS, AND IT IS THE DEFECT THAT COST AN OPERATOR SEVERAL DAYS. A vote extension only
// ever rides on a precommit FOR A BLOCK. A validator that precommits nil — because it did not get the
// proposal in time — sends no extension, by construction and with nothing wrong on its side. The
// aggregation loop did not read `BlockIdFlag`, so it filed that vote under `no_extension`, which reads
// as "your VRF key is not producing a proof" and sends the operator to inspect a key that is perfectly
// healthy. Measured on this chain: a validator showed flag 3 (nil) on 7 of 8 consecutive blocks, its
// seed contribution rate matched that share exactly, and its VRF key was loaded and anchored the whole
// time.
//
// ⚠️ AND THE SILENCE IS STRUCTURAL, NOT JUST OURS. `x/slashing/keeper/infractions.go` computes
// `missed := signed == comet.BlockIDFlagAbsent`: a nil precommit is NOT a missed block. So a validator
// can precommit nil forever, never be jailed, keep its seat and its stake, and contribute nothing to
// the seed — while every liveness signal the chain publishes stays green. This line is the only place
// that difference becomes visible.
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

// formatMuteRoster renders "ADDR:why" pairs for the diagnostic line, in UPPERCASE hex — the exact form
// CometBFT's /validators endpoint prints, so an operator can match a name against their own address
// without converting anything.
//
// ⛔ WHY THIS EXISTS AT ALL, AND IT IS A CLAIM THIS FILE USED TO MAKE FALSELY. Two comments above stated
// that the diagnostic records WHICH validator is mute. It did not: the aggregation loop had
// `v.Validator.Address` in hand and threw it away, emitting `no_extension=1` and nothing else. On a
// two-validator network that reads as "one of you", which is the one question an operator cannot answer
// from their own side — the external operator running this chain asked for it in exactly those words
// after two days of looking. A count is not an identification.
//
// The truncation is ANNOUNCED rather than silent: a roster that quietly dropped names would let an
// operator conclude their validator is fine because it is not listed, which is the opposite of what
// this line exists to say.
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
// ⛔ EXTRACTED SO THAT THE ORDER OF ITS TESTS CAN BE EXERCISED, AND THAT IS THE WHOLE POINT. The defect
// this closes was never the classification, it was the SEQUENCE: reading the extension's size before
// the vote's flag files a nil precommit — which cannot carry an extension, by a CometBFT rule — under
// a VRF fault, and sends the operator to inspect a key that is healthy. A test that only drives the
// `voteFlagReason` table stays green through exactly that mistake: the table is right either way, and
// the mistake lives in where it is called from. So the caller has to be reachable from a bench, and
// this is it.
//
// `totalPower` deliberately counts EVERY vote, contributing or not: the power share is a floor on
// participation, so a validator that is absent must weigh in the denominator or the share would rise
// as the network degrades.
func tallyVotes(votes []abci.ExtendedVoteInfo, alpha []byte, pubkeyOf func(addr []byte) ed25519.PublicKey) seedTally {
	var t seedTally
	for _, v := range votes {
		t.totalPower += v.Validator.Power
		// THE FLAG COMES FIRST, because it changes what the next test MEANS.
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
// the validator's anchored VRF key, VERIFIES the proof against alpha(extHeight), and aggregates the
// valid betas. Deterministic — AggregateBeacons sorts, and the input is the injected commit, identical
// on every node. Returns (seed, contributorCount, powerShareBps, ok); ok=false when no beta is valid.
// powerShareBps = Σ power of the votes carrying a VALID VRF proof ÷ Σ power of ALL votes in the commit,
// ×10000. Validators that are absent or proofless count in the denominator, which is the conservative
// direction. This is the power-weighted measure behind the ⌈2N/3⌉ floor: a floor on cardinality alone
// could be griefed with dust sybils.
func (app *App) aggregateSeed(ctx sdk.Context, ec abci.ExtendedCommitInfo, extHeight int64) ([]byte, int, uint64, bool) {
	alpha := app.vrfVoteAlphaForAggregation(ctx, extHeight)
	t := tallyVotes(ec.Votes, alpha, func(a []byte) ed25519.PublicKey {
		return app.validatorVrfPubkey(ctx, a)
	})
	betas, contribPower, totalPower := t.betas, t.contribPower, t.totalPower
	// OBSERVABILITY (ADR-034). A stuck `contributors=1` is undiagnosable without this: every
	// non-contributing vote is skipped by a `continue` that says nothing of its own, and no query exposes
	// ValidatorVrfPubkey, so an operator cannot even read back their own anchoring. Record WHY
	// contributors are missing — the vote never committed, no VRF extension, no anchored VRF key for that
	// validator, or an invalid proof — throttled so it cannot flood. This is
	// non-consensus: log output enters no state hash, so it is safe to emit here.
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

// validatorVrfPubkey resolves a validator's anchored VRF PUBLIC key from its CONSENSUS address (as
// supplied by ABCI): consAddr -> validator (staking) -> operator account -> anchored key. Returns nil
// when it cannot be found, is not anchored, or is invalid.
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
