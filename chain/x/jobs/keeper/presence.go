package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-048 item 5 — ONLY A MINER THAT PROVED IT IS THERE CAN BE DRAWN.
//
// THE DEFECT. Every draw — the primary of a job, its audit jury, a re-adjudication jury — filtered on
// registration (the frozen pool, ADR-037) and on vitality (a commit or a registration inside the juror
// window, ADR-034). Neither says the machine is ON. The vitality window is deliberately wide (days, so
// that maintenance does not cost a jury seat), so a miner whose machine was switched off stayed drawable
// for days: drawn as primary it answers nothing and the job waits for its expiry; drawn as a juror it
// holds a seat and never votes. On the chain destroyed on 2026-08-13 one machine powered off on 08-11
// was still drawn primary on every job opened afterwards (ADR-042).
//
// THE SIGNAL ALREADY EXISTED. `MsgProveAvailability` answers an unpredictable per-window challenge with
// the miner's VRF key; the miner daemon sends it on every new challenge. It fed the availability payout
// and slash, both disarmed, and nothing else.
//
// THE RULE. With availability windows armed (`avail_epoch_blocks > 0`), a miner is PRESENT at height h
// when it proved presence in the window containing h or in the one before it. Two windows, not one: a
// miner that proved at the start of the previous window must not drop out during the first blocks of the
// current one, before it has had a chance to answer the new challenge.
//
// A NEWCOMER IS PRESENT for the window it registered in and the next one, the same grace vitality gives
// (`eligibleAsJurorInWindow`: presence must never become an ENTRY filter). It also covers the start of a
// chain, before the first challenge exists, and an import, which re-dates every miner.
//
// DISARMED (`avail_epoch_blocks = 0`) IT FILTERS NOTHING, so a chain that does not measure presence keeps
// every draw it had. Absence of a measurement is not absence of the miner.
//
// ⚠️ IT IS NOT HISTORICAL, AND NEITHER IS VITALITY. A miner that proves AFTER height h becomes "present at
// h" too (`last >= e-1` holds). That is harmless for the same reason it is harmless for vitality: the work
// committee is ANCHORED the first time it is derived (ADR-045 12), the jury the moment it is drawn, so a
// later proof never moves a committee already written.
func (k Keeper) minerPresentAt(ctx context.Context, minerId string, height uint64, p types.Params) bool {
	eb := p.AvailEpochBlocks
	if eb == 0 {
		return true
	}
	e := height / eb
	if last, err := k.MinerLastPresentEpoch.Get(ctx, minerId); err == nil && last+1 >= e {
		return true
	}
	// No proof covering this window: only a NEWCOMER is present. A miner without a registration date is
	// a state no normal path produces (create-miner dates, import re-dates); it is not granted presence on
	// a criterion that decides who is paid. The frozen pool already excludes it, and says so.
	reg, err := k.MinerRegisteredHeight.Get(ctx, minerId)
	if err != nil {
		return false
	}
	return reg/eb+1 >= e
}
