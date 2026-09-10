package types

import (
	"fmt"
	"strings"
)

// DefaultAuditJudgeModel — the canonical default judge model (ADR-026 / ADR-027).
// The audit judge is the Qwen3-30B-A3B MoE running on CPU; it clears the ADR-025 §3 gate with a strict
// pass and 0 % on both the nonsense and the forged-answer sets. The audit committee MUST run THIS
// model, otherwise verdicts are not comparable across jurors. An operational fallback to
// qwen3:4b-instruct-2507 exists for hosts under 26 GB of RAM, tolerated because the audit's
// pro-honest bar absorbs one weak juror: the bar is RELATIVE — ⌈2/3⌉ of the anchored seats
// (ADR-032 draws 15) — so if the draw changes, the bar follows it, not any fixed count.
// The off-chain side must stay aligned (judge.py::DEFAULT_JUDGE_MODEL) along with the judge's CPU
// backend. DORMANT: consumed only in verification_mode=1; updating it is a governance act.
// Out-of-distribution validation is a testnet task.
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

// validateAuditJudgeModel accepts either the EMPTY string — dormant, no judge is enforced — or a
// non-empty identifier with no internal whitespace. It deliberately does NOT check that the model is
// already registered and active: genesis initialization order does not guarantee it. The stronger
// check, "the judge is an active Model", is made in the keeper at audit time.
func validateAuditJudgeModel(id string) error {
	if id == "" {
		return nil // dormant
	}
	if strings.TrimSpace(id) != id || strings.ContainsAny(id, " \t\r\n") {
		return fmt.Errorf("audit_judge_model must not contain whitespace: %q", id)
	}
	return nil
}
