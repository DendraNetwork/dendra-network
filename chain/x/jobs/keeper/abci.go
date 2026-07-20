package keeper

import (
	"context"
	"encoding/hex"
	"fmt"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// EndBlock -- RÉVÉLATION DIFFÉRÉE DU COMITÉ (H6, anti-grinding réel).
//
// À chaque bloc, on fige la graine des beacons dont la hauteur de révélation est atteinte, à partir de
// l'AppHash du bloc COURANT. Cet AppHash résulte de l'exécution de blocs POSTÉRIEURS à l'open du job :
// le créateur ne pouvait donc pas le prédire au moment de choisir son jobId -> le grinding du comité
// devient inopérant. Tant que la graine n'est pas figée, CreateCommit et assignedCommittee refusent
// (pas de repli sur une graine grindable). Sans job en attente (régime delay==0, défaut), l'itération
// est vide -> coût négligeable.
func (k Keeper) EndBlock(ctx context.Context) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	// Graine commune du bloc : AppHash (imprévisible à l'open) + hauteur. L'unicité par job est assurée
	// par "|jobId" dans assignedCommittee, donc deux jobs révélés au même bloc gardent des comités distincts.
	ah := hex.EncodeToString(sdkCtx.HeaderInfo().AppHash)
	if ah == "" {
		ah = "genesis" // repli si AppHash vide (tout début de chaîne) -- même garde qu'availability.go (NEW-CM-03)
	}
	seed := k.committeeBaseSeed(ctx, fmt.Sprintf("%s:%d", ah, h))

	// Collecter les jobs dus à CETTE hauteur (préfixe = h), puis appliquer (pas de mutation pendant Walk).
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingReveal.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range due {
		if b, err := k.Beacon.Get(ctx, jobId); err == nil && b.Seed == "" {
			b.Seed = seed
			if err := k.Beacon.Set(ctx, jobId, b); err != nil {
				return err
			}
		}
		if err := k.PendingReveal.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}

	// ADR-025 (M3) — TIRAGE D'AUDIT optimiste. Pour chaque job réglé optimiste programmé à CETTE hauteur,
	// la chaîne s'auto-conteste avec probabilité audit_sample_bps : audit ⇔ H(seed‖jobId) mod 10000 < bps.
	// `seed` (AppHash / VRF décentralisée du bloc courant) est POSTÉRIEUR au commit -> imprévisible quand le
	// mineur répond (anti-grinding, comme la révélation différée). DORMANT : no-op si verification_mode!=1
	// (PendingAudit n'est alimenté que par settleOptimistic, lui-même mode 1).
	if err := k.runOptimisticAudit(ctx, h, seed); err != nil {
		return err
	}
	// ADR-025 (liveness) — auto-résout les audits non honorés arrivés à échéance (dormant si audit_resolve_timeout=0).
	if err := k.runAuditResolveTimeout(ctx, h); err != nil {
		return err
	}
	// PLAN-V2-FEE-HOLD §B — 2e échéance d'APPEL (révélation tardive permissionless du primaire honnête-hors-ligne).
	// DORMANT si appeal_window=0 (PendingAppealResolve jamais alimenté).
	if err := k.runAppealResolveTimeout(ctx, h); err != nil {
		return err
	}

	// Phase 1b -- disponibilité : à chaque frontière d'époque, verser l'AvailPool aux mineurs prouvés
	// présents (pondéré par le bond) puis rouler le défi. OFF par défaut (avail_epoch_blocks=0).
	return k.runAvailabilityEpoch(ctx, h)
}
