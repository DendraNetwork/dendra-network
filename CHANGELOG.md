# Changelog

Each release of this repository is a tag `vMAJOR.MINOR.PATCH` with a section below, and its GitHub
release carries that section. The repository is republished as a single commit, so `git log` cannot
compare two releases: this file does.

## How the version number moves

The number follows the two gates of `VERSION`:

- **`consensus_epoch` moved**: the state machine changed, and a node left on the previous binary forks.
  The **minor** number moves and the patch number returns to 0.
- **`kit_version` moved alone**: the scripts and services an operator runs changed; a kit one version
  behind still joins and is told to update. The **patch** number moves.
- **Neither moved**: documentation, the website, or services only the network runs. No number: the
  repository is republished without a release, save an exceptional release whose section says why; it
  moves the patch number.

`v1.0.0` is the mainnet genesis. The earlier `v0.1` line designated trees of the previous testnet and was
withdrawn; its names are not reused.

## v0.3.0

Kit version 4, consensus epoch 15: the state machine changed. A network on epoch 14 moves to it through the
governed upgrade `epoch-15-jurors-are-judges` (`chain/app/upgrades.go::UpgradeEpoch15`), at the height its
proposal sets, and not when this release is published. Until that height the network runs the epoch 14 binary;
every node on that binary stops at the height and goes past it only on the binary of this release. A node that
starts the binary of this release before the upgrade stops at its first block, and the line in its log depends on
the data it starts from. Data that end before the block where the upgrade proposal passed give
`consensus epoch guard`, with the release to run and the command that reads the chain's module versions
(`chain/app/epoch_guard.go::epochGuard`). Data that hold that block hold the plan, and the upgrade module stops
the node first, with `BINARY UPDATED BEFORE TRIGGER`. In both cases the v0.2.2 binary goes on from its data.
The network declares the new epoch in `network-info.txt` once it publishes it after the upgrade
(`deploy/testnet/publish_network.sh`).

### The chain

- **A judge is declared on chain.** `MsgDeclareJudge` is signed by the miner's operator:
  `dendrad tx jobs declare-judge <miner_id> true`, and `false` to leave the jury pool.
  `dendrad query jobs miner-judge <miner_id>` reads the declaration, its height, the height of the miner's last
  verdict and the draws it did not answer; an id with no entry reads `declared` false. Exit and eviction clear
  the four entries (`chain/x/jobs/keeper/judge.go::clearJudgeState`).
- **The audit jury and the re-adjudication jury seat declared judges only.** After the filters each draw already
  had (frozen pool, vitality and presence for the audit jury; frozen pool and presence for the re-adjudication
  jury), a candidate must be declared, have a verdict or a declaration within
  `judge_verdict_window_blocks` of the draw height, and must not have been seated on `judge_missed_draws_max`
  concluded audits without voting (`chain/x/jobs/keeper/judge.go::eligibleAsJudgeAt`). An answer while
  the audit's seats are anchored resets that count: a verdict from a seat on its audit jury, or a re-commit from a
  seat on its re-adjudication jury that holds a vector the adjudication can parse and comes from a juror that wrote
  no work commit of its own on the job (`chain/x/jobs/keeper/judge.go::noteJudgeAnswer`); a new declaration
  with `true` resets it too and opens the window again from its height. A judge whose seats on the job were all anchored before the declaration it holds is
  not counted when its audit concludes (`chain/x/jobs/keeper/judge.go::seatsPredateDeclaration`), so the audits open at
  the upgrade height do not count against a judge that declares after it. The jury is the smaller of the draw
  size and the eligible judges.
  An audit below the jury floor defers as before, the bar of
  `chain/x/jobs/keeper/antievasion.go::auditRelativeBar` is unchanged, and a jury anchored before the upgrade
  keeps its seats. Job admission does not read the declaration
  (`chain/x/jobs/keeper/pool_freeze.go::enoughEligibleMinersToVerify`): a job can open on a pool without
  enough judges, and its audit then defers at the jury floor; with `audit_unwind_blocks` above 0, it unwinds past
  that bound with the client refunded.
- **What counts as a missed draw.** When an audit is resolved, adjudicated or unwound, and not when it defers,
  the chain walks its audit jury in the anchored order and adds one missed draw to each declared judge seated there
  that did not answer (`chain/x/jobs/keeper/judge.go::recordMissedDraws`, run by
  `chain/x/jobs/keeper/miner_vitality.go::clearAuditCommittee` before the seats are purged). A seat held only
  on the re-adjudication jury adds none: the kit's judge reads the audit jury alone and commits verdicts only
  (`services/judge_worker.py` runs `query jobs audit-committee` without `--redo`). A judge seated on both
  juries is counted once. An audit seat adds none when its miner left the registry, when its miner is not a
  declared judge at that moment, when every seat its judge holds on the job was anchored below the height of the
  declaration it holds (`judge.go::seatsPredateDeclaration`), or when one of those reads fails. One definition of
  an answer serves when the commit lands and when the seats are counted (`judge.go::juryAnswer`): a verdict from a
  seat on the audit jury, or a re-commit from a seat on the re-adjudication jury that holds a vector
  `AdjudicateDispute` can parse and comes from a juror that wrote no work commit of its own on the job (key
  `<job_id>__<miner_id>`). When such an answer lands while the audit's seats are still anchored, the chain records
  its height as the judge's last verdict height and clears its missed draws (`judge.go::noteJudgeAnswer`); an
  answer that lands after the conclusion moves neither. Whether a juror wrote a work commit on the job is one read
  of one key (`chain/x/jobs/keeper/redo_committee.go::contestedAuthor`), so the cost of that check does not
  grow with the commits of the job.
- **The presence proof carries the kit.** `MsgProveAvailability` has a `kit_version` field (`--kit-version` on
  `dendrad tx jobs prove-availability`). When `min_kit_version` is above 0, a proof below it is refused and
  writes no presence (`chain/x/jobs/keeper/msg_server_prove_availability.go::ProveAvailability`); with
  availability windows armed, that miner is then drawn neither as primary nor as juror, and is paid no
  availability reward. A proof without the field reads 0. The kit writes the number and the operator's key
  signs it: the signature proves who wrote it, and nothing proves that it is true.
- **Two parameters for the draws.** `avail_sample_bps` is the share of bonded miners that owe a presence proof
  in an availability epoch, drawn from that epoch's challenge
  (`chain/x/jobs/keeper/availability_sample.go::availabilityProofOwed`); at 0 every bonded miner owes one,
  and a miner that owed none is not billed for its absence. `committee_min_vrf_power_bps` is the power bar of
  the decentralised seed; at 0 the compiled two thirds apply, and `Params.Validate` refuses a posted value below
  them. `dendrad query jobs committee-seed-health` reports the bar in force (`committee_min_vrf_power_bps`).
- **A presence proof in the block that ends an availability epoch answers that epoch.** At each boundary the
  chain now writes, beside the new challenge, the height of the block whose end rolled it (`AvailChallengeHeight`,
  `chain/x/jobs/types/keys.go::AvailChallengeHeightKey`). `ProveAvailability` and
  `dendrad query jobs get-avail-challenge` read the epoch from one definition
  (`chain/x/jobs/keeper/availability.go::availChallengeEpoch`): a proof carried by a boundary block answers
  the epoch that block closes, and a context built on that block's committed state (a query, or the simulation
  behind `--gas auto`) already reads the next one. The deadline set by `avail_deadline_blocks` counts from the
  boundary that opened that same epoch (the epoch times `avail_epoch_blocks`), so while `avail_deadline_blocks` is
  above 0 and below `avail_epoch_blocks`, a proof carried by a boundary block is refused as too late. The epoch 14
  binary dates a proof by its height alone (the height divided by `avail_epoch_blocks`). No migration writes the
  new entry. When the block before the upgrade height is a boundary, the binary of this release reads that block's
  committed state during the halt as the epoch the block closes, in a query and in the check of a proof alike,
  until the upgrade block commits.
- **Removed and renamed.** `rpc SettleJob` and `dendrad tx jobs settle-job` are removed. The emission counter
  `security_pool` is now `security_distributed_total`: the field number is the same, so the stored state does
  not move, but its JSON key changes, and this binary refuses a genesis that writes `security_pool`.
  `dendrad query emission pools` reads the counters of the emission engine.
- **The migration.** The plan's handler runs one migration: the jobs module moves from version 1 to 2
  (`chain/x/jobs/keeper/migrations.go::Migrate1to2`), which writes the five new parameters at 0 and no other
  state. At 0, `min_kit_version` refuses no proof, every bonded miner owes a presence proof, and the two judge
  parameters and the seed bar select their compiled values. Each one moves only by a governance proposal.
  `dendrad query upgrade applied epoch-15-jurors-are-judges` answers the height the plan ran at; a chain that did
  not apply it answers no height, which reads as 0.
- The `toolchain` line of `chain/go.mod` names the compiler that reproduces the released binaries, and the
  file names newer versions of several indirect dependencies.

### What it changes, role by role

- **A miner that does not judge** is no longer drawn into audit juries. Its work draw, its presence and its pay
  are unchanged.
- **A judge** is seated only once declared. The miner daemon declares `true` on the epoch 15 chain while its
  container runs the judge role (`DENDRA_MINER_JUDGE=1`, `services/miner.py::judge_role_on`) and
  `false` once the role is off or the drain is set, each only when a reading of the chain holds the other state,
  since a second `true` resets the missed-draw count (`services/miner.py::JudgeSeat`).
  The one exception is a declared judge the chain no longer draws: its missed draws at `judge_missed_draws_max`, or
  neither its last verdict nor its declaration within `judge_verdict_window_blocks` of the node's latest height, each
  limit read from the chain's params, where 0 selects the chain's compiled value
  (`services/miner.py::judge_exclusion`, the predicate of
  `chain/x/jobs/keeper/judge.go::eligibleAsJudgeAt`). The daemon declares that judge again, once, while its role
  is on, it is not draining, and its judge can answer: a judge worker runs with its model (its judge state tied to
  its live process, `services/miner_selftest.py::judge_worker_model`), the judge engine lists that model and
  the memory guard lets it be used (`miner.py::judge_answer_ready`, `judge_return_action`). A judge the daemon
  declared again that is then seated on a concluded audit, answers none, and is excluded once more, by the count or
  by the window, is not declared again by that process; a restart of the miner's container lifts that. A return whose
  confirmation was not read is recognised at the next reading of the declaration. A limit, a height or a field that
  was not read sends nothing, and the heartbeat's `judge_declaration` says why (its `exclusion`).
  `deploy/testnet-miner/judge.sh on` and `off` set the role and recreate the miner's container, which then
  follows it. The daemon needs a `dendrad` that offers `tx jobs declare-judge` and `--kit-version`: the image
  built from this release has both, the image of v0.2.2 has neither. A miner of v0.2.2 on the epoch 15 chain
  therefore declares nothing and sends its proofs without the field; its log says once that the kit's `dendrad`
  predates the plan. It is seated again once its kit runs this release.
- **A judge's memory** is measured at installation and watched while the miner runs, and the judge model is loaded
  only when the machine has room for it (see "The judge sized to the machine" under The kit). While the judge's
  memory check stays closed, the judge posts no verdict on the audit and tries it again later. If the audit
  concludes first, the epoch 15 chain counts that audit seat as a missed draw, by the rule given under The chain
  ("What counts as a missed draw").
- **A node** stops at the plan's height on the epoch 14 binary. The kit's own path across the halt is below.
- **A validator** (`deploy/join.sh --validator`) has no automatic update and no watch: both belong to the miner
  kit (`deploy/join.sh::install_kit_update_cron`). Once the chain has halted, its operator runs the installer of
  this release with the options of the first installation,
  `bash install.sh --yes --validator --ref v0.3.0 --after-upgrade epoch-15-jurors-are-judges`, or, from a clone
  placed on this release, `bash deploy/join.sh --validator --after-upgrade epoch-15-jurors-are-judges`. The join
  confirms that the node stands at the plan's halt and starts it on this release's node image
  (`deploy/join.sh::after_upgrade_applied`). The node holds the plan's block from the halt and runs it as soon as it
  starts on this release, so it answers the plan applied before the network moves on: the chain resumes once its
  validators run this release. If the node does not answer the plan applied within the join's wait, the join stops,
  says to keep the clone on this tree, and prints the command to run again once the node answers it
  (`deploy/join.sh::after_upgrade_finish`). A new validator node of this release takes `--statesync`: by default
  a validator's node replays from the genesis (`deploy/join.sh::node_sync_plan`), and the binary of this release
  does not replay the history before the plan. `deploy/bond_validator.sh` then asks for
  `--accept-statesynced-node`.
- **A Windows PC** runs `deploy/install.ps1 -Yes` as before. When installing WSL needs a restart, the installer
  now continues by itself at the next logon (see "The application, Windows and the refusals" below). If it does
  not continue, run `deploy/install.ps1 -Yes` again.
- **A miner** can, where its machine allows it, switch the judge role and GPU mining from the Dendra application; a
  switch that cannot be pressed says why. It can also test the machine, read its earnings by role and see its state
  in plain words (same section).
- **Each miner identity** sends its own signed capacity report from its miner container, with or without cron, once
  the chain records the identity under that key's operator, and again every 3600 seconds after a report goes out
  (see "The signed capacity report" under The kit). Under the season's rule set with a kit floor, a day is paid
  only to an identity whose latest signed capacity report at or before the day's last block declares a kit at or
  above `kit_min` (`services/final_season_calc.py::kit_allows_pay`).

### The kit

