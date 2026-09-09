package keeper

import (
	"context"
	"strings"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-034 — MINER VITALITY.
//
// The problem this file exists to close: the only quality a miner used to have was "listed in the
// registry", and that is bought ONCE for `min_stake`. Two consequences follow:
//   (a) silent identities occupy jury seats without ever voting -> repeated no-quorum -> the payment
//       of HONEST miners is clawed back;
//   (b) nothing evicts those identities: `silence_slash` lives INSIDE `clawbackPayment`, which
//       presupposes a prior payment — a miner that produces nothing is never paid, hence never
//       punished. The protocol could punish a paid miner, not evict an inert one.
//
// The shared primitive is `MinerLastCommitHeight`: anchoring a commit is the only act that
// DEMONSTRATES production (signed by the operator, carrying a result). This moves the predicate from
// "registered" to "demonstrably alive", exactly as ADR-032 moved from "registered" to "drawn".

const (
	// jurorFreshnessBlocks — recency window to sit as a JUROR. DELIBERATELY VERY WIDE (~3 days at
	// 1 s/block). An honest miner under maintenance, in a power outage or migrating must NOT lose
	// juror eligibility — otherwise "bill an infrastructure outage to the honest party" reappears one
	// layer down, which is the very fault being corrected. The window is not there to rank good
	// miners against less good ones: it is there to exclude identities that have NEVER produced
	// anything or vanished long ago.
	jurorFreshnessBlocks = uint64(259200)

	// minerPruneBlocks — inactivity beyond which a miner is EVICTED. Wider still than the juror
	// window (~7 days): losing registration costs more than losing a jury seat (the accumulated
	// anti-Sybil `Demand` resets to zero). The order is deliberate: stop summoning first, evict only
	// much later.
	minerPruneBlocks = uint64(604800)
)

// effectiveJurorFreshness / effectivePruneWindow (ADR-034 §5) — both windows are GOVERNABLE. They are
// choices awaiting a measurement (p99 of inter-commit intervals on an open testnet), and a value known
// to need correction from data must not require a binary upgrade. Standard dormant pattern of this
// module: 0 (default) = compiled constant, behaviour STRICTLY unchanged; >0 = governed value. Unlike
// `auditCommitteeDrawSize`, staying a constant was not justified here: changing a window does not
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

// committeeContains — is miner `id` present in a comma-joined member list?
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

// baseJobIdFromCommitteeKey — recovers the underlying jobId of an AuditCommittee key. Three forms
// coexist: `<jobId>` (sampling), `<jobId>__redo` (re-adjudication), `<jobId>__humandispute` (human
// dispute marker). The role suffix is stripped to get back to the job.
func baseJobIdFromCommitteeKey(key string) string {
	key = strings.TrimSuffix(key, "__redo")
	key = strings.TrimSuffix(key, humanDisputeSuffix)
	return key
}

// clearAuditCommittee — REMOVES the committee anchorings of a job whose audit is OVER (ADR-034 C1/H5).
//
// AuditCommittee must be purged: four sites write it. If none removed it, two failures follow.
// (H5) it grows without bound -> the O(n) walk in `hasOpenObligation`, called once per expired miner in
// the EndBlocker, becomes O(n²) and can stall a block. (C1, worse) `hasOpenObligation` then sees the
// seats of CLOSED audits: any miner that ever sat once becomes un-evictable FOR LIFE, and the vitality
// guard only bites on miners NEVER drawn — the exact inverse of its target (the silent identities,
// which are precisely the ones holding seats). The three keys are therefore cleared at resolution: the
// audit is closed, nothing left to vote on, nothing left to immobilise. Best-effort — a missed removal
// only leaves a stale record, which the "job resolved" filter of `hasOpenObligation` already neutralises
// (defence in depth). The selection marker (C3) is removed too, moot once the job is resolved.
func (k Keeper) clearAuditCommittee(ctx context.Context, jobId string) {
	_ = k.AuditCommittee.Remove(ctx, jobId)
	_ = k.AuditCommittee.Remove(ctx, redoAnchorKey(jobId))
	_ = k.AuditCommittee.Remove(ctx, humanDisputeKey(jobId))
	// CommitteeSince is the twin of the seat: the date leaves with it. An orphan date would make
	// `oldest_lock_age` LIE (a lock reported on a closed audit). Only the two seat-bearing keys are
	// dated; removing a date that was never set is a no-op.
	_ = k.CommitteeSince.Remove(ctx, jobId)
	_ = k.CommitteeSince.Remove(ctx, redoAnchorKey(jobId))
	_ = k.AuditSelected.Remove(ctx, jobId)
}

// anchorCommittee — SETS a seat-bearing committee AND its anchoring height, together. A single
// derivation, like selectCommittee/selectCommitteeOrdered: both production sites (audit draw, redo
// draw) go THROUGH HERE — an anchoring without a date would make that juror invisible to the lock
// counter, which is precisely the debt the exit guard requires to be measurable. The `__humandispute`
// key does NOT go through here: it summons no seat and locks nobody.
func (k Keeper) anchorCommittee(ctx context.Context, key, members string) error {
	if err := k.AuditCommittee.Set(ctx, key, members); err != nil {
		return err
	}
	return k.CommitteeSince.Set(ctx, key, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight()))
}

