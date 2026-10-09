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
| Installation URL | the URL of `dendra-<version>.tar.gz` in the release you chose |
| Wallet and worker template | `%WAL%` — the address alone, no worker name |
| Pool URL | empty for the public network, or the `network-info.txt` address of another network |
| Extra config arguments | one `KEY=VALUE` per line, from the list below |

**Extra config** — an allow-list, nothing else is read:

- `YES=1` — **consent**. Without it the package installs and starts **nothing**: it prints the
  installer's plan (`install.sh --check`) in the miner's log and waits. With it, it runs
  `install.sh --yes`.
- `ROLE=miner` (default) or `ROLE=judge`. A judge needs the RAM `deploy/hw_probe.sh` asks for
  (`MOE_CPU_MIN_RAM_MB`) and a larger disk (`MIN_DISK_GB_JUDGE` in `install.sh`). No mining model runs
  on the CPU: a rig without a usable NVIDIA card is a judge or is refused, as `install.sh` reads it from
  `deploy/hw_probe.sh --role` before installing anything (`ROLE=miner` is then refused).
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
archive yourself first, and compare `sha256sum dendra-<version>.tar.gz` with the line in `SHA256SUMS.hiveos`
of the same release.

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
clone's `docker/MINER_IMAGE` — only when the tag names no image, never over a value — and `deploy/join.sh`
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

## Stopping, updating, removing

- **Stop the miner in HiveOS** and what the package started stops too: an installation still running (the
  installer and everything it started, as one process group), then the miner's containers
  (`docker compose -p dendra-miner stop`), and the node's when the package installed one (without
  `LIGHT=1`; its compose project is the one the node kit names). They are started with
  `restart: unless-stopped`, and stopping only the package would leave them running out of sight. Starting
  again resumes them — the node first — without reinstalling, as long as the settings and the release tag
  have not changed.
- **Update** by changing the Installation URL to the package of a newer release; that is also what the
  miner's log says when the network publishes a newer kit. A package never follows a branch and never
  pulls: its files stay on the tag written in its `h-manifest.conf` (the images, see *Disk* above).
- **Leave the network** with `bash /root/dendra-network/deploy/testnet-miner/exit-miner.sh` (it simulates
  first; `--yes` to leave), and **remove the kit** with `bash /root/dendra-network/deploy/uninstall.sh`.
  The clone lives in `dendra-network` in the home of the user HiveOS runs the miner as.
