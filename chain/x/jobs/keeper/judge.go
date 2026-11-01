package keeper

import (
	"context"
	"errors"
	"strings"

	"cosmossdk.io/collections"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Consensus epoch 15: jurors are declared judges that vote.
//
// Until epoch 14 the jury was drawn among every vital, present miner of the frozen pool, and nothing
// on chain told a miner that runs a judge from one that does not. A seat that never votes raises the
// two-thirds bar (`auditRelativeBar`) and never helps reach it, so the draw was wide on purpose and
// most seats stayed mute. Since epoch 15 a seat belongs to a miner that declared
// a judge (`MsgDeclareJudge`) and that answers: its last answer, or its declaration, is inside
// `judge_verdict_window_blocks` of the draw height, and it has not been seated on
// `judge_missed_draws_max` concluded audits without answering. The primary draw (committee.go) is
// unchanged: a judge is drawn as a primary like any present miner.
//
// The shape is the one the juror vitality rule kept: "summoned and never voted" is counted (the missed draws),
// "never summoned" is not. A judge excluded on a quiet network comes back by declaring again, which
// rewrites its declaration height and resets its counter, so no exclusion is for life.
// The bar does not move: `auditRelativeBar` is unchanged in text and in callers, and a
// seat already anchored keeps its place in that audit's denominator whatever happens to its judge.

// judgeMissedDrawsMax is the compiled default of `judge_missed_draws_max`, selected at 0. A count of
// draws, never a duration: one missed draw is a hiccup (a reveal that never came, a judge busy on
// another job), two can be a restart, three concluded audits with no answer in between is a machine
// that does not answer. The counter resets on any answer, so a judge that answers one audit in three
// is never excluded by it.
const judgeMissedDrawsMax = uint64(3)

// effectiveJudgeVerdictWindow is the window a judge's last answer or declaration must fall in. At 0
// it is the juror freshness window itself (`effectiveJurorFreshness`, governed or compiled), so the
// two windows cannot cross; `Params.Validate` refuses a posted window below a posted freshness.
func effectiveJudgeVerdictWindow(p types.Params) uint64 {
	if p.JudgeVerdictWindowBlocks > 0 {
		return p.JudgeVerdictWindowBlocks
	}
	return effectiveJurorFreshness(p)
}

func effectiveJudgeMissedDrawsMax(p types.Params) uint64 {
	if p.JudgeMissedDrawsMax > 0 {
		return p.JudgeMissedDrawsMax
	}
	return judgeMissedDrawsMax
}

// judgeFilter is the part of the candidate predicate that comes from the params, read once per draw
// like the vitality window (`drawAuditCommittee`): a draw runs once per job settled in the block,
// inside an EndBlocker with no gas. Unreadable params select the compiled values, the ungoverned
// behaviour, as the window and the presence do on the same path.
type judgeFilter struct {
	window    uint64
	missedMax uint64
}

func judgeFilterOf(p types.Params, paramsReadable bool) judgeFilter {
	if !paramsReadable {
		return judgeFilter{window: jurorFreshnessBlocks, missedMax: judgeMissedDrawsMax}
	}
	return judgeFilter{window: effectiveJudgeVerdictWindow(p), missedMax: effectiveJudgeMissedDrawsMax(p)}
}

// eligibleAsJudgeAt reports whether `minerId` may be seated on a jury drawn at `height`.
// Three reads, the one that refuses first.
//
// No declaration: not a judge, whatever its history.
// A counter at the cap: excluded until an answer on an audit it is still anchored on, or a
// new declaration. Then the window, on the last answer first and on the declaration second, which is
// the trial a declaration opens. A declared judge with neither date is a state no path produces (both are
// written together, and the import re-dates); it is not a candidate, since a missing date on a
// security criterion is never read as recent.
func (k Keeper) eligibleAsJudgeAt(ctx context.Context, minerId string, height uint64, f judgeFilter) bool {
	declared, err := k.MinerJudge.Has(ctx, minerId)
	if err != nil || !declared {
		return false
	}
	if missed, mErr := k.MinerMissedDraws.Get(ctx, minerId); mErr == nil && missed >= f.missedMax {
		return false
	}
	if last, lErr := k.MinerLastVerdictHeight.Get(ctx, minerId); lErr == nil && withinWindow(height, last, f.window) {
		return true
	}
	if decl, dErr := k.MinerJudgeDeclaredHeight.Get(ctx, minerId); dErr == nil && withinWindow(height, decl, f.window) {
		return true
	}
	return false
}

// withinWindow is the comparison `eligibleAsJurorInWindow` applies to its own dates, written once: a
// date at or after the height is recent (nothing can be stale before it exists), otherwise its age
// must fit the window.
func withinWindow(height, date, window uint64) bool {
	return height <= date || height-date <= window
}

// declareJudge writes a `judge = true` declaration: the pool entry, the declaration height, and the
// counter back to 0 (absent). A judge that declares again while already in the pool reopens its
// trial window and clears its missed draws: that is the way back for a judge the count excluded.
func (k Keeper) declareJudge(ctx context.Context, minerId string, height uint64) error {
	if err := k.MinerJudge.Set(ctx, minerId); err != nil {
		return err
	}
	if err := k.MinerJudgeDeclaredHeight.Set(ctx, minerId, height); err != nil {
		return err
	}
	return k.MinerMissedDraws.Remove(ctx, minerId)
}

// clearJudgeState forgets the four entries. Called by a `judge = false` declaration, by the voluntary
// exit and by the eviction: a judge that left is not a judge, and an id that comes back (derived from
// the address) starts with no judge history. Errors are ignored for the reason the exit
// gives for its other removals: on that path the bond refund has already been sent, and a failed
// bookkeeping removal must not undo a payment.
func (k Keeper) clearJudgeState(ctx context.Context, minerId string) {
	_ = k.MinerJudge.Remove(ctx, minerId)
	_ = k.MinerJudgeDeclaredHeight.Remove(ctx, minerId)
	_ = k.MinerLastVerdictHeight.Remove(ctx, minerId)
	_ = k.MinerMissedDraws.Remove(ctx, minerId)
}

// anchoredSeats returns the ids anchored under one committee key, in the anchored order. The order
// is the stored string's, never a map's, so every validator walks the same sequence.
func (k Keeper) anchoredSeats(ctx context.Context, key string) []string {
	raw, err := k.AuditCommittee.Get(ctx, key)
	if err != nil || strings.TrimSpace(raw) == "" {
		return nil
	}
	var out []string
	for _, id := range strings.Split(raw, ",") {
		if id = strings.TrimSpace(id); id != "" {
			out = append(out, id)
		}
	}
	return out
}

// juryAnswer is the one definition of "this commit is that seat's answer", read when the commit lands
// (`noteJudgeAnswer`) and when the audit concludes (`seatAnswered`). An answer is a vote a tally
// counts. An audit seat answers with a verdict (`<jobId>__verdict__<mid>`, the key `auditVerdictTally`
// reads for the audit jury only). A redo seat answers with a re-commit (`<jobId>__redo__<mid>`) that
// `AdjudicateDispute` counts: a vector it can parse, from a seat that wrote no contested commit on the
// job (`contestedAuthor`, one point read of the set `commitAuthorsOf` walks, which the tally excludes
// because nothing stops a seated juror from writing one after its draw). This runs on every re-commit
// and on every seat at the conclusion, so a walk of the job's commits here cost the commits times the
// seats; the point read costs the same at any jury size. The tally's other exclusion, the original
// committee, is not read again here: `anchorRedoCommittee` drew the redo seats outside it, from the
// same anchored work committee trimmed to the governed size, so a redo seat falls in it only if
// `work_committee_size` was lowered before the redo draw and raised again before the adjudication.
//
// Everything else is no answer: a verdict from a seat that is only on the redo jury and a re-commit
// from a seat that is not on it (`CreateCommit` accepts any role key from the operator, but no tally
// reads those, so dating them would refresh a judge's window for a byte that counts nowhere), and the
// redo-verdict form `<jobId>__redo__verdict__<mid>` that `ParseCommitKey` accepts and no tally reads.
// Two definitions would make one commit an answer when it is written and a missed draw when the audit
// concludes. One definition still reads the state of its moment: a judge seated on both juries that
// writes a contested commit after its re-commit was dated is billed at the conclusion, where the tally
// leaves the re-commit out too.
func (k Keeper) juryAnswer(ctx context.Context, kind types.CommitKeyKind, jobId, mid string, onAudit, onRedo bool, result string) (bool, error) {
	switch kind {
	case types.CommitKeyVerdict:
		return onAudit, nil
	case types.CommitKeyRedo:
		if !onRedo || parseIntVec(result) == nil {
			return false, nil
		}
		author, err := k.contestedAuthor(ctx, jobId, mid)
		if err != nil {
			return false, err
		}
		return !author, nil
	}
	return false, nil
}

// anchoredOn reports whether `mid` holds a seat under one committee key. A missing anchor is no seat.
func (k Keeper) anchoredOn(ctx context.Context, key, mid string) bool {
	members, err := k.AuditCommittee.Get(ctx, key)
	return err == nil && committeeContains(members, mid)
}

// seatAnswered reports whether a juror anchored on the audit jury (`onAudit`), on the redo jury
// (`onRedo`) or on both has a commit on `jobId` that `juryAnswer` counts as its answer. An error is a
// store that could not be read, which says nothing about the answer.
func (k Keeper) seatAnswered(ctx context.Context, jobId, mid string, onAudit, onRedo bool) (bool, error) {
	for _, c := range []struct {
		kind types.CommitKeyKind
		key  string
	}{
		{types.CommitKeyVerdict, jobId + "__verdict__" + mid},
		{types.CommitKeyRedo, jobId + "__redo__" + mid},
	} {
		commit, err := k.Commit.Get(ctx, c.key)
		if errors.Is(err, collections.ErrNotFound) {
			continue
		}
		if err != nil {
			return false, err
		}
		answered, err := k.juryAnswer(ctx, c.kind, jobId, mid, onAudit, onRedo, commit.ResultCommit)
		if err != nil {
			return false, err
		}
		if answered {
			return true, nil
		}
	}
	return false, nil
}

// recordMissedDraws counts, once per audit, the judges seated on `jobId` that never answered.
// It runs inside `clearAuditCommittee`, so every point where an audit leaves the
// disputed state (a real resolution of `resolveDisputedAudit`, `AdjudicateDispute`,
// `unwindStrandedRetention`, `ResolveDispute`) counts before the anchors are purged, and a second
// pass finds no seat: the purge is what makes "once" hold without a marker. A deferral calls neither.
//
// The walk is the audit jury in its anchored order. A seat held only on the re-adjudication jury is
// never billed: the kit has no redo path (services/judge_worker.py reads `query jobs
// audit-committee` without `--redo` and writes verdict keys only), so each such seat would be a missed
// draw that no judge running the kit can avoid. A re-commit is still an answer (`noteJudgeAnswer`
// dates it). A judge seated on both juries is counted once, and its answer is read against both
// seats, as `noteJudgeAnswer` read it when the commit landed. Three audit seats are skipped. A seat
// whose miner left the registry: exit and eviction clear the judge state, and a counter on a
// departed id would be residue that outlives its miner. A seat whose miner is not a
// declared judge at the conclusion (declared `false` while the audit was open, or never declared):
// the counter is judge state, `judge = false` forgets all four entries, and a count on an identity
// that is not a judge would be exported with the pool it is not in. A seat drawn before the
// declaration its judge holds now (`seatsPredateDeclaration`): it did not summon a judge. Every seat
// open at the upgrade height was drawn under the epoch 14 rule, before any declaration existed, and
// a judge that declares again starts afresh, so the seats it held before are not billed
// when they conclude. The anchor keeps its denominator in all three cases. Best effort on
// the store, like the purge next to it: the counter is an eligibility input, never money, and a
// missed write leaves a judge one draw more lenient, which the next conclusion corrects.
func (k Keeper) recordMissedDraws(ctx context.Context, jobId string) {
	audit := k.anchoredSeats(ctx, jobId)
	redo := k.anchoredSeats(ctx, redoAnchorKey(jobId))
	onAudit := make(map[string]bool, len(audit))
	for _, mid := range audit {
		onAudit[mid] = true
	}
	onRedo := make(map[string]bool, len(redo))
	for _, mid := range redo {
		onRedo[mid] = true
	}
	counted := map[string]bool{}
	for _, mid := range audit {
		if counted[mid] {
			continue
		}
		counted[mid] = true
		if registered, err := k.Miner.Has(ctx, mid); err != nil || !registered {
			continue
		}
		if declared, err := k.MinerJudge.Has(ctx, mid); err != nil || !declared {
			continue
		}
		// An unreadable answer is not a missed draw: the seat is skipped, like an unreadable registry.
		if answered, err := k.seatAnswered(ctx, jobId, mid, onAudit[mid], onRedo[mid]); err != nil || answered {
			continue
		}
		if k.seatsPredateDeclaration(ctx, jobId, mid, onAudit[mid], onRedo[mid]) {
			continue
		}
		n, _ := k.MinerMissedDraws.Get(ctx, mid) // absent is 0: the first missed draw of this id
		_ = k.MinerMissedDraws.Set(ctx, mid, n+1)
	}
}

// seatsPredateDeclaration reports whether every seat `mid` holds on `jobId` was anchored strictly
// below the height of the declaration it holds now. The proof is the date `anchorCommittee` writes
// with each seat (`CommitteeSince`); a seat with no date, a judge with no declaration height, or a
// seat anchored in the block of the declaration proves nothing, and the seat is billed. One seat
// drawn under the current declaration is enough to bill a judge seated on both juries. After a
// genesis import the dates compared are the re-anchored ones: the declaration at the import anchor
// (`frozenPoolAnchor`, 0 for an import at height 0 or 1) and the seat by its age. A seat carried
// across a zero-height import is billed (both dates read 0); after an import higher up, a seat whose
// re-anchored date falls below the anchor is not.
func (k Keeper) seatsPredateDeclaration(ctx context.Context, jobId, mid string, onAudit, onRedo bool) bool {
	declared, err := k.MinerJudgeDeclaredHeight.Get(ctx, mid)
	if err != nil {
		return false
	}
	keys := make([]string, 0, 2)
	if onAudit {
		keys = append(keys, jobId)
	}
	if onRedo {
		keys = append(keys, redoAnchorKey(jobId))
	}
	if len(keys) == 0 {
		return false
	}
	for _, key := range keys {
		since, sErr := k.CommitteeSince.Get(ctx, key)
		if sErr != nil || since >= declared {
			return false
		}
	}
	return true
}

// noteJudgeAnswer dates a juror's answer and clears its missed draws. It is
// called by `CreateCommit` on every commit; it moves the two values only when the key is the answer
// of a seat that job's anchored juries give the miner (`juryAnswer`): a verdict from the audit jury,
// or a re-commit from the redo jury that the adjudication counts. The membership is read from the anchors and
// from nothing else, so a commit posted after the conclusion, when `clearAuditCommittee` has purged
// them, moves neither value: a late blind vote on a dead audit buys nothing. The miner need not be a
// declared judge at that moment: a seat anchored before its declaration, or kept after a
// `judge = false` (the anchor keeps its denominator), still answers, and the date is read only when
// the miner is declared at its next draw.
func (k Keeper) noteJudgeAnswer(ctx context.Context, parsed types.ParsedCommitKey, result string, height uint64) error {
	if !parsed.JobScoped() {
		return nil
	}
	onAudit := k.anchoredOn(ctx, parsed.JobId, parsed.MinerId)
	onRedo := k.anchoredOn(ctx, redoAnchorKey(parsed.JobId), parsed.MinerId)
	answered, err := k.juryAnswer(ctx, parsed.Kind, parsed.JobId, parsed.MinerId, onAudit, onRedo, result)
	if err != nil {
		return err
	}
	if !answered {
		return nil
	}
	if err := k.MinerLastVerdictHeight.Set(ctx, parsed.MinerId, height); err != nil {
		return err
	}
	return k.MinerMissedDraws.Remove(ctx, parsed.MinerId)
}
