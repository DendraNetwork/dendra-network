# Changelog

Each release of this repository is a tag `vMAJOR.MINOR.PATCH` with a section below, and its GitHub
release carries that section. The repository is republished as a single commit, so `git log` cannot
compare two releases: this file does.

## How the version number moves

The number follows the two gates of `VERSION` ([ADR-050](docs/adr/ADR-050-release-versioning.md)):

- **`consensus_epoch` moved**: the state machine changed, and a node left on the previous binary forks.
  The **minor** number moves and the patch number returns to 0.
- **`kit_version` moved alone**: the scripts and services an operator runs changed; a kit one version
  behind still joins and is told to update. The **patch** number moves.
- **Neither moved**: documentation, the website, or services only the network runs. No number: the
  repository is republished without a release, save an exceptional release whose section says why; it
  moves the patch number.

`v1.0.0` is the mainnet genesis. The earlier `v0.1` line designated trees of the previous testnet and was
withdrawn; its names are not reused.

## v0.2.1

Kit version 2, consensus epoch 14: the scripts and services an operator runs changed, the state machine did
not. A node left on v0.2.0 keeps following the chain; a kit behind the kit version the network publishes
still joins, and `deploy/join.sh` says `KIT BEHIND` when it runs.

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
  vector with a 384-value anchor, and so abstained on every revealed audit
  ([ADR-057](docs/adr/ADR-057-judge-anchor-check-and-single-judge-model.md)). Two vectors of different
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
  judge refuse to start (`[judge] REFUSED` in the miner's log).
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
  `OLLAMA_NUM_PARALLEL` is not set.
- Five settings reach the miner container from the kit's `.env`, empty by default, and empty means the
  reader's own default: `DENDRA_JOB_MAX_ATTEMPTS`, `DENDRA_REQUEST_MAX_AGE_S`, `DENDRA_JUDGE_TIMEOUT_S`,
  `DENDRA_JUDGE_DIVERGENCE_SLASH` and `DENDRA_JUDGE_ABSTAIN_VOTE`. `join.sh --gpus` carries them to every
  slot. For the first two, a value
  that is not a positive number is said once in the miner's log and the default is used; for the timeout,
  the judge refuses to start; for the divergence switch, any value but `1` keeps the guard, and one that is
  neither `0` nor `1` is warned about at start.

### Keys, stake and payout

- A new miner's keyring is encrypted. `join.sh` creates a random passphrase in
  `~/.config/dendra/miner-secrets/keyring-passphrase` (`DENDRA_SECRETS_DIR`), outside the volume and the
  clone; the kit mounts it read-only into the miner, which hands it to `dendrad` on standard input.
  `--plain-keys` keeps a new keyring in clear. Keys already in clear stay so until
  `deploy/testnet-miner/encrypt-keys.sh` (a plan without `--yes`). Back up the `miner-keys` volume and the
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
- Final Testnet Season, decision 18 of [ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md):
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
- Rented GPU pods (RunPod, Vast): the single-container image `dendra-miner-cloud`, which
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

What to do: from your clone (`~/dendra-network` if the installer made it; on Windows, inside the WSL
distribution), run

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
- Keys in clear stay in clear: `deploy/testnet-miner/encrypt-keys.sh` encrypts them at the same address,
  once their 24-word recovery phrase is written down.
- A miner kit that ran `ollama/ollama:latest` now runs the pinned 0.34.4: where `latest` had pulled a later
  Ollama, the engine moves back to an older version. The Ollama 0.40.2 release notes say that Ollama 0.40
  and later convert a model on its first run and keep the original as a backup copy, and that a model whose
  backup copy was removed has to be pulled again on a version older than 0.40. `model-init` and
  `judge-model-init` pull at every start, so such a model is downloaded again, the judge model included.
  Not measured on a volume written by Ollama 0.40 or later.
- Leave `DENDRA_JUDGE_DIVERGENCE_SLASH` unset: the kit's guard holds while it is anything but `1`
  ([ADR-057](docs/adr/ADR-057-judge-anchor-check-and-single-judge-model.md), decision 3).

## v0.2.0

The `dendra-testnet` network, at kit version 1 and consensus epoch 14.

- A new chain from a fresh genesis. Nothing carries over from the previous testnet: balances,
  registrations and stakes start empty. A miner key survives in its Docker volume and registers again.
- Consensus epoch 14: its state-machine changes are listed in `docker/CONSENSUS_EPOCH`.
- The Final Testnet Season pays miners until 7 November 2026, 23:59 UTC, read on block timestamps
  ([ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md)).
- `deploy/install.sh` (Linux) and `deploy/install.ps1` (Windows) prepare a bare machine and run
  `deploy/join.sh`; on a Linux desktop, `install.sh` also adds the Dendra application.
- Judging runs on the CPU: the judge seat needs at least 26 000 MB of system RAM, with or without a GPU,
  and runs the judge model the network pins; no judge is seated on the GPU.
- Release files: static `dendrad` binaries for Linux and macOS (amd64, arm64) with their checksums, and
  the digest of the prebuilt miner image (`miner-image.txt`).

What to do: join with `deploy/install.sh`, or with `deploy/join.sh` on a host that already runs Docker.
A clone of the previous testnet is brought to this tree by downloading `deploy/install.sh` again and
running it: it moves the existing clone in place, which `git pull` cannot do.
