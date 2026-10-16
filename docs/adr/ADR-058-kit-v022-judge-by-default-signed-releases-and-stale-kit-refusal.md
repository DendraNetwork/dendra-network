# ADR-058 — Kit v0.2.2: the judge on by default, a bounded automatic update from signed releases, and the refusal of stale kits at a published deadline

**Status:** Decided by the owner. The kit side is implemented by release `v0.2.2`; the network side (relay,
faucet, season) is in the same tree and in force at its redeployment; the chain side is ADR-059, not in force.
Written 2026-10-15; decisions 3 (staging and watcher) and 4 amended 2026-10-16. Records the owner's decisions of
2026-10-12 (the judge by default, the automatic update, the four refusals of a stale kit, the seven days)
and of 2026-10-15 (`KIT_MIN`, the signing key, the end of mining without a GPU moved to the kit
deadline, the HiveOS payout lock). The sections *Declared versus proven* and *What this does not decide*
are this record's analysis: they decide nothing.
**Implementation:** Decisions 1 to 4, 6, 7 and the kit side of 5 change what an
operator runs and ship under one `kit_version` bump, release `v0.2.2` ([ADR-050](ADR-050-release-versioning.md):
the kit alone moves, so the patch number moves). The network side of decision 5 (relay, faucet, season)
and decision 8 are redeployments of services on the network's host, without a release number. The season
rule set of decision 5 is recorded as a decision of [ADR-047](ADR-047-final-testnet-season-reward-programme.md)
when it ships. Nothing here changes consensus; what needs the chain is [ADR-059](ADR-059-consensus-epoch-15-jurors-are-declared-judges.md).

## Context

- **An audit concludes at a bar relative to its seats, and a seat without a judge never votes.** A
  slash or a vindication needs `chain/x/jobs/keeper/antievasion.go::auditRelativeBar` votes in its
  direction: the larger of the jury floor (`chain/x/jobs/keeper/antievasion.go::effectiveSlashFloor`)
  and two thirds of the anchored seats still registered. The jury is drawn among present, vital miners
  with no judge role (`chain/x/jobs/keeper/miner_vitality.go::eligibleAsJurorInWindow`,
  `chain/x/jobs/keeper/presence.go::minerPresentAt`), up to
  `chain/x/jobs/keeper/audit_committee.go::auditCommitteeDrawSize` seats unless
  `audit_committee_draw_size` says otherwise. A miner that runs no judge worker holds a seat and says
  nothing; enough such seats keep every audit below the bar, and the audit is deferred, then unwound
  ([ADR-044](ADR-044-mute-miner-payoff-removed.md), [ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md)
  finding 4). How many audits concluded by the jury is a reading, never a statement of this record:
  `resolved_by_quorum` at `https://testnet-proof.dendranetwork.com/proof`.
- **The kit does not seat a judge unless asked.** `deploy/join.sh` starts from the miner role and seats a
  judge on `--judge` or on a machine without a usable card; `deploy/install.sh` asks for the judge role
  by default and `--miner` declines it. The probe answers whether a machine can judge
  (`deploy/hw_probe.sh --can-judge`: `true`, `false` or `unknown`) and what its role is
  (`deploy/hw_probe.sh --role`: `miner`, `judge` or `refused`): a judge runs the model `audit_judge_model`
  names on the CPU, gated on `deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB` of system RAM and not on a GPU
  ([ADR-057](ADR-057-judge-anchor-check-and-single-judge-model.md) decision 1), and needs the disk floor
  `deploy/install.sh::MIN_DISK_GB_JUDGE` for its model. The judge worker is switched by
  `DENDRA_MINER_JUDGE` in the miner's compose file (`deploy/testnet-miner/docker-compose.yml`, service
  `miner`), and its model is pulled by the service `judge-model-init` of the same file.
- **A kit behind the network is told, never refused.** `deploy/join.sh::verify_kit_version` compares
  `docker/KIT_VERSION` of the clone with the `KIT_VERSION` line of `network-info.txt`, which
  `deploy/testnet/publish_network.sh` reads at the latest release tag, and prints the update instruction
  (`deploy/join.sh::kit_update_hint`). `deploy/testnet-miner/miner_health.sh` repeats that comparison
  hourly (check H3) and writes a local alert. The miner daemon says nothing about its version: its
  heartbeat carries the engine's version (`services/miner.py::engine_version`) and the
  signed capacity report carries no kit version (`services/capacity_server.py::_clean`). Neither
  the relay, nor the faucet, nor the season programme reads a kit version anywhere.
