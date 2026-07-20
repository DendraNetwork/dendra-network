package types

import (
	"fmt"
	"strings"
)

// DefaultAuditJudgeModel — modèle-juge canonique par défaut (ADR-027 D4 / ADR-026).
// internal audit verdict 2026-06-22 : juge d'audit = MoE Qwen3-30B-A3B sur CPU (gate ADR-025 §3 STRICT PASS, salade/faux 0 % ;
// artefact services/bench-results/judge-moe30b-cpu-2026-06-22.json). Le comité d'audit DOIT tourner CE modèle
// (cohérence des verdicts) ; fallback opérationnel qwen3:4b-instruct-2507 < 26 Go RAM, toléré par le veto N=5 pro-honnête.
// Alignement off-chain (judge.py::DEFAULT_JUDGE_MODEL) + backend juge CPU = MÊME incrément régen v2.
// DORMANT (consommé seulement en verification_mode=1) ; mise à jour = gouvernance. Validation hors-distribution = testnet P1.1.
const DefaultAuditJudgeModel = "qwen3:30b-a3b-instruct-2507-q4_K_M"

// NewParams creates a new Params instance.
func NewParams(auditJudgeModel string) Params {
	return Params{
		AuditJudgeModel: auditJudgeModel,
	}
}

// DefaultParams returns a default set of parameters.
func DefaultParams() Params {
	return NewParams(DefaultAuditJudgeModel)
}

// Validate validates the set of params.
func (p Params) Validate() error {
	if err := validateAuditJudgeModel(p.AuditJudgeModel); err != nil {
		return err
	}

	return nil
}

// validateAuditJudgeModel — accepte le VIDE (dormant : aucun enforcement du juge) ou un
// identifiant non vide et sans espace interne. On NE vérifie PAS ici que le modèle est déjà
// enregistré/actif dans le registre : l'ordre d'init du genesis ne le garantit pas. Cette
// vérification « le juge est un Model actif » se fait côté keeper au moment de l'audit.
func validateAuditJudgeModel(id string) error {
	if id == "" {
		return nil // dormant
	}
	if strings.TrimSpace(id) != id || strings.ContainsAny(id, " \t\r\n") {
		return fmt.Errorf("audit_judge_model must not contain whitespace: %q", id)
	}
	return nil
}
