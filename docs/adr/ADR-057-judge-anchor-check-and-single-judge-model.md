# ADR-057 — Judge anchor check and a single judge model: the check that kept every kit juror silent, the model every judge serves, and the guard that holds a divergence slash back

**Status:** Accepted. Records a decision of the project owner of 2026-10-07 (decision 1) and three of
2026-10-09 (decisions 2 to 4). The section *What the anchor check does not prove* is this record's
analysis: it decides nothing, and binding a reveal to the delivered answer is not designed.
**Implementation:** Decisions 2 to 4 change what an operator runs (the miner software, the judge, the
kit's compose file) and ship under the next `kit_version` ([ADR-050](ADR-050-release-versioning.md)).
Decision 1 is how the kit seats a judge since the update of 2026-10-07 recorded in
[ADR-031](ADR-031-allow-list-modeles-juges.md); this record writes down what it costs. Nothing here
changes consensus.

## Context

- **How a juror reaches a verdict.** On an audited job the primary reveals its prompt and answer to the
  jury (decision 4). A juror running the kit's judge first checks that the revealed answer is the one the
  primary anchored: the primary's commit carries an embedding of its answer, the juror computes the
  embedding of the revealed answer, and it compares the two by cosine against
  `services/judge_worker.py::ANCHOR_IDENTITY_COS`
  (`services/judge_worker.py::reveal_matches_anchor`). An answer it cannot check is an abstention,
  never a vote. Then `services/judge_worker.py::decide_verdict` returns valid, invalid or no
  verdict, and names the stage it left at: `coherence` (the answer is not a coherent attempt at the
  request), `slash` (three reference answers the judge generates agree with one another and diverge from
  the answer), `legacy` (one reference), and several stages that abstain.
- **The miner's anchor.** The kit's miner embeds its answer with the embedding model of its serving engine
  and keeps the first 384 values, the chain's bound on an anchored vector
  (`services/modea/miner.py::_quantize_embed`; the kit's compose file sets
  `DENDRA_EMBED_MODE=backend`).
- **What the juror computed, in every kit release up to v0.2.0.** `reveal_matches_anchor` called the miner
  module's text embedding, which outside the sentence-transformers mode returns a 64-value word-hash vector
  (`services/modea/miner.py::_feature_embed`). A 64-value vector never has the length of a
  384-value anchor, so the check had no answer and the juror abstained on every revealed audit, from
  2026-08-15. No juror running one of those kits could post a verdict on a revealed audit, and its log
  pointed the operator at another cause.
- **One judge model.** The model registry names one judge model, `audit_judge_model`. The kit's judge
  reads it first (`services/judge_worker.py::resolve_judge_model`), and every judge the kit seats
  runs it on the CPU (ADR-031, update of 2026-10-07). A jury of kit judges is therefore a jury of one
  model: the residual risk ADR-031 §7 accepted, an operator fleet converging on one judge model, is the
  kit's default. Consensus reads no judge model.
- **What a jury of one model did on a bench.** ADR-031 §2 records false-slash rates between 1 % and 19 %
  for a single-model jury, and zero for a heterogeneous mix. The one bench run of a jury of the
  mixture-of-experts judge alone, on 2026-07-03, judged answers of `llama3.1:8b`: one honest job of 96
  received four invalid votes, about 1 %, where two runs mixing the mixture-of-experts with
  `mistral-nemo` gave none. Four invalid votes reach
  `chain/x/jobs/keeper/antievasion.go::auditRelativeBar` — the larger of the jury floor
  (`chain/x/jobs/keeper/antievasion.go::effectiveSlashFloor`) and two thirds of the anchored seats
  still registered — on a jury of up to six seats when `audit_min_quorum` is 4. That run predates two
  corrections of `decide_verdict` that turn unreadable judgements into abstentions and rests on one event
  in 96; the rate of the present code is not measured.
- **What a false conviction costs.** A conviction slashes `slash_leak_bps` of the primary's stake
  (`chain/x/jobs/keeper/antievasion.go::slashCheatedPrimary`). A bond cannot be topped up
  (`chain/x/jobs/keeper/msg_server_miner.go::UpdateMiner` refuses a stake change) and a resolved job
  cannot be disputed (`chain/x/jobs/keeper/msg_server_dispute.go::DisputeVerdict`): an honest miner
  convicted by correlated jurors has no recourse.

Correcting the anchor check alone would therefore open, for the first time, the path by which a jury of
one model convicts. Decisions 2 and 3 ship together for that reason.

## Decisions

1. **Every judge serves the one model the chain names** (owner, 2026-10-07). The kit seats a judge on the
   CPU only, with the model `audit_judge_model` names, which the judge reads and consensus does not. The
   correlated error ADR-031 §7 accepted as a residual risk is accepted for this testnet, bounded by
   decision 3. A jury of several models needs a rule the chain can check and a record of its own (*Open*).
2. **The juror checks the anchor with the miner's own function** (owner, 2026-10-09).
   - One function computes the embedding of an answer, for the miner that anchors it and for the juror
     that checks a reveal (`services/modea/miner.py::answer_embedding`).
   - The juror computes it under its own setting. It never picks a method from the shape of the anchor,
     which the audited miner supplies.
   - Two vectors of different length are still an abstention, and `ANCHOR_IDENTITY_COS` is not lowered.
   - A text that fits in the context of the embedding model keeps exactly the computation of the earlier
     kits, so every anchor already on chain stays checkable. A text the engine refuses for its length was
     not anchorable at all: Ollama answers its embedding route with HTTP 500, "the input length exceeds
     the context length", and the miner refused to commit. With this decision such a text is cut at the
     ASCII whitespace nearest its middle, again for each half the engine still refuses, and the vectors
     of the pieces are averaged, weighted by their length in characters
     (`services/modea/miner.py::embed_split_point`). Both sides run the same code, so both cut at
     the same places as long as their engines refuse the same texts; an engine that truncated a long text
     in silence would give a different vector, and the juror would abstain. That is the direction in which
     this rule is allowed to fail.
   - Measurement of 2026-10-09 on the owner's machine, in temporary containers, with Ollama 0.32.1 and
     `nomic-embed-text` (768 values, of which the first 384 are kept, a context of 2 048 tokens): with the
     same function on a GPU and on a CPU, the lowest cosine over ten real answers was 0.9999948, and the
     length refusal began beyond about 1 600 words. The machine does not bring the check near its
     threshold.
3. **A divergence abstains until the owner lifts the guard** (owner, 2026-10-09).
   - `DENDRA_JUDGE_DIVERGENCE_SLASH` absent or 0, the kit's default: an invalid result of `decide_verdict`
     that left at the stage `slash` or `legacy` is posted as no verdict, an abstention, never as an
     invalid vote.
   - An invalid result at the stage `coherence` is unchanged: an answer that is not a coherent attempt is
     still voted invalid, and convicted when the jury reaches `auditRelativeBar`.
   - Valid votes and abstentions are unchanged.
   - The kit's instruction moves to 1 only by a written decision of the project owner, after a dated C3
     pass in which a jury of the judge model alone judges honest answers of every model the network serves,
     on the programme's request forms (`services/final_season_generator.py::FORMS`), and counts
     each honest job's invalid votes against `auditRelativeBar`.
   - The guard binds the kit's judge, not the chain. An operator who sets the variable to 1 on its own
     machine lifts it for its own judge, and a juror running other software votes as it likes: the chain
     counts either vote the same way.
   - Its price: while it holds, a cheat whose answer is coherent is not convicted. Its fee stays held
     (`hold_bps`) and unwinds to the client after `audit_unwind_blocks`
     ([ADR-044](ADR-044-le-silence-du-mineur-ne-se-punit-pas-il-se-rend-sans-valeur.md)), unless the
     jury upholds the answer: a kit judge votes valid when its references agree with the answer (stage
     `sc-valid`) or when it proves the request ambiguous (stages `sc-diverge` and `multiok`, voted valid by
     `services/judge_worker.py::abstain_to_vote` while `DENDRA_JUDGE_ABSTAIN_VOTE` keeps its
     default, 1), and an upheld answer is paid. Under decision 18 of
     [ADR-047](ADR-047-final-testnet-season-reward-programme.md) the season pays an unwound programme
     request whose answer was recorded and cleared by a grade
     (`services/final_season_rules.py::UNWOUND_AUDIT_WORK`). The unwind is the outcome every audit
     with a revealed answer already had while no kit juror could vote.
   - A juror under the guard votes valid, invalid for an incoherent answer, or abstains. Paying a verdict
     that no concluded audit confirms would then reward a systematic valid vote: the season pays a juror
     only for a verdict consistent with the outcome of a concluded audit
     (`services/final_season_facts.py::audit_outcome`).
4. **A reveal is sealed for the anchored jury** (owner, 2026-10-09). The kit's miner re-seals the prompt
   and the answer of an audited job for the jurors the chain anchored for that audit
   (`GET /dendra/jobs/v1/audit_committee/{job_id}`), and for no other miner. Each copy is sealed to the
   encryption key the juror registered on chain; the chain accepts a registration without one, and for
   such a juror the miner seals to the key the relay serves for it
   (`services/reveal_helpers.py::committee_pubs`), which a relay that replaces that key can open.
   That copy is the one exception to "the jury only"; `SECURITY.md` and the README state it. The kit's
   juror judges the audits it sits on; an audit whose jury cannot be read is judged rather than skipped, since an unknown
   membership is not a refusal. A miner running a kit of an earlier release seals each reveal for every
   other registered miner that has an encryption key, so the public texts name the kit release from which
   a reveal reaches the jury only.

## What the anchor check does not prove

- A cosine says that two texts are close in meaning, not that they are the same text. In the measurement
  of decision 2, a long answer in which one figure was changed kept a cosine of 0.99998 with the original,
  far above `ANCHOR_IDENTITY_COS`. A primary can therefore reveal to its jury an answer that differs from
  the one it delivered in a detail the embedding does not see — a figure, a name, a sign — and the jury
  judges the revealed answer.
- Binding the reveal to the delivered answer needs a hash of the answer in the commit, a field the chain
  does not have. Adding it changes the state machine, hence `consensus_epoch` and the minor release number
  (ADR-050). It is neither designed nor decided.
- Until then the anchor check filters out a reveal unrelated to the anchored answer. It is not a proof that
  the reveal is that answer.

## Amendment, same release (2026-10-09): what the juror grades, and how often

The review of decisions 2 and 3 found three ways a guarded juror could still vote on something it had not
checked, or vote VALID by sampling noise. All three are closed in the kit that carries this record
(`services/judge_worker.py`):

- **A question that cannot be verified is not graded.** `judge_worker.py::judge_revealed` checks the
  revealed question against the commitment the primary anchored (`judge_worker.py::prompt_check`) before
  anything else. A commit that could not be read abstains as `prompt-unreadable` and is tried again; any
  other question that opens no commitment leaves only the coherence stage, which reads the answer alone,
  and abstains as `prompt-unverified` otherwise. An answer is never graded against a question its author
  may have chosen after the fact.
- **A retry is not a second draw at an acquittal.** The stages `sc-unreadable` and `multiok-unreadable`
  are reached only after the juror's references agreed with each other. Once an audit has ended at one of
  them, its later attempts in the same process may not turn an ambiguity into a VALID vote
  (`judge_worker.py::ambiguity_vote_allowed`, `AGREED_REFS_STAGES`). Under decision 3, VALID is the one
  vote noise could buy.
- **The coherence reading is made once.** The coherence stage is the one stage that still votes INVALID
  under decision 3. A definite reading of one answer, for one audit and one judge model, is kept for the
  life of the process (`judge_worker.py::coherence_memo`), so retries do not draw again at a false
  "word salad".

The kit's compose forwards `DENDRA_JUDGE_ABSTAIN_VOTE` to the miner's container, empty by default (an empty
value is the juror's default). Before this release, only the root compose of the repository set it.

## Open

- **Several judge models.** Two models of the judge allow-list chosen per machine by a rule the chain can
  check, with a registry key in place of `audit_judge_model`: a record of its own, for the mainnet genesis
  or a later consensus epoch.
- **Whether a jury concludes.** Decision 2 removes the reason a kit juror could not vote. A conclusion
  still needs `auditRelativeBar` votes from the seats of one jury, and a seat whose miner runs no judge
  votes nothing ([ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md), finding 4). How many seated
  identities run a judge is a reading — `verified.judges_declared` of the capacity registry and the
  committee of each audit — never a property this record could state.
- **The comment of the field.** `chain/proto/dendra/modelregistry/v1/params.proto` describes
  `audit_judge_model` as read by no execution path. No consensus path reads it; the kit's judge does. Its
  wording follows the next regeneration of the protobuf files.
- **Lifting decision 3** needs the pass that decision names, dated and written by the owner.

## Consequences

- With the kit release that carries decisions 2 to 4, a kit juror can post a verdict on a revealed audit.
  A conviction on divergence alone needs the owner's written lifting of decision 3; a conviction for an
  incoherent answer does not.
- A cheat whose answer stays coherent is not convicted while decision 3 holds (its price, above).
- Public texts say for whom each kit release seals a reveal, and never that a juror votes or that a jury
  concludes: both are readings.

## Version

Decisions 2 to 4 change what an operator runs and ship under a `kit_version` bump, whose release number
ADR-050 derives. Decision 1 needs no further change. The answer hash of *What the anchor check does not
prove* would move `consensus_epoch`; nothing in this record does.
