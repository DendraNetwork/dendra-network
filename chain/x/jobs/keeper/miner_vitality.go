package keeper

import (
	"context"
	"strings"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-034: miner vitality.
//
// If the only quality a miner had were "listed in the registry", bought once for `min_stake`,
// two consequences would follow:
//   (a) silent identities occupy jury seats without ever voting, which causes repeated no-quorum
//       outcomes and the clawback of honest miners' payments;
//   (b) nothing evicts those identities: `silence_slash` lives inside `clawbackPayment`, which
//       presupposes a prior payment, and a miner that produces nothing is never paid, so never
//       punished. The protocol could punish a paid miner but not evict an inert one.
//
// The shared primitive is `MinerLastCommitHeight`: anchoring a commit is the only act that
// demonstrates production (signed by the operator, carrying a result). This moves the predicate from
// "registered" to "demonstrably alive", as ADR-032 moved from "registered" to "drawn".

const (
	// jurorFreshnessBlocks is the recency window to sit as a juror. It is deliberately very wide (about 3
	// days at 1 s/block): an honest miner under maintenance, in a power outage or migrating must not
	// lose juror eligibility, otherwise an infrastructure outage would again be billed to the honest
	// party, one layer down. The window only excludes identities that have never produced anything or
	// vanished long ago; it does not rank good miners against less good ones.
	jurorFreshnessBlocks = uint64(259200)

	// minerPruneBlocks is the inactivity beyond which a miner is evicted. It is wider still than the
	// juror window (about 7 days), because losing registration costs more than losing a jury seat (the
	// accumulated anti-Sybil `Demand` resets to zero). The order is deliberate: stop summoning first,
	// evict only much later.
	minerPruneBlocks = uint64(604800)
)

// effectiveJurorFreshness / effectivePruneWindow (ADR-034 §5): both windows are governable. They are
// choices awaiting a measurement (p99 of inter-commit intervals on an open testnet), and a value known
// to need correction from data must not require a binary upgrade. Standard dormant pattern of this
// module: 0 (default) means the compiled constant and unchanged behaviour; >0 is the governed value.
// Unlike `auditCommitteeDrawSize`, a constant was not justified here, since changing a window does not
// change a committee anchoring.
func effectiveJurorFreshness(p types.Params) uint64 {
	if p.JurorFreshnessBlocks > 0 {
		return p.JurorFreshnessBlocks
	}
	return jurorFreshnessBlocks
}

func effectivePruneWindow(p types.Params) uint64 {
	if p.MinerPruneBlocks > 0 {
		return p.MinerPruneBlocks
	}
	return minerPruneBlocks
}

// committeeContains reports whether miner `id` is present in a comma-joined member list.
func committeeContains(members, id string) bool {
	if id == "" {
		return false
	}
	for _, m := range strings.Split(members, ",") {
		if m == id {
			return true
		}
	}
	return false
}

// baseJobIdFromCommitteeKey recovers the underlying jobId of an AuditCommittee key. Three forms
// coexist: `<jobId>` (sampling), `<jobId>__redo` (re-adjudication), `<jobId>__humandispute` (human
// dispute marker). The role suffix is stripped to get back to the job.
func baseJobIdFromCommitteeKey(key string) string {
	key = strings.TrimSuffix(key, "__redo")
	key = strings.TrimSuffix(key, humanDisputeSuffix)
	return key
}

// clearAuditCommittee removes the committee anchorings of a job whose audit is over (ADR-034 C1/H5).
//
// Four sites write AuditCommittee, so it must be purged; otherwise two failures follow. (H5) It grows
// without bound, and the O(n) walk in `hasOpenObligation`, called once per expired miner in the
// EndBlocker, becomes O(n^2) and can stall a block. (C1, worse) `hasOpenObligation` then sees the seats
// of closed audits: any miner that ever sat once becomes impossible to evict, and the vitality guard
// only catches miners never drawn, the inverse of its target (the silent identities, which are the ones
// holding seats). The three keys are therefore cleared at resolution: the audit is closed, with nothing
// left to vote on or to immobilise. Best-effort: a missed removal only leaves a stale record, which the
// "job resolved" filter of `hasOpenObligation` already neutralises. The selection marker (C3) is
// removed too, moot once the job is resolved.
func (k Keeper) clearAuditCommittee(ctx context.Context, jobId string) {
	_ = k.AuditCommittee.Remove(ctx, jobId)
	_ = k.AuditCommittee.Remove(ctx, redoAnchorKey(jobId))
	_ = k.AuditCommittee.Remove(ctx, humanDisputeKey(jobId))
	// CommitteeSince is the twin of the seat, and the date leaves with it: an orphan date would make
	// `oldest_lock_age` wrong (a lock reported on a closed audit). Only the two seat-bearing keys are
	// dated; removing a date that was never set is a no-op.
	_ = k.CommitteeSince.Remove(ctx, jobId)
	_ = k.CommitteeSince.Remove(ctx, redoAnchorKey(jobId))
	_ = k.AuditSelected.Remove(ctx, jobId)
}

// anchorCommittee sets a seat-bearing committee and its anchoring height, together. Like
// selectCommittee/selectCommitteeOrdered it is a single derivation: both production sites (audit draw,
// redo draw) go through here, because an anchoring without a date would make that juror invisible to
// the lock counter, the debt the exit guard requires to be measurable. The `__humandispute` key does
// not go through here: it summons no seat and locks nobody.
func (k Keeper) anchorCommittee(ctx context.Context, key, members string) error {
	if err := k.AuditCommittee.Set(ctx, key, members); err != nil {
		return err
	}
	return k.CommitteeSince.Set(ctx, key, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight()))
}

