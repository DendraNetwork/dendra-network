# ADR-061 — Hardening the judge against an answer that steers it: isolated data, a screen that abstains, a multiplicity step the answer cannot satisfy, a same-fact step bounded by the request, and an answer bound to its commit

**Status:** Proposed. Nothing here is decided or implemented. Written 2026-10-16 for the owner's decision. It
answers the open items of the judge that the divergence guard of
[ADR-057](ADR-057-judge-anchor-check-and-single-judge-model.md) decision 3 waits for: that guard is not lifted
before the measurement of *The gate* is green, and before the C3 pass that decision names. The sections
*Threat model* and *Four known weaknesses* are this record's analysis.
**Implementation:** Kit only, under one `kit_version` bump ([ADR-050](ADR-050-release-versioning.md): the kit
alone moves, so the patch number moves). Nothing here changes consensus; `consensus_epoch` does not move.

## Context

- **A juror reads text the audited party wrote.** The revealed answer comes from the primary under audit, and
  the revealed question from the request it served, bound to the primary's commit by
  `services/judge_worker.py::prompt_check`. The judge model receives that text inside each stage
  prompt (`services/modea/judge.py::PROMPT`, `COHERENCE_PROMPT`, `RELEVANCE_PROMPT`, `MULTI_PROMPT`).
  `services/modea/judge.py::_clip` turns the field delimiters and the example arrow inside the data into
  neutral marks, and every prompt says after the data that an instruction written there is text to judge.
  An instruction inside the data still reaches the model.
- **Every juror runs one model, at temperature 0, on prompts anyone can read.** The kit seats the model
  `audit_judge_model` names (ADR-057 decision 1), the stage calls are made at temperature 0
  (`services/modea/judge.py::_generate`), and the prompts ship with the kit. An answer that steers that
  model once, tested offline as often as its author likes, steers every seat of the jury the same way.
- **What a juror posts is decided in one place.** `services/judge_worker.py::judge_revealed` checks
  the question, then the anchor cosine, then runs `services/judge_worker.py::decide_verdict`, then
  `services/judge_worker.py::divergence_guard` (an invalid from comparing the answer with the judge's
  own references is held while `DENDRA_JUDGE_DIVERGENCE_SLASH` is not exactly 1), then
  `services/judge_worker.py::abstain_to_vote` (the stages `sc-diverge` and `multiok` become a valid
  vote while `DENDRA_JUDGE_ABSTAIN_VOTE` keeps its default). The guard changes invalid votes only: a valid vote
  passes it unchanged.
- **A valid vote pays.** An upheld answer is paid, and the season pays a juror for a verdict consistent with
  the outcome of a concluded audit (ADR-057 decision 3, its price). A wrong answer that gets valid votes from
  its jury is paid as if it were right, and its jurors are paid for upholding it.

## Threat model

- **The adversary** is the primary under audit. It writes the answer, and, when it also sends the request
  (a client and a miner run by the same person), the question. It reads the kit, the prompts and the pinned
  model, and can try its texts against that model offline without limit.
- **Its goals**, in order of cost to the network: a valid vote for a wrong or cheap answer (the answer is
  paid and the audit vindicates it); an abstention where an invalid vote was due (the fee unwinds instead of a
  conviction, and under [ADR-047](ADR-047-final-testnet-season-reward-programme.md) decision 18 the work of
  an unwound programme request can still be paid,
  `services/final_season_rules.py::UNWOUND_AUDIT_WORK`).
- **Its means** are the text it controls: an instruction or a fake verdict addressed to the grader, a closed
  delimiter, a fake example, a role marker, a word salad padded with a readable sentence, several candidate
  answers where one is asked, the right fact next to a wrong claim, and a reveal edited after the anchor.
- **Out of scope here:** a juror that lies (the chain counts its vote like any other; ADR-057 decision 3,
  last point), a jury captured by identities (ADR-056 finding 1), and a forged chain or relay.

## Four known weaknesses

Each is played by the harness this record adds,
`services/tests/test_judge_prompt_harness.py`, through the shipped decision path. By default it runs
fake models whose readings are taken from the calibration notes above
`services/modea/judge.py::PROMPT` where those notes measured them, and marked as not measured where
they did not. A `test_pin_*` case records each weakness as the code decides it, and the change that closes
the weakness flips its pin.