- **A release is not signed.** The project's release script creates an annotated tag under the project's
  identity, pins that identity and re-reads it before the push, and pushes the tag;
  `.github/workflows/release.yml` verifies the tagged tree, builds the binaries and writes `SHA256SUMS`
  and `SHA256SUMS.binaries` in its `publish` job, which creates the release with `--verify-tag`. That
  flag checks that the tag exists on the repository, not who made it. Nothing in `deploy/install.sh`,
  `deploy/join.sh` or the kit checks a signature: the trust of a first installation is HTTPS to the
  forge, as it is for the genesis pin ([ADR-052](ADR-052-installation-trust-anchor-and-node-image.md),
  *Context*). `install.sh` recognises a published tree by its shape, a root commit, when it moves a clone.
- **The owner's rule for installations stands**: an installer is downloaded, read and run, with nothing
  done without `--yes`; no `curl | sh`. An automatic update is a departure from that rule for the
  updates only, and the owner chose it on 2026-10-12 knowing the bounds listed under decision 3.
- **The end of mining without a GPU** below `MOE_CPU_MIN_RAM_MB` was announced for 2026-10-16 23:59 UTC,
  block time (`community/ANNONCE-DISCORD-FIN-MINAGE-CPU.md`). No code read that date: the kit tagged
  `v0.2.1` refuses a new machine without a GPU below the floor (`deploy/hw_probe.sh --role` says
  `refused`, and `deploy/join.sh` exits) and leaves a miner already in place running, and neither the
  chain nor the season rules can tell a CPU from a card. On 2026-10-15 the owner moved that end to the
  deadline of decision 5.
- **Two defects of the joining path.** A new miner sends its two bootstrap deposits (`pub`, `attest`) once,
  right after registration, and marks them done whatever the relay answered
  (`services/miner.py::_bootstrap_deposits`); a relay whose copy of the registry predates
  the registration refuses them as `UNKNOWN_MINER` (`services/modea/relay_write.py::_operator_authority`),
  since `services/relay.py::_refresh_cache` re-reads the registry only when its copy is
  older than `REGISTRY_EXPIRY` and at most once per `REGISTRY_RETRY`. Those deposits are then missing
  until the next restart. The first was promised a fix to a miner on 2026-10-13 (decision 7), the second
  decided the same day (decision 8).
- **The pod declares its payout address locked as soon as it is registered**
  ([ADR-053](ADR-053-cloud-and-hiveos-paths.md) decision 4); the HiveOS package declares it unlocked
  (`deploy/hiveos/dendra/h-run.sh` passes `--payout-address` without a lock). A rig runs its miner as
  root from a flight sheet on a disk the rig's software can read: the reason the pod locks holds for the
  rig.

## Decisions

1. **The judge is on by default on every machine that can judge** (owner, 2026-10-12).
   - `deploy/join.sh`, `deploy/install.sh`, `deploy/install.ps1`, the HiveOS package and, when it carries
     a judge, the cloud image seat one when `deploy/hw_probe.sh --can-judge` answers `true`. `false`
     seats nothing. `unknown` seats nothing and says why: an unread gate is not a pass. `--no-judge`
     declines the seat on a capable machine and is recorded in the kit's `.env`, so a re-run and the
     automatic update keep the refusal; `--judge` keeps its meaning. The cloud pod stays a miner only
     (ADR-053 decision 1) unless that record is amended.
   - Before seating the judge on an update, the kit checks the disk against `MIN_DISK_GB_JUDGE` and
     prints what it downloads (the judge model and its size as the engine reports it); below the floor
     it seats nothing and says so, and the miner keeps running.
   - A machine without a usable card joins as a judge or not at all (ADR-056 decision 3, unchanged); a
     machine with a card mines on the card and judges on the CPU at the same time, which is the probe's
     existing CPU path (`deploy/hw_probe.sh::JUDGE_ON_GPU` stays 0).
   - What proves it: a capable GPU miner updated without any option posts verdicts, read on chain
     against the anchored jury of an audit (`GET /dendra/jobs/v1/audit_committee/{job_id}` and the
     verdict commits of its members), never from the process list.