// commitProvesAssignedWork reports whether this commit attests to assigned work, hence real vitality.
//
// ADR-034 C2/M6: vitality must not be self-issued. `CreateCommit` alone requires neither that the miner
// be assigned to the job nor that it was drawn on the committee. Without this guard,
// `create-commit "<REAL_jobId>__<sybilMiner>"` manufactures fresh vitality for the price of gas (one tx
// per window), hence a permanent juror seat and immunity from eviction, the opposite of what vitality
// is meant to prove. The proof of assignment already exists on-chain as membership in the drawn
// committee. It is reused as the union of the three productive roles, so that an honest miner is never
// stripped of its vitality:
//
//	(a) audit juror drawn and anchored          -> AuditCommittee[jobId]        (verdict)
//	(b) re-adjudication juror drawn and anchored -> AuditCommittee[jobId__redo] (redo and its verdict:
//	    a re-adjudication is production, and not dating it starves the jury of eligible members)
//	(c) member of the job's assigned committee  -> assignedCommittee(jobId)     (primary/backup inference)
//
// A sybil that references a job where it was drawn in none of these roles gets nothing. The commit
// itself is never blocked (only the vitality date is), so no legitimate flow breaks and the guard is
// not punitive.
func (k Keeper) commitProvesAssignedWork(ctx context.Context, commitKey, minerId string) (bool, error) {
	// There is no second derivation here any more. The one that lived here differed from the commit
	// handler's, which made the existence guard dangerous to write. One function now knows the key
	// shapes; an epoch marker, which names no job, proves no assigned work and returns false here.
	parsed, ok := types.ParseCommitKey(commitKey)
	if !ok || !parsed.JobScoped() {
		return false, nil
	}
	jobId := parsed.JobId
	// (a) + (b): anchored jury seats, a direct read, never a new draw.
	if members, err := k.AuditCommittee.Get(ctx, jobId); err == nil && committeeContains(members, minerId) {
		return true, nil
	}
	if members, err := k.AuditCommittee.Get(ctx, redoAnchorKey(jobId)); err == nil && committeeContains(members, minerId) {
		return true, nil
	}
	// (c) the job's assigned committee (primary/backup). Requires an existing job and a revealed seed.
	has, hErr := k.Job.Has(ctx, jobId)
	if hErr != nil {
		return false, hErr
	}
	if !has {
		return false, nil
	}
	comm, cErr := k.assignedCommittee(ctx, jobId, k.workCommitteeSize(ctx))
	if cErr != nil {
		// Seed not revealed or not derivable: assignment cannot be proven here. No date is written
		// (non-punitive: the miner regains vitality on its next assigned commit), and nothing blocks.
		return false, nil
	}
	return comm[minerId], nil
}