- **The judge sized to the machine: the survey.** On a machine that can judge, `deploy/join.sh` asks what it can
  give the judge before it writes the kit's `.env` and override (`deploy/hw_probe.sh --judge-survey`, run by
  `deploy/join.sh::judge_survey_apply`). The survey reads the memory, the file cache and the swap
  `DENDRA_SURVEY_SAMPLES` times, `DENDRA_SURVEY_INTERVAL_S` seconds apart (30 and 1 when unset), then reads what the
  kit's containers hold. It names the host: WSL, a desktop with a graphical session, a server, or a host it could
  not tell when `loginctl` does not answer. It decides a mode. `resident`: the room at the busiest moment watched,
  less the swap in use, holds the model's footprint plus the headroom (under WSL, only when the Windows side was read
  too), and the operator did not choose on demand. `on-demand`: the model is loaded for an audit, then let go. A
  footprint that is not known gives on demand. When the judge engine's own share cannot be read, the mode the `.env`
  already records is kept, and on demand stands in when it records none. `impossible`: the room at the quietest
  moment watched is below the model's footprint plus the headroom, or the model can neither stay loaded nor load in
  time. On a machine with a card, the measured load plus the judging must fit `audit_resolve_timeout` blocks at the
  block interval measured over the last 100 blocks. Without a card, the load must fit the presence probe's bound
  (`DENDRA_PRESENCE_PROBE_S`). `unknown`: a memory figure of the kernel (`/proc/meminfo`) could not be read. The kit's
  `.env` then records the ceiling of the judge's mode (`DENDRA_JUDGE_MODE`: `resident` only when this survey measured
  it, or when the judge engine's share could not be read and the `.env` already recorded `resident`; `on-demand`
  otherwise), the host (`DENDRA_JUDGE_HOST_KIND`), the judge's threads (`DENDRA_JUDGE_NUM_THREAD`) and its memory
  limit (`DENDRA_JUDGE_MEM_LIMIT_MB`). When the seat comes from the default and the kit does not judge yet,
  `impossible` or `unknown` leaves the seat untaken and the miner runs. A kit that already judges, an explicit
  `--judge`, and a machine with no usable card, whose role is the judge on the CPU, keep the seat on demand.
  `bash deploy/testnet-miner/judge.sh on` runs the same survey: it keeps a seat the kit holds on demand where the
  survey reads `impossible`, and refuses, with nothing changed, a first seat there or any seat the survey could not
  measure.
- **The judge engine's limits.** In the override, the CPU judge engine (`ollama-cpu`) gets the survey's limits
  (`deploy/join.sh::judge_guard_block`). Its memory limit equals the room at the quietest moment watched (available
  memory, plus what the judge engine already holds, less the file cache in use). The limit is written only where that
  room is known to hold the loaded model plus the headroom, with no swap of its own where the kernel accounts swap per
  container (`memswap_limit`). Its CPU quota and thread count are `DENDRA_JUDGE_CPU_PCT` percent of the physical
  cores, rounded down, at least one thread (75 when unset, accepted from 75 to 85, refused outside). When the physical
  cores cannot be counted, the quota counts the logical processors and no thread count is written. It also gets a CPU
  weight of 2 (`cpu_shares`) and `oom_score_adj: 1000`, so that the kernel stops the judge first when the whole
  machine runs short. The compose file sets the engine's context to 4096 tokens (`OLLAMA_CONTEXT_LENGTH`,
  `deploy/testnet-miner/docker-compose.yml`), and the override sets its thread count (`LLAMA_ARG_THREADS`) when the
  survey counted the physical cores. No caller of that engine sends a context size or a thread count
  (`services/modea/memwatch.py::engine_options` returns none).
- **The judge's memory watch.** In the container of a miner whose judge role is on, the daemon reads the machine's
  memory every `DENDRA_MEMWATCH_PERIOD_S` seconds (5 when unset) and sets the judge's mode under the ceiling
  (`services/modea/memwatch.py::MemWatch`). It unloads the model when memory pressure (`some avg10`) reaches
  `DENDRA_MEMWATCH_PSI_SOME_PCT` (10 when unset), when pages go to swap while the model is loaded and the room beside
  the file cache is below the headroom or not known, or when the file cache itself is being taken. The judge then
  stays on demand and waits before its next load: 60 seconds, doubled at each strike up to 30 minutes
  (`memwatch.py::retry_delay`). It returns to resident only after `DENDRA_MEMWATCH_PROMOTE_AFTER_S` seconds of clean
  readings (unset: the resident keep-alive, `DENDRA_JUDGE_KEEPALIVE`, one hour by default). A desktop starts on
  demand, a server whose ceiling is resident starts resident, and under WSL the judge stays on demand: a container
  cannot read the Windows side.
- **The check before each load.** Every load of the judge model passes a check of the room first
  (`memwatch.py::gate`, `memwatch.py::check_room`): the memory available beside the active file cache must hold the
  model's footprint plus `DENDRA_JUDGE_HEADROOM_MB`, the footprint must fit the engine's memory limit
  (`DENDRA_JUDGE_MEM_LIMIT_MB`), no pause may run, and, where the kernel reports memory pressure, no task may be
  stalled on memory (`full avg10` above zero). The check stays closed when the available memory, the file cache or
  the footprint was not read, and when the engine's loaded models were not read. The footprint is a figure measured
  on a load when there is one, else an estimate: the model file plus its context cache, from the engine's metadata or
  from the figures the kit declares for the model (`memwatch.py::MemWatch.footprint`). The file alone is a lower
  bound, and the check does not open on it. A juror whose check is closed abstains on that attempt at the stage
  `memory-guard` and tries the audit again later; memory settings written wrong abstain at `memory-settings`, and an
  engine whose loaded models cannot be read at `judge-engine-unread`. A load that runs out of memory pauses the next
  one in the same way; under a memory limit, with a footprint that was only estimated, it records that the model
  takes more than that limit, and the check stays closed until the daemon starts again, as it does when an
  installation writes another limit (`memwatch.py::MemWatch.note_oom`). On a machine whose miner serves the judge
  model on the judge's engine, the presence probe and every request pass the same check: while it is closed no
  presence proof is sent, and the daemon tries again while the availability window is open
  (`services/miner.py::gated_probe`). At start, such a miner no longer loads the model to see where
  it runs; the daemon waits for the engine to list it, within `DENDRA_SERVE_GUARD_WAIT_S` seconds (900 when unset)
  (`services/serve_guard.py::decide`, `miner.py::judge_engine_ready`).
- **What the judge's memory reports.** The daemon's heartbeat carries the decision under `judge_memory` (mode,
  ceiling, the check and its reason, whether the model is loaded, its footprint, the unloads and the out-of-memory
  errors). `bash deploy/testnet-miner/judge.sh status` ends with those lines and with the judge's seat on the chain,
  as the daemon last read it: its declaration, the height of its last verdict and its missed draws against
  `judge_missed_draws_max`. Once the missed draws reach that limit, the chain stops drawing the judge
  (`chain/x/jobs/keeper/judge.go::eligibleAsJudgeAt`), and the status says that the miner declares it again by
  itself once its judge can answer; for a judge the miner already declared again once, it gives the command that
  restarts the miner and the gesture by hand, off then on, where the judge is not the machine's whole role.
  The chain also stops drawing a declared judge once neither its last answer nor its declaration falls within
  `judge_verdict_window_blocks` of the draw height (at 0, the juror freshness window,
  `chain/x/jobs/keeper/judge.go::effectiveJudgeVerdictWindow`). The status does not report that case: its seat
  line compares the missed draws with their limit and reads no window. The daemon reads both cases
  (`services/miner.py::judge_exclusion`) and declares the judge again in either one, under the
  conditions of "A judge" above, since a new declaration clears the count and opens the window again from its
  height (`chain/x/jobs/keeper/judge.go::declareJudge`).
  The signed capacity report declares `judge_mode` (`resident`, `on-demand`, `none` or `unknown`) and, when the
  juror's ledger was read, `judge_memory_abstentions`. The registry publishes only counts of reports by mode
  (`judge_modes`) and by band of abstentions (`judge_memory_abstention_bands`: `0`, `1-9`, `10+`, `unread`), and no
  row carries its own (`services/capacity_server.py::_public_row`). For a kit that asks for the judge, the
  hourly check H7 fails when pages went to swap since the last scheduled run and the judge's model is loaded at the
  time of the check or its mode is resident, or when memory pressure `full avg300` is above zero
  (`deploy/testnet-miner/miner_health.sh`, H7). On HiveOS, the `ver` field of the stats adds `judge resident` or
  `judge on-demand`, followed by `held` while the check is closed, `judge ?` when the reading cannot be read, and
  `judge stale` when it is too old (`deploy/hiveos/dendra/h-stats.sh`, `judge_word`).
- **The judge's settings stay with the kit.** The operator's settings are kept in the kit's `.env` from one run of
  `deploy/join.sh` to the next: `DENDRA_JUDGE_MODE_CHOICE=on-demand` (it can only lower the measured
  mode), `DENDRA_JUDGE_CPU_PCT`, `DENDRA_JUDGE_HEADROOM_MB` (MiB added on top of the footprint), the keep-alive of
  each mode (`DENDRA_JUDGE_KEEPALIVE`, `DENDRA_JUDGE_ONDEMAND_KEEPALIVE`) and the three settings of the watch. The
  join prints how to change the mode, the share of the cores and the headroom, and `--reset-judge-settings` drops
  every kept setting so that the measure decides again. A setting written wrong makes the survey refuse (exit 2),
  and the join stops before it writes the override or the `.env`. In the miner's container, memory settings
  written wrong keep the judge from loading the model until they are fixed.
- **One identity per card: a slot's RAM check is measured.** `DENDRA_RIG_RESERVE_MB` is gone. A slot k starts only
  if the memory available now, less the active file cache and the swing of available memory that the judge survey
  measured, less the judge's footprint while its model is not loaded, covers what slot 0 takes: its engine and its
  miner, measured with its model loaded, or declared with `DENDRA_SLOT_FOOTPRINT_MB`
  (`deploy/join.sh::rig_ram_allows_slot`, `measure_slot_footprint`). A slot that fails the check, or whose figures
  were not read, does not start, and neither do the slots after it. The judge's mode, host kind, memory limit and
  run-time settings are carried from slot 0's `.env` to every slot's, since slot 0's judge engine serves every slot
  (`deploy/join.sh`, `SLOT_CARRIED_KEYS`).
- **The signed capacity report leaves from the miner's container.** `docker/entrypoint-services.sh` starts
  `services/capacity_sender.py` beside the daemon once the daemon has written the identity's key, and
  relaunches it when it stops while the daemon runs. Each slot of a machine with one identity per card runs its own
  container, so each identity sends its own report. A miner whose key does not appear starts no sender and says so in
  its log. The sender builds the report as `deploy/testnet-miner/publish-capacity.sh` does. It starts from the
  hardware inventory stored in the identity's key volume (`capacity-inventory.json`), adds the identity, the judge
  role and memory mode, and the abstentions and the kit version when they are read in the container, and marks a slot
  other than slot 0 as sharing its machine (`host_share`). It signs the report with the identity's key and sends it
  only once the chain records the identity with that key as its operator (`capacity_sender.py::one_round`). The first
  round runs `FIRST_DELAY_S` (60 seconds) after the sender starts. After a report goes out, the next round comes
  `PERIOD_S` (3600 seconds) later; after a round that sent nothing, the wait starts at `RETRY_FIRST_S` (300 seconds)
  and doubles up to `RETRY_MAX_S` (3600 seconds) (`capacity_sender.py::retry_delay`). A registry that answers with a
  redirect is read as a refusal (`capacity_sender.py::_post_default`). A registry address whose host is a loopback or
  private address, or a name without a dot, is refused (`capacity_sender.py::registry_url`, `is_local_host`).
  `publish-capacity.sh` stores the inventory in each running miner container before it sends anything.
  `deploy/join.sh` runs it once the miner runs, alone or with `--inventory`, which stores the inventory and sends
  nothing. `docker/cloud-start.sh` runs the probe and stores the inventory at each start of a rented pod. The hourly
  crontab line stays where crontab exists, as a second sender and as the run that refreshes the inventory after a
  hardware change. Each round writes its outcome where self-test check C8 reads it. Inside the miner container,
  `capacity_sender.py --id <identity> --once` runs one round and exits with the codes of `publish-capacity.sh`. When
  the report was not sent or not counted, the messages of `deploy/join.sh`, `publish-capacity.sh`, check C8 and the
  Dendra application point to this sender and say, in short, that the season pays nothing to an identity without a
  signed report. That rule applies from the season day the season status names in `kit_rules_from_day`
  (`services/final_season_calc.py::kit_allows_pay`).
- **The commitment journal is read or the pass stops.** The daemon journals each commitment before it deposits the
  answer. A journal that exists and cannot be read stops the pass, so no job is served, and the heartbeat names it
  (`journal_error`). A journal that cannot be written leaves the job undeposited and stops the pass too
  (`services/miner.py::_journal_for_pass`, `JournalUnreadable`, `_journal_commitment`). Self-test
  check C9 then fails with the remedy: free the disk or make the key volume writable when the journal cannot be
  written, or move the unreadable file aside and restart the miner when it cannot be read.
- `DENDRA_JUDGE_TRACE=1` writes one file per identity in that identity's key directory
  (`judge-trace-<identity>.jsonl`) in place of `/tmp` (`services/judge_worker.py::trace_destination`). The
  trace file is opened without following a link, and nothing is written to it unless it is a plain file with a
  single link, owned by the user the judge runs as; the verdict goes out either way (`judge_worker.py::_trace_write`).