1. **An instruction at the multiplicity step yields a valid vote.** At the slash threshold (three agreeing
   references, a divergent answer), `decide_verdict` stage 3d asks `services/modea/judge.py::llm_multi_ok`
   whether the question admits both the reference and the audited answer, and shows it the audited answer. A
   yes ends at the stage `multiok`, which `abstain_to_vote` posts as a valid vote, guard or no guard. The
   calibration notes count the breaches of the pinned model on that prompt: 2 of its 39 fills of the tuning
   set and 1 of its 14 held out, all in French; the other three prompts had none. Pin:
   `services/tests/test_judge_prompt_harness.py::test_pin_w1_steered_multiplicity_posts_valid`.
2. **A salad followed by a readable sentence escapes the coherence stage.** The coherence stage is the one
   stage whose invalid the guard lets through (`divergence_guard`, stage `coherence`). The calibration notes
   say that a salad followed by a neutral sentence read as coherent on 4 of 6 controls, and with a correct
   sentence on 6 of 6, and that the whole path was not run. With a neutral sentence the answer then reaches
   the divergence path and is held; with a correct one it can reach `sc-valid`. Pin:
   `services/tests/test_judge_prompt_harness.py::test_pin_w2_salad_followed_by_a_sentence`.
3. **The same-fact step is permissive.** `services/modea/judge.py::PROMPT` never sees the question,
   says that more or fewer details mean the same fact, and alone accepted 37 of 98 English salads and 49 of 98
   French ones (calibration notes). A single call of it decides the stage `sc-valid`, a valid vote
   (`decide_verdict`, stage (b)). An answer that carries the reference's fact next to a wrong claim, or among
   several candidates, can read the same; neither form was measured. Pin:
   `services/tests/test_judge_prompt_harness.py::test_pin_w3_reference_fact_with_a_wrong_claim_posts_valid`.
4. **A cosine is not a proof of identity.** The reveal passes the anchor check when its embedding reaches
   `services/judge_worker.py::ANCHOR_IDENTITY_COS`; one changed figure in a long answer kept a cosine of
   0.99998 in the measurement of ADR-057 decision 2. A primary that delivered a wrong figure can reveal the
   corrected text and be upheld. ADR-057 says that binding the reveal to the delivered answer needs a field the
   commit does not have. The commit already carries one free field: the chain stores `prompt_commit` as given,
   bounded only by its length (`chain/x/jobs/keeper/msg_server_commit.go::maxCommitPayload`), and only the
   kit reads it. Pin, in the harness's hash mode:
   `services/tests/test_judge_prompt_harness.py::test_pin_w4_edited_reveal_clears_the_anchor`.

## Decisions proposed

1. **The audited text is isolated from the instructions.**
   - The rules and examples of each stage go in a system turn and the data alone in the user turn, through the
     engine's chat route; `services/modea/judge.py::_generate` sends one flat prompt.
   - Each piece of data sits between two fence lines that carry a token drawn per call from the system's random
     source, which the audited party cannot know when it writes its text. The rules name the fence of the call;
     a text that contains its token cannot occur except by chance and is refused as unreadable. Benches fix the
     token by a seed.
   - `_clip` keeps its neutralisation of delimiters and arrows, and the line after the data stays.
   - The four prompts are calibrated data (the comment above `services/modea/judge.py::PROMPT`): this
     change ships only with a new calibration run on both corpora and the gate below.
2. **A text that addresses the grader gets an explicit abstention.**
   - A deterministic screen, in the kit and with no model, reads the revealed answer and the revealed question
     before any stage that can lead to a valid vote. It looks for a closed list of forms, each named in the code
     with its reason: an address to a grader, an evaluator, a judge or a model; a verdict word given as the
     reply to the grading (after a colon, an arrow, "reply", "mark it", "verdict"), and a bare yes or no is
     left alone; a
     role marker at the start of a line; the stage prompts' own phrases ("Answer to evaluate", "Example" and a
     number, "Your answer"); an order to ignore or replace instructions or rules; the delimiters of
     `_QUOTE_MARKS` and the fence words; a verdict key in a structured fragment. The French forms are data that
     processes French text and are named one by one in the language guard.
   - On a hit, the coherence stage still runs and can still vote invalid: the screen never removes an invalid
     vote. Every other stage stops, and the juror abstains at a new stage, `instruction-in-data`, which
     `abstain_to_vote` never lists and which, once reached for an audit, no later attempt of the same process
     turns into a vote (the rule of `services/judge_worker.py::AGREED_REFS_STAGES`).
   - An honest answer that quotes such a form abstains. That is its cost, an audit that may not conclude, and
     the harness counts it on its honest controls (`honest-yes`, `honest-quotes-an-instruction`).