2. **One switch for the judge, and a status that reads what the judge produced** (owner, 2026-10-12).
   A new script, `deploy/testnet-miner/judge.sh`, with three verbs. `on` pulls the judge model through
   the compose service `judge-model-init`, sets `DENDRA_MINER_JUDGE` in the kit's `.env`, and recreates
   the miner's container only. `off` clears it and recreates the same container. `status` prints what
   the judge produced on chain for this identity, each figure with the height it was read at: verdicts
   posted (its verdict commits on audits whose anchored jury names it), the height of the last one, and
   the juror's abstentions by stage from its own log (`services/judge_worker.py::decide_verdict`
   names the stage). A process that runs is not a status; a verdict on chain is. The desktop application
   and the HiveOS package expose the same switch and call this script. On the epoch of ADR-059 the
   switch also sends the on-chain declaration (its decision 1).
3. **Every kit tells its operator it is behind, and updates itself within bounds** (owner, 2026-10-12).
   - *The alert.* The miner daemon reads `network-info.txt` over HTTPS once an hour (the TLS route of
     ADR-052, *Amendment*), with the reader of `deploy/join.sh::_load_config_url`: a key-value text,
     parsed and never executed. Behind `KIT_VERSION`, it prints a banner `Update available` with the
     exact command (`deploy/join.sh::kit_update_hint`), writes `kit_behind` in its heartbeat, and
     `miner_health.sh` turns H3 red and the application shows a banner. The file publishes two new
     keys, written by `deploy/testnet/publish_network.sh`: `KIT_MIN`, the kit number below which the
     network's services refuse a kit (the integer `docker/KIT_VERSION` carries at the release named in
     the announcement, the unit `verify_kit_version` already compares), and `KIT_DEADLINE`, the instant
     from which they refuse, in UTC. The daemon says "update before <KIT_DEADLINE>". Reading:
     `curl -sf https://testnet-api.dendranetwork.com/network-info.txt`, keys `KIT_MIN` and `KIT_DEADLINE`.
   - *The capacity report carries `kit_version`*, under the operator's signature when the report is
     signed (`services/capacity_server.py::_proven_miner_of`), next to `judge_role`
     (`services/capacity_server.py::JUDGE_ROLES`). The public adoption table reads it from
     the signed reports only. A report without the field is a kit older than this record, read as 0,
     never as current.
   - *The automatic update, on by default.* The daemon's host side (a timer the kit installs, never the
     container) checks for a newer release once a day and applies it under all of the following bounds,
     each a refusal and not a warning: the candidate is a release tag of the project's repository, of
     the form ADR-050 decision 1 fixes, whose tree carries the same `docker/CONSENSUS_EPOCH` as the
     running clone (a tag of a higher epoch is a new binary and a governed upgrade: the update stages it,
     verified under the same bounds, and the kit's watcher, `deploy/testnet-miner/node_upgrade_watch.sh`,
     moves the clone and the node image only when the chain itself halts at the plan height, never before;
     amended 2026-10-16); the tag's signature verifies under decision 4 with the signers file of the running clone,
     never with the one the candidate tree ships; the candidate tree carries the generator's seal, the
     same test the release script makes before it tags (`.build-complete` present, its `source_commit`
     not `-dirty`, no `warning=` line), so that a tree the generator did not certify is refused even
     under a good signature; the kit never moves to a branch, `main` included; the
     plan is printed before anything moves and the run is logged under the kit's directory; the branch
     `kit-before-update` is set to the tree the clone leaves, as the manual instruction already does;
     the join is re-run with the options recorded in the kit's `.env` (`persist_kit_update` of
     `deploy/join.sh`), and if that join fails, or the miner's container does not reach its first
     heartbeat within a bounded wait, the clone goes back to `kit-before-update` and the join is re-run
     from it; `DENDRA_AUTO_UPDATE=0` in the kit's `.env` turns the whole of it off, which `install.sh`
     and `join.sh` accept as an option and print in the plan. A re-run that the new kit refuses for a
     reason that is a decision and not a failure (decision 5: a machine without a GPU below the floor)
     is not rolled back: the kit prints the exit procedure and stops the miner, since rolling back would
     restore the mining this record ends.
   - The update fetches the signed tag of the clone and no release archive: the kit is the tree, and
     the archives are the node binaries a kit does not run. `SHA256SUMS` and its signature (decision 4)
     therefore bind nothing the update moves; they are verified by whoever downloads an archive.
   - The first installation keeps the owner's rule: downloaded, read, run, nothing without `--yes`. The
     automatic update applies to a kit already installed by that rule, and only to releases of the same
     epoch.
