# ADR-055 — The miner's lifecycle: a self-test with local alerts, an uninstall that keeps the keys, a scripted exit, a judge disk floor read from the probe, and the node's RPC on loopback

**Status:** Accepted by the project owner, 2026-10-08 — implementation in progress.
**Implementation:** Decided, not implemented in the published kit; the work is in progress. Kit change
under the single `kit_version` bump shared by ADR-051 to ADR-055; the miner image changes with it (the
checker and the heartbeat). Not consensus-breaking: `delete-miner` already exists on chain. The drain mode
is deferred (decision 16).

## Context

- A miner can run, look healthy and contribute nothing. A failed registration, a VRF key that does not
  match its anchor, a relay write that fails, a node that lags: none of them shows the operator an error.
  The validator kit has a health script with a scheduled run and an alert file
  (`deploy/validator_health.sh`, installed by `deploy/join.sh::install_health_cron`); the miner kit has
  none.
- Leaving the network is `delete-miner`, signed by the Creator
  (`chain/x/jobs/keeper/msg_server_miner.go::DeleteMiner`) and refused while the miner holds an open
  obligation (`chain/x/jobs/keeper/miner_vitality.go::hasOpenObligation`), which has four clauses: a
  fee retained on a job it served; a seat on the jury of an unresolved audit, but only while the miner is
  still eligible as a juror (`chain/x/jobs/keeper/miner_vitality.go::eligibleAsJuror`, within
  `juror_freshness_blocks` of its last commit, or of its registration when it has none); a job of its own
  under an unresolved dispute; and a commit
  it anchored as a member of the work committee of a job still live (not paid, escrow not returned, not
  resolved). The refusal message of `DeleteMiner` names only the first three. A daemon left running
  registers again.
- Removing the kit by hand puts the keys at risk: `docker compose down -v` erases the key volume, and so
  does a routine `docker volume prune` once no container uses it.
- The installer applies the judge's disk floor (`deploy/install.sh::MIN_DISK_GB_JUDGE`) to a judge request
  even on a machine the probe will downgrade to a miner for lack of RAM
  (`deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB`).
- A miner container reaches its own node through the host gateway (`host.docker.internal`), which is the
  Docker bridge, not loopback. Binding the node's RPC to loopback with
  `deploy/testnet-node/docker-compose.yml::DENDRA_RPC_BIND` therefore disconnects a co-located miner, and
  the knob's default kept the RPC published on every interface.

## Decisions

### Self-test and alerts

1. **One verdict model.** Every check returns `ok`, `ko` or `unmeasured`, with what it measured, the reason,
   and the command that repairs it.
2. **A heartbeat from the daemon.** At each pass the daemon writes a local status file atomically: the time
   of the pass, the last loop error, the last relay listing, the last availability proof (after its
   inclusion) or the reason it was refused, the last commit anchored or refused. A failed write never stops
   the loop. The file serves liveness and diagnosis, never a verdict about the chain; the HiveOS statistics
   read it ([ADR-053](ADR-053-cloud-and-hiveos-paths.md)).
3. **A checker inside the miner image**, run by `exec` where Python, `dendrad`, the keyring and the
   environment already are. It parses JSON only, each document under the zero value of its own types:
   - the RPC answers and the height moves;
   - `catching_up` is a boolean and is false; absent, or not a boolean, is `unmeasured`, never `ok`; the
     lag behind the network's public RPC stays under `avail_deadline_blocks`, read from the chain;
   - the registry holds the miner and its anchored VRF key equals the local one; an absent `vrf_pubkey`
     reads as empty and is `ko`; "not found" is `ko` (not registered), any other error `unmeasured`; the
     VRF secret reaches `dendra-vrf` through the environment, never argv;
   - presence over the last windows, as the chain defines it
     (`chain/x/jobs/keeper/presence.go::minerPresentAt`), measured from the transaction index with
     `services/final_season_chain.py::presence_proofs` after
     `services/final_season_chain.py::require_index_from`; an absent `avail_epoch_blocks` is zero,
     the chain then measures no presence, and the check is `ok` with that mention; a newcomer's grace is
     read from its registration;
   - the relay lists;
   - a signed relay write re-posts the identical bytes of the anchored encryption key, and only when the
     relay's copy equals the chain's; otherwise `ko`, and nothing is written; `--no-write` skips it;
   - the model answers through the path the jobs use, a short generation and an embedding; its latency is
     reported and judged against nothing until the network publishes a response budget;
   - the capacity registry lists the identity as registered on chain and not stale;
   - the reveal and judge workers are alive.
