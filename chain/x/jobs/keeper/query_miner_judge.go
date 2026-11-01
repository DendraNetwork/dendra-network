package keeper

import (
	"context"
	"errors"

	"cosmossdk.io/collections"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// MinerJudge answers the four judge entries epoch 15 keeps per miner, the ones `eligibleAsJudgeAt`
// reads at a draw: `MinerJudge`, `MinerJudgeDeclaredHeight`, `MinerLastVerdictHeight` and
// `MinerMissedDraws`. A client reads them before it declares, since a second `judge = true` rewrites
// the date and clears the count (`declareJudge`).
//
// A read-only handler: it calls Has and Get and nothing else, so a query leaves the store as it found
// it. Each entry is read on its own and answered as stored: a miner that is not declared can still
// carry a last-verdict height, which `noteJudgeAnswer` writes for an anchored seat whatever its
// declaration.
//
// An absent entry is its zero. An id the registry does not hold answers `declared = false` with zeros
// and no error, because a read is not a registration check: the caller reads the registry on its own
// path. Any other store error is returned, so an unreadable store never reads as "not declared".
func (q queryServer) MinerJudge(ctx context.Context, req *types.QueryMinerJudgeRequest) (*types.QueryMinerJudgeResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	// An empty id names no miner: the request is malformed, and answering zeros would hide it.
	if req.MinerId == "" {
		return nil, status.Error(codes.InvalidArgument, "miner_id is required")
	}
	declared, err := q.k.MinerJudge.Has(ctx, req.MinerId)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	resp := &types.QueryMinerJudgeResponse{Declared: declared}
	for _, entry := range []struct {
		store collections.Map[string, uint64]
		into  *uint64
	}{
		{q.k.MinerJudgeDeclaredHeight, &resp.DeclaredHeight},
		{q.k.MinerLastVerdictHeight, &resp.LastVerdictHeight},
		{q.k.MinerMissedDraws, &resp.MissedDraws},
	} {
		v, gErr := entry.store.Get(ctx, req.MinerId)
		switch {
		case gErr == nil:
			*entry.into = v
		case errors.Is(gErr, collections.ErrNotFound):
			// Absent: no date or no count, which is a fact and reads as 0.
		default:
			return nil, status.Error(codes.Internal, gErr.Error())
		}
	}
	return resp, nil
}
