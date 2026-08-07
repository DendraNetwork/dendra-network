package keeper

import (
	"context"
	"crypto/sha256"
	"fmt"
	"math/big"
	"sort"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// CommitteeSize is the number of miners assigned to a job (to be made governable through params later).
const CommitteeSize = 3

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
func (k Keeper) assignedCommitteeOrdered(ctx context.Context, jobId string, size int) ([]string, error) {
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
		miners = append(miners, minerWeight{id: key, stake: m.Stake})
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	return selectCommitteeOrdered(seed, miners, size), nil
}
