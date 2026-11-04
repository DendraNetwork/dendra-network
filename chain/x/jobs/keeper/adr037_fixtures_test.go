package keeper_test

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Why these two helpers exist.
//
// Since the pool freeze a job carries a freeze anchor (`JobPoolFreezeHeight`, set by `OpenJob`) and a miner a
// registration height (`MinerRegisteredHeight`, set by `create-miner`). The committee draw compares the
// two, and a missing anchor is an error, never a permissive default.
//
// About thirty tests write their jobs and miners directly into the collections, bypassing both
// handlers, so their fixtures described a state the chain cannot produce: a job without an anchor.
//
// Relaxing production code would reopen the hole the pool freeze closes, for the convenience of tests, the
// worst reason to lower a security guard. The fixture is fixed instead of the rule.
//
// What these helpers set, and why:
//   - job freeze = gvFreezeH (below): every miner registered at height 0 belongs to that job's pool,
//     so these tests keep measuring what they measured before the pool freeze, and none of them accidentally
//     becomes a test of the freeze;
//   - miner registration height = 0, for the same reason on the miner side.
//
// "Only if absent" matters. Several tests (liveness) set a precise registration height
// themselves to exercise a window, and overwriting it would let the test pass while measuring nothing.
// Only what is missing is filled in.
//
// The freeze test itself (`adr037_pool_freeze_test.go`) does not use these helpers: it sets both
// anchors explicitly, and its third case omits the anchor on purpose to exercise the refusal.

// This fixture used to anchor the freeze at ^uint64(0), which stopped being harmless.
// MaxUint64 served as a sentinel meaning "no pool restriction": `minerInFrozenPool` asks
// `registered <= freeze`, which an impossible height satisfies for everyone. That worked while the
// field had exactly one reader.
//
// The work draw now reads the same field with a second meaning ("was this miner producing when the
// job opened?"), and against MaxUint64 the answer is no for every miner alive, because
// `MaxUint64 - registered` overflows any freshness window. Every committee came back empty, and six
// tests failed on assertions unrelated to pool freezing.
//
// A plausible height keeps both readings true: `registered(0) <= gvFreezeH` still admits the whole
// fixture population, and `gvFreezeH - 0` sits far inside the freshness window. A sentinel chosen to
// mean "unconstrained" for one predicate becomes wrong the day a second predicate gives the field its
// own meaning.
const gvFreezeH = uint64(1000)

func gvSetJob(f *fixture, ctx context.Context, jobId string, j types.Job) error {
	if _, err := f.keeper.JobPoolFreezeHeight.Get(ctx, jobId); err != nil {
		if e := f.keeper.JobPoolFreezeHeight.Set(ctx, jobId, gvFreezeH); e != nil {
			return e
		}
	}
	return f.keeper.Job.Set(ctx, jobId, j)
}

// Since consensus epoch 15 a jury seat belongs to a declared judge, and a declaration is a
// third entry a fixture written straight into the collections never carries. The same reasoning as the
// registration height applies: without it, about twenty tests whose subject is the draw, the bar or the
// deferral would stop seating anyone and become tests of the judge filter. So a fixture miner is
// declared at height 0 (inside the trial window of every draw height these tests use), only if the
// judge state is absent, so that the epoch 15 cases, which set that state themselves, keep measuring
// what they set. A test that needs an undeclared miner writes `Miner.Set` directly.
func gvSetMiner(f *fixture, ctx context.Context, minerId string, m types.Miner) error {
	if _, err := f.keeper.MinerRegisteredHeight.Get(ctx, minerId); err != nil {
		if e := f.keeper.MinerRegisteredHeight.Set(ctx, minerId, 0); e != nil {
			return e
		}
	}
	if declared, err := f.keeper.MinerJudge.Has(ctx, minerId); err == nil && !declared {
		if e := f.keeper.MinerJudge.Set(ctx, minerId); e != nil {
			return e
		}
		if e := f.keeper.MinerJudgeDeclaredHeight.Set(ctx, minerId, 0); e != nil {
			return e
		}
	}
	return f.keeper.Miner.Set(ctx, minerId, m)
}
