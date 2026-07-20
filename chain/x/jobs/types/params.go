package types

import (
	"encoding/hex"
	"fmt"

	"cosmossdk.io/math"
)

// NewParams crée une instance Params.
func NewParams(
	minStake, protocolFeeBps, validatorRewardBps, teamFeeBps,
	minerVestBps, minerVestBlocks, slashLeakBps, burnBps, reporterBps,
	epochBlocks, workGateBps, demandClientCap, feeBurnBps uint64,
) Params {
	return Params{
		MinStake:           minStake,
		ProtocolFeeBps:     protocolFeeBps,
		ValidatorRewardBps: validatorRewardBps,
		TeamFeeBps:         teamFeeBps,
		MinerVestBps:       minerVestBps,
		MinerVestBlocks:    minerVestBlocks,
		SlashLeakBps:       slashLeakBps,
		BurnBps:            burnBps,
		ReporterBps:        reporterBps,
		EpochBlocks:        epochBlocks,
		WorkGateBps:        workGateBps,
		DemandClientCap:    demandClientCap,
		FeeBurnBps:         feeBurnBps,
	}
}

// DefaultParams — valeurs v4 (ADR-017/018).
//
// ⚠️ internal audit 2026-07-03 [HIGH], documenté 2026-07-04 (option retenue : DOCUMENTER, pas basculer) :
// le DÉFAUT Go = verification_mode=0 (redondance k=3 cosinus, SANS le veto pro-honnête N=5 — le veto ne garde
// que le mode-1). Quiconque lance la chaîne SANS l'entrypoint docker (qui arme mode-1 + veto + fee-hold +
// silence_slash au genesis) tourne donc en mode-0. Les bornes basses de dissuasion de Validate() gardent les
// DEUX chemins ; le pitch « veto N=5 » ne vaut que pour les genesis ARMÉS (testnet/lancement = entrypoint).
// Ne pas basculer ce défaut sans adapter les ~27 tests keeper v1 qui en dépendent (hold_bps=0 ⇒ v1 strict).
func DefaultParams() Params {
	p := NewParams(
		1000,  // min_stake
		1500,  // protocol_fee_bps (15%)
		5000,  // validator_reward_bps (50% du cut)
		2000,  // team_fee_bps (20% du cut)
		3000,  // miner_vest_bps (30%)
		200,   // miner_vest_blocks
		8000,  // slash_leak_bps (80%)
		3000,  // burn_bps
		3000,  // reporter_bps
		100,   // epoch_blocks
		15000, // work_gate_bps (1.5x)
		100,   // demand_client_cap
		500,   // fee_burn_bps (v5 : burn doux 5%) -- REEL via BurnCoins a la liquidation
	)
	p.AvailRequireDemand = true // V6-02 (audit v6) : anti-farming dispo ON par défaut (sécurité testnet incentivé)
	// ADR-028 v2 — 3 params gouvernables, TOUS DORMANTS (0) : défaut = comportement v1 strictement inchangé.
	// (Affectation explicite à 0 = documentation de la dormance ; la valeur zéro Go est déjà 0, redondance sûre.)
	p.SilenceSlashBps = 0 // 0 = clawback léger seul (v1)
	p.AppealWindow = 0    // 0 = restitution via gouvernance (v1) ; réservé (pas encore consommé)
	p.AuditMinQuorum = 0  // 0 = v1 (plancher auditSlashFloor ⌈N/2⌉+1 + majorité-stake) ; >0 = VETO : BORNE BASSE du quorum. Depuis ADR-032 amendée le seuil EFFECTIF est max(ce param, ⌈2/3 × sièges ANCRÉS⌉) ET la majorité de stake — le comité tiré faisant 15, un seuil absolu de 4 valait 27 % et non la quasi-unanimité annoncée
	p.HoldBps = 0         // PLAN-V2-FEE-HOLD §A : 0 = paiement optimiste intégral immédiat (v1) ; >0 = rétention fenêtrée jusqu'à finalité
	p.CommitteeMinVrfContributors = 0 // Bootstrap VRF (internal audit 2026-06-26) : 0 = pas de plancher (dormant) ; >0 = graine décentralisée à < N contributeurs -> alerte+repli legacy (jamais de halte). Testnet récompensé : ⌈2N/3⌉.
	// ADR-022 PLEIN — disponibilité slashable (défi VRF). TOUS DORMANTS (0) : inertes tant que AvailEpochBlocks>0 ET ces params posés.
	p.AvailThresholdBps = 0   // 0 = pas de vérif sémantique du défi ; >0 = seuil cosinus (aberrant en dessous)
	p.AvailDeadlineBlocks = 0 // 0 = aucun défi exigible ; >0 = blocs accordés pour répondre
	p.AvailSlashBps = 0       // 0 = aucun slash dispo ; >0 = fraction du stake slashée
	p.AvailSlashMax = 0       // 0 = pas de borne ; >0 = plafond absolu (udndr) du slash dispo
	p.AvailFailWindow = 0     // 0 = jamais de quorum ; >0 = fenêtre glissante de comptage des échecs
	p.AvailFailK = 0          // 0 = jamais de slash (anti-faux-positif total) ; >0 = échecs requis dans la fenêtre
	return p
}

