package keeper

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// CreateCommit -- H1: SIGNATURE BINDING (anti-impersonation). A commit key is "<jobId>__<minerId>".
// Only the miner's OPERATOR (the tx signer) may anchor that miner's commit. Without this, anyone
// could anchor a proof in someone else's name and skew the verdict. Combined with L5 (each miner has
// its own key), this locks down the identity of the proofs.
// Longest payload this chain can actually read back, DERIVED from the bounds it already declares
// for a semantic commit: `embedMaxDim` values, each at most len("-1048576") = 8 bytes, separated by
// `embedMaxDim-1` commas. A hash commit (64 hex bytes) sits far below. No number is chosen here —
// change `embedMaxDim` or `embedMaxMag` and this follows.
const maxCommitPayload = embedMaxDim*len("-1048576") + (embedMaxDim - 1)

// `kind` is a free-form label the sender supplies and that NO on-chain decision reads (its single
// non-generated use is the write below). It is bounded rather than validated for exactly that
// reason: an enum here would be a rule nothing enforces, while a bound stops the field from being a
// place to park bytes. The longest value the shipped code emits is "revealmark" (10).
const maxCommitKind = 32

// maxCommitWeightsHash — DERIVED FROM THE ALGORITHM THE FIELD NAMES. `weights_hash` exists to be
// compared, case-insensitively, with the registry's `weights_sha256`: a SHA-256 written in hex is
// exactly `2 * sha256.Size` characters. Anything longer cannot match any anchor this chain stores, so
// accepting it only buys permanent bytes.
const maxCommitWeightsHash = 2 * sha256.Size

// maxCommitModelId — the identifier of a registered model, and the ONE bound here that is not derived
// from a rule the chain states, because the registry states none: `x/modelregistry` bounds neither the
// ids it accepts nor its digests (measured: no length test outside generated marshalling). So this
// number is anchored on what the shipped configuration actually carries — the longest id it names is
// `qwen3:30b-a3b-instruct-2507-q4_K_M` at 34 bytes — with room for a family of names of the same shape.
//
// ⚠️ THE CONSEQUENCE IS WORTH STATING RATHER THAN HIDING: this bound is the only one in the system, so a
// model registered with a longer id would become uncommittable. The registry ought to carry its own,
// and until it does this one has to be generous.
const maxCommitModelId = 128