// hasOpenObligation reports whether the miner still carries a held payment or an unresolved audit
// seat (ADR-034 F3/C1/H4).
//
// The destruction path is real: audit deferral is unbounded by design, so a job can stay in audit past
// the eviction threshold; the miner, with nothing left to commit, becomes inactive and is evicted; then
// `releaseHeld` finds a miner that no longer exists and the held fee is lost. Eviction is therefore
// refused while an obligation is open.
//
// ADR-034 H4, fail closed: it returns `(bool, error)`. A store error must never be read as "no
// obligation", since failing open would authorise eviction under doubt, the value destruction this
// guard exists to prevent. On error the caller does not evict.
//
// ADR-034 C1: only unresolved audit seats count. Without that filter, and with AuditCommittee left
// unpurged, a miner that sat once would stay immune forever. This works together with
// `clearAuditCommittee` (removal at resolution): if a removal is missed, this filter still holds.
func (k Keeper) hasOpenObligation(ctx context.Context, minerId string) (bool, error) {
	open := false
	// (a) a held fee on a job it is the primary of: money owed to it.
	if err := k.HeldFee.Walk(ctx, nil, func(jobId string, _ uint64) (bool, error) {
		if job, err := k.Job.Get(ctx, jobId); err == nil && job.MinerId == minerId {
			open = true
			return true, nil
		}
		return false, nil
	}); err != nil {
		return true, err // fail closed: under doubt, do not evict
	}
	if open {
		return true, nil
	}
	// (b) a seat anchored in the audit committee of an unresolved job: evicting it would remove a
	// summoned juror from an audit in progress and make its quorum unreachable, the no-quorum this
	// file corrects.
	//
	// ADR-034 bis: this clause only holds a juror that can still vote. It was written against the
	// living miner that flees and did not handle the dead one that stays: an identity whose key is
	// lost never votes and can never leave, neither voluntarily (`DeleteMiner` requires the creator
	// key) nor automatically (pruning goes through here). Its seat would keep the quorum bar high for
	// the whole network, forever.
	//
	// A juror is held so that it votes. If it demonstrably cannot vote any more, that is, if
	// `eligibleAsJuror` already refuses to summon it onto a fresh committee, holding it in an old
	// committee serves nobody and harms everyone. Clauses (a) and (c) are untouched: the audited party
	// never leaves, only the juror role is released, so a slash cannot be avoided by waiting.
	//
	// The rule is written on the predicate, not on a presumed order of the windows. Arguing that
	// "minerPruneBlocks > jurorFreshnessBlocks, so every prune candidate is expired" would hold for
	// the compiled defaults and fail in general, since both windows are governable
	// (effectiveJurorFreshness / effectivePruneWindow). Eligibility is therefore evaluated rather than
	// deduced, and the guard stays correct under any setting. `Params.Validate` also bounds the order,
	// but this guard does not rely on another one.
	if k.eligibleAsJuror(ctx, minerId, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())) {
		if err := k.AuditCommittee.Walk(ctx, nil, func(key string, members string) (bool, error) {
			if !committeeContains(members, minerId) {
				return false, nil
			}
			if job, err := k.Job.Get(ctx, baseJobIdFromCommitteeKey(key)); err == nil && jobIsResolved(job.State) {
				return false, nil // audit closed: this seat holds nothing any more
			}
			open = true
			return true, nil
		}); err != nil {
			return true, err // fail closed
		}
		if open {
			return true, nil
		}
	}
	// (c) primary of an unresolved disputed job: an audit instance is still possible on its work,
	// the definition of "escrowed" (T1, held-summary). Letting it leave with its stake makes the
	// slash moot (the "exit before the verdict" dodge, which brings lying back to 0-EV instead of
	// -EV) and creates the absent primary that decouples `upheld` from `optimisticCheated` in
	// AdjudicateDispute. Clause (a) covers this case only when a fee is held (hold_bps>0); clause (c)
	// covers it through the dispute itself, whatever the params. O(jobs) walk, placed last as the
	// most expensive, in the same accepted cost class as the held-summary walk.
	if err := k.Job.Walk(ctx, nil, func(jobId string, job types.Job) (bool, error) {
		if job.MinerId == minerId && jobIsDisputed(job.State) && !jobIsResolved(job.State) {
			open = true
			return true, nil
		}
		// (d) A work-committee member that has already answered, on a job that is still live.
		//
		// The other clauses walk HeldFee, audit seats and disputed jobs, and read neither `Commit` nor
		// the work committee, so a member that had done the work was held by no clause at all.
		// Measured (exit_after_commit_strands_test.go): committee [sc sb sa], all three committed; `sb`
		// unregisters after answering and is accepted; the committee is derived again from the live
		// registry and comes back [sc sa]; `sb`'s commit is still stored; `SettlePay` then refuses an
		// incomplete committee and the job stays open, holding its fee. No adversary is needed:
		// eviction goes through this same predicate, so an honest miner going quiet does it.
		//
		// The bound is the job, never the miner's goodwill. Clause (b) had to be relaxed because an
		// identity whose key is lost can never vote and could then never leave, keeping a quorum bar
		// high for the whole network. The same trap is avoided here by releasing on the job's own
		// terminal states (paid or settled, escrow returned, resolved), none of which requires
		// anything further from this miner. It has already answered; what is held is the derivability
		// of the committee that owes it a settlement.
		//
		// What this does not bound: a job that is served but never settled and whose escrow is never
		// returned holds its committee indefinitely. The unconditional expiry appointment `OpenJob`
		// books is what ends it, not anything in this function; if that appointment ever stopped
		// being booked, this clause would become a permanent hold.
		if jobIsPaid(job.State) || jobEscrowReturned(job.State) || jobIsResolved(job.State) {
			return false, nil
		}
		if _, cErr := k.Commit.Get(ctx, jobId+"__"+minerId); cErr == nil {
			open = true
			return true, nil
		}
		return false, nil
	}); err != nil {
		return true, err // fail closed
	}
	return open, nil
}

