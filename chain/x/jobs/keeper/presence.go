package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-048 item 5: only a miner that proved it is there can be drawn.
//
// Every draw (the primary of a job, its audit jury, a re-adjudication jury) filtered on registration
// (the frozen pool, ADR-037) and on vitality (a commit or a registration inside the juror window,
// ADR-034). Neither says the machine is on. The vitality window is deliberately wide (days, so that
// maintenance does not cost a jury seat), so a miner whose machine was switched off stayed drawable for
// days: drawn as primary it answers nothing and the job waits for its expiry; drawn as a juror it holds
// a seat and never votes. On a chain where this happened, one powered-off machine was still drawn
// primary on every job opened afterwards (ADR-042).
//
// The signal already existed. `MsgProveAvailability` answers an unpredictable per-window challenge with
// the miner's VRF key, and the miner daemon sends it on every new challenge. It fed the availability
// payout and slash, both disarmed, and nothing else.
//
// The rule: with availability windows armed (`avail_epoch_blocks > 0`), a miner is present at height h
// when it proved presence in the window containing h or in the one before it. Two windows, because a
// miner that proved at the start of the previous window must not drop out during the first blocks of
// the current one, before it has had a chance to answer the new challenge.
//
// A newcomer is present for the window it registered in and the next one, the same grace vitality
// gives (`eligibleAsJurorInWindow`: presence must never become an entry filter). That also covers the
// start of a chain, before the first challenge exists, and an import, which re-dates every miner.
//
// Disarmed (`avail_epoch_blocks = 0`), it filters nothing, so a chain that does not measure presence
// keeps every draw it had. A missing measurement does not mean a missing miner.
//
// It is not historical, and neither is vitality: a miner that proves after height h also becomes
// "present at h" (`last >= e-1` holds). That is harmless for the same reason as for vitality: the work
// committee is anchored the first time it is derived (ADR-045 12), the jury the moment it is drawn, so
// a later proof never moves a committee already written.
func (k Keeper) minerPresentAt(ctx context.Context, minerId string, height uint64, p types.Params) bool {
	eb := p.AvailEpochBlocks
	if eb == 0 {
		return true
	}
	e := height / eb
	if last, err := k.MinerLastPresentEpoch.Get(ctx, minerId); err == nil && last+1 >= e {
		return true
	}
	// No proof covering this window: only a newcomer is present. A miner without a registration date is
	// a state no normal path produces (create-miner dates, import re-dates), and it is not granted
	// presence on a criterion that decides who is paid. The frozen pool already excludes it, and logs it.
	reg, err := k.MinerRegisteredHeight.Get(ctx, minerId)
	if err != nil {
		return false
	}
	return reg/eb+1 >= e
}