// Validate valide les params.
func (p Params) Validate() error {
	if p.ProtocolFeeBps > 10000 {
		return fmt.Errorf("protocol_fee_bps > 10000")
	}
	if p.ValidatorRewardBps+p.TeamFeeBps > 10000 {
		return fmt.Errorf("validator+team > 100%% du cut")
	}
	if p.MinerVestBps > 10000 {
		return fmt.Errorf("miner_vest_bps > 10000")
	}
	if p.AvailPayoutBps > 10000 {
		return fmt.Errorf("avail_payout_bps > 10000")
	}
	if p.CommitteeSeedSource > 1 {
		return fmt.Errorf("committee_seed_source doit être 0 (legacy) ou 1 (vrf décentralisée)")
	}
	if p.VrfBeaconPubkey != "" {
		if b, err := hex.DecodeString(p.VrfBeaconPubkey); err != nil || len(b) != 32 {
			return fmt.Errorf("vrf_beacon_pubkey invalide (attendu 32 octets hex)")
		}
	}
	// ADR-025 (M0) : vérification optimiste, dormant par défaut (0/0).
	if p.VerificationMode > 1 {
		return fmt.Errorf("verification_mode doit être 0 (redundant) ou 1 (optimistic)")
	}
	if p.AuditSampleBps > 10000 {
		return fmt.Errorf("audit_sample_bps > 10000")
	}
	// ADR-028 (anti-évasion) : en mode optimiste, l'audit DOIT pouvoir aboutir à un slash. Sans ces bornes
	// croisées, le slash optimiste est INERTE même activé : (a) sans échantillon, aucun job n'est audité ;
	// (b) si le timeout de résolution n'est pas STRICTEMENT après la fenêtre de dispute, l'auto-résolution
	// (clawback/vindication) se déclenche avant que l'adjudication soit recevable.
	if p.VerificationMode == 1 {
		if p.AuditSampleBps == 0 {
			return fmt.Errorf("verification_mode=1 exige audit_sample_bps > 0 (sinon aucun audit n'est tiré)")
		}
		if p.DisputeWindow == 0 {
			return fmt.Errorf("verification_mode=1 exige dispute_window > 0")
		}
		if p.AuditResolveTimeout <= p.DisputeWindow {
			return fmt.Errorf("verification_mode=1 exige audit_resolve_timeout > dispute_window (sinon le timeout résout avant l'adjudication)")
		}
		// internal audit 2026-07-03 [HIGH] — BORNE BASSE de DISSUASION (pattern ADR-022 « armed => full
		// machinery ») : sans elle, une gouvernance peut poser slash_leak_bps=0 en mode optimiste ARMÉ ->
		// le tricheur pris ne perd RIEN -> triche/self-dealing +EV, et ni Validate ni l'invariant #8
		// (qui garde le wash de SUBVENTION, pas le slash) ne l'attrapent. Plancher STRUCTUREL : 2500
		// (25 % du stake) = « jamais symbolique » ; l'économie fine (-EV exact à audit ~10 %) reste
		// calibrée par ailleurs (valeur expédiée = 8000). Ne touche PAS le mode-0 dormant (défauts intacts).
		if p.SlashLeakBps < 2500 {
			return fmt.Errorf("verification_mode=1 exige slash_leak_bps >= 2500 (dissuasion jamais symbolique ; expédié 8000)")
		}
		if p.AuditSampleBps < 500 {
			return fmt.Errorf("verification_mode=1 exige audit_sample_bps >= 500 (sous 5%% d'audit, la triche redevient +EV même a slash plein)")
		}
		// BORNE BASSE de `audit_min_quorum`. Ce paramètre n'avait AUCUNE borne.
		// Il ne pilote pas que le slash (protégé par le ⌈2/3⌉ des sièges ancrés, ADR-032) : via
		// `effectiveSlashFloor`, il pilote aussi le plancher de VINDICATION. À 1, un juré unique
		// suffisait à libérer la rétention d'un tricheur et à priver le client de son remboursement
		// — une gouvernance pouvait donc désarmer la moitié de la garantie sans la nommer. Plancher
		// = `auditSlashFloor()` côté keeper (⌈AuditCommitteeSize/2⌉+1 = 4) ; valeur expédiée = 4.
		// 0 reste permis : c'est le repli v1 explicite, pas un affaiblissement silencieux.
		if p.AuditMinQuorum > 0 && p.AuditMinQuorum < 4 {
			return fmt.Errorf("verification_mode=1 exige audit_min_quorum >= 4 quand il est gouverné (sous ce plancher, un juré isolé vindique un tricheur ; 0 = repli v1)")
		}
	}
	// ADR-028 v2 — bornes des 3 nouveaux params. TOUTES inactives à la valeur dormante 0 (=> Validate de v1 intact).
	if p.SilenceSlashBps > 10000 {
		return fmt.Errorf("silence_slash_bps > 10000")
	}
	// appeal_window : un appel (révélation tardive) n'a de sens qu'après un timeout de résolution. À 0 (dormant) -> aucune contrainte.
	if p.AppealWindow > 0 && p.AuditResolveTimeout == 0 {
		return fmt.Errorf("appeal_window > 0 exige audit_resolve_timeout > 0")
	}
	// audit_min_quorum : encodage ENTIER (nb de juges frais distincts). 0 = repli sur le plancher v1. Pas de borne
	// haute (un quorum exigé > CommitteeSize rend le slash dur impossible : choix de gouvernance assumé, non invalide).
	// PLAN-V2-FEE-HOLD §A — hold_bps : fraction (bps) du paiement optimiste retenue jusqu'à finalité. Dormant à 0.
	if p.HoldBps > 10000 {
		return fmt.Errorf("hold_bps > 10000")
	}
	// ADR-022 PLEIN — disponibilité slashable. Bornes inactives à la valeur dormante 0 (=> Validate v1 intact).
	if p.AvailThresholdBps > 10000 {
		return fmt.Errorf("avail_threshold_bps > 10000")
	}
	if p.AvailSlashBps > 10000 {
		return fmt.Errorf("avail_slash_bps > 10000")
	}
	if p.AvailFailK > 0 && p.AvailFailWindow < p.AvailFailK {
		return fmt.Errorf("avail_fail_window doit être >= avail_fail_k (sinon le quorum d'échecs est inatteignable)")
	}
	// Cohérence d'ACTIVATION : un slash dispo ARMÉ (avail_slash_bps>0) exige toute la machinerie, sinon il est
	// soit inerte, soit dangereux (faux positifs). Tout à 0 (dormant) -> aucune de ces contraintes ne mord.
	if p.AvailSlashBps > 0 {
		if p.AvailEpochBlocks == 0 {
			return fmt.Errorf("avail_slash_bps > 0 exige avail_epoch_blocks > 0 (sans époque de défi, le slash est inerte)")
		}
		if p.AvailDeadlineBlocks == 0 {
			return fmt.Errorf("avail_slash_bps > 0 exige avail_deadline_blocks > 0 (sans échéance, un défi n'expire jamais -> jamais d'échec)")
		}
		if p.AvailFailK == 0 || p.AvailFailWindow == 0 {
			return fmt.Errorf("avail_slash_bps > 0 exige avail_fail_k > 0 ET avail_fail_window > 0 (anti-faux-positif : un échec isolé ne doit jamais slasher)")
		}
		// SLIDING-WINDOW (lot scaling 2026-07-01) : la fenêtre est un BITMASK 64 bits -> W ≤ 64 à l'armement
		// (au-delà, le keeper clampe silencieusement à 64 = calibration faussée ; on refuse plutôt).
		if p.AvailFailWindow > 64 {
			return fmt.Errorf("avail_fail_window > 64 (fenêtre sliding = bitmask 64 bits ; réduire W ou allonger avail_epoch_blocks)")
		}
	}
	return nil
}

