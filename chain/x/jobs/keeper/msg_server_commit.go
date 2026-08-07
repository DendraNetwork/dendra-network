package keeper

import (
	"context"
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
func (k msgServer) CreateCommit(ctx context.Context, msg *types.MsgCreateCommit) (*types.MsgCreateCommitResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid address: %s", err))
	}

	// 1) extract the minerId from the "<jobId>__<minerId>" key
	idx := strings.LastIndex(msg.JobId, "__")
	if idx < 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"invalid commit key (expected \"<jobId>__<minerId>\")")
	}
	minerId := msg.JobId[idx+2:]

	// H6 (deferred reveal): if THIS job's committee is not revealed yet (beacon present but seed
	// empty), refuse the commit -- otherwise assignedCommittee would fall back on a grindable seed.
	if b, berr := k.Beacon.Get(ctx, msg.JobId[:idx]); berr == nil && b.Seed == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"committee not revealed yet (deferred reveal): retry after the reveal block")
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
