package keeper

import (
	"context"
	"fmt"
	"math/big"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// OpenJob escrows the fee and fixes the job's beacon. The client deposits `fee` into the "jobs"
// module account, so payment can no longer be withdrawn once miners start working, and the chain
// fixes an unpredictable beacon (block height and block time, both decided by consensus) for this
// job. The assigned committee derives from that beacon: the creator cannot shop for a jobId that
// yields a friendly committee.
func (k msgServer) OpenJob(ctx context.Context, msg *types.MsgOpenJob) (*types.MsgOpenJobResponse, error) {
	clientBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// "__" separates the two halves of a Commit key (jobId__minerId), so it is forbidden inside a
	// jobId: otherwise extracting the minerId by LastIndex splits at the wrong place and a vote or a
	// slash gets attributed to the wrong miner.
	if msg.JobId == "" || strings.Contains(msg.JobId, "__") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid jobId (must be non-empty and contain no '__')")
	}

	// Params are read ONCE and reused by the Nash guard and by the reveal delay below.
	//
	// Fail closed, and both consumers depend on it. Treating a failed read as a working mode skipped
	// the Nash guard — so an arbitrarily large fee passed in optimistic mode — and made
	// `committee_reveal_delay` fall back to 0, that is, back to the PREDICTABLE seed that deferred
	// reveal exists to close. Two anti-grinding guards disarmed by the same absence, without a single
	// error being raised. A params read that fails authorises nothing.
	params, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic,
			"module parameters unreadable: neither the Nash guard nor the reveal delay can be evaluated (fail-closed refusal)")
	}
	// Nash guard (ADR-025), optimistic mode only. Cheating must stay -EV: with an audit rate
	// s = audit_sample_bps and a slash P = slash_leak_bps * stake, the Nash inequality s*P > (1-s)*g
	// (with g approximately the fee) requires
	//     audit_sample_bps * slash_leak_bps * min_stake > (10000-audit_sample_bps) * 10000 * fee.
	// The reference is min_stake, the floor every primary is guaranteed to hold, which makes the
	// guard CONSERVATIVE. big.Int keeps the products (around 1e21) overflow-free. Fail-safe: an armed
	// optimistic mode with no audit at all (sample 0) yields lhs = 0 and every job is refused.
	// Dormant in redundant mode (the default), where peer comparison carries verification.
	if params.VerificationMode == 1 {
		// (10000 - s) computed underflow-safe: if s >= 10000 — Validate forbids it, but this is the
		// consensus path and deserves defence in depth — then 1-s = 0, so rhs = 0 and the guard is
		// always satisfied, which is correct: auditing everything makes Nash trivially hold.
		oneMinusS := uint64(0)
		if params.AuditSampleBps < 10000 {
			oneMinusS = 10000 - params.AuditSampleBps
		}
		lhs := new(big.Int).Mul(new(big.Int).SetUint64(params.AuditSampleBps), new(big.Int).SetUint64(params.SlashLeakBps))
		lhs.Mul(lhs, new(big.Int).SetUint64(params.MinStake))
		rhs := new(big.Int).Mul(new(big.Int).SetUint64(oneMinusS), big.NewInt(10000))
		rhs.Mul(rhs, new(big.Int).SetUint64(msg.Fee))
		if lhs.Cmp(rhs) <= 0 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
				"fee too high for the optimistic Nash equilibrium (ADR-025 §2.5): raise min_stake or lower the fee")
		}
	}

	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Fee)))
	if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(clientBz), types.ModuleName, coins); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
	}

	// Anti-grinding beacon. The committee derives from the beacon, so the creator must not be able to
	// pick a jobId that yields a friendly committee. Two regimes, selected by committee_reveal_delay:
	//   - delay == 0: the seed is fixed at open time from `height:time`. Both quantities are visible
	//     to the job creator, so the seed is PREDICTABLE. Devnet only.
	//   - delay  > 0: DEFERRED REVEAL. The seed stays EMPTY; the EndBlocker fixes it at H+delay from
	//     the AppHash of that future block, which is unknown when the job is opened, so grinding the
	//     jobId buys nothing. Commits are refused until the seed is revealed (see CreateCommit and
	//     assignedCommittee).
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	delay := params.CommitteeRevealDelay
	if delay == 0 {
		seed := k.committeeBaseSeed(ctx, fmt.Sprintf("%d:%d", sdkCtx.BlockHeight(), sdkCtx.BlockTime().UnixNano()))
		if err := k.Beacon.Set(ctx, msg.JobId, types.Beacon{JobId: msg.JobId, Seed: seed}); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	} else {
		if err := k.Beacon.Set(ctx, msg.JobId, types.Beacon{JobId: msg.JobId, Seed: ""}); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		revealH := sdkCtx.BlockHeight() + int64(delay)
		if err := k.PendingReveal.Set(ctx, collections.Join(revealH, msg.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}

	// ADR-037 — the juror pool freeze is anchored HERE and nowhere else. By construction the anchor
	// predates every seed, whatever the seed's source (decentralised VRF, AppHash fallback, or any
	// future scheme), which is exactly what makes it safe without re-proving freshness at each draw.
	// An anchor taken later — at each draw — would admit more jurors and would be WRONG under the
	// fallback regime, where the seed is derivable one block ahead.
	// The accepted price: a job's eligible pool SHRINKS as the job ages (ADR-037 §4), which is why
	// Validate() enforces `miner_prune_blocks >= dispute_window`.
	// ⛔ A JOB OPENED INTO AN EMPTY POOL IS BORN UNVERIFIABLE, AND NOTHING EVER REPAIRS IT.
	// The freeze below is permanent by design (ADR-037): whoever is not eligible at this height never
	// becomes eligible for THIS job. The audit jury is drawn from that same frozen pool, minus the
	// primary — so a job opened while fewer than `audit_min_quorum + 1` miners are eligible can never
	// reach a quorum, at any later date, by any transaction. On the chain destroyed on 2026-08-13 that
	// was 42 jobs out of 51, and 272 406 udndr that no path could release.
	//
	// It is not a hypothetical: the very first job of the RELAUNCH reproduced it at block ~30, created
	// by the launcher's own end-to-end check while zero miners were registered. The failure needs no
	// attacker and no bug — only the ordinary order in which an operator brings a network up.
	//
	// Refusing is the ONLY honest answer. Accepting and hoping miners arrive is what the frozen pool
	// makes impossible, and the refusal is legible where the silence was not: the client sees why, at
	// the moment it can still act, instead of waiting forever on a job that looks open.
	//
	// THE POOL IS FROZEN ON THE BLOCK BEFORE THIS ONE, NOT ON THIS ONE.
	// Freezing at the opening height itself ties with a `create-miner` landing in the SAME block, and
	// `minerInFrozenPool` is `reg <= freeze`, so a tie is enough to be in. The block is composed by a
	// proposer that can read this very message from the mempool, so "registered in the same block" is
	// its choice, not a fact about who was already serving the network. See `frozenPoolAnchor`.
	// The cost is one block of latency for a miner that registers and a client that opens together --
	// paid by a fresh miner, never by one already running.
	//
	// ONE ANCHOR, COMPUTED ONCE, FOR THE GUARD AND FOR THE FREEZE. Certifying one population and
	// freezing another is how this guard came to accept a job whose pool was one miner short: the two
	// call sites have to be given the same value, not the same intention.
	anchor := frozenPoolAnchor(sdkCtx.BlockHeight())
	if err := k.enoughEligibleMinersToVerify(ctx, msg.JobId, anchor); err != nil {
		return nil, err
	}
	if err := k.JobPoolFreezeHeight.Set(ctx, msg.JobId, anchor); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// ONE ESCROW PER JOB ID, BECAUSE THE SECOND ONE COULD NEVER COME BACK.
	// Nothing refused a re-open: `Job.Set` below simply OVERWRITES the record, so a client retrying
	// "dup" with a different fee moved a SECOND escrow into the module while the stored `job.Fee`
	// kept only the last one. Every exit — settlement, payout, the expiry refund — computes from
	// `job.Fee`, so the first escrow had no path out of the module at all: not lost to an attacker,
	// simply unreachable for ever. An ordinary application retry is enough to trigger it.
	if has, hErr := k.Job.Has(ctx, msg.JobId); hErr == nil && has {
		return nil, errorsmod.Wrapf(sdkerrors.ErrInvalidRequest,
			"job %q already exists: re-opening it would take a SECOND escrow that no settlement path could ever return", msg.JobId)
	}

	// THE ESCROW GETS A WAY OUT, SET AT THE MOMENT IT IS TAKEN.
	// The fee has just moved from the client to the module. Every other exit from that module account
	// runs through settlement, and settlement needs a primary that commits: a miner that simply never
	// answers therefore left the client's coins there for ever, with no timeout, no message and no
	// authority path. The appointment below is the only thing that brings them back.
	// It is CANCELLED as soon as the job stops being untouched (abci.go checks the state and the commit
	// before refunding), so a served job is never clawed back by it.
	// NO APPOINTMENT WITHOUT AN ESCROW. `expireUnservedJob` returns immediately when `job.Fee == 0`,
	// so booking one for a fee-less job writes an entry that can never do anything.
	// That is not cosmetic: this chain runs with NO per-block gas limit (`max_gas = -1`) and with
	// `--minimum-gas-prices 0udndr`, so an `OpenJob` at Fee=0 is FREE -- without this guard, filling
	// the collection costs nobody anything.
	if msg.Fee > 0 {
		if err := k.PendingJobExpiry.Set(ctx, collections.Join(sdkCtx.BlockHeight()+jobExpiryBlocks, msg.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}

	job := types.Job{JobId: msg.JobId, Client: msg.Creator, MinerId: "", Fee: msg.Fee, State: "open"}
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// THE WORK COMMITTEE ANCHOR (ADR-045, 12), AND ITS PLACE IS A MEASUREMENT, NOT A TASTE.
	// Written higher up -- right after the seed -- it anchored NOTHING: the derivation reads the pool
	// freeze height, which is only stored further down. And the failure was SILENT, because the read
	// falls back to the derivation: green verdict, no anchor. The end-to-end test surfaced it; reading
	// the code never would have.
	// Nothing to anchor when the reveal is deferred: the seed does not exist yet, and the EndBlocker
	// anchors at the moment it arrives.
	if delay == 0 {
		k.anchorWorkCommittee(ctx, msg.JobId)
	}
	return &types.MsgOpenJobResponse{}, nil
}