- **A metrics exporter for the miner's health.** `chain/cmd/dendra-exporter` serves the reports of the miner
  health check (`miner-health.last.json`, one per identity, named by `-file` and `-glob`) as metrics at `/metrics`,
  on `127.0.0.1:9478` unless `-listen` names another address; it reads files and runs nothing.
  A dashboard for those metrics ships with the miner kit. The kit installs neither:
  `chain/cmd/dendra-exporter/README.md` gives the build, the flags, the scrape job and the dashboard's file.
- **HiveOS crosses the upgrade with one change of package.** `deploy/hiveos/build.sh` reads the plan the tree
  registers and writes it into the manifest (`DENDRA_UPGRADE_PLAN`), and `h-run.sh` passes it on every
  installation (`deploy/install.sh --after-upgrade`). Installed once the chain has halted at the plan's height,
  the package of this release starts the rig's node on the new node image and goes on once the node answers the
  plan applied. Installed before the halt, it is refused, and the node container that HiveOS stopped with the
  miner is started again on the image it ran (`deploy/join.sh::node_wake`), so that it reaches the halt. A rig
  with no node yet is refused at once while the network declares epoch 14: its operator installs the package of
  v0.2.2.
  A restart after an installation that did not end applied installs again and never resumes.
  `deploy/hiveos/README.md` gives the procedure.
- **`deploy/join.sh` checks what it crosses.** `--after-upgrade <plan>` is accepted only for a tree one epoch
  ahead of the declared one that registers that plan in `upgradePlans`, and a kit with no node container is
  refused under it: a new node is installed from the release of the declared epoch. A refused epoch asks the chain
  whether the plan is applied (`deploy/join.sh::epoch_plan_probe`). When it is, or when the kit's node stands at the
  halt, the refusal prints the exact re-run with `--after-upgrade <plan>` and does not name the older release; when
  the chain does not answer, it prints that re-run first and the older release after it; otherwise it names the
  newest release of the declared epoch in the clone and its command (`deploy/join.sh::epoch_way_out`). When it cannot
  resolve the release of the declared epoch (origin silent, for example), it prints the same join to run again, and
  never the installer without `--ref`. A node
  that holds the chain below the height where the network applied a plan of this tree is not rebuilt on this
  tree's binary (`deploy/join.sh::node_plan_gate`); a stopped one is started again on its own image first.
- `deploy/install.sh --after-upgrade <plan>` relays the plan to `join.sh` for one run, checked on the whole
  value before anything changes.
- **The installer places the clone on a signed release tag, never on `main`.** Without `--ref`, it reads the epoch
  `network-info.txt` declares, lists the release tags of the repository, and takes the newest one whose
  `docker/CONSENSUS_EPOCH` reads that epoch (`deploy/install.sh::resolve_ref`). A network that declares no readable
  epoch, or no release of it, is a refusal. A tag whose header does not name it, or whose signature does not verify
  with `docker/RELEASE_SIGNERS`, stops the install before the kit runs. A clone that was already there verifies the
  tag before it moves, with the signers file of the tree it sits on (`deploy/install.sh::present_trust`), and the
  tree it leaves stays on the branch `kit-before-update`. Without `--ref`, `install.sh` never moves a clone that is
  already there to an earlier consensus epoch. If the clone is at a higher epoch than `network-info.txt` declares,
  the installer changes nothing, exits 2, and prints your command again with `--ref`: the release tag of the clone's
  tree, or, when no tag names that tree (a clone that followed `main`), the newest release of the clone's epoch. If no
  release of that epoch was ever published, it prints the command with `--ref` and the release of the declared epoch.
  When origin does not answer, it exits 3, changes nothing, and prints the same command to run again. Run the command
  it prints: `join.sh` then asks the chain (`deploy/install.sh::clone_epoch_gate`, `deploy/install.sh::epoch_gate_tags`).
  During a governed upgrade a new installation therefore
  runs the release of the epoch the network still declares.
- **On Windows, `install.ps1 -Yes` makes a clone that `install.sh` recognises as its own.** It clones the repository
  into `~/dendra-network` inside the distribution and writes a marker into the clone's `.git`: a token of its run and
  the commit it cloned (`deploy/install.ps1`, `$FreshToken`). It copies the installer of `main` into the clone's
  `.git` (`dendra-installer`: `install.sh`, `hw_probe.sh`, `kit_pins.txt`) and runs that copy, never the clone's own
  `deploy/install.sh`; for a clone that is already there, it first fetches `main` from the repository
  (`deploy/install.ps1`, `$clone` and `$cmd`). `install.sh` counts that clone
  as made for its own run only while it is still as `git clone` left it: HEAD on the commit the marker names, a
  single entry in the reflog of HEAD, a single local branch, and no file git does not track, ignored files included
  (`deploy/install.sh::fresh_handover_read`). During a governed upgrade, `main` carries epoch 15 while
  `network-info.txt` declares 14. In that case the Windows installer places the new clone on v0.2.2, the newest
  signed release of the declared epoch, and runs `join.sh` from that tree. It does not refuse the clone and does not
  print `--ref v0.3.0`. Running `install.ps1 -Yes` again, or the re-run command it prints after a failed step, runs
  the installer of `main` again, and the clone stays on v0.2.2. If the installation stops before the
  clone is placed, running `install.ps1 -Yes` again is enough. A clone that was already there at a higher
  epoch than the network declares is still refused, exit 2, nothing changed, and the command is printed again with
  `--ref`: a clone not made by `install.ps1`, a copied marker, a clone that moved or holds a kit file or a second
  branch, another token, and a run without the token (`deploy/install.sh::clone_epoch_gate`).
- **The update line names a tag and verifies it before the clone moves.** `deploy/join.sh::kit_update_hint` prints
  `git fetch` of the tag by its full name, `git verify-tag` with `docker/RELEASE_SIGNERS` (git's `gpg.program` set
  to `/bin/false`, so no key of the host's keyring counts), `git branch -f kit-before-update`, and
  `git checkout --detach` of the tag, then the join to run again with the same options. When the tag cannot be
  resolved, or the clone carries no signers file, it prints the installer's command instead, or, for a tree ahead of
  the declared epoch, tells the operator to stay on it. The release notes read the line from the tagged tree
  (`.github/release_notes.sh`). A kit older than this release prints an older line: a kit on v0.2.1 or v0.2.2 prints
  one that ends in `git checkout -B main origin/main`, and a kit on v0.2.0 prints `git fetch origin && git reset --hard origin/main`,
  which leaves no `kit-before-update` branch. A kit on v0.2.0 or v0.2.1 can run its line until the kit deadline
  (`KIT_DEADLINE` in `network-info.txt`) and uses the installer after it, except from the publication of this
  release until the network declares epoch 15: both lines then bring the clone to this tree, which `join.sh`
  refuses (see "A kit below v0.2.2" below). Do not run such a line on a kit of v0.2.2 or later.
  The line recorded in the kit's `.env` (`DENDRA_KIT_UPDATE`), which `miner_health.sh` and the Dendra app print,
  names the release that the last join resolved, so a newer release of the same epoch leaves it moving nothing.
  For a kit whose `.env` records no update line, or records the old line that moves the clone to `main`,
  `miner_health.sh` prints the installer's instruction instead and never `git checkout -B main origin/main`: download
  the installer, read it, then run `DENDRA_DIR=` followed by the clone's path and `bash install.sh --yes`, with the
  options of the first installation (`deploy/testnet-miner/miner_health.sh::UPDATE`).
  The remedy `miner_health.sh` prints is the kit's recorded re-run (`DENDRA_KIT_RERUN`) first: it resolves the
  newest release and prints the line that installs it, and that line comes second. This holds inside one epoch;
  across epochs, see below. `join.sh` keeps the tag it names in the clone's tags. A fetch that fails with
  `would clobber existing tag` means the tag was re-made on the forge: nothing is forced, and
  `git tag -d` with that tag's name is run in the clone only if the project announced that it re-made that release.
- **A validator's kit records the signers file as a miner's does.** The SHA-256 of `docker/RELEASE_SIGNERS` is
  written to the node kit's `.env` for the validator role, and a later run refuses a file that differs
  (`deploy/join.sh::signers_record_env`, `deploy/join.sh::verify_release_signers`).
- **Pinned downloads.** `deploy/kit_pins.txt` names the versions of the Docker packages (Engine, containerd, Buildx,
  Compose) and of the NVIDIA container toolkit that `deploy/install.sh` installs, and the fingerprint of each
  repository's signing key. A key or a
  version other than the pinned one stops the install (exit 2). So does a release of the host's distribution (an
  older or a newer one) for which the repository offers no build of the pinned version: the message says how to
  install Docker by hand. An Engine or a toolkit already installed is kept as it is, whatever its version: when it is
  not the pin, the installer prints the version found and the pin, and goes on. The file names no model.
- **The miner image build names its inputs by content.** `docker/Dockerfile.miner` names each base image by the digest
  of its multi-platform index and installs the Python packages listed in `docker/miner-requirements.txt` by hash.
- `deploy/testnet-miner/kit_update.sh --plans <tree>` prints the upgrade plans a tree's binary registers, as
  `key=value` lines, and moves nothing. The HiveOS build, `join.sh` and the release notes read the plans
  through it.
- `deploy/testnet-miner/node_upgrade_watch.sh`: for a clone moved by hand onto a tree that registers the plan,
  the watch prints the kit's join with `--after-upgrade` instead of asking for a stage.
- `deploy/testnet-miner/judge.sh status` reads the judge worker's counters under the keys it writes
  (`verdicts_anchored`, `verdicts_refused`), and prints `?` for a key the state file does not carry. On v0.2.2
  it read other keys and printed 0 for figures it had not read.
- **A verdict names the model that judged.** The judge worker writes the model it ran, `judge_model`, in the
  `--model-id` of every verdict (`services/judge_worker.py::main`). `DENDRA_JUDGE_MODEL_ID` is only the
  fallback for the model to run when the chain pins none, and it no longer replaces the model named in a verdict;
  the worker prints a warning when the two differ.
- The release notes of a tree that registers a plan say so, with what a joiner does until the network declares
  the new epoch (`.github/release_notes.sh`).
- **The health check and the refusals name the clone by its release.** `deploy/testnet-miner/miner_health.sh::tree_at`
  and `deploy/join.sh::tree_at` print `at v0.3.0`, or `not at a release tag`, where they printed the kit and epoch
  numbers. The refusals of `deploy/join.sh::verify_consensus_epoch` and `deploy/install.sh::clone_epoch_gate`, and the
  messages of `kit_update.sh`, speak of "the network rules" and name a tag in the same way: a refused kit reads
  `network rules: your tree is at v0.3.0 ...`, no longer `consensus epoch mismatch`. The numbers stay in the files
  and in the JSON keys.
- **The daily update accepts a tag only if the tag names itself.** A signature covers the header of the tag object
  and not the name a forge publishes the object under, so the signed object of an older release, republished under
  a newer name, would pass for the newest one. `kit_update.sh` reads the header and refuses an object that does not
  name itself as a release tag of a commit (`deploy/testnet-miner/kit_update.sh`, `CAND_HDR`).
- **The daily update follows a signers rotation only by whole key lines.** A key line that stays with other options
  (`namespaces=`, `cert-authority`, `valid-before=`), a changed comment on a key line, or a line that is not a key
  line changes what the file trusts without adding or removing a key, so it is a change of trust root and stays a
  gesture on the machine (`deploy/testnet-miner/kit_update.sh::changed_beyond_keys`). Comment and blank lines alone
  are not read.
- **The `.env` files that `join.sh` writes are closed before they are written.** The miner kit's file carries the
  relay token. Before writing it, `join.sh` creates the file with mode 0600, or sets an existing one to 0600, so no
  other local user can read what this run writes into it; the node kit's file is handled the same way
  (`deploy/join.sh`, the `umask 077` and `chmod 600` line before each `cat >`).
- **The node image is built on bases named by digest.** The two base images of `docker/Dockerfile.node` are named by
  the digest of their multi-platform index, and the compiler by the `toolchain` line of `chain/go.mod`
  (`GOTOOLCHAIN` is that version, nothing is downloaded). `docker/Dockerfile.cloud` installs the Python packages of the
  miner image from the same lock, by hash (`docker/miner-requirements.txt`, `--require-hashes`). The release
  workflow checks that the `dendrad` of each node image it builds is the released binary, byte for byte, before it
  joins the platforms under one reference; a mismatch leaves `node-image.txt` out of the release
  (`.github/node_image_gate.sh`).
- **On Windows, the installer that `install.ps1 -Yes` runs for a kit that recorded its signers comes from a signed
  release tag, not from `main`.** When the clone is already there, its kit recorded `docker/RELEASE_SIGNERS`
  (`DENDRA_RELEASE_SIGNERS_SHA256`) and the clone still holds that file, `install.ps1` takes `install.sh`,
  `hw_probe.sh` and `kit_pins.txt` from the newest release tag that file verifies, after the same header check as
  `deploy/install.sh::tag_adopt`. A tag that does not verify stops the run; no older tag is taken in its place. A clone
  without a record takes `main`, as a first installation does (`deploy/install.ps1`, `$clone`).
- **The copy that `install.ps1` keeps for its resume task lives in the folder the system names** (the known folder
  for common application data), not in the one the `ProgramData` variable of the process names
  (`deploy/install.ps1::Install-ResumeCopy`).
- **The line for a kit left on v0.2.2 at the halt checks that the tag carries its own name** before the clone moves, as
  the daily update does: a signature alone does not tell which name a forge published the signed object under.