4. **Releases are signed with an offline SSH key of the project, held by the owner** (owner, 2026-10-15).
   - *The key.* An ed25519 SSH key created on a machine without a network, held by the owner, used for
     nothing else. Its comment and the public key file name the project identity (`DendraNetwork`), no
     person. The key never reaches the forge's runners: a key in a workflow is not offline.
   - *The public key ships in the kit*, in `docker/RELEASE_SIGNERS`, in the `allowed_signers` format
     `ssh-keygen -Y verify` reads: one line, the principal `releases@dendranetwork.com`, the option
     `namespaces="git,dendra-release"`, the key. The two namespaces are the two things signed below:
     git signs a tag under the namespace `git`, which git fixes and no option changes, and the sums are
     signed under `dendra-release`. A file that names `dendra-release` alone makes `git verify-tag`
     refuse every tag of the project (measured on 2026-10-16 with a throwaway key, git 2.53.0 and
     OpenSSH 10.2: `key is not permitted for use in signature namespace "git"`). The file arrives with
     the clone, as `docker/GENESIS_SHA256` and `docker/STATESYNC_RPC` do (ADR-052 decision 1). A
     rotation adds a line and removes none for one release, and is announced. The automatic update of
     decision 3 follows a rotation by itself in two forms only: an addition, where the signing key is
     already a recorded line, and a removal, where the signing key stays recorded; a swap, where a new
     key signs and the old line is gone, is refused by the update and needs the operator's named option
     (`deploy/join.sh --accept-new-signers`, which prints the new fingerprint and is never written into
     the recorded re-run of the kit), as is any file that differs from the recorded one otherwise
     (`deploy/testnet-miner/kit_update.sh`, the comment above `keylines`).
   - *What is signed.* The annotated release tag, signed with that key (`git tag -s`, with
     `gpg.format` set to `ssh` and `user.signingkey` naming the key file), verified under the namespace
     `git`; and the checksum lists, `SHA256SUMS` and the HiveOS package's `SHA256SUMS.hiveos`, each with a
     detached signature (`SHA256SUMS.sig`, `SHA256SUMS.hiveos.sig`) made by
     `ssh-keygen -Y sign -f <key> -n dendra-release`, verified under `dendra-release`. A signature of
     the sums under `git`, or a tag checked under `dendra-release`, does not verify, and the merge bench
     of decision 4 plays both refusals.
   - *How the release tooling changes.* The release script signs the tag when it creates it, with the
     key file its operator names, and refuses to push an unsigned tag; its identity pin and re-read of
     the tagger stay. `.github/workflows/release.yml` gains, in its `verify` job, `git verify-tag` with
     `gpg.ssh.allowedSignersFile` pointing at the tagged tree's `docker/RELEASE_SIGNERS`, and fails on a
     tag that does not verify; this proves that the tag was made by a key the tree names, not that the
     tree is the project's, which is why the kit verifies with the file it already holds (decision 3).
     `SHA256SUMS` is written by the `publish` job from the reproducible build, on the runner, where no
     key is; it is signed afterwards, offline, by a second verb of the release script that downloads
     the attached lists, checks that each names the archives of the job that attached it and nothing
     else, signs each, and attaches `SHA256SUMS.sig` and `SHA256SUMS.hiveos.sig`. ADR-050 decision 5 stands: the workflow alone creates the release,
     and the signature is attached to it afterwards, like the pins of the ADR-050 amendment. The
     signature of the sums attests that the project publishes these sums for this tag; it is not a
     rebuild, and a third party who wants more rebuilds the binaries as the workflow's header says.
   - *How the kit verifies.* `deploy/install.sh` verifies the tag whenever a tag is what it installs
     (`--ref <tag>`, the HiveOS package, whose release tag is baked in, ADR-053 decision 12), with the
     signers file of the tree it fetched, and records that file's SHA-256 in the kit's `.env`; a first
     installation from `main` trusts HTTPS to the forge for that first file, as it does for the genesis
     pin, and says so in its plan. `deploy/join.sh` refuses, at every run, a `docker/RELEASE_SIGNERS`
     that differs from the recorded one, unless the operator passes a named option that prints the new
     key's fingerprint; and verifies the tag whenever the clone is at one. The automatic update verifies
     the candidate tag with the running clone's file before anything moves (decision 3). A node operator
     who downloads a release binary verifies `SHA256SUMS.sig` with the same file,
     `ssh-keygen -Y verify -f docker/RELEASE_SIGNERS -I releases@dendranetwork.com -n dendra-release -s SHA256SUMS.sig < SHA256SUMS`,
     then the sums (`sha256sum -c SHA256SUMS`); the release notes print both commands. A HiveOS rig
     owner does the same with `SHA256SUMS.hiveos` and `SHA256SUMS.hiveos.sig` before giving the archive's
     URL to the flight sheet. Reading, for
     anyone: `git -c gpg.ssh.allowedSignersFile=docker/RELEASE_SIGNERS verify-tag v0.2.2` in a clone of
     the public repository. The merge bench of this decision signs a tag and a sums file with a
     throwaway key on a fabricated tree and verifies both with the delivered `docker/RELEASE_SIGNERS`
     line, each under its namespace, and refuses each under the other's.
