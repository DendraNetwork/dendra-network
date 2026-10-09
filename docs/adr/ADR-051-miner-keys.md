# ADR-051 — Miner keys: an encrypted keyring by default, a declared payout address, and an optional cold owner

**Status:** Accepted by the project owner, 2026-10-08 — implementation in progress.
**Implementation:** Decided, not implemented in the published kit; the work is in progress. Decisions 1
to 10 change only what an operator runs and ship under the single `kit_version` bump shared by ADR-051 to
ADR-055. Decision 11 is consensus-breaking and is deferred to the mainnet genesis; until then this record
is its only form.

## Context

- The miner's Cosmos key lives in a `test` keyring inside the miner's key volume: anyone who reads the
  volume, or one of its backups, holds the key. The same volume holds the VRF secret, the X25519
  encryption key and the recovery phrase written when the key was created
  (`services/miner.py::keep_recovery_phrase`).
- Reading the key is also what decides to create one (`services/miner.py::keys_addr`). A
  reader that takes "no address returned" for "no key" creates a new key, hence a new identity whose bond
  has to be posted again, whenever the keyring cannot be read. With an encrypted keyring a missing or wrong
  passphrase is exactly that case: `dendrad` answers that the name is not a valid name or address.
- The chain already separates two roles on a miner
  (`chain/x/jobs/keeper/msg_server_miner.go::CreateMiner`). The `Creator` pays the bond, alone may
  update or delete the miner, and receives the refund
  (`chain/x/jobs/keeper/msg_server_miner.go::UpdateMiner`,
  `chain/x/jobs/keeper/msg_server_miner.go::DeleteMiner`). The `Operator` anchors the commits
  (`chain/x/jobs/keeper/msg_server_commit.go::CreateCommit`), proves availability
  (`chain/x/jobs/keeper/msg_server_prove_availability.go::ProveAvailability`), claims the subsidy
  (`chain/x/jobs/keeper/msg_server_claim_subsidy.go::ClaimSubsidy`), is paid
  (`chain/x/jobs/keeper/msg_server_payout.go::Payout`) and rotates the anchored keys
  (`chain/x/jobs/keeper/msg_server_miner.go::RotateMinerKeys`). The miner id is derived from the
  Creator's address (`chain/x/jobs/types/miner_id.go::DeriveMinerID`): one address registers exactly
  one miner. `UpdateMiner` writes the operator the message carries, an empty one included.
- The Final Testnet Season pays the address an identity declared, else its operator
  (`services/final_season_facts.py::day_identities`,
  [ADR-047](ADR-047-final-testnet-season-reward-programme.md)), and each point becomes one mainnet DNDR
  credited to that address in the mainnet genesis
  ([ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md), decision 2). A declaration is signed
  by the identity's key, checked by `services/final_season_address.py::payable_address` on the
  season service (`services/final_season_server.py::_payout`), and accepted at most once per
  `services/final_season_server.py::DECLARE_EVERY_BLOCKS`.

## Decisions

1. **Every new installation gets an encrypted keyring.** `join.sh` generates a random passphrase into a
   file outside the key volume and outside the clone (a directory under the user's configuration, mode
   0700, the file mode 0600), and the miner's compose mounts that directory read-only. `--plain-keys`
   refuses the encryption explicitly. An existing installation is never migrated in silence (decision 6).
2. **The backend follows the state of the disk, never an environment variable.** Only a `test` keyring
   present: `test` mode, with the warning that the keys are in clear. A `file` keyring present: `file`
   mode, and the passphrase is required; its absence is a named error, never a fallback to `test`. Both
   present: refusal, rather than a guess. The daemon, the reveal and judge workers, the relay and capacity
   signatures and the application all go through one helper.
3. **The passphrase travels on stdin.** It is read from the mounted file and handed to each `dendrad` call
   on its standard input: never in argv, which `ps` and `/proc` show, never in the container's
   environment, which `docker inspect` shows. The compose never passes `DENDRA_MINER_PASSPHRASE`; that
   variable stays readable for an installation outside a container, with a warning.
4. **Reading the key has three answers.** Key absent, keyring unreadable, key present; only the first
   creates a key. The `dendrad` message for a missing or wrong passphrase is translated into a named
   cause, so that the operator does not go looking for a missing key.
5. **One envelope for every secret at rest.** In `file` mode the same passphrase encrypts the VRF secret,
   the X25519 key, the attestation key and the recovery phrase. Each file is created directly with mode
   0600. A key found in clear while a passphrase is provided is re-encrypted atomically. The warning about
   keys in clear is derived from the files themselves.
6. **Existing installations migrate by script.** `encrypt-keys.sh` prints what it would do; with `--yes`
   it stops the miner, generates the passphrase, exports then imports the key inside the container, checks
   that the address is unchanged, and removes the `test` keyring. It refuses while nothing shows that the
   recovery phrase is written down: a lost passphrase file with no recovery phrase written down is the
   account, and the season's pay, lost. A key created before the kit kept its phrase has no phrase file
   and never will (`services/miner.py::keep_recovery_phrase` writes one only for a key it
   creates), so the confirmation the application records cannot exist for it. Its owner then states which
   of two is true: the 24 words are on paper (`--recovery-phrase-written`), or the key never had words its
   owner holds (`--no-recovery-phrase="<why>"`, a reason required). With the second, the passphrase file
   and the key volume become the only backup of the key, and the script says so before it changes
   anything.