- **A verdict and a marker carry the weights digest of their model.** `services/reveal_worker.py::ModelAnchor`
  reads the SHA-256 digest of the model from the engine that serves the call (`read_weights_digest`, the `/api/tags`
  listing) and writes it with `--weights-hash`, next to `--model-id`, on the commit. A digest that is not found is not written, nothing is
  guessed, and the read is repeated every `WEIGHTS_RETRY_S` while it is missing. The judge worker does this for
  verdicts (`judge_worker.py::main`) and the reveal worker for markers. The chain checks the digest against the
  model registry only while `enforce_model_registry` is on. With it on and weights anchored in the registry, a commit
  without the digest is refused (`chain/x/jobs/keeper/msg_server_commit.go::CreateCommit`).
- `deploy/testnet/join_validator.sh` refuses before it builds, stops or writes anything when the binary built from
  the clone cannot replay the remote chain (`deploy/testnet/join_validator.sh::upgrade_gate`).
- `services/cli.py` reads the committee size of a submission from the chain's verification mode
  when `--k` is not given, in place of a fixed 3 (`services/client.py::_submit_committee_k`).

### The application, Windows and the refusals

- **Two switches in the Dendra application.** Judge runs `deploy/testnet-miner/judge.sh on` or `off`, and GPU mining
  moves the miner between the card and the CPU. A switch is greyed, with its reason, when the machine cannot take
  it: it cannot judge (the RAM floor of `deploy/hw_probe.sh`), it has no usable card (judging on the CPU is its whole
  role there), or it runs one identity per card (`deploy/app/dendra_app.py::switches_view`, `switch_judge`,
  `switch_gpu`). With the card off, the miner answers on the CPU with the judge model: the application seats the
  judge if it is not on, writes the four lines of `GPU_OFF_KEYS` in the kit's `.env` (`DENDRA_SERVE_ENDPOINT` and
  `DENDRA_SERVE_MODEL_ID` for the miner's engine and model, `DENDRA_CPU_KEEP_ALIVE` and `DENDRA_CPU_NUM_PARALLEL`
  for the CPU instance, all read by `deploy/testnet-miner/docker-compose.yml`), recreates the miner, then stops the card's
  engine; if the miner does not start, the `.env` is put back as it was (`_gpu_off`). `judge.sh off` refuses while the
  card is off, and the hourly check expects the idle card (`deploy/testnet-miner/miner_health.sh`, `SERVE_EP`).
- **"Test my machine".** The application measures the speed of the miner's engine in words per second and the time
  of one verdict, inside the miner container, by the calls the miner and the judge make. A figure it cannot read is
  shown as not measured, with the reason, and a machine that does not judge has no verdict to time. The last result
  is kept per identity (`dendra_app.py::run_machine_test`, `machine_view`, `speed_part`, `verdict_part`,
  `save_machine_test`).
- **Earnings by role.** The application splits what the ranked days of the Final Testnet Season earned the identity
  into work, verdicts, presence, the judge presence reward and the share of a probation day, each part being the published formula rerun on the
  day's published facts (`dendra_app.py::roles_view`, `earnings_view`). It also shows an estimate for the rest of
  the season, drawn from the average per day of the last `PROJECTION_DAYS` ranked days under the rules in force,
  and names the inputs it used, among them, while the season runs, the interval between blocks measured over the
  chain's last `day_blocks` blocks (`projection_view`, `block_seconds`, `blocks_left`). The estimate promises nothing.
- **The state in plain words.** One table of rules gives green, orange or red, each rule with the words that say it,
  the gesture to make and the page of the site that settles it. Green needs every fact to have been read: a fact
  that was not read is a rule of its own and fires orange (`dendra_app.py::STATUS_RULES`, `status_view`).
- **The header shows that state.** The pill at the top of the page shows the level and the word that `status_view`
  draws from the same table, so it cannot read green over an orange or red state; a state that was not read shows
  `state not read` (`deploy/app/ui/index.html::headerPill`). A rule of the table fires red when the health check C8
  fails: no capacity registry address, or no fresh signed capacity report of the identity that the chain attributes
  to its operator (`dendra_app.py::STATUS_RULES`, rule `capacity_report`;
  `services/miner_selftest.py::c8_capacity`).
- **A first screen of cards, and more views.** The application opens on a dashboard: one card per subject (the
  judge's mode and abstentions, the capacity report, the kit, the node, the relay, the work of the latest ranked
  day), each green, amber or red with one sentence (`dendra_app.py::dashboard_view`). A diagnosis view lists each
  problem the existing outputs name, with why it matters and what to do about it (`diagnosis_view`). A pay view
  lists every ranked day with its figures and what it paid, and the ranking's reason in plain words for a day that
  paid less than its gross; when one ranking cannot be read, the list is left out (`programme_view`,
  `pay_days_view`). A cards view gives one row per card and its identity: what it serves, its judge's memory, its
  work and pay of the latest ranked day, and its judge (`cards_route`). When something blocks the pay of the
  identity on screen, a desktop notification says so through `notify-send`, where the machine has it, and only
  while the page is open; a blocker is notified once, and again only after every blocker of that identity has
  cleared, and a setting turns the notifications off (`pay_blockers`, `alerts_tick`). The notifier reads its text as
  markup, so the application escapes it first and a value the miner's container wrote shows as plain text
  (`_notify`). The containers' log lines come with a plain sentence when the application has one for that line, and
  the raw line is one click away (`explain_log_line`).
- **The Judge lines.** The Judge line shows the judge's memory as the miner's container last recorded it: the mode,
  whether the memory check lets the model load (and why, when it holds the model back), whether the model is
  loaded, and the audits left out for lack of memory. The Judge seat line shows the judge's declaration as the
  daemon last read it from the chain, the height of the last verdict and the missed draws against
  `judge_missed_draws_max`; at or above that limit it says that, once the cause is removed, the daemon declares the
  judge again by itself, and gives the gesture by hand for a judge the daemon already declared again once: the
  Miner card's Restart, or the Judge switch off, then on once the chain holds no judge declaration
  (`dendra_app.py::judge_memory_view`, `judge_seat_from`, `judge_seat_limit`). It reads no
  `judge_verdict_window_blocks`, so a judge the window excluded (see "What the judge's memory reports") shows no
  exclusion there; the daemon declares that judge again by itself as well. A figure that was not read is shown as
  `?` or `not read`, and not as zero.
- **It opens in the Windows browser.** `deploy/app/open_windows.ps1` starts the application inside the WSL
  distribution and opens the Windows browser on it. The token is in no URL, command line or terminal: a one-time
  token goes to the application on its standard input, the browser opens an access page written in the Windows
  user's local application data, and that page posts the token once. Before the browser opens, the script checks
  that the program on the port holds the token (`dendra_app.py::windows_handoff`, `HANDOFF_PROOF_LABEL`). WSL forwards
  the fixed port to the loopback of Windows only, and the application stops when the script's window closes.
- **The session has two halves.** The session token travels only in a cookie (HttpOnly, SameSite=Strict, named
  after the port) that `/handoff` sets when it spends the one-time token; it is in no URL, page body or storage a
  script can read. A browser sends that cookie to every port of 127.0.0.1, so the cookie alone does not get in. The
  answer of `/handoff` also leaves a second value in the tab's sessionStorage, which only a page of the
  application's port can read, and the page sends it in a header with every call (`X-Dendra-Session`;
  `dendra_app.py::Handler._handoff`, `Handler._auth`, `SESSION_HEADER`). A tab opened by hand at the application's
  address holds no such value and does not get in. The tab that `/handoff` answered keeps the value when it is
  reloaded. Every launch gets in that way: the access file on Linux, the native window, and the access page of
  `deploy/app/open_windows.ps1`. A request that the browser marks as sent from any origin but the application's own
  (`Sec-Fetch-Site`, `Origin`) is refused even with both halves (`Handler._auth`), and each page the application
  serves carries a Content-Security-Policy that runs its own scripts only, by their hash (`Handler._send`,
  `page_csp`).
- **The page stops when the answers are not its application's.** Every answer to the session carries a third value
  (`X-Dendra-Instance`) that `/handoff` also left in the tab and that the page does not send (`Handler._send`,
  `INSTANCE_HEADER`). On a refused session (HTTP 401), or on an answer without that value, such as one from another
  program that took the port after the application stopped, the page stops its timers, makes no further call and
  says why in the pill at the top of the page (`deploy/app/ui/index.html::api`, `endTab`).
- **Two gestures take a code.** Showing the 24 recovery words and changing the payout address need a confirmation
  code of eight characters, which the application prints in the terminal or window that started it and does not
  send to the page. A code is given for one purpose and one change: for the payout, the address typed and the
  identity named, or every identity; for the recovery words, the identity whose words are shown. The line that
  carries the code names what it confirms: the purpose, the payout address, and the identity or every identity on a
  machine that runs several. A code typed for another change is refused (`dendra_app.py::App.issue_code`,
  `App.spend_code`, `code_target`). No code is printed for an address or an identity that the route rejects as
  written, so the payout's code needs a valid address first. A code works once, for five minutes (`CODE_SECONDS`). Each purpose
  keeps its own code, and a refused try counts against that code alone, and the code is dropped at its third
  refused try (`CODE_TRIES`). A launch that opens the browser without a terminal, such as one from the
  applications menu, prints no code: asking for one is refused with a sentence that says to start Dendra from a
  terminal by typing `dendra`, or to install `python3-gi` and `gir1.2-webkit2-4.1` for its own window
  (`NO_CODE_SINK`, `stdout_is_terminal`).
- **Stop comes first.** Stop interrupts the action in progress, in this copy of the application or in another copy
  on the same kit: the process group of the action's subprocess gets SIGTERM, then SIGKILL `STOP_GRACE_S`
  (5 seconds) later. Stop then stops the miner of the identity it names and of every identity the interrupted
  action acts on, as this copy recorded them or as the other copy wrote them in the kit's lock file
  (`dendra_app.py::App.take_stop`, `action_slots`, `Handler._stop`); when the action has not ended after
  `STOP_WAIT_S` (20 seconds), it stops them beside it. On slot 0, Stop names the judge's profile, so the judge's
  engine stops too, and when the interrupted action is the Judge or GPU switch, Stop also removes the one-off
  container that pulls the judge's model (`remove_judge_pull`). In the copy where Stop was pressed, no other action
  starts until Stop has ended (`App.release_stop`). Two copies on one kit, one opened from Windows and one from
  Linux, run one action at a time through a file lock in the user's configuration directory: an action asked of one
  copy while the other runs one is refused with the name of the action that runs (`kit_action_lock`). `judge.sh`
  changes the kit's `.env` one line at a time, through a temporary file in the same directory and one rename that
  keeps the file's mode, so a Stop that ends it does not leave the file empty or cut short
  (`deploy/testnet-miner/judge.sh::env_set`, `deploy/testnet-miner/slots.sh::slot_env_set`).
- **Node reachability.** `deploy/node_reachability.sh --json` prints one line of JSON and nothing else on the
  standard output (state, case, advertised address, peers in and out, height); the exit codes are the text mode's:
  0 other nodes have connected in, 1 the node cannot be reached or nothing has connected in, 2 not measured
  (`deploy/node_reachability.sh::emit_json`). The application shows whether the node can be reached from outside,
  names the public address when it can, and says how to open port 26656, which stays optional, with a case for a
  wildcard, loopback, private or link-local address, carrier-grade NAT, and a node nothing has connected to. The
  check only reads who connected in: it cannot dial the node from outside (`dendra_app.py::reachability_view`,
  `REACH_SAYS`, `open_steps`, `wsl_steps`).
- **The Windows installer continues by itself after the restart.** When `deploy/install.ps1 -Yes` has just installed
  WSL, it copies itself to a folder under the common application data folder that the system names, which only the
  LocalSystem account and administrators can change (read back after it is set), registers the one-time logon task `Dendra install resume` and stops. At the next
  logon, 30 seconds after, the task runs that copy with the same switches and `-Resume`, as the same user with
  administrator rights, in a window that stays open. When the distribution has not been opened once yet, the window
  goes on once that Linux user exists; after 30 minutes without one it stops and asks to run the file again. A resumed run disables the task when it starts, the
  task expires 14 days after it was registered and is then deleted, and a run started by hand with `-Yes` removes the task and the
  copy. Only one run works at a time, held by a Windows mutex of the logon session. `-NoResume` leaves no task
  (`deploy/install.ps1::Register-ResumeTask`, `Install-ResumeCopy`, `Wait-DistroAccount`, `Remove-ResumeTask`,
  `Enter-SingleRun`). **Limit:** the task is registered with administrator rights (run level Highest) and keeps
  them until it is deleted: a resumed run only disables it, and it is deleted when it expires or when a run started
  by hand with `-Yes` removes it. If the installation does not continue after the restart, run `deploy/install.ps1 -Yes` again, with the same
  switches.
- **A refusal points to the section of the site that settles it.** The kit scripts end a refusal with a `More:` line
  holding the address of that section (`site_url`, `site_topic` and `refusal_link` in `deploy/install.sh`,
  `deploy/join.sh`, `deploy/testnet-miner/judge.sh`, `kit_update.sh`, `backup-keys.sh`, `encrypt-keys.sh`,
  `exit-miner.sh` and `publish-capacity.sh`). When the relay refuses a request for access and sends a `help`
  field, the miner's log quotes it after the relay's own reason, and prints nothing in its place when the field is
  missing (`services/relay_client.py::_relay_fields`). The text a program reads in a refusal is unchanged:
  the address travels in a field of its own.
  Tests: `services/tests/test_refusal_help.py`.

