package keeper_test

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══ ADR-037 — WHY THESE TWO HELPERS EXIST ═══════════════════════════════════════════════════════
//
// Since ADR-037 a job carries a FREEZE ANCHOR (`JobPoolFreezeHeight`, set by `OpenJob`) and a miner a
// REGISTRATION HEIGHT (`MinerRegisteredHeight`, set by `create-miner`). The committee draw compares
// the two, and a MISSING anchor is an ERROR — never a permissive default.
//
// About thirty tests write their jobs and their miners DIRECTLY into the collections, bypassing both
// handlers. Their fixtures therefore described a state the chain cannot produce: a job without an
// anchor.
//
// THE REMEDY IS NOT TO RELAX PRODUCTION CODE. Making the absence permissive would reopen exactly the
// hole ADR-037 closes, and would do so for the convenience of tests — the worst reason to lower a
// security guard. Fix the FIXTURE, not the rule.
//
// What these helpers set, and why those values:
//   · job freeze = MaxUint64  -> every miner belongs to that job's pool, so the behaviour of these
//     tests is EXACTLY the pre-ADR-037 one: they keep measuring what they measured, and none of them
//     accidentally becomes a test of the freeze;
//   · miner registration height = 0 -> same reasoning on the miner side.
//
// "ONLY IF ABSENT" IS LOAD-BEARING. Several tests (liveness, ADR-034/036) set a precise registration
// height themselves in order to exercise a window. Overwriting it would empty their green of meaning
// — the test would pass while measuring nothing. Only what is missing is filled in.
//
// The freeze test itself (`adr037_pool_freeze_test.go`) does NOT use these helpers: it sets both
// anchors explicitly, and its third case DELIBERATELY omits the anchor to exercise the refusal.

func gvSetJob(f *fixture, ctx context.Context, jobId string, j types.Job) error {
	if _, err := f.keeper.JobPoolFreezeHeight.Get(ctx, jobId); err != nil {
		if e := f.keeper.JobPoolFreezeHeight.Set(ctx, jobId, ^uint64(0)); e != nil {
			return e
		}
	}
	return f.keeper.Job.Set(ctx, jobId, j)
}

func gvSetMiner(f *fixture, ctx context.Context, minerId string, m types.Miner) error {
	if _, err := f.keeper.MinerRegisteredHeight.Get(ctx, minerId); err != nil {
		if e := f.keeper.MinerRegisteredHeight.Set(ctx, minerId, 0); e != nil {
			return e
		}
	}
	return f.keeper.Miner.Set(ctx, minerId, m)
}