// commitProvesAssignedWork — does this commit attest to ASSIGNED WORK (hence REAL vitality)?
//
// ADR-034 C2/M6 — VITALITY MUST NOT BE SELF-ISSUED. `CreateCommit` on its own requires neither that the
// miner be assigned to the job nor that it was drawn on the committee: without this guard,
// `create-commit "<REAL_jobId>__<sybilMiner>"` manufactures fresh vitality FOR THE PRICE OF GAS
// (1 tx per window), hence a perpetual juror seat AND immunity from eviction — the exact opposite of
// what vitality is meant to prove. The proof of assignment already exists on-chain: membership in the
// DRAWN committee. It is reused as the UNION of the three productive roles, so that an honest miner is
// NEVER stripped of its vitality (the cure would be worse than the disease):
//
//	(a) audit juror drawn and anchored          -> AuditCommittee[jobId]        (verdict)
//	(b) re-adjudication juror drawn and anchored -> AuditCommittee[jobId__redo] (redo AND its verdict:
//	    a re-adjudication IS production; not dating it starves the jury of eligible members)
//	(c) member of the job's assigned committee  -> assignedCommittee(jobId)     (primary/backup inference)
//
// A sybil that references a job where it was drawn in NONE of these roles gets NOTHING. The commit
// itself is never blocked (only the vitality DATE is): no legitimate flow breaks, the guard is not
// punitive.
func (k Keeper) commitProvesAssignedWork(ctx context.Context, commitKey, minerId string) (bool, error) {
	// THE SECOND DERIVATION IS GONE. It lived here, different from the commit handler's, and that gap
	// is what made the existence guard dangerous to write. One function now knows the key shapes; an
	// epoch marker, which names no job, proves no assigned work and leaves here as it did before.
	parsed, ok := types.ParseCommitKey(commitKey)
	if !ok || !parsed.JobScoped() {
		return false, nil
	}
	jobId := parsed.JobId
	// (a) + (b): ANCHORED jury seats — a direct read, never a re-draw.
	if members, err := k.AuditCommittee.Get(ctx, jobId); err == nil && committeeContains(members, minerId) {
		return true, nil
	}
	if members, err := k.AuditCommittee.Get(ctx, redoAnchorKey(jobId)); err == nil && committeeContains(members, minerId) {
		return true, nil
	}
	// (c) the job's assigned committee (primary/backup). Requires an EXISTING job and a revealed seed.
	has, hErr := k.Job.Has(ctx, jobId)
	if hErr != nil {
		return false, hErr
	}
	if !has {
		return false, nil
	}
	comm, cErr := k.assignedCommittee(ctx, jobId, CommitteeSize)
	if cErr != nil {
		// Seed not revealed / not derivable: assignment CANNOT be proven here. No date is written
		// (non-punitive: the miner regains vitality on its next assigned commit). Never blocking.
		return false, nil
	}
	return comm[minerId], nil
}