5. **A stale kit is refused from `KIT_DEADLINE`, by the relay, the faucet and the season, and that same
   deadline ends mining without a GPU below `MOE_CPU_MIN_RAM_MB`** (owner, 2026-10-12 and 2026-10-15).
   - `KIT_MIN` is the kit number of release `v0.2.2`. `KIT_DEADLINE` is the announcement of `v0.2.2`
     plus seven days, in UTC. Both are published in `network-info.txt` before the deadline and in the
     announcement, in English.
   - *The relay* refuses, from the deadline, a signed deposit whose body declares `kit_version` below
     `KIT_MIN`, or declares none, with a named refusal code next to `UNKNOWN_MINER`
     (`services/modea/relay_write.py`). The version is in the signed body, covered by the
     deposit's signature (`services/relay.py::_canon`), so a relay cannot be made to
     refuse an honest miner by a forged header. Reads stay open: the policy is per route
     (`services/relay.py`, the write policy), and a stale kit still reads what it needs
     to leave.
   - *The faucet* refuses, from the deadline, a grant whose request declares `kit_version` below
     `KIT_MIN` or none (`services/faucet.py::do_POST`, before `_rate_ok`, so no quota
     is spent on a refusal), with a message that names the update.
   - *The season* stops paying, from the first season day that begins at or after the deadline, an
     identity whose latest signed capacity report, that day or before, declares `kit_version` below
     `KIT_MIN`; an identity with no signed report, or a report without the field, reads 0 and is not
     paid either. This is a new rule set appended to `services/final_season_rules.py::RULE_HISTORY`,
     deployed before the first day it applies to, which keeps every published day under its own rules
     (`services/final_season_rules.py::rules_for_day`); its day is fixed once by the service as
     decision 23 of ADR-047 fixed its own. The rule set is the one that also carries the judge presence
     reward of [ADR-060](ADR-060-judge-presence-reward-amends-adr-038.md) and the probation the owner
     decided on 2026-10-15, and it is recorded as a decision of ADR-047 when it ships.
   - *The chain* is the fourth refusal, and it is ADR-059 decision 4, on the epoch that carries it.
   - *The end of mining without a GPU.* A kit tagged `v0.2.0` or `v0.2.1` is below `KIT_MIN`, so from the
     deadline the relay refuses its deposits, the faucet its grants and the season its pay: a machine
     mining on its CPU with one of those kits is no longer served. A machine that updates to `v0.2.2`
     without a GPU and below `MOE_CPU_MIN_RAM_MB` is refused at join (`deploy/hw_probe.sh --role`,
     `refused`), with the exit procedure printed: `deploy/testnet-miner/exit-miner.sh` returns the bond.
     With enough RAM it joins as a judge (ADR-056 decision 3). The announcement of `v0.2.2` names this
     date as the end, in place of 2026-10-16; if the earlier announcement was posted, a correction goes
     out before 2026-10-16 23:59 UTC.
   - `KIT_MIN` moves only with a release the owner announces. The mechanism: `KIT_MIN` and
     `KIT_DEADLINE` are published together in `network-info.txt` and the announcement names the notice
     it gives; the seven days are the owner's decision for `v0.2.2`, and a later move names its own
     notice the same way. Reading: `curl -sf https://testnet-api.dendranetwork.com/network-info.txt`,
     the two keys.
