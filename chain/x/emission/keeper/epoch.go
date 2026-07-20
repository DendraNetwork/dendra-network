package keeper

import (
	"context"
	"strconv"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"dendra/x/emission/types"
)

// EpochBlocks — nombre de blocs entre deux libérations Réserve (repli si le genesis ne fixe pas
// `epoch_blocks`). DÉFAUT RÉALISTE = ~1 an de blocs (~1 s/bloc) : avec reserve_release_bps=2200, la
// Réserve libère alors ~22 %/AN de son RESTANT (courbe décroissante), PAS 22 % par époque. Un DEVNET
// observable doit l'override via genesis (`emission.params` : epoch_blocks court + reserve_release_bps faible).
const EpochBlocks uint64 = 31_536_000

// GenesisReserveU — Réserve initiale (udndr) = 3,3 M DNDR (alloc v5). Posée au genesis ; init paresseuse sinon.
const GenesisReserveU uint64 = 3_300_000_000000

// RunEpoch — appelé par BeginBlock. Tous les EpochBlocks, libère une tranche DÉCROISSANTE de la
// Réserve (moteur v5, ADR-023) et l'alloue aux 3 pools. MVP = COMPTABILITÉ on-chain déterministe :
// la Réserve (compteur udndr) décroît, les pools cumulent ; le MOUVEMENT de coins (paiement depuis
// le compte de module) = increment suivant. `nonRecDemand = 0` pour l'instant -> le flux TRAVAIL est
// gaté à 0 (anti-self-dealing par défaut) tant que la mesure des frais non-récup. par époque n'est pas câblée.
func (k Keeper) RunEpoch(ctx context.Context) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := uint64(sdkCtx.BlockHeight())

	// EM-04 : paramètres d'émission GOUVERNABLES (lus on-chain). Repli sur les défauts v5 (locaux) si
	// non configurés (genesis vide / chaîne déjà lancée) -> rétro-compatible, pas de halt.
	// ⚠️ `0` SIGNIFIE « N'EMETS RIEN », PAS « NON CONFIGURE ».
	// L'ancienne forme testait `if pp.ReserveReleaseBps != 0` avant d'adopter les params gouvernes :
	// poser `reserve_release_bps = 0` — le geste EXACT d'une gouvernance qui veut STOPPER l'emission,
	// et une valeur que `Validate()` accepte — faisait donc ignorer TOUT le jeu de params et retomber
	// sur les defauts codes en dur, soit 22 % de la Reserve relaches PAR EPOQUE, plus les splits et le
	// gate votes ecrases au passage. Une decision de gouvernance produisait son exact contraire.
	//
	// Le seul repli legitime est l'ABSENCE de params en store (chaine anterieure a EM-04), que
	// `Params.Get` signale par une ERREUR. Des lors que des params EXISTENT, ils font foi tels quels.
	ep := DefaultParams()
	epochBlocks := EpochBlocks
	if pp, perr := k.Params.Get(ctx); perr == nil {
		ep = Params{ReserveReleaseBps: pp.ReserveReleaseBps, WorkSplitBps: pp.WorkSplitBps, AvailSplitBps: pp.AvailSplitBps, WorkGateBps: pp.WorkGateBps}
		epochBlocks = pp.EpochBlocks
		if ep.ReserveReleaseBps == 0 {
			sdkCtx.Logger().Info("emission: reserve_release_bps=0 -> AUCUNE liberation cette epoque (parametre gouverne, pas un defaut)", "height", h)
		}
		if epochBlocks == 0 {
			// Un epoch_blocks nul ferait tomber l'epoque a CHAQUE bloc. Validate() le refuse desormais ;
			// cette garde couvre un etat pose avant ce durcissement.
			sdkCtx.Logger().Error("emission: epoch_blocks=0 en store -> epoque DESACTIVEE (repli sur le defaut interdit : il ferait emettre a chaque bloc)", "height", h)
			return nil
		}
	}

	last, err := k.LastEpoch.Get(ctx)
	if err != nil {
		last = 0 // jamais exécuté
	}
	// `last + epochBlocks` DEBORDE en uint64 pour un epochBlocks proche de 2^64 : la somme repasse sous
	// `h`, la condition devient fausse, et l'epoque tombe a CHAQUE BLOC — la Reserve entiere part en
	// quelques centaines de blocs. Une proposition « ne plus jamais emettre » produisait l'emission
	// maximale. On soustrait au lieu d'additionner : aucune somme, donc aucun debordement possible.
	if h < last {
		// `last` est une hauteur ABSOLUE, restaurée telle quelle par le genesis. Si la chaîne
		// redémarre PLUS BAS que la dernière époque exécutée — un export/import qui repart à 1, ce que
		// `forZeroHeight` fait précisément — alors `h < last` reste vrai pendant des milliers de blocs
		// et l'émission se tait, sans erreur ni journal. Le module `x/jobs` a résolu le même problème
		// en transportant du TEMPS RESTANT plutôt que des hauteurs ; ici la donnée est un point de
		// départ, pas une échéance, donc on ne peut pas la relativiser — mais on peut refuser de
		// rester muet. Une époque en avance sur la chaîne est une incohérence d'import : on la NOMME
		// et on repart de la hauteur courante plutôt que d'attendre des heures en silence.
		sdkCtx.Logger().Error("emission: last_epoch en AVANCE sur la hauteur courante (import d'un etat issu d'une chaine plus haute) -> recalage sur la hauteur courante ; sans ce recalage l'emission resterait muette sans rien dire.",
			"last_epoch", last, "height", h)
		if err := k.LastEpoch.Set(ctx, h); err != nil {
			return err
		}
		return nil
	}
	if h-last < epochBlocks {
		return nil // pas encore l'heure
	}

	reserve, err := k.Reserve.Get(ctx)
	if err != nil {
		reserve = GenesisReserveU // init paresseuse si le genesis ne l'a pas posée
	}
	if reserve == 0 {
		return k.LastEpoch.Set(ctx, h) // Réserve épuisée : rien à libérer
	}

	// DEMANDE non-récupérable = baisse de supply depuis la dernière époque. Les burns de frais (v5,
	// FeeBurnBps) DÉTRUISENT des coins et, la frappe étant nulle, la supply ne fait que baisser -> son
	// recul mesure la demande réelle. Lu DIRECTEMENT via bank (AUCUN câblage cross-module jobs→emission).
	// Gate le flux TRAVAIL à 1,5× cette demande (anti-self-dealing, ADR-017) : pas de demande -> pas de
	// travail émis ; de la demande (des jobs réglés qui brûlent) -> le flux travail s'active.
	curSupply := k.bankKeeper.GetSupply(ctx, "udndr").Amount.Uint64()
	lastSupply, lsErr := k.LastSupply.Get(ctx)
	if lsErr != nil || lastSupply == 0 {
		lastSupply = curSupply // 1re époque (ou non initialisé) -> delta 0
	}
	var demand uint64
	if lastSupply > curSupply {
		demand = lastSupply - curSupply
	}
	r := EpochRelease(reserve, demand, ep)

	// FLUX RÉELS (devnet) — destinations HONNÊTES par flux (le modèle v5 répartit le libéré en
	// work/avail/security) :
	//   - SÉCURITÉ -> fee_collector (=> validateurs/délégateurs via x/distribution). SEUL flux distribué.
	//   - DISPONIBILITÉ + TRAVAIL -> RESTENT au compte de module (compteurs AvailPool/WorkPool), en
	//     attente de leur mécanisme de versement dédié (registre de dispo / paiement au travail). On ne
	//     prétend PAS les verser aux validateurs.
	// INVARIANT vérifiable on-chain : solde(module emission) == Reserve + WorkPool + AvailPool
	// (seule la sécurité sort du module). BEST-EFFORT : compte non financé -> on DIFFÈRE tout (compteurs
	// et reserve inchangés, pas de halt) et on retentera à l'époque suivante.
	if r.Security > 0 {
		sec := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(r.Security)))
		if err := k.bankKeeper.SendCoinsFromModuleToModule(ctx, types.ModuleName, authtypes.FeeCollectorName, sec); err != nil {
			sdkCtx.Logger().Info("EMISSION: compte de module à financer (genesis) -> libération différée",
				"module_account", authtypes.NewModuleAddress(types.ModuleName).String(), "err", err.Error())
			return k.LastEpoch.Set(ctx, h) // avance l'époque ; reserve/pools inchangés -> retry au prochain
		}
	}

	if err := addItem(ctx, k.WorkPool, r.Work); err != nil {
		return err
	}
	if err := addItem(ctx, k.AvailPool, r.Avail); err != nil {
		return err
	}
	if err := addItem(ctx, k.SecurityPool, r.Security); err != nil {
		return err
	}
	if err := k.Reserve.Set(ctx, r.NewReserve); err != nil {
		return err
	}
	if err := k.LastEpoch.Set(ctx, h); err != nil {
		return err
	}
	if err := k.LastSupply.Set(ctx, curSupply); err != nil { // mémorise la supply pour le delta suivant
		return err
	}

	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"emission_epoch",
		sdk.NewAttribute("height", strconv.FormatUint(h, 10)),
		sdk.NewAttribute("released", strconv.FormatUint(r.Released, 10)),
		sdk.NewAttribute("avail", strconv.FormatUint(r.Avail, 10)),
		sdk.NewAttribute("security", strconv.FormatUint(r.Security, 10)),
		sdk.NewAttribute("reserve_left", strconv.FormatUint(r.NewReserve, 10)),
	))
	sdkCtx.Logger().Info("EMISSION Réserve (TK-02) — sécu distribuée, dispo/travail retenus au module",
		"height", h, "released", r.Released, "travail_retenu", r.Work, "avail_retenu", r.Avail, "secu_distribuee", r.Security, "demande", demand, "reserve_left", r.NewReserve)
	return nil
}

// addItem : incrémente un Item[uint64] (0 si non initialisé).
func addItem(ctx context.Context, item collections.Item[uint64], amt uint64) error {
	cur, err := item.Get(ctx)
	if err != nil {
		cur = 0
	}
	return item.Set(ctx, cur+amt)
}
