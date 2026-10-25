# Mining on a HiveOS rig

Each release publishes a HiveOS custom-miner package next to its binaries: **`dendra-<version>.tar.gz`**
and its checksum, **`SHA256SUMS.hiveos`** (built by [`build.sh`](build.sh) from the tagged tree). The
package does not carry a miner of its own: it runs [`deploy/install.sh`](../install.sh) — the same
installer as on any Linux host — on the **release tag** it was built for, hands it the reference of that
release's miner image (see *Disk* below), then follows the miner's logs.

## The flight sheet

| Field | Value |
|---|---|
| Coin | any name of your choice (the wallet below belongs to it) |
| Wallet | your **payout address**, `dendra1…`: where the Final Testnet Season pays this rig's miner. The public address of a key you keep; never a recovery phrase or a private key — a value that looks like either is refused, and not printed. |
| Miner | **Custom** |
| Installation URL | the URL of `dendra-<version>.tar.gz` in the latest release; while a governed upgrade is pending, in the release of the epoch `network-info.txt` declares (see *A new consensus epoch* below) |
| Wallet and worker template | `%WAL%` — the address alone, no worker name |
| Pool URL | empty for the public network, or the `network-info.txt` address of another network |
| Extra config arguments | one `KEY=VALUE` per line, from the list below |

**Extra config** — an allow-list, nothing else is read:

- `YES=1` — **consent**. Without it the package installs and starts **nothing**: it prints the
  installer's plan (`install.sh --check`) in the miner's log and waits. With it, it runs
  `install.sh --yes`.
- `ROLE=judge` (default) or `ROLE=miner`. The judge is on by default: a rig that can judge
  (`deploy/hw_probe.sh --can-judge`: `true`, which needs `MOE_CPU_MIN_RAM_MB` of RAM, and the judge's
  disk floor, `MIN_DISK_GB_JUDGE` in `install.sh`) mines on its card and judges on its CPU; one that
  cannot keeps the miner role, and `join.sh` says so. `ROLE=miner` declines the seat (`join.sh --no-judge`,
  recorded in the kit). Switching later: change `ROLE` and restart the miner (the package re-runs the
  installer with the new role), or on the rig `bash ~/dendra-network/deploy/testnet-miner/judge.sh on|off|status`,
  where `status` reads what the judge produced on chain. No mining model runs on the CPU: a rig without a
  usable NVIDIA card is a judge or is refused, as `install.sh` reads it from `deploy/hw_probe.sh --role`
  before installing anything (`ROLE=miner` is then refused).
- The payout address is declared **locked** with its first declaration (`install.sh --payout-lock`, passed by
  the package): a locked address cannot be corrected afterwards. `h-config.sh` and `h-run.sh` print it before
  anything starts; check it before `YES=1`. A rig whose identity already declared without a lock is not locked
  by this: the programme accepts a lock as the first declaration only.
- The rig does not update itself: the package passes `DENDRA_AUTO_UPDATE=0`, and the clone stays on the tag
  its package names. Updating means the package of a newer release (below).
- `LIGHT=1` — read the chain from the network's public RPC instead of running a node on the rig
  (`install.sh --light`): less disk, and the trade `deploy/join.sh --remote-rpc` describes.

A key outside the list is **ignored and named** in the log. A value outside its allowed set, or one that
carries a quote, a dollar sign, a space or another character outside letters, digits and `. _ : / @ , = ? % + ~ -`,
is **refused**: the package then starts nothing and says why in the miner's log. A refused wallet is shown
only when it starts with `dendra1` (a checksum error is worth seeing); any other value is never printed.
When the configuration cannot be written (a full disk, a read-only directory), the previous one is replaced
by a refusal or removed — never left for the miner to apply.

**When the installer stops.** A failure (exit 1) or a refusal (exit 2) is printed and the package waits: it
does not retry on its own, since HiveOS would restart it into the same failure. Exit 3 is read apart. When
`join.sh` started the miner and could not measure its health within its bound (the miner keeps registering),
the installation is applied and the miner's logs follow; when nothing was started (the host or its role could
not be read), the package says so and waits, as for a failure. Restarting the miner in HiveOS resumes an
applied installation without installing again.

## Check the archive before you use its URL

The flight sheet has no field for a checksum: HiveOS installs what the Installation URL serves. Download the
archive yourself first. Verify the list's signature with the signers file of the repository,
`ssh-keygen -Y verify -f docker/RELEASE_SIGNERS -I releases@dendranetwork.com -n dendra-release -s SHA256SUMS.hiveos.sig < SHA256SUMS.hiveos`,
then compare `sha256sum dendra-<version>.tar.gz` with the line in `SHA256SUMS.hiveos` of the same release.