### Across the upgrade: what the kit does, and what needs a gesture

- **A kit on v0.2.2 that runs its own node, with the automatic update on: nothing.** Its daily run verifies
  this tag and stages it without applying it (`deploy/testnet-miner/kit_update.sh::stage_candidate`): a clone
  of the tag under `upgrade-stage/`, the plans it registers, and its node image: the one the tagged tree pins, or,
  when it pins no usable one or that image cannot be pulled for this host's platform, one built on the host from
  `docker/Dockerfile.node` (`kit_update.sh::stage_image`).
  `deploy/testnet-miner/node_upgrade_watch.sh`, every minute, moves the clone and the node to this release when
  the node stops at the plan's height, then runs the kit's join with `--after-upgrade`. Reading on the kit:
  `bash deploy/testnet-miner/kit_update.sh --stage-check` (exit 0: a stage that holds, with its node image). A
  kit whose daily run has not staged it yet stages it with one run by hand:
  `bash deploy/testnet-miner/kit_update.sh`.
- **A kit on v0.2.2 without a node of its own (`--remote-rpc`, which the installer's `--light` sets), with the automatic update on: nothing.** It
  moves at its first daily run after the chain it reads answers the plan applied
  (`kit_update.sh::remote_upgrade`).
- **A v0.2.2 kit with the automatic update off** (`--no-auto-update`), HiveOS aside: turn it on before the halt,
  with `bash deploy/join.sh --auto-update` and the options you first gave the join. `kit_update.sh` does nothing
  while it is off.
- **A kit below v0.2.2** has no automatic update and no watch: move it to v0.2.2 before the halt. Download the
  installer of that tag, read it, then run it with the options you first gave it:
  `curl -fsSLO https://raw.githubusercontent.com/DendraNetwork/dendra-network/v0.2.2/deploy/install.sh`, then
  `bash install.sh --yes --ref v0.2.2`, with `DENDRA_DIR` set to the clone when it is not `~/dendra-network`.
  A kit on v0.2.0 or v0.2.1 can run the update line it prints (`git fetch origin && git reset --hard origin/main`
  on v0.2.0, ending in `git checkout -B main origin/main` on v0.2.1) until the kit deadline
  (`KIT_DEADLINE` in `network-info.txt`), and uses the installer after it. Do not run that line on a kit of v0.2.2 or later. From the publication of this release, that line brings the clone to this tree, which
  `join.sh` refuses until the network declares epoch 15: use the installer above instead. A clone moved there by the v0.2.1 line goes back with
  `git checkout -B main kit-before-update` (the branch that line wrote), and then follows the installer above;
  the v0.2.0 line writes no branch, so a clone it moved follows the installer above directly. A kit that was on v0.2.2 needs no installer,
  but it lost its stage: the `kit_update.sh` of the tree it moved to removes a staged release whose epoch the clone
  already runs. After the move back, `bash deploy/testnet-miner/kit_update.sh` stages this release again, and
  `bash deploy/testnet-miner/kit_update.sh --stage-check` exits 0 once the stage holds.
- **After the chain has applied the plan, before `network-info.txt` declares epoch 15,** a kit whose clone is
  already on this release is refused by `join.sh` unless the run names the plan (`network rules: your tree is at
  v0.3.0`). The refusal asks the chain whether the plan is applied, and prints the exact re-run with
  `--after-upgrade epoch-15-jurors-are-judges` when it is, or when the kit's node stands at the halt, without naming
  v0.2.2. The kit runs the line `join.sh` prints. A kit that the upgrade watch set up records its re-run with the
  plan (`deploy/join.sh::kit_update_hint`). Once the network declares epoch 15 the option is no longer needed, and
  the next join records the re-run without it.
- **A kit that stayed on v0.2.2 at the halt** (its watch was off, nothing was staged, or the only update line it
  printed ended in `git checkout -B main origin/main`, a line not to run on a v0.2.2 kit): running `join.sh` again names v0.2.2, and
  `--after-upgrade` on the v0.2.2 tree does nothing. Move the clone to the verified v0.3.0 tag, then run the
  recorded re-run of that tree with the plan. From the folder of the clone that holds `deploy/join.sh`, with nothing
  to replace, the command below does nothing if the tree is not at epoch 14, if no re-run is recorded, or if the
  chain does not answer that the plan is applied. The join confirms the halt (`node_halt_confirmed`) and starts the
  node on the v0.3.0 image.
  If it refuses before it says that the node starts on this tree's node image, `git checkout kit-before-update` puts
  the clone back: the node has not changed image. From that line on, the node's `.env` no longer names the old image
  (`deploy/join.sh::node_env_image_reconcile`), so keep the clone where it is: moving it back may leave the node on
  the new image under the older kit. That covers a failed `docker compose up` for the node, and the refusal that
  says `Nothing is rolled back` and `Keep the clone on this tree` (the plan is not answered applied yet), which
  prints the command to run once the node answers the plan applied (`deploy/join.sh::after_upgrade_finish`).

  ```
  test "$(head -1 docker/CONSENSUS_EPOCH)" = 14 && R="$(sed -n 's/^DENDRA_KIT_RERUN=//p' deploy/testnet-miner/.env | tail -1)" && test -n "$R" && python3 -c 'import json,sys,urllib.request; h=json.load(urllib.request.urlopen("http://testnet-api.dendranetwork.com:1317/cosmos/upgrade/v1beta1/applied_plan/epoch-15-jurors-are-judges",timeout=20)).get("height",0); sys.exit(0 if str(h).isdigit() and int(h)>0 else 1)' && git fetch origin refs/tags/v0.3.0:refs/tags/v0.3.0 && git -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile=docker/RELEASE_SIGNERS verify-tag refs/tags/v0.3.0 && test "$(git cat-file tag refs/tags/v0.3.0 2>/dev/null | sed -n 's/^tag //p;/^$/q')" = v0.3.0 && git branch -f kit-before-update && git checkout --detach refs/tags/v0.3.0 && eval "$R --after-upgrade epoch-15-jurors-are-judges"
  ```

- **A HiveOS rig** keeps its miner running and changes its Installation URL to the package of this release once
  the chain has halted at the plan's height; a rig with `LIGHT=1`, once the chain has passed that height.
- **A rented-pod image** is redeployed on the image of this release once `network-info.txt` declares epoch 15
  (`docker/cloud-start.sh` refuses another epoch through `deploy/join.sh::verify_consensus_epoch`).
- **A new installation** takes v0.2.2 until the halt (`bash install.sh --yes` places the clone on the newest
  release of the declared epoch), and a kit installed then is staged like any v0.2.2 kit. That release carries no pin file (`deploy/kit_pins.txt`), so a machine with no Docker and Compose v2 (or, with a GPU, no NVIDIA container toolkit) installs them by hand first: the installer stops, exit 2, before installing anything from those repositories. This is a limit of the window: a release that carries the pin file installs them itself. On Windows, `install.sh` reads the pin file next to it (`deploy/install.sh::KIT_PINS`) and the copy `install.ps1` runs has one, so the limit does not apply there. From the halt until
  `network-info.txt` declares epoch 15, wait before installing a new machine: `bash install.sh --yes` still places
  the clone on v0.2.2, whose node binary stops at the plan's height. `join.sh` accepts a clone of this tree only
  when the run names the plan and the kit's node answers the plan applied or stands at its halt, or, for a kit
  without a node of its own, the chain RPC it reads answers the plan applied; a kit with no node container yet is refused, since a new node is not started at a
  plan's halt (`deploy/join.sh::verify_consensus_epoch`, `after_upgrade_applied`). Once the network declares epoch
  15, the installer places the clone on this release as it does for any release.
- **A node started on the binary of this release before the upgrade**, such as a node mounted by hand with
  `docker compose`, stops at its first block, and its data stay those of epoch 14: the v0.2.2 binary goes on from
  them. What its log says depends on those data. Data that end before the block where the upgrade proposal passed
  give the line `consensus epoch guard`: a node that was following the chain keeps running, its RPC answering at a
  height that no longer moves, and a node that was catching up exits, and so does each restart. Data that hold that
  block hold the plan, and the upgrade module stops the node first: its log says `BINARY UPDATED BEFORE TRIGGER`,
  and the guard writes nothing.
  Watch the node's log for these lines, and its height, not the state of its container. A node of this release does
  not replay the history of `dendra-testnet` from its genesis either: it refuses that genesis at its start
  (`chain/app/epoch_guard.go::refuseEpoch14Genesis`), and with it a zero-height export of that chain, which
  keeps its chain id and genesis time. It joins by state sync from a snapshot taken after the plan's height, or
  replays with v0.2.2 up to the halt and then moves. A state sync restores the newest snapshot its peers offer and,
  after one that fails, an older one, which can lie below the plan's height while the network still keeps snapshots
  taken before it: such a node stops at its first block, and the join says so and names the gesture, a new node
  volume (`deploy/join.sh::wait_for_sync`). A network that skips the plan with `--unsafe-skip-upgrades` stays on
  v0.2.2.

Reading, for anyone: `curl -sf https://testnet-api.dendranetwork.com/network-info.txt` (the `CONSENSUS_EPOCH`
line), `curl -s http://testnet-api.dendranetwork.com:1317/cosmos/upgrade/v1beta1/current_plan` (the pending
plan and its height) and
`curl -s http://testnet-api.dendranetwork.com:1317/cosmos/upgrade/v1beta1/applied_plan/epoch-15-jurors-are-judges`
(the height the plan ran at, 0 or absent until it ran).

### Services run by the network

- The services read the chain over the node's REST and RPC in process, without starting `dendrad` for each read
  (`services/client.py`); transaction search and decoding stay on the CLI.
  `DENDRA_CHAIN_READ=cli` in the network's deployment returns every read to the CLI, and any other value but
  `rest` or empty stops the services at start.
- The relay keeps its `/list` answer per suffix while the store has not changed, bounded in entries and in
  bytes (`services/relay.py`). The Proof builds its feed in a background thread, one build at a
  time, and waits between two builds the longer of `DENDRA_PROOF_REFRESH_S` and four times the last build
  (`services/the_proof.py::refresh_gap`). The capacity registry caches its on-chain reads
  (`DENDRA_CAPACITY_ONCHAIN_CACHE_S`, `DENDRA_CAPACITY_NODE_CACHE_S`), and the season service reads the chain's
  status every `DENDRA_FINAL_SEASON_WATCH_S`.
- In `docker-compose.yml`, the other services that start without a profile and those of the `public` profile carry
  a lower CPU weight than the chain, the validator a negative out-of-memory score adjustment (`oom_score_adj`), and
  the chain container takes a Go memory limit from `DENDRA_CHAIN_GOMEMLIMIT`, empty by default.
- `deploy/testnet/publish_network.sh` publishes nothing while the snapshot it would publish lies below the height
  where the chain applied an upgrade plan of its tree, or when it cannot read whether the chain applied one: the
  file served before the run stays, and the run names the height to wait for.
- The faucet's refusal of a stale kit names the installer instead of an update line that ends in
  `git checkout -B main origin/main`: download `install.sh`, read it, and run `bash install.sh --yes` with the
  options of the first installation (`services/faucet.py::REFUSAL_STALE_KIT`).
- The capacity registry follows the kit floor. When `DENDRA_KIT_MIN` names a floor the registry can read, the
  reports that declare a kit below it, or none, leave the declared list `nodes` and the top-level totals, and
  `below_kit_min` counts them, stale ones included (`services/capacity_server.py::kit_filter_state`,
  `aggregate`). `kit_filter.state` reads `applied`, `dormant` (no floor set, nothing left out) or `unreadable` (a
  floor is set and cannot be read, and nothing is left out); `below_kit_min` is null unless the state is
  `applied`. The `verified` block keeps the signed reports of every kit, `verified.kit_versions` included.
  `GET /capacity` on the registry carries both fields, and `/metrics` carries `dendra_capacity_below_kit_min`
  while the filter applies.
- The capacity registry answers 503 when its store cannot be used: `GET /capacity` and `/metrics` answer
  `capacity store unavailable` while the file cannot be read, a report gets the same answer while the file cannot
  be read or written, and each of these logs the way out (`capacity_server.py::capacity_view`,
  `store_unavailable`). A store that does not exist yet reads as empty.
- The gateway's free tier charges an unsettled reservation across a restart. With `DENDRA_QUOTA_FILE` set, a
  free-tier reservation whose real use has no proven ceiling (the flat rate: `DENDRA_DYN_PRICING` other than `1`)
  is recorded as open in the same atomic save as the counters, and leaves with its settlement. A restart on the
  same day that finds one still open charges that key its daily cap (`DENDRA_FREE_TOK_IP`) and closes the free
  tier to every key until the daily reset (`services/gateway.py::_charge_open_at_cap`). A
  reservation the gateway cannot save is refused, and the client receives HTTP 503 with the code
  `quota_unavailable` (`gateway.py::quota_reserve`, `run_dendra`). With metered pricing, a settlement is
  capped at the estimate the escrow covers (`quota_settle`).
- The faucet saves its anti-abuse state before it grants a drip; when that save fails it answers 503 with
  `state unavailable` and grants nothing (`services/faucet.py`, `_rate_ok`). Its default listen
  address (`DENDRA_FAUCET_HOST`) is the loopback. Listening beyond it with `DENDRA_FAUCET_POW_BITS` at 0 prints a
  warning that names that setting, and `DENDRA_PUBLIC=1` without proof of work still refuses to start
  (`faucet.py::exposure_check`).

## v0.2.2

Kit version 3, consensus epoch 14: the scripts and services an operator runs changed, the state machine
did not. A node left on v0.2.1 keeps following the chain. A kit behind the published kit version still
joins; `deploy/join.sh` prints the update line and the miner prints "Update available" until the kit is
updated. A kit on v0.2.1 takes that line once, by hand: the automatic update starts with this release.
From `KIT_DEADLINE`, the relay refuses a kit below `KIT_MIN`, and the season stops paying it from the day it
announces (below); the faucet refuses a request that declares a kit below `KIT_MIN`. The network publishes
both values in `network-info.txt`.

### The name was pushed twice

On 2026-10-16 a first `v0.2.2` was tagged on commit `5a668979f2` at 12:36 UTC, and its release was published
at 12:46 UTC. It lacked the path to the next consensus epoch; the release was withdrawn and its tag deleted
at 13:31 UTC. This release is the second one under the name: tagged on commit `0821f82961` at 22:39 UTC,
published at 22:49 UTC. A clone that fetched the first tag still holds it, since `git fetch` does not move a
tag the clone already has. `git rev-parse 'v0.2.2^{commit}'` prints the commit the clone's tag names; when
it starts with `5a668979`, `git fetch --force --tags origin` moves the tag to this release. The name
`v0.2.1` was pushed twice as well, on 2026-10-11, with no release under the first tag.

### The judge

- The judge is on by default. When `deploy/hw_probe.sh --can-judge` answers `true`, `deploy/join.sh`
  seats the judge at installation and at each update; `false` or `unknown` seats nothing and says why.
  On a machine with a usable GPU, `--no-judge` declines, and the choice is kept for later runs and for the
  automatic update. Before a first default seating, at installation or on an update, the kit reads the free
  disk where the judge model lands against `DENDRA_MIN_DISK_GB_JUDGE` and prints what it downloads; a free
  disk it cannot read seats nothing.
- `deploy/testnet-miner/judge.sh on`, `off` and `status` are the switch of a single-identity kit; a kit with
  one identity per card switches by running `deploy/join.sh` again with `--judge` or `--no-judge`. `status`
  reads what the judge produced on chain, with the height the chain was read at; a running process proves
  nothing by itself.

### Updating

- The kit updates itself, within bounds. `deploy/testnet-miner/kit_update.sh` runs once a day from a
  crontab line `deploy/join.sh` installs. It moves the clone to a newer release of the same consensus
  epoch, verifies the tag's signature with the clone's `docker/RELEASE_SIGNERS`, prints its plan,
  keeps the tree it leaves on the branch `kit-before-update`, writes `.kit-update.pending` while it runs,
  and returns to that tree when the miner's heartbeat does not come back within a bounded wait. It never
  moves to a branch; a release of another consensus epoch is handled as the next section says.
  `DENDRA_AUTO_UPDATE=0` in the kit's `.env`, or `deploy/join.sh --no-auto-update`, turns it off. A first
  installation is never automatic.
- A kit release on a node that was installed with a pinned node image is adopted: `deploy/join.sh` pulls the
  new pin by digest and recreates the node without a build (`deploy/join.sh::node_env_image_reconcile`);
  when the tree pins none, the node is built from the clone; another consensus epoch is refused, outside
  the join after a governed upgrade (next section).
- The miner's heartbeat carries `kit_behind` and `kit_latest`, null until both numbers were read
  (`services/miner.py`), and the desktop application shows a banner when the kit is behind.

### Across a consensus upgrade

- On a kit that runs its own node, a release of a higher consensus epoch is staged and not applied by the
  daily update: the update clones and verifies it and pulls or builds its node image, and
  `deploy/testnet-miner/node_upgrade_watch.sh`, run every minute from a cron line installed next to the
  daily one, moves the clone and the node only when the chain halts at the plan height and the node
  confirms the plan and the height. The join it then runs, `deploy/join.sh --after-upgrade <plan>`, accepts
  the epoch difference once the node answers `dendrad query upgrade applied <plan>` with a height. A failed
  join is tried again, up to `DENDRA_UPGRADE_JOIN_MAX_ATTEMPTS` joins in all, then the watch prints the
  command to run by hand. A kit without a node of its own moves at the first daily run after the chain it
  reads answers a plan of that release as applied (`deploy/testnet-miner/kit_update.sh::remote_upgrade`).
  A rig with the automatic update off (HiveOS) makes that move by hand: `deploy/hiveos/README.md` says how.
- The miner daemon reads whether the chain applied the epoch 15 plan (`dendrad query upgrade applied
  epoch-15-jurors-are-judges`, then the jobs module version). On the epoch 14 chain it sends no
  `kit_version` in its presence proofs and declares no judge, since that chain decodes neither; on the epoch
  15 chain the declaration follows the container's judge role and its drain, sent only when a reading of
  the chain holds the other state (`services/miner.py`).

### Signed releases

- Each release tag is signed with an offline SSH key of the project. `SHA256SUMS.sig`, attached after the
  release is built, signs `SHA256SUMS`, the checksums of the release archives; the HiveOS package has its
  own list, `SHA256SUMS.hiveos`, with its own signature `SHA256SUMS.hiveos.sig`. The public key ships as `docker/RELEASE_SIGNERS` (allowed-signers format,
  principal `releases@dendranetwork.com`). `deploy/join.sh` and `deploy/install.sh` verify the tag's
  signature whenever they install or update a tagged release, and accept an SSH signature only. The
  release workflow verifies the tag before it builds anything. Reading, for anyone, in a clone:
  `git -c gpg.ssh.allowedSignersFile=docker/RELEASE_SIGNERS verify-tag v0.2.2`; for the archives,
  `ssh-keygen -Y verify -f docker/RELEASE_SIGNERS -I releases@dendranetwork.com -n dendra-release -s SHA256SUMS.sig < SHA256SUMS`,
  then `sha256sum -c SHA256SUMS`; for the HiveOS package, the same two commands with `SHA256SUMS.hiveos`
  and `SHA256SUMS.hiveos.sig`. A first installation from `main` trusts HTTPS to the repository, and the
  installer says so in its plan.
- The automatic update follows a `docker/RELEASE_SIGNERS` that adds or removes a key. One that swaps a
  key is a manual step: `deploy/join.sh --accept-new-signers` prints the new fingerprints and records them.

### Joining and the relay

- A new miner retries its two start-up deposits until the relay accepts them
  (`services/miner.py`), and the relay re-reads the registry when it meets a miner it does
  not know yet, at most once per `REGISTRY_RETRY` (`services/relay.py`). Before, a miner
  registered after the relay's last read got `UNKNOWN_MINER` on its deposits, and did not try them again
  until it restarted.
- Signed deposits, the capacity report and the faucet request carry `kit_version`, read from the file the
  image ships (`services/modea/kit_version.py`). From `KIT_DEADLINE`, the relay refuses a deposit
  from a kit below `KIT_MIN`, or one that declares none (`STALE_KIT`). The faucet refuses a request that
  declares a kit below `KIT_MIN` and serves one that declares none, since the wallet page, the gateway's
  funding and `deploy/testnet/join_validator.sh` send none (network side,
  `services/faucet.py::H.do_POST`).
  Both values are set in the network's deployment (`DENDRA_KIT_MIN`, `DENDRA_KIT_DEADLINE`), the pair
  `network-info.txt` publishes; the relay prints at start whether its kit floor is dormant, announced
  or armed, and `GET /stats` reports it. Reads stay open, so an old kit can still read what it needs to
  leave.

### HiveOS

- The payout address is declared locked with its first declaration (`h-run.sh` passes `--payout-lock`),
  and `h-config.sh` prints it before anything starts. A locked address cannot be corrected afterwards.
- `deploy/testnet-miner/backup-keys.sh` writes one encrypted archive per key volume, its keyring passphrase
  file inside, under a passphrase the operator gives and that is never printed, and restores it after
  verifying it; `deploy/uninstall.sh`
  backs up through it. The package carries the script, and `h-config.sh` says where the backup goes and
  that a lost key is a lost identity.

### Season services, run by the network

- A rule set the season switches to on an announced day (`services/final_season_rules.py`,
  `DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY`): no pay for the day to an identity whose latest signed
  capacity report, at or before the day's end, declares a kit below `KIT_MIN`, or that has no such report;
  0.5 DNDR per active judge per day under the five conditions of `final_season_facts.py::judge_presence`; 60 percent of the pay during a
  new identity's first full day. Its fingerprint names the floor; the season status publishes the day
  (`kit_rules_from_day`) once it is set, and the floor in `rules` from that day on.
- Support for a second grader on the CPU of the network's host, with its own token (compose profile
  `grader-cpu`, `DENDRA_FINAL_SEASON_GRADER_CPU_TOKEN`). The release ships the code, and the grader stays
  off until the operator starts that profile, pulls its model, sets the token and schedules the grading run
  on the host; an answer is incoherent only when every
  grader that graded it says so (`services/final_season_facts.py::answer_incoherent`). The status carries
  `ungraded_ranked_days`, the ranked days whose sampled answers received no grade.