6. **The HiveOS package declares the payout address locked by default** (owner, 2026-10-15; amends
   ADR-053 decision 4, whose scope was the pod). `deploy/hiveos/dendra/h-run.sh` passes the lock with
   the first declaration, and `h-config.sh` prints the address in the plan before anything starts, since
   a locked address cannot be corrected (ADR-053 decision 4, its accepted cost). A rig whose identity
   already declared unlocked is not locked by this change: the programme accepts a lock as the first
   declaration only, or from a Creator that has always declared in owner mode. A banner at the top of
   ADR-053 names this record.
7. **The bootstrap deposits are retried** (owner, 2026-10-13).
   `services/miner.py::_bootstrap_deposits` retries `pub` and `attest` with a growing
   delay, bounded above, for as long as the relay refuses them, and marks them done only when each was
   accepted or already present; the daemon's loop keeps calling it until then. A deposit refused for a
   reason that a retry cannot change (an encryption key that differs from the anchored one,
   `ENC_MISMATCH`) is not retried, as today.
8. **The relay re-reads the registry when it meets an unknown miner** (owner, 2026-10-13).
   On `UNKNOWN_MINER` with a copy of the registry younger than `REGISTRY_EXPIRY`,
   `services/relay.py::_refresh_cache` re-reads it once and the check is made again,
   bounded by `REGISTRY_RETRY` so that a stream of invented identifiers cannot drive the REST gateway.
   An identifier absent from the chain stays refused. This is a redeployment of the relay alone, with
   `v0.2.2` and not before (owner, 2026-10-15). The access bench `services/test_relay_access.py`
   gains three cases: a miner registered after the last read is accepted at its first deposit; an
   identifier the chain does not know is refused; and no public route returns an unencrypted prompt.

## Declared versus proven

- The kit version a deposit, a grant request or a capacity report declares is a field a client writes
  and signs. The signature proves who wrote it, not that it is true: a modified client declares any
  version it likes. Decision 5 reaches kits that are honest and old, which is its target; it does not
  reach a client built to lie, and no text says it does.
- Whether a judge runs is proven by what it produces on chain (decision 2), not by `judge_role` in a
  report, which is declared (`capacity_server.py::JUDGE_ROLES`, and the comment above it).
- A signed tag proves that a key named in `docker/RELEASE_SIGNERS` made it. Which file a kit trusts is
  decided at its first installation, over HTTPS to the forge; from then on the file is pinned. A first
  installation made on a forged forge would pin a forged key, and nothing in the kit would tell: the
  same limit the genesis pin has, and the same answer, the project's hosts and the fingerprint
  published with the announcement.
- The signature of `SHA256SUMS` is an endorsement of what the workflow built from the signed tag; the
  equality of a binary with its source is the workflow's reproducibility, which anyone can rerun.
- The automatic update verifies a tag with the signers file the kit already holds and refuses another
  epoch. It cannot tell a release that breaks a fleet from one that does not; the rollback to
  `kit-before-update` and `DENDRA_AUTO_UPDATE=0` are the answers, and the plan printed before each
  update is the trace.

## Consequences

- From `v0.2.2` a GPU miner updated with no option is a judge: the number of seats that can vote rises
  with the adoption of the kit, which the adoption table shows from the signed reports and the verdicts.
  Whether a given audit concludes stays a reading (`resolved_by_quorum`), since a seat is drawn with no
  role until ADR-059.
- A judge on by default costs each capable machine the disk of the judge model and CPU time per audited
  job; the kit says so before it downloads anything.
- An operator who wants no automatic update sets `DENDRA_AUTO_UPDATE=0` at installation or later; the
  public texts say that updates are automatic by default and how to turn them off, and that a first
  installation never is.
- A release now needs the owner's key at tagging time and once more to sign the sums: two gestures on
  the owner's machine, none in the workflow. A release the key cannot sign is not published.
