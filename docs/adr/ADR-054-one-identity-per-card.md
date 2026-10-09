# ADR-054 — One identity per card, on request: never more identities than cards, and one CPU judge per machine

**Status:** Accepted by the project owner, 2026-10-08 — implementation in progress. The question of a
payout cap per miner is left open by the owner (decision 14); this record decides no cap.
**Implementation:** Decided, not implemented; the work is in progress. Kit change under the single
`kit_version` bump shared by ADR-051 to ADR-055; the capacity registry's change is network-side. Not
consensus-breaking.

## Context

- What earns is an identity and its stake, not a card. The chain draws the work among present miners,
  weighted by stake up to `assignment_stake_cap_multiple` × `min_stake`
  (`chain/x/jobs/keeper/committee.go::capAssignmentWeights`), over a volume the programme fixes
  (`services/final_season_rules.py::RULES`, `requests_per_day`); one card can serve the work of
  several identities ([ADR-047](ADR-047-final-testnet-season-reward-programme.md), *What was measured*).
- The kit runs one identity per machine, whatever its cards: one compose project, one key volume, one
  Ollama that sees every card.
- One address registers one miner, under an id derived from it
  (`chain/x/jobs/types/miner_id.go::DeriveMinerID`): several identities are several keys, several
  bonds of `min_stake`, and several faucet grants, which the faucet limits per source address
  (`services/faucet.py::IP_DAILY`).
- A judge runs on the CPU, one per machine, gated by the RAM `deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB`
  names. The chain draws jurors among living miners
  (`chain/x/jobs/keeper/miner_vitality.go::eligibleAsJuror`) and records no judge capability; only a
  node started with the judge role posts a verdict. A seat drawn on a miner without a judge is mute: it
  counts among the anchored seats the verdict bar is computed from
  (`chain/x/jobs/keeper/antievasion.go::auditRelativeBar`) and supplies no verdict.
- The audit tally records how many distinct `Operator` addresses stand behind the votes
  (`chain/x/jobs/keeper/antievasion.go::auditVerdictTally`). Identities with their own keys are
  distinct operators to the chain.
- The capacity registry sums the reports it receives (`services/capacity_server.py::aggregate`):
  several reports from one machine count its RAM and cores several times.

## Decisions

1. **Opt-in.** `join.sh --gpus all`, or a list of cards, creates the per-card identities. Without it,
   nothing changes: one identity per machine.
2. **Exactly one identity per card, never more.** The kit aligns identities with hardware, and does not
   industrialise the split of a stake beyond the cards present. It does change what a machine earns: each
   identity is drawn for work on its own, is paid its own presence and holds its own jury seats
   ([ADR-047](ADR-047-final-testnet-season-reward-programme.md), *Consequences*), so a machine with several
   identities earns as several.
3. **Slots.** Slot 0 is the existing installation and is never renamed: its staked key lives in its
   volumes. Each further card gets its own compose project, environment file and volumes, hence its own
   key, address, identity and bond. A slot is bound to its card by UUID, never by index.
4. **Card pinning lives in each slot's override only.** The override sets `CUDA_VISIBLE_DEVICES` to the
   card's UUID on that slot's Ollama, plus a device id where the engine supports it. The base compose never
   carries a variable that defaults to empty: an empty `CUDA_VISIBLE_DEVICES` means no GPU, which is
   exactly how the base compose keeps the CPU judge off the card
   (`deploy/testnet-miner/docker-compose.yml::CUDA_VISIBLE_DEVICES`). After start, a verdict with three
   answers reads the card each Ollama uses: pinned, not pinned, unknown.
5. **The probe measures one card.** `hw_probe.sh --gpu <uuid>` measures that card; an unknown UUID is a
   refusal, never a fallback to the CPU tier; `--list-gpus` lists the cards. The model family is still
   picked from a hash of the machine key (`deploy/hw_probe.sh::MACHINE_KEY`), not of the identity, so the
   slots of one machine stay in one family.
6. **A plan first, then a sequential start.** `--plan` prints the slots and changes nothing; the installer
   relays `--gpus` and shows it in its own plan. The node starts once; then slot 0, with the image, the
   judge and the model; then its health; then the RAM its containers use is measured. Each further slot
   starts from the same image without building, and only if the available memory, less a reserve and less
   `MOE_CPU_MIN_RAM_MB` while the judge has not loaded its model, covers the measured footprint. The slots
   share one model store.
7. **One CPU judge per machine, shared.** Slot 0's CPU judge joins an external Docker network under an
   alias. With the judge role, on a machine that can judge, every slot posts its verdicts through it; the
   judge profile never starts on another slot.
8. **Rigs that cannot judge are allowed, with a warning that counts the mute seats.** The installation
   says how many identities it registers without a judge, computed when it runs.
   `DENDRA_JUDGE_REQUIRED=1` turns the warning into a refusal. The remedy is on chain and outside this
   record: a juror eligibility the chain can check, which a role merely declared would not give, since
   declaring costs nothing. It is listed, undecided, under *Open for the mainnet genesis* in
   [ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md).
