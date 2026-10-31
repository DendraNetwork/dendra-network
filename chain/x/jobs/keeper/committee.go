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

// CommitteeSize is the compiled default of `work_committee_size`, the number of miners
// assigned to a job. Production code reads `effectiveWorkCommitteeSize` / `workCommitteeSize`; a site
// that needs a fixed jury floor reads `redoQuorumFloor` (policy_params.go), never this.
const CommitteeSize = 3

// assignmentStakeCapMultiple is how many times the pool's own floor a miner may weigh in the work draw.
//
// Two is not an arbitrary round number. At 13 miners the fair share of the primary slot is 1/13 =
// 7.7 %; with the weights saturated at twice the floor, the best-funded miner reaches about 15 %, twice
// its share instead of twelve times it. Raising this to 4 already puts it near 30 %, the concentration
// this constant exists to refuse. Lowering it to 1 would remove stake weighting from the assignment
// altogether, a defensible position but a different decision from bounding it, and one that belongs in
// a written decision rather than in a constant.
//
// This is now the compiled default of `assignment_stake_cap_multiple`, selected when the
// parameter is 0 (`effectiveStakeCapMultiple`, policy_params.go). The recorded debt ("a Go
// constant, not governable") is paid; the reasoning above for the value 2 is unchanged.
const assignmentStakeCapMultiple = 2

// minerWeight carries a miner's identity and bond, for the weighted selection.
type minerWeight struct {
	id    string
	stake uint64
}

// selectCommittee is the pure core (testable without a keeper) of committee selection.
//
// Stake weighting: each miner gets a score = hash(seed|minerId) / stake, compared by cross product in
// big.Int (integers only, no floating point, hence identical on every validator). The `size` smallest
// scores are kept: the larger the stake, the smaller the score, and the more often the miner is
// selected. Because the bond is real, (a) controlling more slots costs proportionally more locked
// coins, and (b) splitting a stake across N identities does not raise its chance of slot 0: N split
// scores h/stake are never likelier to hold the smallest than one score at the summed stake
// (pool_freeze.go: 51.4 % -> 38.6 % at sixteen), which makes the selection anti-sybil, unlike a uniform
// draw. With `size` > 1 a split can hold more seats. A zero stake (a fully slashed miner) is relegated
// to the end and never takes priority.
// (b) holds for the weights the caller passes. The work draw caps them (capAssignmentWeights): stake
// above the ceiling weighs nothing, so a split into identities that each still reach it draws more,
// one below it can draw less, and at a multiple of 1 every identity at `min_stake` weighs the same (the
// uniform draw).
func selectCommittee(seed string, miners []minerWeight, size int) map[string]bool {
	set := map[string]bool{}
	for _, id := range selectCommitteeOrdered(seed, miners, size) {
		set[id] = true
	}
	return set
}

// selectCommitteeOrdered is the same draw as selectCommittee, returning the consensus sort order.
//
// There is one derivation: selectCommittee is written on top of this one (prefix, then set), so that
// two copies of the sort never exist; two copies of a consensus computation diverge eventually. The
// prefix property follows from the total order: the top-1 is the first element of the top-k, so
// members[0] is exactly the primary that `settleOptimistic` pays (assignedCommittee(., 1)). The
// AssignedCommittee query exposes that.
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

// lessByStakeScore is the stake-weighted comparator, the single source of truth for the sort.
//
// h_i/stake_i < h_j/stake_j  <=>  h_i*stake_j < h_j*stake_i  (exact big.Int cross product).
// Integers only, no floating point, so the order is the same on every validator; otherwise the chain
// forks. A zero stake (a fully slashed miner) is relegated to the end and never takes priority; ties
// are broken by id for determinism.
//
// It is shared with drawAuditMembers because both draws must sort identically. Two copies of
// this arithmetic would diverge eventually, and an order divergence between validators costs a fork,
// not a red test.
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

// assignedCommittee derives a job's committee deterministically, weighted by stake.
//
// Anti-grinding: when a beacon has been fixed for the job (by open-job, from unpredictable block
// randomness), the seed is beacon|jobId, so the creator cannot grind the jobId to pick a complicit
// committee, since it does not control the beacon. Without a beacon (older jobs) the seed is the jobId
// alone, for backward compatibility.
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

// assignedCommitteeOrdered returns the same committee, ordered (assignedCommittee is a set built on top
// of it). The AssignedCommittee query consumes it: the client reads this draw instead of deriving it
// again, and members[0] is the primary settleOptimistic pays. The anchor comes first; the derivation
// only runs when there is no anchor yet.
//
// The derivation froze membership and vitality but read the ordering weight
// live from the miner record. Slashing a member for an unrelated job therefore moved an open job's
// committee: the member that had answered was no longer assigned, a stranger was, and settlement
// refused an incomplete committee, leaving an escrow with no path. Measured: [cm2 cm1 cm5] became
// [cm2 cm5 cm6], while the control (slashing a non-member) moved nothing, so the weight of a drawn
// miner was what moved the draw.
//
// The anchor is written the first moment the committee exists, not at open time: with a reveal delay
// the beacon seed is not there yet and nothing can be drawn, and anchoring an impossible derivation
// would amount to inventing a committee.
//
// The fallback to the derivation deliberately stays: a job opened before this epoch has no anchor, and a
// derived committee beats an error. It goes away on its own, since a fresh genesis is mandatory at this
// epoch and no anchorless job survives.
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