- From `KIT_DEADLINE` an identity on a kit below `KIT_MIN` earns nothing from the season and is refused
  by the relay and the faucet; its bond is still returned by exit. The announcement says so seven days
  ahead, and the end of mining on a CPU below the floor is that same date.
- A capacity registry unreachable on a season day leaves every identity of that day without a signed
  report under the season's rule: the day would pay nobody. The rule therefore reads the latest report
  at or before the day, and the service's deployment checks the registry before it fixes the first day.
  A day once ranked is not recomputed (ADR-047, *A ranking carries its rules*).
- On HiveOS a wrong wallet in the flight sheet is locked for good once declared; `h-config.sh` prints it
  first, and the documentation says to check it before `YES=1`.
- Relay and faucet refusals are counted and printed in their logs by code, so that a reading of the
  deadline's effect is a count of refusals, never a guess.

## What this does not decide

- The chain-side refusal of a stale kit, the judge declaration on chain and the jury of judges:
  [ADR-059](ADR-059-consensus-epoch-15-jurors-are-declared-judges.md).
- The amount and the condition of the judge presence reward: [ADR-060](ADR-060-judge-presence-reward-amends-adr-038.md).
- The probation of new identities (60 % of the season pay during the first full day,
  `audit_probation_jobs` above 0), decided by the owner on 2026-10-15 and shipped in the same rule set
  as decision 5: recorded with that rule set as a decision of ADR-047, not designed here.
- The dates themselves: the announcement day, hence `KIT_DEADLINE`, is the owner's, and this record
  writes no date for it. The reading is `network-info.txt`.
- The enforcement of the model registry on the testnet once every served model is registered, which
  reopens ADR-056 decision 4 (owner, 2026-10-15): a record of its own.
- Marking `silence_slash_bps` as having no effect in the explorer, The Proof and the documentation,
  without a vote (owner, 2026-10-15): a text change that ADR-038 §1(b) already supports.
- A per-miner cap on the season's pay: the owner decided none (2026-10-15; ADR-054 decision 14 closed).

## Pointers to add at merge

The code of decisions 1 to 8 is not in the tree yet; each item is cited by file and symbol once it lands,
in the same change as the code.

- The judge default and `--no-judge` in `deploy/join.sh`, `deploy/install.sh`, `deploy/install.ps1`,
  the HiveOS package and the cloud entrypoint; the disk check before seating a judge on an update.
- `deploy/testnet-miner/judge.sh` and its callers in `deploy/app/` and `deploy/hiveos/dendra/`.
- The hourly read of `network-info.txt`, `kit_behind` and the banner in `services/miner.py`;
  `KIT_MIN` and `KIT_DEADLINE` in `deploy/testnet/publish_network.sh`; `kit_version` in the capacity
  report's signed body and in `services/capacity_server.py`.
- The update timer, its bounds, its log and its rollback, under `deploy/testnet-miner/`;
  `DENDRA_AUTO_UPDATE` in `deploy/install.sh` and `deploy/join.sh`.
- `docker/RELEASE_SIGNERS`; the signing and the sums verb of the release script (kept outside the public tree); the `verify-tag` step
  of `.github/workflows/release.yml`; the verification in `deploy/install.sh` and `deploy/join.sh` and
  the pin of the signers file in the kit's `.env`; the signing bench (a throwaway key on a fabricated
  tree, the tag verified under `git` and the sums under `dendra-release` with the delivered signers
  line, each refused under the other namespace).
- The refusal code and the deadline reading in `services/modea/relay_write.py` and
  `services/relay.py`; the refusal in `services/faucet.py`; the rule set
  at the end of `services/final_season_rules.py::RULE_HISTORY` and its reader of the capacity
  registry.
- The lock in `deploy/hiveos/dendra/h-run.sh`; the retry in `services/miner.py`; the
  re-read in `services/relay.py` and the cases of `services/test_relay_access.py`.

## Version

Decisions 1 to 4, 6 and 7, and the kit side of decisions 3 and 5, ship under one `kit_version` bump,
release `v0.2.2`, whose number [ADR-050](ADR-050-release-versioning.md) derives (the kit alone moves).
The relay, the faucet and the season (decisions 5 and 8) are redeployed without a number.
`consensus_epoch` does not move.