4. **A host wrapper that needs only Docker.** `miner_health.sh` checks the containers and their restarts,
   the running image against the clone's pin, the `KIT_VERSION` and `CONSENSUS_EPOCH` of the clone and of
   the image against network-info (the configuration URL is written to the miner's `.env` for that), and
   the scheduled line with the age of its last run; then it runs the checker in the container. An image
   without the checker gives `unmeasured`, "rerun join.sh". A kit behind the network is `ko`, since that is
   what the alert is for; `deploy/join.sh::verify_kit_version` itself still never refuses.
5. **Exit codes 0, 1, 2.** 0: every check `ok`. 1: at least one `ko`. 2: no `ko` and at least one
   `unmeasured`, or a run that could not take place; a run that executed no check is 2, never 0.
6. **An hourly scheduled run**, installed by `join.sh` and listed in the installer's plan, so `--yes`
   covers it. With `--cron FILE`, the alert file exists if and only if the exit code is not 0, under the
   rules of the validator kit's watch. A run that measured something and did not clear rewrites the alert
   in place and keeps the time the problem was first seen, a run left partly unmeasured included
   (`deploy/validator_health.sh::_alert_write`); one that could not read its own node leaves a standing
   alert as it is. A run that measured nothing writes or refreshes a note of its own and never overwrites
   a standing alert (`deploy/validator_health.sh::_cron_note`). The last result is always written.
7. **The container healthcheck reads the heartbeat's freshness only.** It is a display signal: Docker does
   not restart an unhealthy container, and the verdict stays with the wrapper.
8. **A Health card in the application:** the last result and its age, where "never ran" is not `ok`, and
   an authenticated, time-bounded "Run now" whose quick mode skips the model and the write.
9. **No remote notification.** No webhook, push service or mail: the alert file, the card, the log lines
   and the healthcheck. A remote destination, if one comes later, is one the operator writes in its own
   `.env`, never one read from network-info.
10. **No new chain query for presence.** The transaction index already answers, and the kit already
    requires it.

### Uninstall

11. **`deploy/uninstall.sh`, and `install.ps1 -Uninstall`,** which calls it inside the distribution and
    removes the logon task. It measures what exists (compose projects by name, volumes, scheduled lines
    that invoke a file of this clone, the application's files, the Docker network) and prints the plan.
    Without `--yes` it exits 2 and touches nothing; when Docker cannot be read it says so and never
    reports "nothing to remove".
12. **With `--yes`, in this order:** a verified backup of the miner's and the node's keys under the user's
    home, outside the clone, readable by the user only; a validator guard on voting power, where above
    zero is a refusal and unreadable keeps the node's data; `down` with the images, never `-v`; removal of
    the model volumes, and of the node's data only when it was measured not to be a validator's; removal
    of this clone's scheduled lines only, the table read in three states and checked after writing; removal
    of the application.
13. **Kept by default:** the miner's keys, the clone, and the system level (Docker, the NVIDIA toolkit,
    the docker group, the apt sources); the commands that remove them are printed. `--delete-keys` is
    accepted only after a verified backup in the same pass, and there is no way to skip the backup.
    `wsl --unregister` is never run: it is printed, with the warning that it erases every volume, keys
    included. On Windows the backup stays in the distribution's home, shown by its `\\wsl$` path, never
    copied into a profile folder that a sync client may upload.

### Leaving the network

14. **`deploy/testnet-miner/exit-miner.sh`.** It reads the identity and its bond, where "not registered" is
    not "query failed". Without `--yes` it simulates `delete-miner` from the Creator's address and reports
    one of three answers: accepted, refused with the chain's message quoted, or unreadable. With `--yes` it
    stops the miner by its project name, so that the daemon does not register again, simulates again,
    broadcasts, confirms the inclusion (an absent `code` is 0), and checks that the miner is gone. The
    refund goes to the Creator, and only when the remaining bond is above zero
    (`chain/x/jobs/keeper/msg_server_miner.go::DeleteMiner`), so the script reads the Creator's
    balance, the cold key's in owner mode, and requires it to have risen only for a bond read above zero:
    a miner slashed to zero leaves with no refund, and that exit is a success. A refusal for an open
    obligation exits 2 and leaves the miner stopped: a reading to repeat later, not a failure. Restarting
    the miner after an exit registers it and bonds it again; the script says so. In owner mode
    ([ADR-051](ADR-051-miner-keys.md)) the Creator signs, so the script prints an unsigned transaction.
15. **No button in the application.** The exit is irreversible and its wait on obligations has no bound.
    The application shows a read-only panel: the bond, the registration, the command.
16. **A drain mode is deferred.** Stopping new work while still voting the seats already held would avoid
    mute seats. Without it, a stopped miner's unresolved seats block `delete-miner` only while the miner is
    still eligible as a juror, that is until `juror_freshness_blocks` have passed since its last commit
    (or its registration, when it has none); past that, a seat no longer blocks the exit. It still counts
    among the seats of its audit's bar for as long as the miner stays in the registry
    (`chain/x/jobs/keeper/antievasion.go::auditVerdictTally` counts every anchored member still
    registered): a mute seat until the miner leaves or is removed.

### Judge disk floor

17. **The probe decides the floor.** `hw_probe.sh --can-judge` prints `true`, `false` or `unknown`;
    unreadable RAM is `unknown`, never zero. The installer asks the probe next to it. Judge and `true`: the
    judge's floor. Judge and `false` on a machine with a GPU: the miner's floor, said with the RAM read and
    the name `MOE_CPU_MIN_RAM_MB`, never its number. On a machine without a GPU the answer applies whatever
    role was asked, since such a machine joins as a judge or not at all
    ([ADR-047](ADR-047-final-testnet-season-reward-programme.md), decision 21, which ships in the same
    `kit_version`): `true` is the judge's floor, and `false` does not join, so no floor applies and the
    installer says why. Judge and `unknown`, or no probe: the judge's floor, with the reason. `--judge` still reaches `join.sh`, which already downgrades.
    `install.ps1` cannot probe before WSL exists: it keeps the miner's floor, drops the judge's, and says
    that `install.sh` decides inside the distribution.

### The node's RPC

18. **A Docker network, `dendra-chain`, owned by the node kit,** on which the node answers under an alias,
    its project name. The miner joins it through an overlay compose file that `join.sh` enables with
    `COMPOSE_FILE` in the miner's `.env`, together with the GPU override when there is one, and
    `DENDRA_NODE` names the alias.
19. **The RPC is published on loopback by default.** `DENDRA_RPC_BIND=0.0.0.0` is for a node that serves a
    public RPC, and is then declared. Tools on the host keep `127.0.0.1`; the scripts that translate a
    container address into a host address learn the alias.

## Set aside

- **A chain query for a miner's last present window:** every node a miner reads would need a new binary,
  and the transaction index already answers.
- **Everything in bash on the host:** the nominal host has no JSON parser, and reading JSON as text is how
  absent and zero become indistinguishable.
- **The whole self-test inside the daemon's loop:** the model probe and the searches would hold up the
  jobs.
- **Docker's healthcheck as the verdict:** it has two states and cannot say `unmeasured`.
- **A latency threshold from `job_expiry_blocks`:** that is the escrow's refund delay, not a response
  deadline. Nor from the gateway's timeout, which is not published to miners.
- **`install.sh --uninstall` in place of a dedicated script:** the uninstall must run when the installer's
  prerequisites fail.
- **A `delete-miner` button now:** see decision 15.
- **Binding the RPC to the bridge address:** with Linux's weak host model a neighbour on the same segment
  can route to it; the bridge address is configurable; rootless Docker and Docker Desktop differ; and host
  tools would need a second binding.
- **A non-external network declared in both compose files:** a warning at every `up`, and the first
  project started owns it.
- **The external network in the miner's base compose:** it breaks miners on `--remote-rpc` and the manual
  path.
- **`network_mode: host` for the miner:** the `ollama` service name no longer resolves.
- **Copying `MOE_CPU_MIN_RAM_MB` into the installers:** a threshold is cited from what applies it.

## Consequences

- False alerts wear trust down. A newcomer's grace, a model still loading and a node catching up after a
  restart are named states, not `ko`.
- A badly guarded signed write would replace the miner's published key; only identical bytes are written,
  and only after the comparison.
- An image older than the checker gives `unmeasured`, never a guessed `ok` or `ko`.
- Where no scheduler runs (WSL without cron, a cloud pod, a rig), the scheduler check and the age of the
  last result make the gap visible.
- An operator who recreates the node by hand without running `join.sh` again keeps a miner pointed at the
  host gateway, now closed: a blind miner, which the alert reports and the change log announces.
- `docker compose -f docker-compose.yml up` bypasses the overlay and the GPU override; the documentation
  moves to the `-p` forms.
- Compose's handling of a shared network varies between versions; it is measured on the real engine with
  throwaway names. Whether an older engine lets a neighbour reach a port published on loopback, and
  whether `localhost` resolves to `::1` first for host tools, are to be checked, not asserted.
- `crontab -` replaces the whole table: a failed read is never taken for an empty table.
- A validator whose RPC is listed as a state-sync endpoint ([ADR-052](ADR-052-installation-trust-anchor-and-node-image.md))
  declares `DENDRA_RPC_BIND=0.0.0.0`, or the loopback default closes it.

## Version

The change touches `join.sh`, `install.sh`, `install.ps1`, `hw_probe.sh`, the compose files, the
application, two new scripts and the miner image: it ships under the one `kit_version` bump shared by
ADR-051 to ADR-055, in one release whose number [ADR-050](ADR-050-release-versioning.md) derives.
`consensus_epoch` does not move. After the release, the network publishes the new `KIT_VERSION` in
`network-info.txt`, so that kits left behind are told.

## Pointers to add at merge

The code below is not in the published tree yet; each item is cited by file and symbol once it lands.

- The heartbeat writer in `services/miner.py` and the checker
  `services/miner_selftest.py`.
- `deploy/testnet-miner/miner_health.sh`, the miner's scheduled run in `deploy/join.sh`, and the health
  routes of `deploy/app/dendra_app.py`.
- `deploy/uninstall.sh` and the `-Uninstall` switch of `deploy/install.ps1`.
- `deploy/testnet-miner/exit-miner.sh`.
- `--can-judge` in `deploy/hw_probe.sh` and its use by `deploy/install.sh`.
- `deploy/testnet-miner/docker-compose.local-node.yml`, and the `dendra-chain` network and loopback
  default in `deploy/testnet-node/docker-compose.yml`.