- The payments tool writes the season's list of addresses and amounts with its SHA-256
  (`services/final_season_payout.py`, `--season-list`) and archives the published rankings
  (`--archive-rankings`); the generator opens no request inside the stop margin before the season's end.
- The faucet and the gateway sign from their own keyring volumes (`faucet-keys`, `gateway-keys`), each
  adopted once from the chain's shared keyring (`docker/entrypoint-services.sh::adopt_service_key`).

### Also

- The Proof page partitions requests by origin (the season generator, the chat gateway, other clients),
  and the explorer shows an adoption table that counts live nodes by the kit version they report; a report
  without the field reads "not reported".
- The Proof feed dates the age of the last block at the moment it read the chain (`chain.read_at`) and
  publishes how long its build took (`refresh.build_s`).
- The README's client section says how a first request reaches the network today, with runnable examples
  under `examples/`.
- The roadmap board on both roadmap pages is drawn from `community/roadmap-discord/roadmap.json`.
- The docs page gains a coverage section: what each tier of verification sees and what it does not; the
  confidentiality pages say who reads a prompt in plain text on the public network.
- `deploy/launch/ship_chain_image.sh` builds a chain image, ships it to a host and compares it, without
  starting anything there.

## v0.2.1

Kit version 2, consensus epoch 14: the scripts and services an operator runs changed, the state machine did
not. A node left on v0.2.0 keeps following the chain; a kit behind the kit version the network publishes
still joins, and `deploy/join.sh` warns that its kit is behind when it runs.

### Which machine does what

- No mining model runs on the CPU. `deploy/hw_probe.sh --role` decides the role of a machine (`miner`,
  `judge`, `refused` or `unknown`, followed by the reason), and `deploy/join.sh`, `deploy/install.sh` (with
  `deploy/install.ps1` and the HiveOS package through it) and the rented-pod image apply that one decision.
  A machine with a usable NVIDIA GPU mines, unchanged. Without one, it joins as a judge on the CPU when its
  system RAM reaches `MOE_CPU_MIN_RAM_MB` (`deploy/hw_probe.sh`) and is refused below it, with what is
  missing; a RAM that cannot be read decides nothing. `--no-gpu` decides as if no card were usable, for a
  card that Docker cannot reach.
- A judge on the CPU pulls no mining model. Its CPU instance (`ollama-cpu`) holds the judge model and the
  embedder, serves the requests the chain assigns to the machine with the judge model, and keeps it loaded
  for an hour after each use (`OLLAMA_KEEP_ALIVE`; the judge's own calls ask the same through
  `DENDRA_JUDGE_KEEPALIVE`). `--judge` is implied, and a `DENDRA_MODEL_ID` naming another model is refused.
- `DENDRA_JUDGE_MODEL` must name a model on the judge allow-list of `deploy/hw_probe.sh`
  (`bash deploy/hw_probe.sh --judge-allowed <model>` answers `true` or `false`); `join.sh` refuses any other
  before anything is written (exit 2).
- The miner's container checks, before the miner starts, where its engine holds the served model
  (`serve_guard.py`, run by `docker/entrypoint-services.sh`): on a GPU, or on the CPU for the judge role only.
  A mining model the CPU holds is refused (`SERVE REFUSED` in the miner's log), whichever way the container
  was started; a placement that cannot be read within `DENDRA_SERVE_GUARD_WAIT_S` starts nothing either.
- `deploy/install.sh` reads the role before anything changes: `--miner` on a host without a usable GPU is
  refused (exit 2), as is a host with no role, and a host whose role is not decided gets nothing installed
  (exit 3). Downloaded alone, without the probe next to it, it places the clone first and asks the clone's
  probe, before Docker, the NVIDIA toolkit or the docker group are touched. The judge's disk floor
  (`MIN_DISK_GB_JUDGE`) is lowered to the miner's only when `deploy/hw_probe.sh --can-judge` answers
  `false`; any answer that is not a reading keeps the judge floor. A refusal of `join.sh` (its exit 2) is
  passed on as exit 2 (`join_refused`), never folded into a failure. On HiveOS, `ROLE=miner` on a rig
  without a usable card is refused; the rented-pod image mines only when the probe answers `miner`.
- `deploy/install.ps1` registers its logon task after `install.sh`, and not when `install.sh` refuses the PC.
- `deploy/hw_probe.sh` reads the system RAM with `LC_ALL=C`: a translated `free` no longer leaves it unread.
- The miner logs how long each answer took (`answered in N s, M tokens`) and records it in its heartbeat
  (`gen_s`).

### Joining

- First start: the miner makes its first relay deposits after its registration, and `deploy/join.sh`
  judges the miner from its current start and its heartbeat. An account the faucet has not credited yet is
  a normal first state, not a failure. `join.sh` exits 0 for a healthy miner, 1 for a failure, and 3 when
  the miner started but its health could not be measured within the bound (it keeps running and
  registering).
- A registration the faucet refused is tried again (`DENDRA_REGISTER_RETRY_S`, longer after an address
  cooldown); the log says `REGISTRATION DEFERRED` with the faucet's reason (address cooldown, IP quota,
  global cap, proof of work).
