package keeper

import (
	"context"
	"encoding/hex"
	"math/bits"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"dendra/x/jobs/types"
)

// runAvailabilityEpoch -- Phase 1b. Appelé à chaque bloc par EndBlock. Si la disponibilité est activée
// (avail_epoch_blocks>0) ET qu'on est sur une frontière d'époque, on (1) VERSE l'AvailPool aux mineurs
// prouvés présents à l'époque précédente (pondéré par le bond) puis (2) ROULE le défi de la nouvelle
// époque depuis l'AppHash (imprévisible à l'époque écoulée). OFF par défaut -> e2e intacte.
func (k Keeper) runAvailabilityEpoch(ctx context.Context, h int64) error {
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil // pas de params -> rien
	}
	eb := int64(params.AvailEpochBlocks)
	if eb <= 0 || h <= 0 || h%eb != 0 {
		return nil // disponibilité OFF, ou pas une frontière d'époque
	}
	epoch := h / eb

	// (1) ADR-022 PLEIN (liveness slashable) : slasher les mineurs BONDÉS qui n'ont PAS prouvé leur disponibilité
	//     à l'époque précédente, AVANT que payAvailability ne purge les présences. Dormant si avail_slash_bps==0.
	if epoch >= 1 {
		if err := k.runAvailabilitySlash(ctx, epoch-1, params); err != nil {
			return err
		}
		if err := k.payAvailability(ctx, epoch-1, params.AvailPayoutBps, params.AvailRequireDemand, params.VerificationMode == 1); err != nil {
			return err
		}
	}

	// (2) rouler le défi de la nouvelle époque = AppHash de ce bloc (imprévisible à l'époque écoulée)
	challenge := hex.EncodeToString(sdk.UnwrapSDKContext(ctx).HeaderInfo().AppHash)
	if challenge == "" {
		challenge = "genesis" // repli si AppHash vide (tout début de chaîne)
	}
	return k.AvailChallenge.Set(ctx, challenge)
}

// payAvailability répartit une FRACTION (payoutBps) de l'AvailPool entre les mineurs prouvés présents à
// `epoch`, PONDÉRÉE PAR LE BOND (anti-sybil GO-04 : splitter son stake ne multiplie pas le revenu). Le
// calcul de part est en big.Int (anti-overflow). Le reste (division entière) reste dans le pool. Les
// présences de l'époque sont purgées qu'on ait payé ou non.
func (k Keeper) payAvailability(ctx context.Context, epoch int64, payoutBps uint64, requireDemand bool, requireVrf bool) error {
	type present struct {
		operator string
		stake    uint64
	}
	var miners []present
	var allKeys []collections.Pair[int64, string]
	var totalStake uint64
	rng := collections.NewPrefixedPairRange[int64, string](epoch)
	if err := k.Available.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		allKeys = append(allKeys, key)
		if m, e := k.Miner.Get(ctx, key.K2()); e == nil {
			// NEW-AV-05 (audit v5) : si avail_require_demand, seuls les mineurs ayant servi de la
			// DEMANDE réelle (demand>0) sont rémunérés -> la liveness seule (echo de l'AppHash,
			// farmable SANS GPU) ne rapporte rien. La présence est tout de même purgée plus bas.
			if requireDemand && m.Demand == 0 {
				return false, nil
			}
			// ADR-022 (défense au versement) : pas de vrf_pubkey en mode incentivé -> aucune part (même si une
			// présence legacy traînait). Le gate primaire est dans ProveAvailability ; ceci est ceinture+bretelles.
			if requireVrf && m.VrfPubkey == "" {
				return false, nil
			}
			miners = append(miners, present{operator: m.Operator, stake: m.Stake})
			totalStake += m.Stake
		}
		return false, nil
	}); err != nil {
		return err
	}

	// budget = fraction de l'AvailPool courant, réparti au prorata du bond
	if payoutBps > 0 && totalStake > 0 {
		poolBal, err := k.emissionKeeper.AvailPoolBalance(ctx)
		if err != nil {
			return err
		}
		// NEW-AV-03 (audit v5) : budget en math.Int avec GARDE EXPLICITE avant toute conversion uint64
		// (jamais négatif ; borné par poolBal car payoutBps<=10000). On répartit directement en big.Int.
		budgetI := math.NewIntFromUint64(poolBal).Mul(math.NewIntFromUint64(payoutBps)).QuoRaw(10000)
		if !budgetI.IsNegative() && budgetI.IsPositive() {
			totalI := math.NewIntFromUint64(totalStake)
			for _, p := range miners {
				shareI := budgetI.Mul(math.NewIntFromUint64(p.stake)).Quo(totalI)
				if !shareI.IsPositive() || !shareI.IsUint64() {
					continue
				}
				share := shareI.Uint64()
				opBz, e := k.addressCodec.StringToBytes(p.operator)
				if e != nil {
					continue
				}
				if _, e := k.emissionKeeper.PayAvail(ctx, sdk.AccAddress(opBz), share); e != nil {
					return e
				}
			}
		}
	}

	// purge des présences de l'époque (payée ou non)
	for _, key := range allKeys {
		if err := k.Available.Remove(ctx, key); err != nil {
			return err
		}
	}
	return nil
}

