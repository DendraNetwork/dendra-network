package keeper

import (
	"context"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// GetAvailChallenge returns the current availability challenge (which the miner must answer with
// MsgProveAvailability), the epoch of that challenge and the epoch length. An empty challenge
// together with avail_epoch_blocks=0 means availability proofs are disabled: there is nothing to prove.
// The epoch is availChallengeEpoch, the one ProveAvailability records a proof under and starts the
// deadline from, so the query and the handler read one definition on the same state.
func (q queryServer) GetAvailChallenge(ctx context.Context, req *types.QueryGetAvailChallengeRequest) (*types.QueryGetAvailChallengeResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	resp := &types.QueryGetAvailChallengeResponse{}
	if p, err := q.k.Params.Get(ctx); err == nil {
		resp.AvailEpochBlocks = p.AvailEpochBlocks
		if p.AvailEpochBlocks > 0 {
			epoch, eErr := q.k.availChallengeEpoch(ctx, int64(p.AvailEpochBlocks))
			if eErr != nil {
				return nil, status.Error(codes.Internal, eErr.Error())
			}
			resp.Epoch = epoch
		}
	}
	if c, err := q.k.AvailChallenge.Get(ctx); err == nil {
		resp.Challenge = c
	}
	return resp, nil
}