// lastCommitHeight returns the height of the last anchored commit; `ok=false` means no entry is known.
func (k Keeper) lastCommitHeight(ctx context.Context, minerId string) (uint64, bool) {
	h, err := k.MinerLastCommitHeight.Get(ctx, minerId)
	if err != nil {
		return 0, false
	}
	return h, true
}

// minerVitalAt answers "was this identity producing at height h?", under a name that says so.
//
// It delegates to `eligibleAsJuror` because the predicate is role-neutral: it asks whether a miner has
// anchored anything, or registered, within the freshness window. The name `eligibleAsJuror` predates
// its second caller, the work draw (`committee.go`), and a jury-shaped name read from the assignment
// path is the kind of mislabel that misleads readers.
//
// The old name stays for now: it has many references, several of them tests pinned to security
// behaviour, and renaming it inside a consensus-breaking change would mix a rename into a diff that
// has to be read for what it does to the protocol. The rename belongs to its own pass, with the tests
// green before and after.
func (k Keeper) minerVitalAt(ctx context.Context, minerId string, height uint64) bool {
	return k.eligibleAsJuror(ctx, minerId, height)
}

// eligibleAsJuror reports whether the miner may sit on the audit jury.
//
// The default is deliberately permissive: a missing entry means eligible. Two innocent cases produce
// it: (i) a new miner that has not anchored a commit yet, since vitality must never become an entry
// filter and a newcomer must be able to mine and judge without a history; (ii) a genesis import, the
// collection not being exported (see keys.go), where without this default a migration would empty
// every jury at once and stall verification network-wide.
//
// ADR-034 F1: the missing-entry case must be split by the registration height. Treating `!ok` as
// eligible unconditionally made the window inapplicable forever when no commit entry existed: a
// registered identity that produces nothing stayed a juror forever, while the guard only caught miners
// that produced and then stopped, the honest population and the inverse of the target. The flaw was in
// the encoding (one signal, `!ok`, for two opposite states), and the registration height separates
// them without weakening either safeguard.
func (k Keeper) eligibleAsJuror(ctx context.Context, minerId string, height uint64) bool {
	// Governed window when set (ADR-034 §5), otherwise the compiled constant. Unreadable params (which
	// should never happen after genesis) fall back to the constant: the ungoverned behaviour, never a refusal.
	window := jurorFreshnessBlocks
	if p, pErr := k.Params.Get(ctx); pErr == nil {
		window = effectiveJurorFreshness(p)
	}
	return k.eligibleAsJurorInWindow(ctx, minerId, height, window)
}

