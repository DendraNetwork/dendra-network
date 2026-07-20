package types_test

import (
	"testing"

	"dendra/x/jobs/types"
)

// ADR-022 PLEIN — disponibilité slashable (défi VRF-échantillonné). Ces tests FIGENT la dormance + les bornes
// de cohérence d'activation. Tant que les params avail_* sont à 0, Validate() est STRICTEMENT inchangé (v1).

// armedAvail renvoie des DefaultParams avec la machinerie de disponibilité COMPLÈTE et cohérente (slash armé).
func armedAvail() types.Params {
	p := types.DefaultParams()
	p.AvailEpochBlocks = 300   // époque de défi
	p.AvailDeadlineBlocks = 50 // échéance de réponse
	p.AvailThresholdBps = 7000 // seuil cosinus (aberrant en dessous)
	p.AvailSlashBps = 500      // 5 % du stake
	p.AvailSlashMax = 1000000  // plafond absolu (udndr)
	p.AvailFailWindow = 10     // fenêtre glissante
	p.AvailFailK = 3           // 3 échecs / 10 avant slash (anti-faux-positif)
	return p
}

// TestAvail_DormantByDefault — params expédiés : disponibilité slashable OFF -> Validate inchangé.
func TestAvail_DormantByDefault(t *testing.T) {
	p := types.DefaultParams()
	if p.AvailSlashBps != 0 || p.AvailFailK != 0 || p.AvailDeadlineBlocks != 0 ||
		p.AvailThresholdBps != 0 || p.AvailFailWindow != 0 || p.AvailSlashMax != 0 {
		t.Fatalf("ADR-022 doit être DORMANT par défaut (tous les avail_* à 0)")
	}
	if err := p.Validate(); err != nil {
		t.Fatalf("DefaultParams (dispo dormante) doit valider : %v", err)
	}
}

// TestAvail_ArmedComplete — la machinerie complète et cohérente valide.
func TestAvail_ArmedComplete(t *testing.T) {
	if err := armedAvail().Validate(); err != nil {
		t.Fatalf("config dispo armée COMPLÈTE doit valider : %v", err)
	}
}

// TestAvail_ArmedIncompleteRejected — un slash ARMÉ sans sa machinerie est REJETÉ (inerte ou dangereux).
func TestAvail_ArmedIncompleteRejected(t *testing.T) {
	cases := []struct {
		name  string
		mutate func(p *types.Params)
	}{
		{"sans_epoch", func(p *types.Params) { p.AvailEpochBlocks = 0 }},
		{"sans_deadline", func(p *types.Params) { p.AvailDeadlineBlocks = 0 }},
		{"sans_fail_k", func(p *types.Params) { p.AvailFailK = 0 }},
		{"sans_fail_window", func(p *types.Params) { p.AvailFailWindow = 0 }},
	}
	for _, c := range cases {
		p := armedAvail()
		c.mutate(&p)
		if err := p.Validate(); err == nil {
			t.Fatalf("[%s] slash dispo armé sans sa machinerie DOIT être rejeté (anti-inertie/faux-positif)", c.name)
		}
	}
}

// TestAvail_WindowGEFailK — la fenêtre doit pouvoir contenir le quorum d'échecs.
func TestAvail_WindowGEFailK(t *testing.T) {
	p := armedAvail()
	p.AvailFailWindow = 2
	p.AvailFailK = 3 // 3 échecs requis dans une fenêtre de 2 = inatteignable
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_fail_window < avail_fail_k doit être rejeté (quorum inatteignable)")
	}
}

// TestAvail_WindowLE64 — SLIDING-WINDOW (lot scaling 2026-07-01) : la fenêtre est un bitmask 64 bits ->
// W > 64 armé est REJETÉ (sinon clamp silencieux = calibration faussée). Dormant (slash=0) : W>64 toléré.
func TestAvail_WindowLE64(t *testing.T) {
	p := armedAvail()
	p.AvailFailWindow = 65
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_fail_window > 64 armé doit être rejeté (capacité du bitmask sliding)")
	}
	p = armedAvail()
	p.AvailFailWindow = 64 // borne exacte = acceptée
	if err := p.Validate(); err != nil {
		t.Fatalf("avail_fail_window = 64 doit valider : %v", err)
	}
	p = types.DefaultParams() // dormant : aucune contrainte ne mord
	p.AvailFailWindow = 300
	if err := p.Validate(); err != nil {
		t.Fatalf("W>64 DORMANT (slash=0) ne doit pas être rejeté : %v", err)
	}
}

// TestAvail_BpsBounds — les bps restent bornés à 10000.
func TestAvail_BpsBounds(t *testing.T) {
	p := armedAvail()
	p.AvailThresholdBps = 10001
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_threshold_bps > 10000 doit être rejeté")
	}
	p = armedAvail()
	p.AvailSlashBps = 10001
	if err := p.Validate(); err == nil {
		t.Fatalf("avail_slash_bps > 10000 doit être rejeté")
	}
}