9. **The faucet's quota per source IP address stays, and the daemon retries.** The quota is
   `services/faucet.py::IP_DAILY`, a setting of the faucet service that the owner lowered on
   2026-10-08 ([ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md)); the limit per receiving address
   is another one, and is not what a rig meets. The daemon retries the faucet and its registration in its
   loop at a slow pace, reads the reason the faucet gives, and does not report ready before it is
   registered. At the first refusal for the per-IP quota, the following slots do not start, and the command
   that resumes them is printed: a rig with more cards than the quota behind one IP address registers its
   further slots on later days.
10. **Capacity.** One scheduled line loops over the slots. Each report is signed inside its slot's own
    compose project, and reports other than slot 0's say that they share a host, so that the registry
    counts the machine's RAM and cores once.
11. **The application discovers the slots,** shows one view per identity, acts on one slot or on all, and
    declares one payout address for every slot, signed by each identity.
12. **Removing a card's slot stops it and keeps its volumes,** never `down -v`. The `delete-miner` command
    of each identity is printed ([ADR-055](ADR-055-miner-lifecycle.md)).
13. **Self-jury is written, not hidden.** An operator's identities can sit on the audit of one another's
    work and vote alike: same model, same endpoint. It is already possible with several machines (ADR-047,
    *Consequences*); the kit makes it a flag. The chain cannot exclude it without a link between an
    identity and its operator, which it does not have. The public documentation says so.
14. **This record decides no cap; the question of a payout cap per miner stays open.** The season has no
    cap per identity by the owner's decision 11 of ADR-047; its caps are the programme's own
    (`cap_programme_day` and `cap_season` in `RULES`). On 2026-10-08 the owner left open whether a payout
    cap per miner should follow, to be seen later; on this chain a miner is an identity. Any cap proposed
    then has to say what it does against a split, and three facts bound the answer. One identity per card
    makes splitting a flag. A cap per machine or per operator cannot be enforced: no machine identity
    exists on chain, the miner id is derived from an address, and capacity reports are declared by their
    sender. And the stake ceiling of the work draw (`assignment_stake_cap_multiple` × `min_stake`) is per
    identity: splitting a stake into identities each at the ceiling goes around it too (ADR-047,
    *Consequences*).

## Set aside

- **One identity with one Ollama across every card:** it allows a larger model, but the season pays one
  rate whatever the model (ADR-047, decision 8) and the work draw weighs an identity, whatever its cards:
  the second card earns nothing.
- **One identity with one Ollama per card behind a balancer:** throughput is not the constraint, since the
  programme's volume is fixed for the whole network and one card already serves several identities.
- **Several identities per card:** possible, and it industrialises the split without hardware.
- **One identity per card by default:** each identity costs a faucet grant, a bond and, without a judge, a
  mute seat; the operator chooses it.
- **One judge per slot:** as many copies of the judge's model in RAM as cards, against one judge per
  machine.
- **The CPU judge on a host port:** a loopback port is not reachable through the host gateway, and binding
  every interface would expose it.
- **A machine-wide judge project now:** cleaner, but it downloads the judge's model again unless the
  existing volume is mounted; later.
- **A model volume per slot:** isolation, at the cost of one model's size per card; the shared store with
  a sequential start is chosen.

## Consequences

- Per-card selection under WSL2 is not guaranteed through Docker's device ids; hence the pin at the Ollama
  level and the verdict after start. It is to be measured on a machine with several cards.
- Compose precedence (`-p` against `name:`, `--env-file` against the default `.env`, the automatic
  override file) is proven with `docker compose config`, never assumed: a slot that read slot 0's `.env`
  would sign with slot 0's key.
- The shared judge serialises its verdicts. Their time is compared with `audit_resolve_timeout`, when that
  parameter is not zero, before the judge is enabled on every slot.
- A `down -v`, a renamed project or a deleted slot directory orphans a staked key. Slot 0 never changes
  name, and the uninstall lists every slot's volumes (ADR-055).
- When a card is removed or replaced, the slot whose UUID is gone is reported with the command that
  reassigns it, never reassigned or deleted in silence.
- Until the capacity registry is redeployed, it counts a machine's RAM and cores once per slot.
- `deploy/testnet-miner/publish-capacity.sh` finds the miner container by a name filter, which signs in the
  wrong container once several projects run; the per-project form replaces it.
- In owner mode each identity needs its own cold key ([ADR-051](ADR-051-miner-keys.md)).
- A HiveOS rig typically has several cards and too little RAM to judge: decisions 7 and 8 are what it meets
  first ([ADR-053](ADR-053-cloud-and-hiveos-paths.md)).

## Version

The change touches what an operator runs (`join.sh`, `hw_probe.sh`, the miner's compose, the capacity
script, the daemon): it ships under the one `kit_version` bump shared by ADR-051 to ADR-055, in one release
whose number [ADR-050](ADR-050-release-versioning.md) derives. The capacity registry is a network service:
a redeployment, no bump. `consensus_epoch` does not move.

## Pointers to add at merge

The code below is not in the published tree yet; each item is cited by file and symbol once it lands.

- The `--gpus` and `--plan` flags, the slot plan, the slot environment and override writers, the external
  network and the sequential start in `deploy/join.sh`.
- `--gpu` and `--list-gpus` in `deploy/hw_probe.sh`.
- The slot loop and the per-project signature in `deploy/testnet-miner/publish-capacity.sh`.
- The registration retry in `services/miner.py`.
- The shared-host handling in `services/capacity_server.py`.
- The slot discovery and per-slot compose in `deploy/app/dendra_app.py`.
