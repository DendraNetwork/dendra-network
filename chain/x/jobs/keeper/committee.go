package keeper

import (
	"context"
	"crypto/sha256"
	"fmt"
	"math/big"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// CommitteeSize is the number of miners assigned to a job (to be made governable through params later).
const CommitteeSize = 3

// assignmentStakeCapMultiple — how many times the pool's own floor a miner may weigh in the WORK draw.
//
// Two is not a round number picked for comfort. At 13 miners the fair share of the primary slot is
// 1/13 = 7.7 %; with the weights saturated at twice the floor, the best-funded miner reaches about
// 15 % — twice its share, not twelve times it. Raising this to 4 already puts it near 30 %, which is
// the concentration this constant exists to refuse. Lowering it to 1 would delete stake weighting
// from the assignment altogether — a defensible position, but a different decision from bounding it,
// and one that belongs in an ADR rather than in a constant.
//
// ⚠️ This is a Go CONSTANT and therefore not governable, exactly like `CommitteeSize` above. That is
// a known debt, recorded in ADR-042: making it a parameter means a proto field and the migration
// that comes with it. A constant that bounds the failure is worth more today than a parameter that
// does not exist yet — but it is a constant, and this comment is where that is admitted.
const assignmentStakeCapMultiple = 2

// minerWeight carries a miner's identity and bond, for the weighted selection.
type minerWeight struct {
	id    string
	stake uint64
}

// selectCommittee — the PURE core (testable without a keeper) of committee selection.
//
// STAKE WEIGHTING: each miner gets a score = hash(seed|minerId) / stake, compared by CROSS PRODUCT in
// big.Int (integers only, ZERO floating point, hence identical on every validator). The `size` SMALLEST
// scores are kept: the larger the stake, the smaller the score, the more often the miner is selected.
// Because the bond is real, (a) controlling more slots costs proportionally more locked coins, and
// (b) SPLITTING a stake across N identities does not change total influence — which is what makes the
// selection anti-sybil, unlike a UNIFORM draw where every identity counts the same. A zero stake (a fully
// slashed miner) is relegated to the very end and never takes priority.
func selectCommittee(seed string, miners []minerWeight, size int) map[string]bool {
	set := map[string]bool{}
	for _, id := range selectCommitteeOrdered(seed, miners, size) {
		set[id] = true
	}
	return set
}

// selectCommitteeOrdered — the same draw as selectCommittee, but returning the consensus sort ORDER.
//
// There is ONE derivation: selectCommittee is written on top of this one (prefix, then set), so that two
// copies of the sort never exist. Two copies of a consensus arithmetic diverge eventually. The PREFIX
// property is guaranteed by the total order: the top-1 is the first element of the top-k, so members[0]
// is EXACTLY the primary that `settleOptimistic` pays (assignedCommittee(., 1)). That is what the
// AssignedCommittee query exposes.
func selectCommitteeOrdered(seed string, miners []minerWeight, size int) []string {
	type scored struct {
		id    string
		h     [32]byte
		stake uint64
	}
	all := make([]scored, 0, len(miners))
	for _, m := range miners {
		all = append(all, scored{id: m.id, h: sha256.Sum256([]byte(seed + "|" + m.id)), stake: m.stake})
	}
	sort.Slice(all, func(i, j int) bool {
		return lessByStakeScore(all[i].h, all[i].stake, all[i].id, all[j].h, all[j].stake, all[j].id)
	})
	if size > len(all) {
		size = len(all)
	}
	out := make([]string, 0, size)
	for i := 0; i < size; i++ {
		out = append(out, all[i].id)
	}
	return out
}

// lessByStakeScore — the STAKE-WEIGHTED comparator, single source of truth for the sort.
//
// h_i/stake_i < h_j/stake_j  <=>  h_i*stake_j < h_j*stake_i  (EXACT big.Int cross product).
// Integers only, ZERO floating point, so the order is the same on every validator; otherwise the chain
// forks. A zero stake (a fully slashed miner) is relegated to the end and never takes priority; ties are
// broken by id for determinism.
//
// Extracted from selectCommittee to be SHARED with drawAuditMembers (ADR-032): both draws must sort
// identically. Two copies of this arithmetic would have diverged eventually, and an order divergence
// between validators is paid for with a fork, not with a red test.
func lessByStakeScore(hi [32]byte, si uint64, idi string, hj [32]byte, sj uint64, idj string) bool {
	if si == 0 || sj == 0 {
		if si != sj {
			return si > sj // a stake > 0 sorts before a stake == 0
		}
		return idi < idj
	}
	li := new(big.Int).Mul(new(big.Int).SetBytes(hi[:]), new(big.Int).SetUint64(sj))
	lj := new(big.Int).Mul(new(big.Int).SetBytes(hj[:]), new(big.Int).SetUint64(si))
	if c := li.Cmp(lj); c != 0 {
		return c < 0
	}
	return idi < idj
}

// assignedCommittee derives a job's committee DETERMINISTICALLY and WEIGHTED BY STAKE.
//
// ANTI-GRINDING: when a BEACON has been fixed for the job (by open-job, from unpredictable block
// randomness), the seed is beacon|jobId, so the creator CANNOT grind the jobId to pick a complicit
// committee — it does not control the beacon. Without a beacon (older jobs) the seed is the jobId alone,
// for backward compatibility.
func (k Keeper) assignedCommittee(ctx context.Context, jobId string, size int) (map[string]bool, error) {
	ordered, err := k.assignedCommitteeOrdered(ctx, jobId, size)
	if err != nil {
		return nil, err
	}
	set := make(map[string]bool, len(ordered))
	for _, id := range ordered {
		set[id] = true
	}
	return set, nil
}

// assignedCommitteeOrdered — the same derivation, ORDERED (single source: assignedCommittee is just a set
// built on top). Consumed by the AssignedCommittee query: the client READS this draw instead of
// re-deriving it, and members[0] is the primary paid by settleOptimistic.
// assignedCommitteeOrdered -- THE ANCHOR FIRST, the derivation only when there is not one yet.
//
// ADR-045 (12): the derivation froze MEMBERSHIP and VITALITY (ADR-037) but read the ordering WEIGHT
// live from the miner record. Slashing a MEMBER for an UNRELATED job therefore moved an open job's
// committee: the member that had answered was no longer assigned, a stranger was, and the settlement
// refused for an incomplete committee -- an escrow with no path. Measured: [cm2 cm1 cm5] became
// [cm2 cm5 cm6], while the control (slashing a NON-member) moved nothing, so what moved the draw was
// the weight of a DRAWN miner.
//
// The anchor is written the first moment the committee EXISTS, not at open time: with a reveal delay
// the beacon seed is not there yet and nothing can be drawn. Anchoring an impossible derivation would
// amount to inventing a committee.
//
// The fallback to the derivation stays, and it is not a weakness: a job opened BEFORE this epoch has no
// anchor, and a derived committee beats an error. It disappears on its own -- a fresh genesis is
// mandatory at this epoch, so no anchorless job survives.
func (k Keeper) assignedCommitteeOrdered(ctx context.Context, jobId string, size int) ([]string, error) {
	if raw, err := k.WorkCommittee.Get(ctx, jobId); err == nil && raw != "" {
		ids := strings.Split(raw, ",")
		if len(ids) > size {
			ids = ids[:size]
		}
		return ids, nil
	}
	return k.deriveCommitteeOrdered(ctx, jobId, size)
}

// deriveCommitteeOrdered -- the draw itself. Called once per job now, at anchoring time.
func (k Keeper) deriveCommitteeOrdered(ctx context.Context, jobId string, size int) ([]string, error) {
	seed := jobId
	if b, err := k.Beacon.Get(ctx, jobId); err == nil {
		// Beacon present but seed EMPTY means the committee is awaiting its deferred reveal. Do NOT fall
		// back to the jobId alone, which is grindable. Refuse until the EndBlocker reveals the seed.
		if b.Seed == "" {
			return nil, fmt.Errorf("committee not revealed for job %q (deferred reveal pending)", jobId)
		}
		seed = b.Seed + "|" + jobId
	}
	// ADR-037 — FROZEN POOL. Without this filter the walk re-read the LIVE pool: once `b.Seed` is
	// public, anyone can grind an `id` offline and place itself in slot 0 of an ALREADY OPEN job with a
	// dust stake (94 % success at 2^24 attempts, against a millionth of the stake). An unpredictable seed
	// stops a miner that is ALREADY REGISTERED; it does not stop a miner CREATED AFTERWARDS. See
	// pool_freeze.go.
	freeze, fErr := k.jobPoolFreezeHeight(ctx, jobId)
	if fErr != nil {
		return nil, fErr
	}
	var miners []minerWeight
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if !k.minerInFrozenPool(ctx, jobId, key, freeze) {
			return false, nil
		}
		// ⛔ THE WORK DRAW HAD NO LIVENESS TEST AT ALL, AND THAT IS WHAT EMPTIED THE NETWORK.
		// The JURY draw calls a vitality predicate (audit_committee.go); this one filtered on the
		// frozen pool and nothing else. Measured on the chain destroyed on 2026-08-13: one machine
		// was powered off on 08-11 and still drew primary on every job opened afterwards — 51 out of
		// 51 — because nothing on chain noticed it had stopped. The protocol owned a liveness test,
		// applied it to the role that only reviews, and omitted it from the role that has to answer.
		//
		// ⚠️ EVALUATED AT `freeze`, NEVER AT THE CURRENT HEIGHT. This draw is recomputed on every
		// read: a predicate that moved with the clock would silently reassign the primary of an
		// already-settled job, so `assigned_committee` would stop agreeing with what was paid. At the
		// freeze height the question is the right one anyway — "was this identity producing when the
		// job opened?" — and it is exactly the case that failed: a job opened after the machine died
		// no longer sees it. A miner that dies MID-job is a different problem, and it is the audit's.
		if !k.minerVitalAt(ctx, key, freeze) {
			return false, nil
		}
		miners = append(miners, minerWeight{id: key, stake: m.Stake})
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	// The ceiling is derived from `min_stake`, so the parameters have to be READ. An unreadable
	// params store is answered like a missing freeze anchor a few lines above — with an ERROR, not
	// with a guess. Falling back to "no cap" would restore the exact monopoly this bounds, and it
	// would do it silently, on the one branch nobody exercises.
	params, pErr := k.Params.Get(ctx)
	if pErr != nil {
		return nil, fmt.Errorf("params unreadable for job %q: the assignment ceiling cannot be derived, draw REFUSED rather than uncapped: %w", jobId, pErr)
	}
	return selectCommitteeOrdered(seed, capAssignmentWeights(miners, params.MinStake), size), nil
}

// capAssignmentWeights — SATURATES the weight a single miner can carry in the WORK draw.
//
// ⛔ WITHOUT THIS, ONE MINER TOOK 94 % OF THE WORK BY OUTBIDDING THE FLOOR. On the destroyed chain
// the dominant miner held 5 000 000 against 50 000 for every other — a hundredfold — and
// `lessByStakeScore` gives it a score a hundred times smaller, so it sorted first on essentially
// every draw. Measured: primary on 51 jobs out of 51, including 9 out of 9 where it had competitors.
// No cap existed anywhere: `msg_server_miner.go` bounds the stake from BELOW (min_stake) and from
// nowhere else, and no parameter in `params.proto` expresses a ceiling.
//
// THE SCALE IS `min_stake`, A GOVERNED VALUE — AND THE FIRST VERSION OF THIS FUNCTION GOT IT WRONG.
// It derived the ceiling from the SMALLEST STAKE PRESENT IN THE POOL, which reads as elegant and is
// an attack: the floor is whatever the poorest participant chose, so anyone can stake dust and
// collapse every honest weight down to `2 x dust`. The repository's own bench found it — `c2Setup`
// plants a sybil at stake 1 precisely so it is "deterministically excluded from the top 3", and the
// pool-relative cap brought that 1e12-fold gap down to twofold. A ceiling an adversary can lower is
// not a ceiling. `min_stake` moves only by governance, and it is already the value every miner must
// clear to exist at all.
//
// Two properties follow:
//   - the cap binds only ABOVE the entry price, so a dust or fully-slashed stake is untouched and
//     stays relegated by the comparator, exactly as before;
//   - a pool where everyone staked near the floor is UNCHANGED — this only ever clips an outlier.
//
// Staking beyond the cap therefore buys NOTHING in the draw. That is the point: the incentive to
// hoard weight disappears instead of being merely bounded. The bond still means something — it is
// what a slash takes — so the ceiling costs the protocol nothing it was using.
//
// ⚠️ It does NOT touch `lessByStakeScore`: that comparator is shared with the audit draw
// (ADR-032), where the stake is the anti-sybil weight and must stay whole. Capping there would have
// changed the jury's arithmetic as a side effect of fixing the work assignment.
func capAssignmentWeights(miners []minerWeight, minStake uint64) []minerWeight {
	if minStake == 0 {
		// No entry price means no scale to derive a ceiling from. Returning the weights untouched is
		// the only honest answer: inventing a number here would bound the draw by something nobody
		// governed. A chain that wants the cap sets `min_stake`, which it must set anyway.
		return miners
	}
	ceiling := minStake * assignmentStakeCapMultiple
	if ceiling < minStake {
		return miners // overflow: refuse to invent a ceiling below the floor it derives from
	}
	out := make([]minerWeight, len(miners))
	for i, m := range miners {
		if m.stake > ceiling {
			m.stake = ceiling
		}
		out[i] = m
	}
	return out
}