// eligibleAsJurorInWindow is the same decision, with the window already read.
//
// The predicate above is called once per registered miner, inside a draw that runs once per job
// settled in the block. Reading the params inside it multiplied a store read by miners x jobs, in an
// EndBlocker that has no gas to bound it: the measured cost was about 9.7 s per block at 3000 miners x
// 3000 jobs, for a chain whose block interval is about 5 s. A caller that already holds the params
// passes them here; the decision is unchanged, only how often the same value is read.
func (k Keeper) eligibleAsJurorInWindow(ctx context.Context, minerId string, height, window uint64) bool {
	if last, ok := k.lastCommitHeight(ctx, minerId); ok {
		return height <= last || height-last <= window
	}
	// No commit known: new (innocent), or registered and sterile (the target)? Only the registration
	// height decides.
	reg, rErr := k.MinerRegisteredHeight.Get(ctx, minerId)
	if rErr != nil {
		return true // neither date: imported/genesis state, so permissive
	}
	// A new miner gets the same window to produce its first commit: it can mine and judge without a
	// history, so vitality never becomes an entry filter.
	return height <= reg || height-reg <= window
}

// pruneInactiveMiners evicts inert miners.
//
// Three required properties, all held here:
//   - Objective: the only criterion is on-chain and verifiable by anyone (no commit anchored for
//     `minerPruneBlocks`). Nobody decides who leaves.
//   - Non-discretionary: no governance parameter designates a target. A chosen removal would be a
//     censorship vector, since an honest competitor could be evicted.
//   - Non-confiscatory: the remaining bond returns to its creator. Inactivity is an absence, not a
//     fault: this evicts and does not punish. (What was already slashed stays with the module.)
//
// Side effect: the cost of the silent-identity attack is no longer a one-off payment, since the
// attacker must register and bond again, indefinitely.
func (k Keeper) pruneInactiveMiners(ctx context.Context, h int64) error {
	if h <= 0 {
		return nil
	}
	// Governed window when set (ADR-034 §5). Unreadable params: nothing is evicted (fail closed:
	// eviction is destructive, and doubt abstains, the same rule as H4).
	p, pErr := k.Params.Get(ctx)
	if pErr != nil {
		return nil
	}
	pruneWindow := effectivePruneWindow(p)
	height := uint64(h)
	var doomed []string
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		// ADR-034 F1: with no commit, the registration height is the reference; otherwise a
		// registered, sterile identity would never be evicted (no entry, no applicable window), while it
		// is the one this guard targets.
		ref, known := k.lastCommitHeight(ctx, key)
		if !known {
			reg, rErr := k.MinerRegisteredHeight.Get(ctx, key)
			if rErr != nil {
				return false, nil // neither date: imported state, touch nothing
			}
			ref = reg
		}
		if height <= ref || height-ref <= pruneWindow {
			return false, nil
		}
		// ADR-034 F3: never evict a miner that still carries an open obligation.
		// H4: on a store error, abstain (fail closed) and never destroy under doubt.
		if obligated, oErr := k.hasOpenObligation(ctx, key); oErr != nil || obligated {
			return false, nil
		}
		doomed = append(doomed, key)
		return false, nil
	})
	if err != nil {
		return err
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	for _, id := range doomed {
		m, gErr := k.Miner.Get(ctx, id)
		if gErr != nil {
			continue
		}
		// Refund before removal, and a failed refund cancels the removal: an evicted miner whose bond
		// stayed with the module would be a silent confiscation.
		if m.Stake > 0 && m.Creator != "" {
			toBz, aErr := k.addressCodec.StringToBytes(m.Creator)
			if aErr != nil {
				continue
			}
			refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(m.Stake)))
			if sErr := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), refund); sErr != nil {
				sdkCtx.Logger().Error("inactive-miner prune: bond refund failed -> removal cancelled (never a silent confiscation)",
					"miner_id", id, "stake", m.Stake, "err", sErr.Error())
				continue
			}
		}
		if rErr := k.Miner.Remove(ctx, id); rErr != nil {
			continue
		}
		_ = k.MinerLastCommitHeight.Remove(ctx, id)
		// Same symmetry as the voluntary exit: registration wrote two dates, eviction forgets both.
		_ = k.MinerRegisteredHeight.Remove(ctx, id)
		_ = k.MinerLastPresentEpoch.Remove(ctx, id) // ADR-048 item 5: the third date leaves with the other two
		sdkCtx.Logger().Info("inactive miner evicted (bond refunded to its creator; inactivity is not a fault)",
			"miner_id", id, "stake_refunded", m.Stake, "height", h)
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"miner_pruned_inactive",
			sdk.NewAttribute("miner_id", id),
			sdk.NewAttribute("refunded_udndr", math.NewIntFromUint64(m.Stake).String()),
		))
	}
	return nil
}