// WashSubsidyNegativeEV — INVARIANT #8 (anti-bulle), internal audit 2026-06-26.
//
// `Demand` (= team+treasury par job, crédité si client != operateur, settle_semantic.go:251) débloque une
// subvention de WorkPool plafonnée à `Demand × WorkGateBps/10000` (x/emission, emission.go:54). Le compteur ne
// distingue PAS un acheteur EXTERNE d'un Sybil self-dealer (problème de l'oracle, INSOLUBLE on-chain : deux
// adresses sont indiscernables) -> `r_settlement` reste un PROXY de volume, jamais une preuve de traction.
// Le seul risque RÉEL n'est donc PAS la vanité du dashboard mais un DRAIN d'émission — qui n'existe QUE si le
// wash devient +EV. Or par job washé l'opérateur PERD `cut + burn` pour ne débloquer que
// `WorkGateBps × (team+treasury)`. Tant que (le facteur fee s'annule) :
//
//	WorkGateBps × (team+treasury)  <  (cut + burn)
//
// le wash est strictement -EV -> pas de drain. C'est PARAM-DÉPENDANT (une hausse gouvernance de work_gate_bps,
// ou un glissement du split cut->team/treasury, pourrait casser le -EV SILENCIEUSEMENT) -> on le PIN par un
// test (params_invariant_test.go). C'est LE fix R v2 (axe économique), pas un anti-Sybil on-chain (impossible).
//
// Tout en bps de la fee F (qui s'annule) — cut=ProtocolFeeBps ; team+treasury=cut−validators ; burn=FeeBurnBps :
//
//	-EV  <=>  WorkGateBps × ProtocolFeeBps × (10000 − ValidatorRewardBps)  <  (ProtocolFeeBps + FeeBurnBps) × 10000²
//
// `workGateBps` = le MÊME gate que x/emission applique (passé en argument pour éviter tout cycle d'import).
func (p Params) WashSubsidyNegativeEV(workGateBps uint64) bool {
	vr := p.ValidatorRewardBps
	if vr > 10000 {
		vr = 10000 // borné par Validate (validator+team ≤ 10000) ; clamp défensif anti-underflow
	}
	// math.Int : overflow-proof même si la gouvernance pose un work_gate_bps absurde.
	lhs := math.NewIntFromUint64(workGateBps).
		Mul(math.NewIntFromUint64(p.ProtocolFeeBps)).
		Mul(math.NewIntFromUint64(10000 - vr)) // subvention débloquée (échelle 10000³·F)
	if lhs.IsZero() {
		return true // aucun subside débloqué (work_gate=0 / pas de cut) -> aucun drain possible : params sûrs
	}
	rhs := math.NewIntFromUint64(p.ProtocolFeeBps + p.FeeBurnBps).
		Mul(math.NewIntFromUint64(100000000)) // coût cut+burn (même échelle 10000²·F)
	return lhs.LT(rhs)
}