func (k msgServer) CreateCommit(ctx context.Context, msg *types.MsgCreateCommit) (*types.MsgCreateCommitResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid address: %s", err))
	}

	// 1) DECOMPOSE THE KEY, ONCE, THROUGH THE SHARED DERIVATION.
	//
	// This used to be a local `LastIndex("__")`, and `commitProvesAssignedWork` held a second,
	// DIFFERENT one that also trimmed the `__verdict` / `__redo` roles. Two derivations for one key
	// shape stayed harmless while neither refused anything; the guard below refuses, so they cannot
	// disagree any more. `types.ParseCommitKey` is now the only place that knows the shapes.
	parsed, pok := types.ParseCommitKey(msg.JobId)
	if !pok {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"invalid commit key: expected \"<jobId>__<minerId>\", a role variant of it, or an epoch marker")
	}
	minerId := parsed.MinerId

	// H6 (deferred reveal): if THIS job's committee is not revealed yet (beacon present but seed
	// empty), refuse the commit -- otherwise assignedCommittee would fall back on a grindable seed.
	// Read on the DERIVED job id: with the old `key[:idx]` a verdict key looked up
	// `Beacon["<job>__verdict"]`, which never exists, so this check silently never applied to them.
	if parsed.JobScoped() {
		if b, berr := k.Beacon.Get(ctx, parsed.JobId); berr == nil && b.Seed == "" {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
				"committee not revealed yet (deferred reveal): retry after the reveal block")
		}
	}

	// 1bis) THE JOB NAMED BY THE KEY MUST EXIST.
	//
	// Without this, any REGISTERED miner writes a permanent entry under a jobId no job ever opened:
	// `UpdateCommit`/`DeleteCommit` are neutralised and the overwrite guard below refuses to replace
	// it, so the key is occupied for the life of the chain. It costs the writer nothing beyond gas —
	// there is no custom ante-handler, and no ValidateBasic anywhere in this module.
	//
	// ⛔ AND IT IS GATED ON `JobScoped()`, WHICH IS NOT A DETAIL. The epoch marker
	// `e<N>__reveals__<minerId>` is a legitimate commit that names NO job (produced by the reveal
	// worker, armed by default). Requiring a job for it would refuse every marker, at every epoch, on
	// every judge node — the audit layer cut by the guard meant to protect it.
	//
	// Fail-closed: a store that cannot answer is not an authorisation. `Has` returning an error means
	// the chain's state is unknown, and an unknown on a security criterion is a refusal.
	if parsed.JobScoped() {
		known, jerr := k.Job.Has(ctx, parsed.JobId)
		if jerr != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic,
				"job existence unreadable: the commit cannot be evaluated (fail-closed refusal)")
		}
		if !known {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound,
				"no job with this id: a commit cannot be anchored under a job that was never opened")
		}
	}

	// 1ter) LENGTH BOUNDS — DERIVED, NOT CHOSEN.
	//
	// Nothing bounded these fields, so a single transaction could carry roughly a mebibyte of
	// permanent state (CometBFT's default MaxTxBytes was the only limit). The bound is computed from
	// what the chain ALREADY declares about the payload it accepts: a semantic commit is at most
	// `embedMaxDim` signed integers of magnitude `embedMaxMag`, comma-separated
	// (`msg_server_verify_semantic.go`). Anything longer is not a commit this chain can read.
	if len(msg.ResultCommit) > maxCommitPayload {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "result_commit too long")
	}
	if len(msg.PromptCommit) > maxCommitPayload {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "prompt_commit too long")
	}
	if len(msg.Kind) > maxCommitKind {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "kind too long")
	}
	// ⛔ THE TWO MODEL-REGISTRY FIELDS ESCAPED THE LIST THREE LINES ABOVE, IN THIS FUNCTION.
	// The bound above exists because a single transaction could otherwise carry roughly a mebibyte of
	// permanent state; `model_id` and `weights_hash` are stored by the same write and were not bounded.
	// MEASURED before this line existed: a commit carrying 1 MiB in EACH field was accepted and stored —
	// 2 MiB of state from one transaction, on a chain whose block carries no gas limit and whose
	// validators run at a zero minimum gas price. And it cannot be reclaimed: `DeleteCommit` refuses by
	// design ("IMMUTABLE commit (H1): deletion is forbidden (tally evasion)"), so the bytes stay for the
	// life of the chain.
	if len(msg.ModelId) > maxCommitModelId {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "model_id too long")
	}
	if len(msg.WeightsHash) > maxCommitWeightsHash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "weights_hash too long")
	}

	// 2) the miner must exist, and the signer must be ITS operator
	miner, err := k.Miner.Get(ctx, minerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown miner for this commit")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"only the miner's operator may anchor its commit")
	}

	// 3) refuse to overwrite an existing commit
	ok, err := k.Commit.Has(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	} else if ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "index already set")
	}

	// If the model registry is ENFORCED (governance parameter, OFF by default), the model served MUST
	// be registered and active. OFF keeps a devnet without registered models working.
	//
	// ⭐ FAIL CLOSED. On a SECURITY criterion, a params read that fails does not mean "registry
	// disarmed": it means the state of the chain is unknown. A `perr == nil &&` turned that ignorance
	// into authorisation, on the attacker's side, since the commit then passed with NO model_id <->
	// artefact binding at all. Absence must be an ERROR, never a "compliant".
	params, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic,
			"module parameters unreadable: the model registry cannot be evaluated (fail-closed refusal)")
	}
	if params.EnforceModelRegistry {
		if strings.TrimSpace(msg.ModelId) == "" {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "model_id required (model registry enforced)")
		}
		if !k.modelRegistryKeeper.IsActive(ctx, msg.ModelId) {
			return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "model not registered or inactive")
		}
		// model_id <-> ARTEFACT BINDING. The commit carries the weights_hash of the model ACTUALLY
		// served, and it is compared to the weights_sha256 ANCHORED in the registry. A miner can no
		// longer declare one model_id and serve something else without either diverging from the
		// committee (semantic slash) or presenting a weights_hash that differs from the registry
		// (refused here). To be honest about what this buys: it BINDS the declaration to an artefact, it
		// is NOT a proof of execution — the optimistic/statistical model is the accepted trade-off for
		// consumer GPUs. Gated on the registry holding a NON-EMPTY weights_sha256 for that model;
		// otherwise nothing is required, which keeps devnet backward compatible.
		if want, ok := k.modelRegistryKeeper.ExpectedWeights(ctx, msg.ModelId); ok && strings.TrimSpace(want) != "" {
			if strings.TrimSpace(msg.WeightsHash) == "" {
				return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "weights_hash required (registry enforced with a weights anchor)")
			}
			if !strings.EqualFold(strings.TrimSpace(msg.WeightsHash), strings.TrimSpace(want)) {
				return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "weights_hash does not match the weights registered for this model_id")
			}
		}
	}

	commit := types.Commit{
		Creator:      msg.Creator,
		JobId:        msg.JobId,
		PromptCommit: msg.PromptCommit,
		ResultCommit: msg.ResultCommit,
		Kind:         msg.Kind,
		ModelId:      msg.ModelId,
		WeightsHash:  msg.WeightsHash,
	}
	if err := k.Commit.Set(ctx, commit.JobId, commit); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// ADR-034 — PROOF OF VITALITY. Anchoring a commit is the only act that demonstrates a miner
	// PRODUCES: it is signed by its operator and carries a result. The last occurrence is dated, and
	// that is what distinguishes a live miner from a registered, silent identity. Without this date,
	// "registered" was the only known quality of a miner — and it is bought once for `min_stake`, which
	// was enough to occupy jury seats without ever voting. Written for EVERY commit, audit verdicts
	// included: judging IS production.
	//
	// VITALITY MUST NOT BE SELF-ISSUED. `CreateCommit` on its own requires neither that the miner be
	// assigned to the job nor that it was DRAWN onto the committee. Guarding only the EXISTENCE of the
	// job (`Job.Has`) is insufficient: `create-commit "<REAL_jobId>__<mSybil>"` still manufactured fresh
	// vitality FOR THE PRICE OF GAS, hence a perpetual juror seat and immunity from eviction. The
	// missing proof of assignment is membership in the drawn committee, which is what
	// `commitProvesAssignedWork` checks (the union primary ∪ audit ∪ redo, see miner_vitality.go).
	// Re-adjudication (`__redo__`) counts as production too; trimming only the `__verdict` suffix missed
	// it, so dedicated jurors lost their vitality and the pool spiralled towards death.
	//
	// Only the writing of the DATE is gated, never the commit itself: no existing path breaks, and the
	// guard is not punitive — an unproven miner regains its vitality on its next ASSIGNED commit.
	if h := sdk.UnwrapSDKContext(ctx).BlockHeight(); h > 0 {
		proves, pErr := k.commitProvesAssignedWork(ctx, msg.JobId, minerId)
		if pErr != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, pErr.Error())
		}
		if proves {
			if err := k.MinerLastCommitHeight.Set(ctx, minerId, uint64(h)); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
		}
	}
	return &types.MsgCreateCommitResponse{}, nil
}

// NEUTRALISED. An anchored commit is IMMUTABLE — that is the foundation of H1. Otherwise an operator
// watches everyone else's PUBLIC commits, then rewrites its own to align with the majority (escaping
// the slash) or deletes it to vanish from the tally, making the on-chain verdict forgeable at will.
// Only `CreateCommit` anchors, once, signed by the operator. Any correction goes through a
// governance-gated dispute.
func (k msgServer) UpdateCommit(ctx context.Context, msg *types.MsgUpdateCommit) (*types.MsgUpdateCommitResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "IMMUTABLE commit (H1): an anchored result cannot be rewritten")
}

func (k msgServer) DeleteCommit(ctx context.Context, msg *types.MsgDeleteCommit) (*types.MsgDeleteCommitResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "IMMUTABLE commit (H1): deletion is forbidden (tally evasion)")
}