// runAvailabilitySlash -- ADR-022 PLEIN (internal audit 2026-06-27 : LIVENESS SLASHABLE + ALL-BONDED + BURN ; PAS de
// vérif sémantique solo — une réf publique serait pré-calculable, ne prouverait pas le GPU ; la qualité reste
// gardée par l'audit TRAVAIL + AvailRequireDemand). À la frontière d'époque, pour CHAQUE mineur bondé, un ABSENT
// des présences de l'époque écoulée (= n'a pas répondu À TEMPS à la preuve VRF sur le défi imprévisible) compte un
// ÉCHEC.
//
// SLIDING-WINDOW BITMASK (lot scaling 2026-07-01, remplace le tumbling v1 AVANT tout armement live) :
// par mineur, un BITMASK 64 bits des dernières époques (bit0 = époque courante, bit i = il y a i époques),
// décalé à gauche à chaque époque traitée. Slash si POPCOUNT(mask ∩ fenêtre W) ≥ avail_fail_k — c.-à-d. ≥ k
// absences dans N'IMPORTE QUEL span de W époques (exact), là où le tumbling ne comptait que dans des fenêtres
// FIXES : un absent chronique pouvait caler (k-1) absences avant la frontière + (k-1) après SANS slash (gaming
// du reset), et tenir (k-1)/W d'absence à jamais avec du timing. Le sliding l'élimine (aucune frontière à jouer).
// Stockage : réutilise AvailFailCount (= bitmask) + AvailFailWindowStart (= dernière époque traitée) — mêmes
// types uint64, AUCUNE régén proto ; sémantique changée tant que le slash est DORMANT partout (aucun état v1
// armé à migrer). CONTRAINTE : avail_fail_window ≤ 64 à l'armement (capacité du mask, borné par Validate).
// Slash borné min(stake·avail_slash_bps/10000, avail_slash_max), **BURN** (aucune victime ici → dissuasion pure
// déflationniste, pas de bénéficiaire qui inciterait à sur-slasher), mask reset après slash (grâce de k époques).
// Anti-faux-positif = avail_fail_k>1 (un échec isolé réseau/latence ne slashe jamais). ENTIÈREMENT DORMANT tant
// que avail_slash_bps==0 (Validate exige la machinerie complète à l'activation). L'époque 0 (bootstrap, aucun
// défi encore roulé) est exemptée. ⚠️ Recalibrage k/W OBLIGATOIRE vs tumbling (le FP par époque évaluée change) :
// cf. tokenomics/avail_slash_calibration_sliding.py + artefact bench-results/avail-slash-calibration-sliding.json.
//
// HONNÊTETÉS (red-team 2026-07-02) : (a) la dissuasion est ÉCONOMIQUE, pas absolue — un adversaire qui ENCAISSE
// des slashes récurrents (5 % + absence non payée) peut soutenir une densité > (k-1)/W ; (b) un mineur dé-bondé
// n'a PAS d'obligation (ses époques hors-bond glissent en zéros — même vieillissement que la présence, aucun
// raccourci de blanchiment vs être présent) ; (c) la ROTATION de minerId remet le mask à zéro — friction
// existante (Demand reset + probation M6), le lier à l'opérateur = arbitrage internal audit (multi-GPU légitime).
func (k Keeper) runAvailabilitySlash(ctx context.Context, epoch int64, params types.Params) error {
	if params.AvailSlashBps == 0 || params.AvailFailK == 0 || params.AvailFailWindow == 0 {
		return nil // dormant
	}
	if epoch < 1 {
		return nil // époque 0 = bootstrap (aucun défi roulé avant la 1ʳᵉ frontière) → personne n'a pu prouver
	}

	// présences prouvées (à temps) à l'époque écoulée
	present := make(map[string]struct{})
	if err := k.Available.Walk(ctx, collections.NewPrefixedPairRange[int64, string](epoch),
		func(key collections.Pair[int64, string]) (bool, error) {
			present[key.K2()] = struct{}{}
			return false, nil
		}); err != nil {
		return err
	}

	e := uint64(epoch) // époque traitée (≥1)
	w := params.AvailFailWindow
	var winMask uint64
	if w >= 64 {
		w = 64 // capacité du bitmask (Validate borne l'armement à ≤64 ; clamp défensif)
		winMask = ^uint64(0)
	} else {
		winMask = uint64(1)<<w - 1
	}
	var toSlash []string

	// 1er passage : MAJ des bitmasks (sliding window). On NE mute PAS Miner pendant l'itération.
	if err := k.Miner.Walk(ctx, nil, func(minerId string, m types.Miner) (bool, error) {
		if m.Stake == 0 || m.Stake < params.MinStake {
			return false, nil // pas bondé → pas d'obligation de disponibilité (les époques sautées glissent en zéros)
		}
		var mask uint64
		last, errL := k.AvailFailWindowStart.Get(ctx, minerId)
		if errL == nil && last < e {
			gap := e - last // ≥1 ; >1 si le mineur était dé-bondé (époques sans obligation = zéros)
			if prev, errC := k.AvailFailCount.Get(ctx, minerId); errC == nil && gap < 64 {
				mask = prev << gap
			}
		}
		// errL != nil (1ʳᵉ fois) ou last >= e (ne devrait pas arriver : époques strictement croissantes) -> mask=0
		if _, proved := present[minerId]; !proved {
			mask |= 1 // absence à l'époque e
		}
		mask &= winMask // seules les W dernières époques comptent (les plus anciennes expirent en glissant)
		if uint64(bits.OnesCount64(mask)) >= params.AvailFailK {
			toSlash = append(toSlash, minerId)
			mask = 0 // reset après slash : grâce de k époques avant tout nouveau slash (borne la cadence)
		}
		// Anti-churn (audit 2026-07-02) : un mineur NET (mask=0) ne stocke RIEN — on PURGE sa trace au lieu
		// de la réécrire à chaque époque (l'absence d'entrée ≡ mask 0, cf. errL plus haut). L'honnête
		// majoritaire ne coûte AUCUN write d'état par époque ; seuls les mineurs à absences récentes en ont.
		if mask == 0 {
			if errL == nil { // une trace existait -> purge
				if err := k.AvailFailCount.Remove(ctx, minerId); err != nil {
					return true, err
				}
				if err := k.AvailFailWindowStart.Remove(ctx, minerId); err != nil {
					return true, err
				}
			}
			return false, nil
		}
		if err := k.AvailFailCount.Set(ctx, minerId, mask); err != nil {
			return true, err
		}
		if err := k.AvailFailWindowStart.Set(ctx, minerId, e); err != nil {
			return true, err
		}
		return false, nil
	}); err != nil {
		return err
	}

	// 2e passage : appliquer les slashes (mute Miner + BURN) APRÈS le walk
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	for _, minerId := range toSlash {
		m, err := k.Miner.Get(ctx, minerId)
		if err != nil {
			continue // mineur disparu entre-temps
		}
		amt := m.Stake * params.AvailSlashBps / 10000
		if mx := params.AvailSlashMax; mx > 0 && amt > mx {
			amt = mx
		}
		if amt == 0 {
			continue
		}
		m.Stake -= amt
		if err := k.Miner.Set(ctx, minerId, m); err != nil {
			return err
		}
		// BURN depuis le module (custody du bond) → books équilibrés (Σ stakes == solde module), déflationniste
		if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName,
			sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(amt)))); err != nil {
			return err
		}
		sdkCtx.Logger().Info("ADR-022 dispo: mineur slashe (liveness non tenue) + brule",
			"miner", minerId, "amount", amt, "epoch", epoch)
	}
	return nil
}