- A miner's new node state-syncs. For a node whose volume it creates, `join.sh` derives the light-client
  trust point over TLS from the servers `docker/STATESYNC_RPC` names in the clone, and requires all of them to serve the same
  block; it never reads a trust point from `network-info.txt`. A node whose peers offer no snapshot stays at
  height 0, and `join.sh` says so. `--replay` replays from the genesis for any role, and a validator's node
  replays unless `--statesync` is given. A trust point applies only to the genesis it was derived under
  (`STATESYNC_GENESIS_SHA256`), and `deploy/bond_validator.sh` refuses a node started from a snapshot
  unless `--accept-statesynced-node` is given.
- `docker/NODE_IMAGE` can pin a prebuilt node image by digest, for linux/amd64 and linux/arm64, whose
  `dendrad` the release compared with the released binary. `join.sh` pulls it for a new node only, on a
  Docker engine of its platform, and builds from the clone otherwise. A pinned miner image likewise starts
  only on an engine of its platform.
- The node kit publishes its RPC (26657) on `127.0.0.1` by default (`DENDRA_RPC_BIND` in
  `deploy/testnet-node/.env`; `0.0.0.0` for a node that serves a public RPC). A miner on the same machine
  reaches its node on the `dendra-chain` Docker network (`deploy/testnet-miner/docker-compose.local-node.yml`,
  enabled by the `COMPOSE_FILE` line `join.sh` writes into the miner kit's `.env`): start that kit from its
  directory, without `-f`.
- The default address of the network's settings is HTTPS:
  `https://testnet-api.dendranetwork.com/network-info.txt` (`deploy/install.sh`, `deploy/install.ps1`, the
  rented-pod image).
- New installer options: `deploy/install.sh --ref vX.Y.Z` clones a release tag and stays on it;
  `--payout-address`, `--owner` and `--gpus` are relayed to `join.sh` (`-PayoutAddress` and `-Gpus` on
  `deploy/install.ps1`). The addresses and `--ref` are checked on the whole value before anything changes.
- `deploy/join.sh` records in the miner kit's `.env` how this copy of the kit updates (`DENDRA_KIT_UPDATE`)
  and the command that runs it again with the options it was given (`DENDRA_KIT_RERUN`);
  `deploy/testnet-miner/miner_health.sh` and the desktop application print them. A package pinned to a
  release tag (HiveOS, `install.sh --ref`) keeps its own update instruction there (`DENDRA_KIT_UPDATE_HINT`,
  from `DENDRA_UPDATE_HINT`), read back by a re-run that names none, so the hourly check never sends a pinned
  copy to `main`. The recorded re-run writes card lists (`--gpus 0,2`, card UUIDs) as they are and quotes
  the clone's path. A value holding a character Compose would interpret is not written, and `join.sh` says
  why.
- `deploy/testnet/join_validator.sh` refuses a bond below one unit of consensus power
  (`sdk.DefaultPowerReduction`, 1000000 udndr) before any faucet drip, and counts the bond only once the
  chain reports the validator `BOND_STATUS_BONDED` after `create-validator`, never on the transaction's code
  alone. A faucet solver that cannot run is reported as such.

### Running

- `deploy/testnet-miner/miner_health.sh` checks a miner on its host and inside its container: exit 0 when
  every check is ok, 1 when one fails, 2 when one could not be measured. `join.sh` runs it at the end of a
  join and schedules it hourly; `deploy/testnet-miner/miner-health.ALERT` exists while a check fails (an
  alert) or could not be measured (a note). The miner image declares a Docker health check on the daemon's
  heartbeat.
- `miner_health.sh` reads the judge role inside the miner container (`active`, `mute`, `off` or `unknown`;
  `judge_role` in its document) and adds advice, which never changes its exit code: an identity whose role
  is `off` holds jury seats that give no verdict. Its fix for a missing schedule suggests `crontab -e`, not
  piping `crontab -l` into `crontab -`, which replaces the whole table when the read fails. `join.sh`
  schedules the capacity report and the validator jail watch as it schedules the self-test: a crontab it
  could not read is not rewritten, and the line to add is printed instead.
- A miner proves its availability only right after a test inference on the model it serves has answered
  within `DENDRA_PRESENCE_PROBE_S` (forwarded to the container by the kit's compose file, as
  `DENDRA_REGISTER_RETRY_S` is) and the engine has embedded a short text. A missing or silent model, or an
  engine that cannot embed, proves nothing and gives no request up; the heartbeat records the probe's state
  and duration.
- The miner reads its own slice of the relay's work queue (`GET /list?suffix=__<its id>`, revalidated with
  its ETag, so an unchanged slice costs no body). Against a relay that does not serve it, it reads the
  whole queue and keeps its own keys, as before, and says so once.
- `deploy/testnet-miner/publish-capacity.sh` sends only a signed capacity report of a registered miner. It
  reads, before signing, whether the chain records the identity with the signing key as its operator. Exit
  codes: 0 published, 1 a failure (nothing unsigned is ever sent), 2 the chain does not record the miner
  with this key as its operator (not registered yet, or another operator), 3 the registration could not be
  read. `join.sh` no longer posts an unsigned report when the containers start: it schedules the hourly
  publisher and runs it at once when it sees the registration. After publishing, it reads the registry's
  `onchain` block exactly as the public chat page reads it.
- The containers of the miner kit rotate their logs (`logging` in `deploy/testnet-miner/docker-compose.yml`).
- Desktop application: it shows the judge role the last check read in the miner container and the update
  instruction `join.sh` recorded. Its header pill no longer stays green on a quick check while the scheduled
  check is stale, or once the quick check itself is older than two schedule periods; the age of a quick
  check counts the time the page has kept it.
- HiveOS `h-stats.sh`: a health document that names no schedule period shows `stale?` (age not judged)
  instead of looking fresh.
- `deploy/install.ps1` prints the command that reads the PC's health from Windows, and where its alert file
  lives inside the distribution.

### Serving requests

- A request whose inference fails, or whose sealed answer the relay refuses, is no longer tried again at
  every pass. Each failure is kept in `job-failures.json` next to the miner's keys; the next attempt waits
  60 s after the first failure, 300 s after the second and 1800 s after each later one. A refused deposit
  sends the same sealed answer again, never a new inference, and neither it nor a refused anchoring is
  given up while the relay lists the request. A request is given up after `DENDRA_JOB_MAX_ATTEMPTS` failed
  inferences (default 3), each counted only when the engine answered and embedded a test request after it:
  a failure while the engine cannot is the engine's, and gives nothing up.
- The miner serves its newest requests first, and does not serve a new request older than
  `DENDRA_REQUEST_MAX_AGE_S` (default 600 s), whose age is read from its identifier; a request whose age
  cannot be read is served after the others. A request is skipped as too old only when this machine's clock
  and the time of the chain's latest block both say so (while no block time has been read, this machine's
  clock decides alone): a machine clock running ahead no longer leaves a present miner serving nothing, and
  the gap is reported in the log and the heartbeat (`clock_ahead_of_chain_s`).
- The availability proof comes first: the challenge is read at start, at the head of every pass and between
  two requests, at most every 30 s, and a new one is proven before the next request is served. A registry
  that cannot be read at start no longer holds back the proof of a registered miner; a miner the chain is
  known not to record still proves nothing.
- Owner mode: a failed reading of the registry or of `min_stake` is tried again at the next pass, instead
  of putting the prepared registration off by the reprint delay (`DENDRA_OWNER_REPRINT_S`); its notice is
  printed once per that delay.
- The miner compares the encryption key it decrypts with to the one the chain anchors for it. While they
  differ it sends no availability proof, prints the `rotate-miner-keys` command that anchors this node's
  key, and records the state in its heartbeat (`enc_key`); it never rotates the key itself.
- An answer too long for the embedding model's context is no longer refused at commit: it is embedded in
  pieces, cut at the ASCII whitespace nearest the middle while the engine refuses a piece, and the vectors
  are averaged by length (`services/modea/miner.py::answer_embedding`). An answer that fits is
  embedded as before, so an anchor already on chain stays checkable.
- A served model named without a tag is looked up in the engine as `<name>:latest`: its commits no longer
  carry an empty `weights_hash`.
- The heartbeat adds the engine's version (`engine_version`), `draining`, and counters since the daemon
  started: `inference_failed`, `deposit_refused`, `abandoned`, `requests_stale`, and `answers_on_time`,
  `answers_late` and `answers_wait_undeclared`, read against the wait a request declares (`wait_s`).

### Judging and reveals

- The judge checks a revealed answer against the primary's anchor with the miner's own function
  (`services/modea/miner.py::answer_embedding`). The judge of every earlier kit compared a 64-value
  vector with a 384-value anchor, and so abstained on every revealed audit.
  Two vectors of different
  lengths, or a cosine below `ANCHOR_IDENTITY_COS`, still abstain.
- While `DENDRA_JUDGE_DIVERGENCE_SLASH` is anything but exactly `1` (unset by default), an invalid result
  that comes from comparing the answer with the judge's own references (stages `slash` and `legacy`) is
  posted as an abstention, and an answer that is not a coherent attempt (stage `coherence`) is still voted
  invalid. A coherent cheat is therefore not convicted by kit judges: its retained fee unwinds to the
  client unless the jury upholds the answer. A kit judge votes valid when its references agree with the
  answer or when it proves the request ambiguous (`DENDRA_JUDGE_ABSTAIN_VOTE`, default 1), and an upheld
  answer is paid. The judge's log says at start which way the guard is set.
- Each reference answer is bounded by the output cap of the audited request (`max_out`, in its envelope at
  the relay); a request that set no cap leaves the engine's default (`OLLAMA_NUM_PREDICT`). Only the stages
  that generate references wait for that envelope: word salad is voted invalid even when it cannot be read.
- The judge judges the audits whose anchored jury names it (`dendrad query jobs audit-committee`); a jury
  it cannot read is judged anyway, since an unknown membership is not a refusal.
- An abstention that means the judge could not judge (an engine, the relay or the chain did not answer, a
  reference could not be generated, or the judge model's answer to the same-fact comparison or to the
  multiplicity check could not be read) is tried again after a pause that starts at 60 s and doubles up to
  30 minutes. Any other abstention is a judgment, given once per process. A failure on one audit no longer
  ends the pass.
- A revealed question is graded only once it opens the prompt commitment the primary anchored. A reveal
  without its salt, or a commitment that does not commit to the question, leaves only the coherence stage
  to vote, and the judge abstains otherwise (stage `prompt-unverified`, final); a commit that could not be
  read is tried again (`prompt-unreadable`). An audit retried after this judge's references agreed
  (`sc-unreadable`, `multiok-unreadable`) is not voted valid on a divergence sampled at the retry, and the
  coherence of one answer is read once per process, retries included.
- Each call to the judge model is bounded by `DENDRA_JUDGE_TIMEOUT_S` (120 s when unset): a call past it is
  an abstention on that audit, tried again later, and a value that is not a positive number makes the
  judge refuse to start (`[judge] refused` in the miner's log).
- A verdict the chain refuses, or whose inclusion was not seen, is kept and posted again later, never
  judged again; a verdict already on chain ends the audit for this judge. The judge prints its counters
  (seats, verdicts anchored, abstentions by stage, refusals) and writes them into its state file.
- Judge worker options: `--passes N` and `--retry-pause SECONDS` (defaults unchanged: run until stopped;
  60 s, doubling up to 30 minutes). `--once` is `--passes 1`, and also ends after a pass whose job list
  could not be read.
- A miner of this release seals the reveal of an audited job only for the jury the chain anchored on that
  job (`dendrad query jobs audit-committee`), each copy to the encryption key the juror registered on
  chain. A juror that registered none gets a copy sealed to the key the relay serves for it, which a relay
  that replaces that key can open. A miner of an earlier kit sealed each reveal for every other registered
  miner with an encryption key. The reveal's key and format are unchanged: a juror of an earlier kit opens
  its copy as before.
- Nothing is sealed while the anchored jury cannot be read, is not seated yet or is empty, or while a human
  dispute has a re-adjudication jury and no audit jury; no copy is ever sealed to a re-adjudication jury.
  The audit anchor is read again after a back-off until the job is resolved, and the jury read for a job is
  kept while the job stays open. A juror key that is not a usable X25519 key (not 64 hex digits, or a
  low-order point) counts as no key: that juror is reported and the others are served. A failure on one job
  no longer stops the reveal of the other jobs of the round. A `dendrad` without `query jobs
  audit-committee` is named in the reveal worker's log, and seals nothing.
- The reveal worker and the judge worker sign their relay deposits with the miner's resolved identity
  (`dm1...`, the name in `resolved-identity`). On an earlier kit they kept the key name the identity had
  before the kit renamed it, so `dendrad keys show` failed and their deposits went out unsigned; a relay
  that requires signed writes refuses them. On such a kit, setting `DENDRA_SIGN_KEY` in the kit's `.env` to
  the full resolved identity and restarting the miner container has the same effect.

### The miner kit's containers

- The reveal and judge workers are supervised (`docker/entrypoint-services.sh`): a worker that stops is
  started again after a pause that begins at 5 s and doubles up to 300 s, back to 5 s after a run of 600 s
  or more. `docker compose logs miner` gives the worker's own exit code, and worker lines reach the log as
  they are printed.
- The container stops with the miner daemon, also while the entrypoint waits for the miner's key (up to
  180 s) or for the judge model (up to 30 min). No worker starts after the daemon has stopped, and
  `restart: unless-stopped` starts the daemon and its workers again.