7. **The recovery phrase leaves the machine when it is confirmed.** The application's confirmation
   (`deploy/app/dendra_app.py::_recovery_confirm`) deletes the phrase file, and only after the words it
   asks for are checked; the button says so ("I wrote it down — remove it from this machine"). A refused
   or stale confirmation deletes nothing.
8. **A declared payout address.** `join.sh --payout-address dendra1…` checks the address with
   `payable_address` before changing anything and writes it to the miner's `.env`; the daemon declares it
   once the miner is registered, and again only when it changes. The application shows a banner for as
   long as the season pays the machine's own key.
9. **A cold owner and a hot operator, as an option.** `join.sh --owner dendra1…` uses the Creator/Operator
   pair the chain already has. The identity is derived from the cold address; the daemon does not register
   itself: it asks the faucet to fund the cold address, prints the `create-miner … --generate-only`
   transaction that names the machine's key as operator, with the offline signing and broadcast commands
   to run on the cold device, and waits until the registry names its key as operator. The cold key holds
   the bond: only it can delete the miner, and the refund goes to it. When Creator and Operator differ, the
   season service accepts a payout declaration from the Creator only. The option needs a second device
   with a release `dendrad`.
10. **What the cold owner protects is the capital, not the income.** The kit's documentation says it: a
    stolen hot key still signs commits that can get the cold bond slashed, is paid the jobs and the
    subsidy (`Payout` and `ClaimSubsidy` credit the Operator), and rotates the anchored keys.
11. **The on-chain payout address waits for the mainnet genesis.** A `payout_address` field set by the
    Creator, an `UpdateMiner` that keeps the operator when the message leaves it empty, and a bech32 check
    of the operator are consensus-breaking. They are decided here and enter the mainnet genesis
    (ADR-049), not an upgrade of the running testnet.

A locked mode, where the passphrase is typed into the application at each start and kept in memory only,
stays an option. It is not the default, because the default must restart unattended.

## What the default protects, and what it does not

- It protects the key volume and its backups: a copy of the volume no longer gives the key.
- It does not protect against a compromised host: the passphrase file sits on the same host, readable by
  its administrator. The documentation says so.

## Set aside

- **The `os` and `kwallet` keyring backends:** they need a desktop session, which a container does not
  have.
- **The `pass` backend:** it needs `gpg-agent` in the image.
- **The passphrase in argv or in the environment:** readable with `ps`, `/proc/<pid>/environ` or
  `docker inspect`.
- **Docker secrets:** they belong to swarm mode; the kit runs compose without swarm.
- **`authz` or `feegrant` to separate the roles:** the chain already has the Creator/Operator pair.
- **An upgrade of the running testnet for `payout_address`:** a new consensus epoch, coordinated
  validators and a governed upgrade near the end of the season, for a protection the season service's
  checks already give in the meantime. The mainnet genesis is the moment.
- **Erasing the recovery phrase at creation:** an operator who did not write it down would lose the
  account and the pay.
- **Migrating existing installations automatically:** a migration that fails half-way, or runs before the
  phrase is written down, loses the identity.

## Consequences

- A lost passphrase file makes the keyring unreadable. Only the recovery phrase restores it, and once it
  is erased from the disk it exists only where the operator wrote it.
- An `.env` that names no secrets directory still starts: the default mount is an empty directory of the
  kit, and the backend follows the disk.
- Every `dendrad` call decrypts the keyring. Its cost on the time-bound paths, the reveal and the
  verdict, is to be measured.
- `dendrad` sub-commands do not read the passphrase the same number of times (`keys add` on a new keyring
  asks for it twice); every sub-command the kit uses is exercised against the real binary.
- In owner mode one cold key is one identity: per-card identities
  ([ADR-054](ADR-054-one-identity-per-card.md)) need one cold key each.
- An `update-miner` without an operator silences the miner. The kit never sends one, and the
  documentation says that `update-miner` always carries the operator and that `delete-miner` is signed by
  the Creator, the cold key in owner mode ([ADR-055](ADR-055-miner-lifecycle.md)).
- Cloud pods and HiveOS rigs have no application: the passphrase and the confirmation of the phrase go
  through the installer there ([ADR-053](ADR-053-cloud-and-hiveos-paths.md)).

## Version

Decisions 1 to 10 change what an operator runs. They ship under the one `kit_version` bump shared by
ADR-051 to ADR-055, in the commit that empties `docker/MINER_IMAGE`, and in one release whose number
[ADR-050](ADR-050-release-versioning.md) derives (`kit_version` alone moves the patch number).
`consensus_epoch` does not move. The season service's Creator check is network-side: a redeployment,
no bump. Decision 11 moves `consensus_epoch` and therefore waits for the mainnet genesis.

## Pointers to add at merge

The code below is not in the published tree yet; each item is cited by file and symbol once it lands.

- The keyring helper, a new module under `services/modea/`: backend from the disk, passphrase on
  stdin, named causes.
- The three-answer key reader and the owner mode in `services/miner.py`.
- The envelope and the re-encryption of a key found in clear in `services/modea/crypto.py`.
- `deploy/testnet-miner/encrypt-keys.sh`.
- The flags `--payout-address`, `--owner` and `--plain-keys`, and the passphrase generation, in
  `deploy/join.sh`.
- The Creator-only payout declaration in `services/final_season_server.py`.
- The deletion at confirmation in `deploy/app/dendra_app.py`.
- The Go test that fixes the Creator/Operator split, under `chain/x/jobs/keeper/`.
