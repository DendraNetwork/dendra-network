package keeper

import (
	"context"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"dendra/x/jobs/types"
)

// CommitteeSeedHealth (bootstrap VRF, internal audit 2026-06-26) — expose la santé de l'aléa de comité décentralisé.
// L'exporter en dérive `dendra_committee_grinding_inactive` = 1 SSI committee_seed_source==1 ET (pas de graine
// récente OU latest_contributors < committee_min_vrf_contributors) -> anti-grinding INACTIF -> panneau Grafana
// ROUGE. Rend « alerte persistante = précondition d'ouverture » OPÉRABLE (le repli (b)/(c) est un log noyé).
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
	// graine décentralisée la plus récente : le PreBlocker la pose à req.Height -> tester H puis H-1.
	for _, cand := range []int64{h, h - 1} {
		if cand <= 0 {
			continue
		}
		if seed, ok := q.k.GetDecentralizedSeed(ctx, cand); ok && len(seed) > 0 {
			resp.HasRecentSeed = true
			resp.LatestSeedHeight = cand
			resp.LatestContributors, _ = q.k.GetDecentralizedSeedContributors(ctx, cand)
			break
		}
	}
	return resp, nil
}