// deriveCommitteeOrdered is the draw itself, called once per job, at anchoring time.
func (k Keeper) deriveCommitteeOrdered(ctx context.Context, jobId string, size int) ([]string, error) {
	seed := jobId
	if b, err := k.Beacon.Get(ctx, jobId); err == nil {
		// A beacon with an empty seed means the committee is awaiting its deferred reveal. Do not fall
		// back to the jobId alone, which is grindable; refuse until the EndBlocker reveals the seed.
		if b.Seed == "" {
			return nil, fmt.Errorf("committee not revealed for job %q (deferred reveal pending)", jobId)
		}
		seed = b.Seed + "|" + jobId
	}
	// Frozen pool. Without this filter the walk read the live pool: once `b.Seed` is public,
	// anyone can grind an `id` offline and place itself in slot 0 of an already open job with a dust
	// stake (94 % success at 2^24 attempts, against a millionth of the stake). An unpredictable seed
	// stops a miner that is already registered, not a miner created afterwards. See pool_freeze.go.
	freeze, fErr := k.jobPoolFreezeHeight(ctx, jobId)
	if fErr != nil {
		return nil, fErr
	}
	// The ceiling is derived from `min_stake`, and presence from the availability window, so the
	// parameters have to be read. An unreadable params store is answered like a missing freeze anchor
	// above, with an error instead of a guess. Falling back to "no cap" would silently restore the
	// monopoly this bounds, on the one branch nobody exercises.
	params, pErr := k.Params.Get(ctx)
	if pErr != nil {
		return nil, fmt.Errorf("params unreadable for job %q: the assignment ceiling cannot be derived, draw refused rather than uncapped: %w", jobId, pErr)
	}
	var miners []minerWeight
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if !k.minerInFrozenPool(ctx, jobId, key, freeze) {
			return false, nil
		}
		// The work draw needs its own liveness test, or it empties the network.
		// The jury draw calls a vitality predicate (audit_committee.go); a work draw that filtered on the
		// frozen pool alone would keep drawing a dead miner. On a chain where this happened, one machine
		// was powered off and still drew primary on every job opened afterwards, 51 out of 51, because
		// nothing on chain noticed it had stopped: the liveness test covered the role that only reviews
		// and not the role that has to answer.
		//
		// It is evaluated at `freeze`, never at the current height. This draw is recomputed on every
		// read, and a predicate that moved with the clock would silently reassign the primary of an
		// already settled job, so `assigned_committee` would stop agreeing with what was paid. At the
		// freeze height the question is the right one anyway ("was this identity producing when the job
		// opened?"), and it covers the case that failed: a job opened after the machine died no longer
		// sees it. A miner that dies in the middle of a job is a different problem, handled by the audit.
		if !k.minerVitalAt(ctx, key, freeze) {
			return false, nil
		}
		// Vitality says the identity produced within days, not that the machine is on.
		// Presence does, from the availability challenge it answered, at the same height.
		if !k.minerPresentAt(ctx, key, freeze, params) {
			return false, nil
		}
		miners = append(miners, minerWeight{id: key, stake: m.Stake})
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	return selectCommitteeOrdered(seed, capAssignmentWeights(miners, params.MinStake, effectiveStakeCapMultiple(params)), size), nil
}

// capAssignmentWeights saturates the weight a single miner can carry in the work draw.
//
// Without it, one miner took 94 % of the work by outbidding the floor. On a chain where this happened
// the dominant miner held 5 000 000 against 50 000 for every other, a hundredfold, and
// `lessByStakeScore` gave it a score a hundred times smaller, so it sorted first on nearly every draw:
// primary on 51 jobs out of 51, including 9 out of 9 where it had competitors. No cap existed:
// `msg_server_miner.go` bounds the stake from below (min_stake) only, and no parameter in
// `params.proto` expresses a ceiling.
//
// The scale is `min_stake`, a governed value. A first version derived the ceiling from the smallest
// stake present in the pool, which looks elegant and is an attack: the floor is whatever the poorest
// participant chose, so anyone can stake dust and collapse every honest weight down to `2 x dust`. The
// repository's own test found it: `c2Setup` plants a sybil at stake 1 so it is "deterministically
// excluded from the top 3", and the pool-relative cap brought that 1e12-fold gap down to twofold. A
// ceiling an adversary can lower is not a ceiling. `min_stake` moves only by governance, and every
// miner must already clear it to exist at all.
//
// Two properties follow:
//   - the cap binds only above the entry price, so a dust or fully slashed stake is untouched and
//     stays relegated by the comparator, as before;
//   - a pool where everyone staked near the floor is unchanged; this only clips an outlier.
//
// Staking beyond the cap therefore buys nothing in the draw, so the incentive to hoard weight
// disappears instead of being merely bounded. The bond still matters, since a slash takes it, so the
// ceiling costs the protocol nothing it was using.
//
// It does not touch `lessByStakeScore`: that comparator is shared with the audit draw, where
// the stake is the anti-sybil weight and must stay whole. Capping there would have changed the jury's
// arithmetic as a side effect of fixing the work assignment.
func capAssignmentWeights(miners []minerWeight, minStake, multiple uint64) []minerWeight {
	if minStake == 0 {
		// No entry price means no scale to derive a ceiling from, so the weights are returned untouched:
		// inventing a number here would bound the draw by something nobody governed. A chain that wants
		// the cap sets `min_stake`, which it must set anyway.
		return miners
	}
	// The multiple is now `assignment_stake_cap_multiple`, 0 selecting the compiled 2.
	// The overflow test below still holds for any multiple >= 1, which Validate guarantees.
	ceiling := minStake * multiple
	if multiple == 0 || ceiling/multiple != minStake || ceiling < minStake {
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
