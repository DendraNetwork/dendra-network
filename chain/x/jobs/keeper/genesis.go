package keeper

import (
	"context"
	"errors"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	for _, elem := range genState.MinerMap {
		if err := k.Miner.Set(ctx, elem.MinerId, elem); err != nil {
			return err
		}
	}
	// RÉ-ANCRAGE DES DISPUTES (pendant du BlocksRemaining ci-dessous).
	// Dans le genesis, `DisputeHeight` ne porte PAS une hauteur absolue mais le nombre de blocs
	// ÉCOULÉS depuis l'ouverture de la dispute (cf. ExportGenesis). On le ré-ancre sur la hauteur
	// d'init : le TEMPS RESTANT de la fenêtre est ainsi préservé à l'identique.
	// Le résultat peut être NÉGATIF (fenêtre déjà écoulée avant l'export) — c'est voulu et sûr :
	// `DisputeHeight` est un int64 signé, aucun code métier ne teste sa valeur (une dispute se
	// reconnaît à l'état `+disputed`), et une valeur négative rend l'adjudication immédiatement
	// possible, ce qui est exactement le comportement dû à une fenêtre expirée.
	initHJobs := sdk.UnwrapSDKContext(ctx).BlockHeight()
	for _, elem := range genState.JobMap {
		if elem.DisputeHeight != 0 {
			elem.DisputeHeight = initHJobs - elem.DisputeHeight
		}
		if err := k.Job.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
	}
	if genState.Pools != nil {
		if err := k.Pools.Set(ctx, *genState.Pools); err != nil {
			return err
		}
	}
	for _, elem := range genState.CommitMap {
		if err := k.Commit.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
	}
	for _, elem := range genState.BeaconMap {
		if err := k.Beacon.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
	}

	// Les échéances arrivent en TEMPS RESTANT : on les ré-ancre sur la hauteur de reprise, sinon un
	// rendez-vous exporté en absolu tomberait derrière la chaîne relancée et n'aurait jamais lieu.
	initH := sdk.UnwrapSDKContext(ctx).BlockHeight()

	// --- ÉTAT AJOUTÉ (champs 7+ du GenesisState) ---------------------------------------------------
	// Tout ce bloc est tolérant au vide : un genesis produit par un binaire antérieur n'a simplement
	// aucun de ces champs, et l'import doit alors se comporter exactement comme avant.
	for _, e := range genState.HeldFee {
		if err := k.HeldFee.Set(ctx, e.JobId, e.Amount); err != nil {
			return err
		}
	}
	for _, e := range genState.HeldBurn {
		if err := k.HeldBurn.Set(ctx, e.JobId, e.Amount); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingReveal {
		if err := k.PendingReveal.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingAudit {
		if err := k.PendingAudit.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingAuditResolve {
		if err := k.PendingAuditResolve.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingAppealResolve {
		if err := k.PendingAppealResolve.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.AuditCommittee {
		if err := k.AuditCommittee.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.ValidatorVrfPubkey {
		if err := k.ValidatorVrfPubkey.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.MinerOptimisticCount {
		if err := k.MinerOptimisticCount.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.AvailFailCount {
		if err := k.AvailFailCount.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.AvailFailWindowStart {
		if err := k.AvailFailWindowStart.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	if genState.AvailChallenge != "" {
		if err := k.AvailChallenge.Set(ctx, genState.AvailChallenge); err != nil {
			return err
		}
	}

	// INVARIANT #8 (internal audit 2026-06-26) — défense runtime : refuser au chargement un genesis dont les params
	// rendraient le self-dealing Sybil +EV (drain d'émission). Double la garde de GenesisState.Validate() au cas
	// où un chemin d'init contournerait validate-genesis.
	if !genState.Params.WashSubsidyNegativeEV(genState.Params.WorkGateBps) {
		return errors.New("invariant #8 viole : params de genesis rendraient le self-dealing Sybil +EV (drain d'emission)")
	}

	// INSTRUMENT ANTI-GRIEF SANS MORDANT — rendu visible.
	//
	// Ouvrir une dispute escrowe `dispute_bond`, et une dispute infondée le perd (cf. resolveDisputedAudit).
	// À 0, cette confiscation ne confisque rien : le mécanisme existe, il est inerte, et contester
	// n'importe quel job réglé devient gratuit. Or `dispute_window > 0` suffit à ACTIVER les disputes.
	//
	// On ne refuse pas : `config.yml` et un genesis écrit à la main ne posent pas ce champ, et le
	// refuser ferait paniquer le démarrage d'une chaîne neuve — le piège dans lequel une garde trop
	// zélée tombe deux fois plutôt qu'une. On le NOMME, avec sa conséquence.
	if genState.Params.VerificationMode == 1 && genState.Params.DisputeWindow > 0 && genState.Params.DisputeBond == 0 {
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"SECURITE: dispute_bond=0 alors que les disputes sont ACTIVES (dispute_window>0) -> contester est GRATUIT et la confiscation anti-grief ne confisque rien. Poser dispute_bond > 0 au genesis (ancrage suggere : min_stake).",
			"dispute_window", genState.Params.DisputeWindow, "min_stake", genState.Params.MinStake)
	}
	return k.Params.Set(ctx, genState.Params)
}

// ExportGenesis returns the module's exported genesis.
func (k Keeper) ExportGenesis(ctx context.Context) (*types.GenesisState, error) {
	var err error

	genesis := types.DefaultGenesis()
	genesis.Params, err = k.Params.Get(ctx)
	if err != nil {
		return nil, err
	}
	if err := k.Miner.Walk(ctx, nil, func(_ string, val types.Miner) (stop bool, err error) {
		genesis.MinerMap = append(genesis.MinerMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	// ÉCHÉANCE DE DISPUTE : exportée en RELATIF, jamais en absolu.
	// `DisputeHeight` est une hauteur de l'ANCIENNE chaîne. Exportée telle quelle et réimportée
	// dans une chaîne repartant de zéro, la garde `BlockHeight() < DisputeHeight + DisputeWindow`
	// (msg_server_adjudicate.go) reste vraie pendant TOUTE la hauteur de l'ancienne chaîne :
	// l'adjudication devient inatteignable et le bond du disputeur reste immobilisé — le job est
	// gelé définitivement. On exporte donc les blocs ÉCOULÉS depuis l'ouverture ; InitGenesis les
	// ré-ancre. Même raisonnement que `BlocksRemaining` pour les files d'attente.
	expHJobs := sdk.UnwrapSDKContext(ctx).BlockHeight()
	if err := k.Job.Walk(ctx, nil, func(_ string, val types.Job) (stop bool, err error) {
		if val.DisputeHeight != 0 {
			val.DisputeHeight = expHJobs - val.DisputeHeight
		}
		genesis.JobMap = append(genesis.JobMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	pools, err := k.Pools.Get(ctx)
	if err != nil && !errors.Is(err, collections.ErrNotFound) {
		return nil, err
	}
	genesis.Pools = &pools
	if err := k.Commit.Walk(ctx, nil, func(_ string, val types.Commit) (stop bool, err error) {
		genesis.CommitMap = append(genesis.CommitMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.Beacon.Walk(ctx, nil, func(_ string, val types.Beacon) (stop bool, err error) {
		genesis.BeaconMap = append(genesis.BeaconMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}

	// --- ÉTAT AJOUTÉ (cf. genesis.proto champs 7+) --------------------------------------------------
	// NE SONT VOLONTAIREMENT PAS EXPORTÉS : DecentralizedSeed, DecentralizedSeedContributors,
	// DecentralizedSeedContributorPower, BlockHash — et `Available`.
	//
	// Les quatre premières sont des caches indexés par HAUTEUR, produits par les vote-extensions.
	// `Available` est indexé par ÉPOQUE (msg_server_prove_availability.go:100) et purgé à chaque
	// frontière (availability.go:114). Après un genesis, ni une hauteur ni un index d'époque ne
	// désignent plus rien : les transporter produirait des clés qui ne pointent nulle part, et leur
	// appliquer un calcul en hauteurs ne produirait qu'un nombre sans signification.
	//
	// Leur absence est donc un CHOIX documenté, pas un oubli — distinction qui compte, parce que
	// « collection non exportée » ne veut pas dire « donnée perdue ».
	if err := k.HeldFee.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.HeldFee = append(genesis.HeldFee, types.HeldAmount{JobId: k2, Amount: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.HeldBurn.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.HeldBurn = append(genesis.HeldBurn, types.HeldAmount{JobId: k2, Amount: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	// TEMPS RESTANT, pas hauteur absolue (cf. genesis.proto/PendingEntry). Plancher à 1 : une échéance
	// déjà dépassée au moment de l'export doit se déclencher au bloc suivant la reprise, pas rester
	// derrière la hauteur courante — où le walk, qui matche la hauteur EXACTE, ne la verrait jamais.
	now := sdk.UnwrapSDKContext(ctx).BlockHeight()
	remaining := func(deadline int64) int64 {
		if r := deadline - now; r > 0 {
			return r
		}
		return 1
	}

	// Les 5 KeySet à clé composite : Walk explicite pour chacun. Un helper générique exigerait une
	// interface dont la signature doit coller exactement à celle de collections.KeySet ; l'écrire à la
	// main est plus long mais ne peut pas se tromper de contrat.
	if err := k.PendingReveal.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingReveal = append(genesis.PendingReveal, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingAudit.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingAudit = append(genesis.PendingAudit, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingAuditResolve.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingAuditResolve = append(genesis.PendingAuditResolve, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingAppealResolve.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingAppealResolve = append(genesis.PendingAppealResolve, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.AuditCommittee.Walk(ctx, nil, func(k2, v string) (bool, error) {
		genesis.AuditCommittee = append(genesis.AuditCommittee, types.StringEntry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.ValidatorVrfPubkey.Walk(ctx, nil, func(k2, v string) (bool, error) {
		genesis.ValidatorVrfPubkey = append(genesis.ValidatorVrfPubkey, types.StringEntry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.MinerOptimisticCount.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.MinerOptimisticCount = append(genesis.MinerOptimisticCount, types.Uint64Entry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.AvailFailCount.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.AvailFailCount = append(genesis.AvailFailCount, types.Uint64Entry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.AvailFailWindowStart.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.AvailFailWindowStart = append(genesis.AvailFailWindowStart, types.Uint64Entry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if ac, e := k.AvailChallenge.Get(ctx); e == nil {
		genesis.AvailChallenge = ac
	} else if !errors.Is(e, collections.ErrNotFound) {
		return nil, e
	}

	return genesis, nil
}
