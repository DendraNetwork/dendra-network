package keeper

import (
	"context"
	"errors"
	"strconv"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// DeclareJudge enters or leaves the jury pool (consensus epoch 15). Signed by the miner's operator,
// the key that signs its verdicts: a declaration by anyone else would let a stranger seat or unseat a
// miner. Free of gas like every transaction on this network, and unbonded: the cost of a seat is
// still an identity, and this handler prices nothing.
//
// `judge = true` writes the pool entry and the declaration height and resets the missed draws, from a
// declared or an undeclared state. That is the way back for a judge whose window ran
// out on a quiet network, and its price is stated there: an identity that never votes stays a
// candidate until `judge_missed_draws_max` of the audits it was drawn on under that declaration have
// concluded, then declares again. The counter moves at the conclusion and the draw happens before it,
// so the seats such an identity holds on audits still open at its last counted conclusion are bounded
// by the number of open audits, not by the cap. Each declaration is a signed transaction with an
// event, so the count of declarations per identity is a reading. `judge = false` forgets the four
// entries, as exit and eviction do. A seat already anchored is touched by neither: the anchor keeps
// its denominator and the seat answers like any other. At the conclusion it is counted only
// when it was drawn under the declaration its judge then holds (`recordMissedDraws`), so a seat drawn
// before a `judge = true` is not billed.
func (k msgServer) DeclareJudge(ctx context.Context, msg *types.MsgDeclareJudge) (*types.MsgDeclareJudgeResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	miner, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown miner")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "only the miner's operator may declare its judge")
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := uint64(sdkCtx.BlockHeight())
	if msg.Judge {
		if err := k.declareJudge(ctx, msg.MinerId, h); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	} else {
		k.clearJudgeState(ctx, msg.MinerId)
	}
	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"judge_declared",
		sdk.NewAttribute("miner_id", msg.MinerId),
		sdk.NewAttribute("judge", strconv.FormatBool(msg.Judge)),
		sdk.NewAttribute("height", strconv.FormatUint(h, 10)),
	))
	return &types.MsgDeclareJudgeResponse{}, nil
}