- `model-init` fails when any of its pulls fails, and the miner does not start without it.
  `judge-model-init` is tried again when its pull fails (`restart: on-failure`). A network failure heals on
  its own. A full disk heals only once space is freed: until then, each retry takes back space freed on the
  disk where Docker keeps its volumes, the node's volume included when the node runs on the same machine.
- The Ollama image is pinned by version and multi-platform digest,
  `ollama/ollama:0.34.4@sha256:8262851b2846b87c649eddf3e76beb270c52f4d1bc94559f47efde16b0841551`
  (linux/amd64, linux/arm64), for `ollama`, `ollama-cpu`, `model-init` and `judge-model-init`; it is the
  image `docker/Dockerfile.cloud` starts from, and the one `deploy/join.sh` runs to check that Docker sees
  the cards. Inspect it by its digest, not by its tag:
  `docker buildx imagetools inspect ollama/ollama@sha256:8262851b2846b87c649eddf3e76beb270c52f4d1bc94559f47efde16b0841551`.
  The kit's compose file does not set `OLLAMA_NUM_PARALLEL`. On a machine without a usable GPU, the
  override `deploy/join.sh` writes gives the CPU instance two slots (`OLLAMA_NUM_PARALLEL=2`), so the
  miner's requests and its presence probe do not wait behind a whole judge generation; each slot holds its
  own KV cache.
- Five settings reach the miner container from the kit's `.env`, empty by default, and empty means the
  reader's own default: `DENDRA_JOB_MAX_ATTEMPTS`, `DENDRA_REQUEST_MAX_AGE_S`, `DENDRA_JUDGE_TIMEOUT_S`,
  `DENDRA_JUDGE_DIVERGENCE_SLASH` and `DENDRA_JUDGE_ABSTAIN_VOTE`. `join.sh --gpus` carries them to every
  slot. For the first two, a value
  that is not a positive number is reported once in the miner's log and the default is used; for the timeout,
  the judge refuses to start; for the divergence switch, any value but `1` keeps the guard, and one that is
  neither `0` nor `1` is warned about at start.

### Keys, stake and payout

- A new miner's keyring is encrypted. `join.sh` creates a random passphrase in
  `~/.config/dendra/miner-secrets/keyring-passphrase` (`DENDRA_SECRETS_DIR`), outside the volume and the
  clone; the kit mounts it read-only into the miner, which hands it to `dendrad` on standard input.
  `--plain-keys` keeps a new keyring unencrypted. Keys that are already unencrypted stay so until
  `deploy/testnet-miner/encrypt-keys.sh` encrypts them (it prints a plan without `--yes`). Back up the `miner-keys` volume and the
  passphrase file together: an encrypted keyring does not open without its passphrase.
- `--payout-address` names where the Final Testnet Season pays a miner: its checksum is verified before
  anything changes, and the miner declares it once registered.
- `--owner` (owner mode): the named key, on another device, registers the miner and holds its stake; this
  machine's key operates it and never sends `create-miner`. It protects the stake, not the income: payments,
  the subsidy and the season's default payout go to the operator key.
- The desktop application shows, for each identity, the keys at rest, the payout address, the last health
  run and the exit command, and the recovery phrase until three of its words are typed back, after which it
  removes the phrase from the machine.
- The faucet (`faucet.py`, run by the network) can drip the chain's `min_stake`, read from the chain
  (`DENDRA_FAUCET_AMOUNT=min_stake`): what one identity locks to register. A `min_stake` that cannot be
  read, or that reads 0, refuses the drip. `GET /` on the faucet says the amount and where it comes from.
- Final Testnet Season, the unwound-audit rule set (`final_season_rules.RULES`):
  a programme request refunded because no audit of it concluded in time is paid as work, from the day the
  season service publishes as `unwound_audit_work_from_day`, when its answer reached the programme and a
  grading clears it: its own answer graded coherent, or, when it was not graded itself, its miner's grades
  that day not voiding the day's work. A request with no grade at all is not paid, nor one on which its
  miner was slashed or clawed back, and an unwound audit pays no juror. Under this rule set a day is final
  `audit_unwind_blocks` + `audit_resolve_timeout` + 200 blocks after its last counted block.

### More cards, other hosts

- `join.sh --gpus all` (or card indices, or card UUIDs) runs one miner identity per NVIDIA card, each with its
  own key, stake, faucet drip and recovery phrase, and one CPU judge per machine; `--plan` shows the plan and
  changes nothing. See `deploy/testnet-miner/README.md`.
- Rented GPU pods: the single-container image `dendra-miner-cloud`, which
  `docker/CLOUD_IMAGE` pins by digest once its release has built it, runs a miner only (no judge, no node of its own) on linux/amd64.
  `DENDRA_PAYOUT_ADDRESS` is required, and the pod declares it with a lock (`DENDRA_PAYOUT_LOCK=1`), which
  the programme accepts only as an identity's first declaration, or from its owner in owner mode. See
  `deploy/cloud/README.md`.
- HiveOS: a custom-miner package, `dendra-X.Y.Z.tar.gz` with `SHA256SUMS.hiveos`, runs `deploy/install.sh`
  on the release tag it was built for; without `YES=1` in its extra config it installs nothing. See
  `deploy/hiveos/README.md`.

### Leaving

- `deploy/testnet-miner/exit-miner.sh` reads the miner and simulates `delete-miner`, changing nothing. With
  `--yes` it first sets the drain, a file `drain` in the miner's key directory: the daemon then proves no
  presence and takes no new work, keeps filing the reveals its own audited jobs wait for, and says
  `draining` in its heartbeat. A draining daemon that restarts registers nothing while the file is there,
  even once the chain no longer lists it. Once the chain accepts the exit, the script stops the miner, deregisters it,
  checks that the remaining stake came back, and removes the drain. While the miner holds an obligation the
  chain refuses: the script prints the chain's message and what it can read of what holds the miner (its
  seats on open audit juries, its own jobs under audit and, when the chain bounds them, the height from
  which they unwind), and exits 2 with the drain left set: no presence proof and no new work, so no reward
  for either. `--wait` asks the chain again every minute until it accepts; `--undrain` gives the exit up,
  and every run that ends with the drain set says so and names `--undrain`. A refusal met after the script
  stopped a running miner, or a simulation that could not be read then, starts the miner again; a miner that
  was already stopped stays stopped.
- A jury seat keeps the exit refused while the miner stays eligible as a juror (`juror_freshness_blocks`
  after its last commit) and until that audit resolves or unwinds, up to `audit_unwind_blocks` +
  `audit_resolve_timeout` blocks after the job's settlement. Beside a refusal the script reads the
  availability-slash parameters (`avail_slash_bps`, `avail_slash_max`, `avail_fail_k`, `avail_fail_window`,
  `avail_epoch_blocks`) and says whether a draining or stopped miner is exposed: armed, dormant, or not read.
- A miner of an earlier kit does not know the drain: the script says so when the heartbeat does not confirm
  it, and `--wait` reads the heartbeat again (`draining: false` from a current daemon that has not finished
  its pass is not taken for an earlier kit). A drain marker left on a miner the chain no longer lists is
  reported, and removed with `--yes` once the miner is stopped. `--status` builds and pulls nothing: without
  the miner image, the registration is reported unknown, with the image named. `--help` gives the procedure
  for a machine without a usable NVIDIA GPU registered as a miner under an earlier kit: read what holds it,
  wait for the miner's own reveals (`docker compose -p dendra-miner logs miner | grep reveal`), then stop it
  and leave.
- `deploy/uninstall.sh` prints its plan; with `--yes` it backs the keys up outside the clone, verifies the
  backup, and removes what the kit set up, keeping the miner's keys volume and passphrase file
  (`--delete-keys` removes them, after that backup); `--keep-registration` removes the kit of a miner still
  registered, or whose registration cannot be read, and keeps its keys. `deploy/install.ps1 -Uninstall`
  runs it on Windows (`-DeleteKeys`, `-KeepRegistration`). It
  reads the registration once per miner key volume of this clone: a volume no reading covers is unknown, and
  a reading for a project started from another clone is set aside instead of refusing this one.

### Also

- Desktop application: a ranked day's formula is rerun under the rule set its ranking's fingerprint names
  among the sets the season published (`final_season_rules.rule_set_of`); a ranking with no fingerprint, or
  one that matches no published set, is shown without a rerun.
- Client: a refused `open-job` reports what the refusal said (its raw log, else its code, else the output).
  When the miner registry cannot be read, a request is no longer sealed with a key read earlier, which may
  have been rotated since: each committee member's own on-chain record is read, and the request is refused
  otherwise. An operator balance that cannot be read is reported as not read (`balance` is None), never as
  0, and the registry stays readable: the chain does not check that a miner's operator is an address, and
  one such registration made the registry unreadable for the exporter, the leaderboard and the CLI. The
  exporter sums the balances it read and publishes how many it could not (`dendra_miners_balance_unread`);
  an exporter that has not read the registry yet keeps its miner gauges and computes no R2, instead of
  failing at every refresh.
- `docker-compose.yml`, `local` profile (a single-box development run): it needs an NVIDIA GPU. `ollama`
  takes the host's cards, `model-init` pulls `DENDRA_MODEL_ID` before `miner1` starts, and `miner1` serves
  that model (`OLLAMA_MODEL`) and restarts (`unless-stopped`). An empty `DENDRA_MODEL_ID` stops `model-init`
  before any pull, and `miner1` does not start.
- `RegistryCache.summary()` replaces `resume()`; its counters are `reads_ok` and `reads_failed`, its last
  failure `last_error`, and its summary line reads `miners=` and `failed=`. `JobRegistry` follows.
- Release files, besides the `dendrad` binaries and `miner-image.txt`: the release workflow builds the node
  image (`node-image.txt`), the rented-pod image (`cloud-image.txt`) and the HiveOS package, and attaches
  each file once its job has passed.
- Documentation file names are in English: `docs/instantanes/` is now `docs/snapshots/`. A link to the old
  directory name no longer resolves.

What to do, for a kit on v0.2.0 (the line works until the kit deadline, `KIT_DEADLINE` in `network-info.txt`; a kit on v0.2.2 or later never runs it):
from your clone (`~/dendra-network` if the installer made it; on Windows, inside the WSL distribution), run

```
git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main
```

then run `deploy/join.sh` again with the same options. `git pull` does not work: each publication of this
repository is a new root commit. The tree the clone leaves stays on the branch `kit-before-update`.

- A node that serves its RPC to other machines: add `DENDRA_RPC_BIND=0.0.0.0` to
  `deploy/testnet-node/.env` before running `join.sh` again. A miner kit written by the v0.2.0 `join.sh`
  names `host.docker.internal`, which the loopback bind cuts off; the new run rewrites it.
- A machine without a usable NVIDIA GPU: the new run makes it a judge, or refuses it (exit 2) without
  stopping or removing what already runs; the refusal names the command that stops a miner kit already
  installed (`docker compose -p dendra-miner stop`). Before stopping a miner registered under an earlier
  kit, read `bash deploy/testnet-miner/exit-miner.sh --help`: its procedure reads what holds the miner,
  waits for the miner's own reveals and stops it, then deregisters it and gets the remaining stake back.
- Unencrypted keys stay unencrypted until `deploy/testnet-miner/encrypt-keys.sh` encrypts them at the same
  address, once their 24-word recovery phrase is written down.
- A miner kit that ran `ollama/ollama:latest` now runs the pinned 0.34.4: where `latest` had pulled a later
  Ollama, the engine moves back to an older version. From 0.40 on, the engine converts a model on its first
  run and keeps the original as a backup copy, and a model whose backup copy was removed has to be pulled
  again on a version older than 0.40. `model-init` and
  `judge-model-init` pull at every start, so such a model is downloaded again, the judge model included.
  Not measured on a volume written by Ollama 0.40 or later.
- Leave `DENDRA_JUDGE_DIVERGENCE_SLASH` unset: the kit's guard holds while it is anything but `1`.

## v0.2.0

The `dendra-testnet` network, at kit version 1 and consensus epoch 14.

- A new chain from a fresh genesis. Nothing carries over from the previous testnet: balances,
  registrations and stakes start empty. A miner key survives in its Docker volume and registers again.
- Consensus epoch 14: its state-machine changes are listed in `docker/CONSENSUS_EPOCH`.
- The Final Testnet Season pays miners until 7 November 2026, 23:59 UTC, read on block timestamps.
- `deploy/install.sh` (Linux) and `deploy/install.ps1` (Windows) prepare a bare machine and run
  `deploy/join.sh`; on a Linux desktop, `install.sh` also adds the Dendra application.
- Judging runs on the CPU: the judge seat needs at least 26 000 MB of system RAM, with or without a GPU,
  and runs the judge model the network pins; no judge is seated on the GPU.
- Release files: static `dendrad` binaries for Linux and macOS (amd64, arm64) with their checksums, and
  the digest of the prebuilt miner image (`miner-image.txt`).

What to do: join with `deploy/install.sh`, or with `deploy/join.sh` on a host that already runs Docker.
A clone of the previous testnet is brought to this tree by downloading `deploy/install.sh` again and
running it: it moves the existing clone in place, which `git pull` cannot do.
