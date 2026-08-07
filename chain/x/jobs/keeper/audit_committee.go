package keeper

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"

	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-032 — ANCHORED AUDIT COMMITTEE.
//
// The hole this file closes was critical: the verdict tally authenticated the voting set only by "is a
// registered miner". But `CreateMiner` is permissionless and the bond is refunded on exit, so four
// identities at `min_stake` (0.2 DNDR) were enough to slash 80 % of an HONEST miner's stake for the price
// of gas. The anti-false-slash guard — the N=5 veto — had become the VECTOR of the false slash.
//
// The principle violated: an action that costs nothing and risks nothing is not a security primitive. The
// fix does not harden the decision rule; it authenticates the VOTING SET. Voting requires having been
// DRAWN, and the draw is WEIGHTED BY STAKE. An attack by multiplying identities becomes an attack by
// capital, correctly priced.

// auditCommitteeDomain — domain separation for the audit-committee draw. Distinct from `auditDomain` (the
// yes/no draw deciding whether a job is audited) and from the assignment seed: two draws over the same
// block seed must never be correlated.
const auditCommitteeDomain = "dendra/audit-committee/v1|"

// auditCommitteeDrawSize — number of juror members drawn and anchored per audited job.
//
// DELIBERATE OVERSAMPLING (ADR-032 §e). Drawing exactly `audit_min_quorum` members would make the quorum
// unreachable in practice: only miners actually running a `judge_worker` answer, and nothing on-chain
// distinguishes them. The draw is therefore wide (15) while the quorum stays unchanged: the anti-sybil
// bound holds — one must be drawn, and the draw is stake-weighted — while participation becomes
// reachable again.
//
// DELIBERATE DEVIATION FROM THE SPEC: the ADR asked for a governed param `audit_committee_draw_size`.
// `Params` is a PROTO message (`params.pb.go`), so adding a field would require proto regeneration, which
// the same ADR forbids for storage. This stays a Go constant until the next regeneration window, as
// `CommitteeSize` (committee.go) already does. The security property does not depend on this value being
// governable; only liveness does.
const auditCommitteeDrawSize = 15

// drawAuditMembers — the PURE core of the draw (testable without a keeper), deterministic and
// FLOAT-FREE.
//
// It deliberately reuses the proven scoring of `selectCommittee`: score = H(seed|id) / stake, compared by
// integer CROSS-PRODUCT, hence identical on every validator. The `k` SMALLEST scores are kept: the larger
// the stake, the smaller the score, the more often the miner is drawn. Splitting a stake across N
// identities therefore does NOT change the expected number of seats — exactly the anti-sybil property
// that was missing.
//
// Returns a SORTED list (score order): the anchor must be reproducible byte for byte, otherwise two
// validators would write different strings for the same draw and the chain would fork.
func drawAuditMembers(seed, jobId string, cands []minerWeight, k int) []string {
	return drawMembersWithDomain(auditCommitteeDomain, seed, jobId, cands, k)
}

// drawMembersWithDomain — the same draw with a parameterised DOMAIN. Extracted for ADR-033 (the
// RE-ADJUDICATION committee): two committees drawn for the same job over the same seed must be
// decorrelated, otherwise knowing one gives the other. The domain is the only separator.
func drawMembersWithDomain(domain, seed, jobId string, cands []minerWeight, k int) []string {
	if k <= 0 || len(cands) == 0 {
		return nil
	}
	h := sha256.Sum256([]byte(domain + seed + "|" + jobId))
	drawSeed := hex.EncodeToString(h[:])

	// selectCommittee returns a SET (map order, non-deterministic on iteration) and an ordered LIST is
	// needed here, so the same sort is replayed and then truncated.
	type scored struct {
		id    string
		h     [32]byte
		stake uint64
	}
	all := make([]scored, 0, len(cands))
	for _, m := range cands {
		all = append(all, scored{id: m.id, h: sha256.Sum256([]byte(drawSeed + "|" + m.id)), stake: m.stake})
	}
	sort.Slice(all, func(i, j int) bool {
		return lessByStakeScore(all[i].h, all[i].stake, all[i].id, all[j].h, all[j].stake, all[j].id)
	})

	if k > len(all) {
		k = len(all) // fewer eligible miners than seats -> take everyone (ADR-032 §b)
	}
	out := make([]string, 0, k)
	for i := 0; i < k; i++ {
		out = append(out, all[i].id)
	}
	return out
}

// drawAuditCommittee — draws and returns the members entitled to judge `jobId`.
// Eligible: every registered miner EXCEPT the primary (it does not judge itself) and EXCEPT the disputer
// when the disputer is itself a miner (it does not judge its own accusation).
func (k Keeper) drawAuditCommittee(ctx context.Context, seed, jobId, primaryId, disputerId string) ([]string, error) {
	h := uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())
	// ADR-037 — FROZEN POOL. The vitality guard below does NOT close this hole, deliberately:
	// `eligibleAsJuror` explicitly admits a BRAND-NEW miner, because vitality must never become an
	// ENTRY filter (miner_vitality.go) — otherwise a fresh node that has never judged would be
	// ineligible for life. The freeze therefore works on a DIFFERENT dimension: the REGISTRATION height
	// compared against the job's anchor, never activity. See pool_freeze.go.
	freeze, fErr := k.jobPoolFreezeHeight(ctx, jobId)
	if fErr != nil {
		return nil, fErr
	}
	var cands []minerWeight
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if key == primaryId || (disputerId != "" && key == disputerId) {
			return false, nil
		}
		if !k.minerInFrozenPool(ctx, jobId, key, freeze) {
			return false, nil
		}
		if !k.eligibleAsJuror(ctx, key, h) {
			return false, nil
		}
		cands = append(cands, minerWeight{id: key, stake: m.Stake})
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	size := auditCommitteeDrawSize
	// Safety net: a committee smaller than the quorum would make the slash mathematically inert. No
	// configuration is allowed to produce a guard that can never bite.
	if p, pErr := k.Params.Get(ctx); pErr == nil && p.AuditMinQuorum > asUint64(size) {
		size = int(p.AuditMinQuorum)
	}
	return drawAuditMembers(seed, jobId, cands, size), nil
}

// asUint64 — comparison adapter (AuditMinQuorum is a uint64, the draw size is an int).
func asUint64(n int) uint64 {
	if n < 0 {
		return 0
	}
	return uint64(n)
}

// auditCommitteeAllowed — reads the ANCHORED list and returns it as a set.
// `ok=false` means "no anchor", and the caller MUST then fail closed (no hard slash). The asymmetry is
// deliberate: missing the capture of a cheater is bounded and stays negative-expected-value for the
// cheater (ADR-025), whereas slashing an honest miner is catastrophic and irreversible in reputation.
func (k Keeper) auditCommitteeAllowed(ctx context.Context, jobId string) (map[string]bool, bool) {
	raw, err := k.AuditCommittee.Get(ctx, jobId)
	if err != nil || strings.TrimSpace(raw) == "" {
		return nil, false
	}
	allowed := make(map[string]bool, 8)
	for _, id := range strings.Split(raw, ",") {
		if id = strings.TrimSpace(id); id != "" {
			allowed[id] = true
		}
	}
	if len(allowed) == 0 {
		return nil, false
	}
	return allowed, true
}