3. **Each stage says what it refuses, and a valid vote needs two readings.**
   - *Every stage:* a reply that is not one verdict word is unreadable and abstains. The calibration notes
     recorded no other reply over 4 734.
   - *Coherence:* unchanged on the whole answer. The answer is also cut at sentence and line boundaries, and
     each segment of a few words or more is read alone, up to `DENDRA_JUDGE_SEGMENT_MAX` segments. One segment
     read as salad while the whole reads coherent ends at a new stage, `coherence-partial`: an abstention,
     never a valid vote. Making it an invalid vote is a later decision, taken on the honest measurement below.
   - *Relevance:* unchanged; it only abstains.
   - *Same fact:* decision 5. `sc-valid` needs the answer read the same as two of the judge's references, each
     in its own call, so one steered call does not buy a valid vote.
4. **The multiplicity step reads the request and the judge's own texts, never the audited answer.**
   - At the slash threshold, the judge first asks whether the question alone admits several correct answers,
     with no answer and no reference shown. A no goes on to the divergence path, held by the guard. An
     unreadable reply abstains at `multiok-unreadable`, as today.
   - On a yes, the judge draws further references at the self-consistency temperature, up to
     `DENDRA_JUDGE_MULTIOK_SAMPLES`. The audited answer earns the benefit of the doubt only when the same-fact
     step of decision 5 reads it the same as one of those samples: stage `multiok`, a valid vote as today.
     Otherwise it abstains at a new stage, `multiok-unmatched`, which `abstain_to_vote` does not list.
   - `MULTI_PROMPT` and `llm_multi_ok` are removed, and no flag restores a prompt that asks the model whether
     the audited answer is acceptable. `DENDRA_JUDGE_MULTIOK=0` keeps its meaning (the step is
     skipped and the divergence path follows).
   - Its cost: an honest answer to an open question that the judge's samples do not produce abstains where it
     was upheld. The honest measurement counts it.
5. **The same-fact step is bounded by the request.**
   - The prompt carries the question, and asks whether the answer gives the same answer to that question as
     the reference.
   - Where the request asks for one answer, an answer that gives several candidates, hedges between values, or
     follows its answer with a different one reads not the same.
   - A claim in the answer that contradicts the reference reads not the same. A valid vote certifies the
     answer to the request; the public texts say that it does not certify every other sentence.
   - An answer longer than the request's output cap allows, counted by the juror's engine with a margin set by
     measurement, abstains at a new stage, `over-cap`, and is never upheld. Where the engine gives no count the
     rule is skipped and the trace says so.
6. **The reveal is bound to the delivered answer through `prompt_commit`.**
   - The miner writes in `prompt_commit` the question commitment of
     `services/modea/crypto.py::prompt_commitment` followed by a salted digest of the answer's bytes,
     with the same salt (`services/modea/crypto.py::prompt_salt`) and a distinct domain prefix. The
     answer is in hand when the commitment is made (`services/modea/miner.py`, the job handler that
     computes `prompt_commit`), and the length stays far below `maxCommitPayload`.
   - The juror reads both forms. A digest that does not open on the revealed answer abstains at a new stage,
     `answer-mismatch`, for the reason a question mismatch abstains (a rotated key gives another salt).
   - A juror of an older kit reads the new form as a question mismatch and abstains. The miner therefore writes
     the new form only once `KIT_MIN` in `network-info.txt` names a kit whose juror reads it
     ([ADR-058](ADR-058-kit-v022-judge-by-default-signed-releases-and-stale-kit-refusal.md) decision 3, the
     key the miner already reads hourly). From the deadline of that `KIT_MIN`, a commit without an answer
     digest abstains at `answer-unbound`: no vote rests on a cosine alone.
   - This refines ADR-057, *What the anchor check does not prove*: the binding needs no new chain field. The
     consensus check of the commit is unchanged.

## The gate before the divergence guard is lifted

All three readings are green on the hardened kit, on the model `audit_judge_model` names, on the CPU path of
the kit, each dated in the record that lifts the guard:

1. **The harness, end to end.** `services/tests/test_judge_prompt_harness.py::test_local_model_gate`,
   with its corpus extended at merge by the injection set of the calibration tool played as answers, in English
   and in French, and by a held-out part written after the hardened prompts are fixed. Pass: no adversarial
   answer posted valid and no honest answer posted invalid, with the guard lifted in the run. The reading is the
   line `judge-harness model=<model> armed=1 adversarial_valid=<k>/<n> honest_invalid=<k>/<n>` printed by
   `DENDRA_JUDGE_HARNESS_MODEL=<model> DENDRA_JUDGE_HARNESS_ENDPOINT=http://127.0.0.1:11434 python3 -m pytest -s services/tests/test_judge_prompt_harness.py -k local_model_gate`.
   A zero over `n` bounds the rate at about `3/n` at 95 % confidence; the record that lifts the guard names
   `n` and the held-out part separately.
2. **The prompts, stage by stage.** The injection bench of the project's calibration tool: no breach on the
   tuning set or on the held-out set, and the count of fills that could never count (the blind spot the
   calibration notes describe) printed next to it. The same tool's calibration gate on both corpora.
3. **Honest answers, distributed.** The project's distributed honest run at 100 jobs with the hardened judge:
   the three values of the artifact's `triplet` field are zero (`hard_slash_at_quorum_rate`, the honest
   miners slashed, the unresolved jobs), with a number of audited honest jobs above zero read from its
   `veto_margins` (a rate over no audited job is not a pass). The same run reports the abstentions at
   `instruction-in-data`, `coherence-partial`, `multiok-unmatched` and `over-cap` on honest jobs, which are the
   liveness cost of decisions 2 to 5.

## Declared versus proven

- The fakes of the harness measure the decision path. They say which stages a steered reading reaches and
  what is posted; they say nothing of how often a model is steered. That is reading 1 of the gate, with a
  model.
- The screen of decision 2 recognises the forms it lists. A paraphrase it does not list passes it, and only
  the model-side decisions stand behind it. The harness's corpus tests it on its own cases and on no others.
- A per-call fence keeps the data from writing the prompt's structure. It does not stop a model that reads
  an instruction inside the data and follows it.
- The answer digest proves that the revealed text is the committed one. It does not prove that the committed
  text reached the client; that is the relay's record.

## Consequences

- More audits end in an abstention, on honest answers too (screen hits, partial salads, unmatched open answers,
  over-cap texts). An audit without enough votes is deferred, then unwound, with the fee held
  ([ADR-044](ADR-044-mute-miner-payoff-removed.md)); the honest run of the gate counts it before the change
  ships.
- Each audit costs the juror more calls: the segments of decision 3, a second same-fact call, the question
  alone and the samples of decision 4. The calibration run reports the time per audit on the CPU path.
- A juror that votes valid less often is paid by the season for fewer verdicts consistent with an upheld
  answer; nothing in the season rules changes.
- The public texts that describe the judge say what a valid vote certifies (decision 5) and that a text
  addressed to the grader is not judged (decision 2).

## What this does not decide

- Lifting the divergence guard. That is the owner's written decision, after the gate above and the C3 pass of
  ADR-057 decision 3.
- A jury of several models (ADR-057, *Open*), which is the structural answer to a correlated error.
- Turning `coherence-partial` into an invalid vote.
- A deterministic same-fact step without a language model
  ([ADR-035](ADR-035-deterministic-verification.md), deferred).
- The values of `DENDRA_JUDGE_SEGMENT_MAX`, `DENDRA_JUDGE_MULTIOK_SAMPLES` and the over-cap margin: each is
  set by the calibration run and written next to its reader with that run's date.
- Any change to the chain, the relay, the faucet or the season.

## Pointers to add at merge

None of the code below is in the tree; each item is cited by file and symbol once it lands, in the same change.

- The chat route with a system turn and the per-call fence in `services/modea/judge.py`, with the new
  calibration counts in the notes above the prompts.
- The screen of decision 2 and its list of forms; the stages `instruction-in-data`, `coherence-partial`,
  `multiok-unmatched`, `over-cap`, `answer-mismatch` and `answer-unbound` in
  `services/judge_worker.py`, with their lines in `_ABSTAIN_WHY`.
- The segment reading, the two-reference `sc-valid`, the question-alone multiplicity reading and its samples,
  and the bounded same-fact prompt.
- The answer digest in the miner's commitment and its reader in `prompt_check`, gated on `KIT_MIN`.
- The flipped pins and the extended corpus of `services/tests/test_judge_prompt_harness.py`.

## Version

One `kit_version` bump carries decisions 1 to 6, released as a patch under ADR-050. The miner's side of
decision 6 is written but stays off until `KIT_MIN` names that kit. `consensus_epoch` does not move.
