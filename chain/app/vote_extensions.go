package app

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"os"
	"strconv"
	"strings"

	abci "github.com/cometbft/cometbft/abci/types"
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
// node that legitimately has no key. The chain's own diagnostic can only report `no_extension=1`, i.e.
// WHICH validator is mute, never WHY, so the cause has to be carried out of here or it is lost. A silent
// nil in a security path is indistinguishable from a healthy one.
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

// setupVoteExtensions wires the ABCI++ ExtendVote / VerifyVoteExtension handlers. Called from New()
// BEFORE app.Load(), i.e. before the baseapp is sealed. Inert while vote extensions are off.
func (app *App) setupVoteExtensions() {
	nodeSK, vrfKeyReason := loadNodeVrfKey() // may be nil (node without a VRF key -> empty extension)

	// SAID ONCE, AT BOOT, WHERE AN OPERATOR LOOKS. The key is read here and never re-read, so this line
	// is the whole truth about whether this node can contribute at all: nothing later in the process
	// restates it. A node with no key says so and why; a node with one prints the public key, which is
	// exactly what has to match the anchored value.
	if len(nodeSK) == ed25519.PrivateKeySize {
		app.Logger().Info("VRF: node key loaded, this node WILL contribute to the committee seed",
			"pubkey", hex.EncodeToString(nodeSK.Public().(ed25519.PublicKey)),
			"check", "it must equal the key anchored by register-validator-vrf-key")
	} else {
		app.Logger().Error("VRF: NO node key -> this node contributes NOTHING to the committee seed "+
			"(its vote extension stays empty; consensus and liveness are unaffected)", "reason", vrfKeyReason)
	}

	// ExtendVote: the node signs alpha(height) with ITS VRF key -> an 80-byte ECVRF proof carried as
	// the extension. Never fail a vote over a VRF problem: return an empty extension instead.
	// ⚠️ Both branches below produce the SAME empty extension. Without the boot line above and the
	// throttled error below, they would be indistinguishable from each other and from "no key on purpose".
	proveErrs := 0
	app.SetExtendVoteHandler(func(ctx sdk.Context, req *abci.RequestExtendVote) (*abci.ResponseExtendVote, error) {
		if len(nodeSK) != ed25519.PrivateKeySize {
			return &abci.ResponseExtendVote{VoteExtension: []byte{}}, nil
		}
		pi, err := vrf.Prove(nodeSK, app.vrfVoteAlphaWithHash(ctx, req.Height, req.Hash))
		if err != nil {
			// Throttled: this runs every block, and a log that floods gets filtered out, which is a
			// second way of being silent. Non-consensus — log output enters no state hash.
			proveErrs++
			if proveErrs == 1 || proveErrs%100 == 0 {
				ctx.Logger().Error("VRF: the key loaded but PROVING failed -> empty extension, no contribution",
					"height", req.Height, "occurrences", proveErrs, "err", err)
			}
			return &abci.ResponseExtendVote{VoteExtension: []byte{}}, nil
		}
		return &abci.ResponseExtendVote{VoteExtension: pi}, nil
	})

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
		if voteExtensionsActive(ctx, req.Height) {
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
				_ = app.JobsKeeper.DeleteDecentralizedSeedContributorPower(ctx, old)
			}
			if len(req.Txs) > 0 && bytes.HasPrefix(req.Txs[0], vrfInjectPrefix) {
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
	var betas [][]byte
	var contribPower, totalPower int64
	var noExt, noPk, badProof int
	for _, v := range ec.Votes {
		totalPower += v.Validator.Power
		if len(v.VoteExtension) != vrf.ProofSize {
			noExt++
			continue
		}
		pk := app.validatorVrfPubkey(ctx, v.Validator.Address)
		if pk == nil {
			noPk++
			continue
		}
		if ok, beta := vrf.Verify(pk, alpha, v.VoteExtension); ok {
			betas = append(betas, beta)
			contribPower += v.Validator.Power
		} else {
			badProof++
		}
	}
	// OBSERVABILITY (ADR-034). A stuck `contributors=1` is undiagnosable without this: every
	// non-contributing vote is skipped by a `continue` that says nothing of its own, and no query exposes
	// ValidatorVrfPubkey, so an operator cannot even read back their own anchoring. Record WHY
	// contributors are missing — no VRF extension, no anchored VRF key for that validator, or an invalid
	// proof — throttled so it cannot flood. This is
	// non-consensus: log output enters no state hash, so it is safe to emit here.
	if (noExt+noPk+badProof) > 0 && extHeight%20 == 0 {
		ctx.Logger().Info("VRF: votes not contributing to the decentralized seed (diagnostic)",
			"height", extHeight, "votes_total", len(ec.Votes), "contributing", len(betas),
			"no_extension", noExt, "vrf_key_not_anchored", noPk, "invalid_proof", badProof)
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