// hasOpenObligation — does the miner still carry a HELD payment or an UNRESOLVED audit SEAT?
// (ADR-034 F3/C1/H4)
//
// The destruction path is real: audit deferral is UNBOUNDED by design -> a job can stay in audit past
// the eviction threshold -> the miner, having nothing left to commit, becomes "inactive" -> it is
// evicted -> `releaseHeld` finds a miner that no longer exists and the held fee is lost. The
// countermeasure: refuse eviction while an obligation is open.
//
// ADR-034 H4 — FAIL CLOSED. Returns `(bool, error)`. A store error must NEVER be read as "no
// obligation" (fail open would authorise eviction under doubt, i.e. exactly the value destruction this
// guard exists to prevent). On error the caller abstains from evicting.
//
// ADR-034 C1 — only UNRESOLVED audit seats count. Without that filter, and with AuditCommittee left
// unpurged, a miner that sat once stays immune for life. Defence in depth with `clearAuditCommittee`
// (removal at resolution): if a removal is missed, this filter still holds.
func (k Keeper) hasOpenObligation(ctx context.Context, minerId string) (bool, error) {
	open := false
	// (a) a HELD fee on a job it is the primary of = money owed to it.
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
	// (b) a SEAT anchored in the audit committee of an UNRESOLVED job: evicting it would remove a
	// summoned juror from an audit in progress, making its quorum unreachable — the very no-quorum
	// this file corrects.
	//
	// ADR-034 bis — THIS CLAUSE ONLY HOLDS WHAT CAN STILL VOTE. Written against the living miner that
	// FLEES, it did not handle the dead one that STAYS: an identity whose key is lost never votes AND
	// can never leave — neither voluntarily (`DeleteMiner` requires the creator key) nor automatically
	// (pruning goes THROUGH HERE). Its seat then keeps the quorum bar high FOR THE WHOLE NETWORK,
	// forever.
	//
	// The reason to hold a juror is THAT IT VOTES. If it demonstrably cannot vote any more — that is,
	// if `eligibleAsJuror` ALREADY refuses to summon it onto a FRESH committee — holding it in an OLD
	// committee serves nobody and harms everyone. Holding someone the protocol refuses to draw is
	// incoherent. Clauses (a) and (c) are untouched: the AUDITED party never leaves, only the JUROR
	// role is released — so a slash does not become avoidable by waiting.
	//
	// WRITTEN ON THE PREDICATE, NEVER ON A PRESUMED ORDER OF THE WINDOWS. Justifying the rule by
	// "minerPruneBlocks > jurorFreshnessBlocks, therefore every prune candidate is expired by
	// construction" would hold for the COMPILED defaults and be false in general: both windows are
	// GOVERNABLE (effectiveJurorFreshness / effectivePruneWindow). Eligibility is therefore EVALUATED
	// rather than deduced, and the guard stays correct under any setting. `Params.Validate` also bounds
	// the order — but a guard does not lean on another guard: it stands on its own.
	if k.eligibleAsJuror(ctx, minerId, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())) {
		if err := k.AuditCommittee.Walk(ctx, nil, func(key string, members string) (bool, error) {
			if !committeeContains(members, minerId) {
				return false, nil
			}
			if job, err := k.Job.Get(ctx, baseJobIdFromCommitteeKey(key)); err == nil && jobIsResolved(job.State) {
				return false, nil // audit closed: this seat immobilises nothing any more
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
	// (c) PRIMARY of an unresolved DISPUTED job: an audit instance is still possible on its work —
	// the EXACT definition of "escrowed" (T1, held-summary). Letting it leave with its stake makes the
	// slash MOOT (the "exit before the verdict" dodge: lying goes back to 0-EV instead of -EV) and
	// manufactures the absent primary that decouples `upheld` from `optimisticCheated` in
	// AdjudicateDispute. Clause (a) covers this case only when a fee is HELD (hold_bps>0); clause (c)
	// covers it through the DISPUTE itself, independently of params. O(jobs) walk, placed LAST (the
	// most expensive) — same accepted cost class as the held-summary walk.
	if err := k.Job.Walk(ctx, nil, func(jobId string, job types.Job) (bool, error) {
		if job.MinerId == minerId && jobIsDisputed(job.State) && !jobIsResolved(job.State) {
			open = true
			return true, nil
		}
		// (d) A WORK-COMMITTEE MEMBER THAT HAS ALREADY ANSWERED, on a job that is still live.
		//
		// This clause walked HeldFee, audit seats and disputed jobs, and read NEITHER `Commit` NOR the
		// work committee -- so a member that had done the work was held by no clause at all.
		// MEASURED (exit_after_commit_strands_test.go): committee [sc sb sa], all three committed; `sb`
		// unregisters AFTER answering and is accepted; the committee is re-derived from the LIVE
		// registry, so it comes back [sc sa]; `sb`'s commit is still stored; `SettlePay` then refuses
		// for an incomplete committee and the job stays "open", holding its fee. NO ADVERSARY IS
		// NEEDED -- eviction goes through this same predicate, so an honest miner going quiet does it.
		//
		// ⛔ THE BOUND IS THE JOB, NEVER THE MINER'S GOODWILL, and that distinction is the whole design.
		// Clause (b) had to be relaxed because an identity whose key is lost can never vote and could
		// then never leave, keeping a quorum bar high for the whole network. The same trap is avoided
		// here by releasing on the JOB's own terminal states -- paid or settled, escrow returned,
		// resolved -- none of which requires anything further from this miner. It has already answered;
		// what is being held is the derivability of the committee that owes it a settlement.
		//
		// ⚠️ WHAT THIS DOES NOT BOUND, said plainly: a job that is served but never settled and whose
		// escrow is never returned holds its committee indefinitely. The mechanism that ends it is the
		// unconditional expiry appointment `OpenJob` books, not anything in this function -- and if that
		// appointment ever stops being booked, this clause becomes a permanent hold.
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

// lastCommitHeight — height of the last anchored commit. `ok=false` = NO entry known.
func (k Keeper) lastCommitHeight(ctx context.Context, minerId string) (uint64, bool) {
	h, err := k.MinerLastCommitHeight.Get(ctx, minerId)
	if err != nil {
		return 0, false
	}
	return h, true
}

// minerVitalAt — "was this identity producing at height h?", under the name that says so.
//
// It delegates to `eligibleAsJuror` because the PREDICATE is role-neutral: it asks whether a miner
// has anchored anything, or registered, within the freshness window. The name `eligibleAsJuror`
// predates its second caller — the WORK draw (`committee.go`) — and a jury-shaped name read from the
// assignment path is exactly the kind of mislabel this repository keeps paying for.
//
// ⚠️ WHY THE OLD NAME SURVIVES ANYWAY: it has 18 references across 8 files, six of them benches
// pinned to security behaviour. Renaming it inside a consensus-breaking change would mix a rename
// into a diff that has to be read for what it does to the protocol. The debt is named here, not
// hidden: the rename belongs to its own pass, with the benches green before and after.
func (k Keeper) minerVitalAt(ctx context.Context, minerId string, height uint64) bool {
	return k.eligibleAsJuror(ctx, minerId, height)
}

// eligibleAsJuror — may the miner sit on the audit jury?
//
// PERMISSIVE DEFAULT, DELIBERATELY. A MISSING entry means ELIGIBLE. Two innocent cases produce it:
// (i) a NEW miner that has not anchored a commit yet — vitality must never become an ENTRY filter, a
// newcomer must be able to mine and to judge without a history; (ii) a genesis import, the collection
// not being exported (cf. keys.go) — without this default a migration would empty every jury at once
// and stall verification network-wide.
//
// ADR-034 F1 — the missing-entry case must be split by the REGISTRATION height. Treating `!ok` as
// eligible unconditionally makes the window inapplicable forever when no commit entry exists: a
// registered identity that produces nothing then stays a juror FOR LIFE, while the guard bites only on
// miners that produced and then stopped — the honest population, the exact inverse of the target. The
// flaw is not in the principle but in the ENCODING: one signal (`!ok`) for two opposite states. The
// registration height separates them without weakening either safeguard.
func (k Keeper) eligibleAsJuror(ctx context.Context, minerId string, height uint64) bool {
	// Governed window when set (ADR-034 §5), otherwise the compiled constant. Unreadable params (which
	// should never happen post-genesis) -> the constant: the ungoverned behaviour, never a refusal.
	window := jurorFreshnessBlocks
	if p, pErr := k.Params.Get(ctx); pErr == nil {
		window = effectiveJurorFreshness(p)
	}
	return k.eligibleAsJurorInWindow(ctx, minerId, height, window)
}

// eligibleAsJurorInWindow — THE SAME DECISION, WITH THE WINDOW ALREADY READ.
//
// The predicate above is called once per registered miner, inside a draw that itself runs once per job
// settled in the block. Reading the params inside it therefore multiplied a store read by
// miners x jobs, in an EndBlocker that has no gas to bound it: the measured cost was ~9.7 s per block
// at 3000 miners x 3000 jobs, for a chain whose block interval is ~5 s. A caller that already holds the
// params passes them here; nothing about the decision changes, only how often the same value is read.
func (k Keeper) eligibleAsJurorInWindow(ctx context.Context, minerId string, height, window uint64) bool {
	if last, ok := k.lastCommitHeight(ctx, minerId); ok {
		return height <= last || height-last <= window
	}
	// No commit known: NEW (innocent) or REGISTERED AND STERILE (the target)? Only the registration
	// height decides.
	reg, rErr := k.MinerRegisteredHeight.Get(ctx, minerId)
	if rErr != nil {
		return true // NEITHER date => imported/genesis state => permissive
	}
	// A new miner gets the same window to produce its first commit: it can mine AND judge without a
	// history, so vitality never becomes an ENTRY filter.
	return height <= reg || height-reg <= window
}

// pruneInactiveMiners — EVICTION of inert miners.
//
// Three required properties, all held here:
//   - OBJECTIVE: the only criterion is on-chain and verifiable by anyone (no commit anchored for
//     `minerPruneBlocks`). Nobody "decides" who leaves.
//   - NON-DISCRETIONARY: no governance parameter designates a target. A chosen removal would be a
//     CENSORSHIP vector — an honest competitor could be evicted.
//   - NON-CONFISCATORY: the remaining bond RETURNS to its creator. Inactivity is not a fault, it is an
//     absence: this evicts, it does not punish. (What was already slashed stays with the module.)
//
// Side effect worth stating: the cost of the silent-identity attack stops being a ONE-OFF payment —
// the attacker must re-register, hence re-bond, indefinitely.
func (k Keeper) pruneInactiveMiners(ctx context.Context, h int64) error {
	if h <= 0 {
		return nil
	}
	// Governed window when set (ADR-034 §5). Unreadable params -> NOTHING IS EVICTED (fail closed:
	// eviction is destructive, doubt abstains — the same rule as H4).
	p, pErr := k.Params.Get(ctx)
	if pErr != nil {
		return nil
	}
	pruneWindow := effectivePruneWindow(p)
	height := uint64(h)
	var doomed []string
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		// ADR-034 F1: with no commit, the REGISTRATION height is the reference — otherwise a
		// registered, sterile identity is NEVER evicted (no entry = no applicable window), while it is
		// precisely the one this guard targets.
		ref, known := k.lastCommitHeight(ctx, key)
		if !known {
			reg, rErr := k.MinerRegisteredHeight.Get(ctx, key)
			if rErr != nil {
				return false, nil // neither date => imported state => touch nothing
			}
			ref = reg
		}
		if height <= ref || height-ref <= pruneWindow {
			return false, nil
		}
		// ADR-034 F3: NEVER evict a miner that still carries an open obligation.
		// H4: on a store error, abstain (fail closed) — never destroy under doubt.
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
		// Refund BEFORE removal, and a failed refund CANCELS the removal: an evicted miner whose bond
		// stayed with the module would be a silent confiscation.
		if m.Stake > 0 && m.Creator != "" {
			toBz, aErr := k.addressCodec.StringToBytes(m.Creator)
			if aErr != nil {
				continue
			}
			refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(m.Stake)))
			if sErr := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), refund); sErr != nil {
				sdkCtx.Logger().Error("inactive-miner prune: bond refund FAILED -> removal CANCELLED (never a silent confiscation)",
					"miner_id", id, "stake", m.Stake, "err", sErr.Error())
				continue
			}
		}
		if rErr := k.Miner.Remove(ctx, id); rErr != nil {
			continue
		}
		_ = k.MinerLastCommitHeight.Remove(ctx, id)
		// SAME SYMMETRY AS THE VOLUNTARY EXIT: registration wrote two dates, eviction forgets both.
		_ = k.MinerRegisteredHeight.Remove(ctx, id)
		sdkCtx.Logger().Info("INACTIVE miner evicted (bond refunded to its creator; inactivity is not a fault)",
			"miner_id", id, "stake_refunded", m.Stake, "height", h)
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"miner_pruned_inactive",
			sdk.NewAttribute("miner_id", id),
			sdk.NewAttribute("refunded_udndr", math.NewIntFromUint64(m.Stake).String()),
		))
	}
	return nil
}