## Turn the hashrate watchdog off

An inference miner computes no hashes. The package reports **`khs` = 0** and **`hs` = [0]** — never a
number of requests or tokens dressed up as a hashrate. HiveOS therefore shows 0 H/s, and **its hashrate
watchdog must be disabled for this rig**, or it restarts the miner in a loop.

What the stats do report:

- `ver` — the package's version and the `kit_version` of the clone it installed;
- `uptime` — since the miner's container started;
- `ar` — commits anchored and commits refused, read from the miner's own heartbeat when it carries those
  two counters. They count the miner's `create-commit` transactions **since its process started** (a
  restart of the miner sets both back to zero): *anchored* when the commitment was then read on the chain,
  *refused* when the chain answered the transaction with a non-zero code — a job retried after a refusal
  counts each refused attempt. A transaction whose fate was not read counts in neither: no answer, not
  found in time, or refused while the node estimated its gas (it is then never broadcast, and the miner's
  log names the reason). A miner that anchored nothing shows `[0, 0]`; when the heartbeat cannot be read,
  or does not carry them, `ar` is **left out** rather than shown as zero.

## Disk

`install.sh` refuses a host whose free disk is below its floor (`MIN_DISK_GB`, higher for a judge) before
it installs anything, because the models and images would not fit and the failure would come an hour
later. A rig booted from a small flash drive usually needs a second drive for Docker's data root; moving it
is a change to the system that this package does not make for you.

**The miner image comes with the package.** A release tag pins no image (`docker/MINER_IMAGE` carries no
value in a tagged tree: an image is pinned on `main`, after the release that built it). So the package of a
release carries the two lines of **that release's** `miner-image.txt` (`lib/miner-image.txt`: a
`# built-for:` line, then the image by digest), and `build.sh` refuses to pack one that names another
release or another kit. Once the clone is checked on the tag, `install.sh` writes those two lines into the
clone's `docker/MINER_IMAGE` (only when the tag names no image, never over a value), and `deploy/join.sh`
judges them like any pin: the official namespace and a full digest, `built-for` gates equal to the clone's
`docker/KIT_VERSION` and `docker/CONSENSUS_EPOCH`, and the image's platform equal to the Docker engine's.
When one of them does not hold, or the pull fails, it builds the image from the tag instead and says why.

