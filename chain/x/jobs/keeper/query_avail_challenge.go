package keeper

import (
	"context"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"dendra/x/jobs/types"
)

// GetAvailChallenge -- Phase 1b. Renvoie le défi de disponibilité courant (que le mineur doit renvoyer
// via MsgProveAvailability), l'époque de disponibilité courante et la période. challenge vide +
// avail_epoch_blocks=0 => disponibilité désactivée (rien à prouver).
func (q queryServer) GetAvailChallenge(ctx context.Context, req *types.QueryGetAvailChallengeRequest) (*types.QueryGetAvailChallengeResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	resp := &types.QueryGetAvailChallengeResponse{}
	if p, err := q.k.Params.Get(ctx); err == nil {
		resp.AvailEpochBlocks = p.AvailEpochBlocks
		if p.AvailEpochBlocks > 0 {
			resp.Epoch = sdk.UnwrapSDKContext(ctx).BlockHeight() / int64(p.AvailEpochBlocks)
		}
	}
	if c, err := q.k.AvailChallenge.Get(ctx); err == nil {
		resp.Challenge = c
	}
	return resp, nil
}
