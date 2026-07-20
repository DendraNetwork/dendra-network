package types_test

import (
	"testing"

	"dendra/x/jobs/types"
)

// TestInvariant8_SybilWashNegativeEV — pin du fix R v2 (internal audit 2026-06-26).
//
// `Demand` (crédité si client != operateur) ne mesure PAS la traction externe (oracle insoluble on-chain) ->
// `r_settlement` est un PROXY. Le seul risque RÉEL est un DRAIN d'émission, qui n'existe QUE si le self-dealing
// Sybil devient +EV. Aux params EXPÉDIÉS il est -EV (l'opérateur perd cut+burn pour débloquer
// WorkGate×(team+treasury)). Ce test FIGE ce -EV : si une hausse gouvernance de work_gate_bps OU un glissement
// du split cut->team/treasury cassait l'inégalité, le wash deviendrait un vrai drain -> ce test DOIT échouer.
func TestInvariant8_SybilWashNegativeEV(t *testing.T) {
	p := types.DefaultParams()
	wg := p.WorkGateBps // 15000 = le gate du subside par-mineur que le wash exploite (msg_server_claim_subsidy.go:39)

	if !p.WashSubsidyNegativeEV(wg) {
		t.Fatalf("INVARIANT #8 CASSÉ : self-dealing Sybil +EV aux params expédiés "+
			"(ProtocolFee=%d ValidatorReward=%d FeeBurn=%d WorkGate=%d) -> DRAIN d'émission possible",
			p.ProtocolFeeBps, p.ValidatorRewardBps, p.FeeBurnBps, wg)
	}

	// Sanity sur l'ordre de grandeur audité (internal audit) : subvention 1125 bps de fee < coût 2000 bps, marge ~1,78x.
	// team+treasury = ProtocolFee×(10000−ValidatorReward)/10000 = 1500×5000/10000 = 750 bps ;
	// subvention = WorkGate/10000 × 750 = 1,5 × 750 = 1125 bps ; coût = ProtocolFee+FeeBurn = 2000 bps.
	if p.ProtocolFeeBps == 1500 && p.ValidatorRewardBps == 5000 && p.FeeBurnBps == 500 && wg == 15000 {
		tt := p.ProtocolFeeBps * (10000 - p.ValidatorRewardBps) / 10000 // 750
		sub := wg * tt / 10000                                          // 1125 (multiplier AVANT de diviser)
		cost := p.ProtocolFeeBps + p.FeeBurnBps                         // 2000
		if tt != 750 || cost != 2000 || sub != 1125 || sub >= cost {
			t.Fatalf("sanity rompue : team+treasury=%d sub=%d cost=%d (attendu 750 / 1125 / 2000)", tt, sub, cost)
		}
	}
}

// TestInvariant8_GuardDiscriminates — le helper DISCRIMINE réellement : un work_gate_bps assez haut rend le wash
// +EV et le garde DOIT renvoyer false. Seuil de bascule : subvention >= coût <=> WorkGate/10000 × 750 >= 2000
// <=> WorkGate >= 26666,67 -> 26667 est +EV (false), 26666 reste -EV (true).
func TestInvariant8_GuardDiscriminates(t *testing.T) {
	p := types.DefaultParams()
	if p.WashSubsidyNegativeEV(26667) {
		t.Fatalf("le garde n'a pas mordu : WorkGate=26667 devrait rendre le wash +EV (donc NON -EV)")
	}
	if !p.WashSubsidyNegativeEV(26666) {
		t.Fatalf("faux positif du garde : WorkGate=26666 devrait rester -EV")
	}
}

// TestInvariant8_GenesisRejectsPlusEV — résidu #2 (internal audit 2026-06-26) : la garde -EV ne vit pas QUE dans
// MsgUpdateParams. GenesisState.Validate() (donc `validate-genesis`) refuse aussi un genesis dont les params
// rendraient le wash +EV -> un Params.Set au chargement du genesis ne peut pas contourner l'invariant.
func TestInvariant8_GenesisRejectsPlusEV(t *testing.T) {
	gs := types.DefaultGenesis()
	if err := gs.Validate(); err != nil {
		t.Fatalf("genesis par defaut (-EV) rejete a tort : %v", err)
	}
	gs.Params.WorkGateBps = 26667 // rend le wash +EV au split par défaut
	if err := gs.Validate(); err == nil {
		t.Fatal("genesis +EV (work_gate=26667) DOIT etre rejete par l'invariant #8")
	}
}

// TestValidate_Mode1FloorsDissuasion — internal audit 2026-07-03 [HIGH] : en mode optimiste ARMÉ, une
// gouvernance ne peut pas ZÉRO-ER la dissuasion (slash_leak_bps=0 => slash gratuit => triche +EV, non
// couvert par l'invariant #8 qui garde la SUBVENTION). Bornes basses ACTIVES en mode-1 seulement :
// slash_leak_bps >= 2500, audit_sample_bps >= 500. Mode-0 dormant : planchers INACTIFS (v1 intact).
func TestValidate_Mode1FloorsDissuasion(t *testing.T) {
	p := types.DefaultParams() // mode-0 : les planchers ne s'appliquent pas
	if err := p.Validate(); err != nil {
		t.Fatalf("defauts (mode-0) rejetes a tort : %v", err)
	}
	// armer le mode optimiste proprement (bornes N2 : timeout STRICTEMENT > fenetre de dispute)
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-1 aux valeurs expediees (slash_leak=8000) rejete a tort : %v", err)
	}
	p.SlashLeakBps = 0
	if err := p.Validate(); err == nil {
		t.Fatal("mode-1 + slash_leak_bps=0 DOIT etre rejete (dissuasion zero-ee par gov)")
	}
	p.SlashLeakBps = 2499
	if err := p.Validate(); err == nil {
		t.Fatal("mode-1 + slash_leak_bps=2499 DOIT etre rejete (plancher 2500)")
	}
	p.SlashLeakBps = 2500
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-1 + slash_leak_bps=2500 (plancher exact) rejete a tort : %v", err)
	}
	p.AuditSampleBps = 499
	if err := p.Validate(); err == nil {
		t.Fatal("mode-1 + audit_sample_bps=499 DOIT etre rejete (plancher 500)")
	}
	p.AuditSampleBps = 500
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-1 + audit_sample_bps=500 rejete a tort : %v", err)
	}
	// retour mode-0 : dormance v1 STRICTEMENT intacte (slash_leak=0 redevient valide)
	p.VerificationMode = 0
	p.SlashLeakBps = 0
	if err := p.Validate(); err != nil {
		t.Fatalf("mode-0 + slash_leak=0 doit rester VALIDE (dormant) : %v", err)
	}
}
