package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ResolveDisputedAuditForDecision8Test exposes resolveDisputedAudit with its allowAppeal flag. Through
// EndBlock, an appeal deadline written at the current height is consumed by the appeal pass of the same
// block, so only a direct call shows whether the deferral wrote one.
func (k Keeper) ResolveDisputedAuditForDecision8Test(ctx context.Context, h int64, jobId string, p types.Params, allowAppeal bool) error {
	return k.resolveDisputedAudit(ctx, h, jobId, p, allowAppeal)
}
