package keeper

import (
	"context"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// CommitteeSeedHealth exposes the health of the decentralized committee randomness (VRF bootstrap).
// The exporter derives `dendra_committee_grinding_inactive` = 1 IFF committee_seed_source==1 AND (no recent
// seed OR latest_contributors < committee_min_vrf_contributors) -> anti-grinding INACTIVE -> RED Grafana
// panel. This makes "a persistent alert is a precondition for opening" operable: the (b)/(c) fallback is
// otherwise only a log line that nobody reads.
func (q queryServer) CommitteeSeedHealth(ctx context.Context, req *types.QueryCommitteeSeedHealthRequest) (*types.QueryCommitteeSeedHealthResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	h := sdk.UnwrapSDKContext(ctx).BlockHeight()
	resp := &types.QueryCommitteeSeedHealthResponse{CurrentHeight: h}
	if p, err := q.k.Params.Get(ctx); err == nil {
		resp.CommitteeSeedSource = p.CommitteeSeedSource
		resp.CommitteeMinVrfContributors = p.CommitteeMinVrfContributors
	}
	// Most recent decentralized seed: the PreBlocker writes it at req.Height -> test H then H-1.
	for _, cand := range []int64{h, h - 1} {
		if cand <= 0 {
			continue
		}
		if seed, ok := q.k.GetDecentralizedSeed(ctx, cand); ok && len(seed) > 0 {
			resp.HasRecentSeed = true
			resp.LatestSeedHeight = cand
			resp.LatestContributors, _ = q.k.GetDecentralizedSeedContributors(ctx, cand)
			// THE SECOND BAR, WHICH NO QUERY EXPOSED. `committeeBaseSeedSourced` refuses the seed
			// under EITHER the head count above OR this power share against `MinVrfContributorPowerBps`.
			// A set whose COUNT is met while a large contributor stays silent falls under this one and
			// silently returns to the legacy fallback — anti-grinding inactive — while a report reading
			// only the count announces the floor as held. Published so that a health check can read
			// both bars instead of one.
			resp.ContributorPowerBps, _ = q.k.GetDecentralizedSeedContributorPower(ctx, cand)
			break
		}
	}
	return resp, nil
}