That write is the clone's only local change, and `install.sh` accepts it as such only when the file holds
exactly the bytes it wrote (it keeps a copy in the clone's `.git`); it undoes it before moving the clone to
another tag, so a newer package never runs the previous release's image. Any other edit to the clone is
refused as local work, as before.

**When the package carries no image** — a package built by hand with `build.sh` without its third argument;
the release workflow always gives it — the rig builds the miner image from the tag's `docker/Dockerfile.miner`,
which compiles `dendrad` in Go: the first installation then takes longer, and needs more disk and memory
while it builds. The miner's log says which of the two happened. The base images of that build are named by
tag, not by digest: the kit's files stay on the release tag, those images do not. The Ollama image of
`deploy/testnet-miner/docker-compose.yml` is pinned by version and digest (its `x-ollama-image` line).

## Keys: back them up, and restore them after a reflash

The rig's identity is its key: the chain derives the miner id from the key's address
(`chain/x/jobs/types/miner_id.go::DeriveMinerID`). Same key, same address, same identity. **A lost key
is a lost identity**: its stake, and the season points it would go on earning. Points already earned are
paid to the payout address their rows name (`services/final_season_payout.py::parse_day`), which the
key does not hold.

Where things live on the rig:

- the key: the Docker volume `dendra-miner_miner-keys`;
- the keyring passphrase: `~/.config/dendra/miner-secrets/keyring-passphrase`;
- what the package remembers between starts (the settings last applied): `~/.config/dendra/hiveos`, outside
  the package directory, which a package update replaces;
- the backups: `~/.config/dendra/backups`.

`~` is the home of root when the tool runs as root (it reads it with `getent passwd 0`, whatever `HOME` a
`sudo` kept), so the backups and a pending restore land where the package, run as root, looks.

All of them are on the system drive, and **a reflash rewrites the system drive**. Only a copy kept off the
rig survives it.

**Back up**, once the miner runs, in one gesture, with the copy of the tool the package carries (`lib/` of
the package directory; the miner's log prints its full path):

    sudo bash <package directory>/lib/backup-keys.sh backup

It asks for a passphrase twice (at least `BK_MIN_PASSPHRASE` characters; it is never written to a file and
never printed), and writes one archive, `dendra-keys-<volume>-<UTC time>.dkb`, readable by root only. The
archive holds the key and the keyring passphrase together, encrypted with your passphrase; it is read back and
opened before it is reported as written. Copy it off the rig: another computer, or a drive you unplug (`--to <dir>` writes it there directly, on a
file system with or without hard links).
Without its passphrase nobody can open it, you included. Every time HiveOS applies the flight sheet, the
miner's log names the latest backup on the rig, or says there is none.

**After a reflash**, restore before the miner first starts, since a miner that starts on an empty volume
creates a new key, and so a new identity:

1. install the package with the same flight sheet, **without** `YES=1`;
2. install Docker Engine and Compose v2 by hand (<https://docs.docker.com/engine/install/>): the package's
   installer would do it, and it waits for `YES=1`, which must come after the restore;
3. copy the archive onto the rig, and run `sudo bash <package directory>/lib/backup-keys.sh restore <archive>`:
   the package carries the tool, so the restore needs no clone;
4. then set `YES=1`.

On a rig with one identity per card, name the slot with `--volume` (the restore prints the volume the archive
was made from, and refuses another one unless `--accept-other-volume` is given). `deploy/uninstall.sh`
writes its key backup in the same encrypted format, one archive per key volume.

The restore checks the whole archive in memory before it writes anything: a wrong passphrase or an altered
byte is refused, and nothing is touched. It creates the volume, writes the key and the passphrase file, and
reads both back byte for byte. It never writes over a key: a volume that already holds files, or a
passphrase file with other bytes, is refused. When Docker is missing it writes nothing and records the
restore as pending (`~/.config/dendra/restore-pending`): the package then installs nothing until the restore
is finished, or that file is removed to start a new identity on purpose. `backup-keys.sh verify <archive>`
checks an archive and changes nothing.

## A new consensus epoch: the governed upgrade
A rig that runs its own node (without `LIGHT=1`) has the automatic update off: `h-run.sh` passes
`DENDRA_AUTO_UPDATE=0`, and `join.sh` then schedules no upgrade watch (`deploy/join.sh::install_kit_update_cron`).
At the height of a governed upgrade plan the rig's node stops, and it stays stopped until its owner acts. The
announcement of the release of the new epoch names the plan, its height and its package. Once the chain has
halted at that height:

1. change the Installation URL to the package of the new epoch. Its installer moves the clone to the new tag
   and runs the join without `--after-upgrade`, which the package does not pass: that join refuses an epoch
   `network-info.txt` does not declare yet (`deploy/join.sh::verify_consensus_epoch`), and a node image of the
   previous epoch pinned for a node that holds chain state (`deploy/join.sh::node_env_image_reconcile`);
2. on the rig, as root, from the clone, run the join the kit recorded (the value of the `DENDRA_KIT_RERUN` line
   of `deploy/testnet-miner/.env`) followed by `--after-upgrade <plan>`. The chain confirms that the node stands
   at that plan's halt, the node starts on the node image of the new tree, and the join goes on once the node
   answers `dendrad query upgrade applied <plan>` with a height (`deploy/join.sh::after_upgrade_applied`).

## Stopping, updating, removing

- **Stop the miner in HiveOS** and what the package started stops too: an installation still running (the
  installer and everything it started, as one process group), then the miner's containers
  (`docker compose -p dendra-miner stop`), and the node's when the package installed one (without
  `LIGHT=1`; its compose project is the one the node kit names). They are started with
  `restart: unless-stopped`, and stopping only the package would leave them running out of sight. Starting
  again resumes them, the node first, without reinstalling, as long as the settings and the release tag
  have not changed.
- **Update** by changing the Installation URL to the package of a newer release; that is also what the
  miner's log says when the network publishes a newer kit. A package never follows a branch and never
  pulls: its files stay on the tag written in its `h-manifest.conf` (the images, see *Disk* above).
- **Leave the network** with `bash /root/dendra-network/deploy/testnet-miner/exit-miner.sh` (it simulates
  first; `--yes` to leave), and **remove the kit** with `bash /root/dendra-network/deploy/uninstall.sh`.
  The clone lives in `dendra-network` in the home of the user HiveOS runs the miner as.
