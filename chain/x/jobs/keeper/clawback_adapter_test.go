package keeper

import (
	"context"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

func (k Keeper) ClawbackPaymentForCoverage(ctx context.Context, job *types.Job, p types.Params, mute bool) error {
	return k.clawbackPayment(ctx, job, p, mute)
}
