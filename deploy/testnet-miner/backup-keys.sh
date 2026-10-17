#!/usr/bin/env bash
# backup-keys.sh -- the miner's keys in one encrypted archive, and the restore of that archive.
#
# The miner's identity is its key: the chain derives the miner id from the key's address
# (chain/x/jobs/types/miner_id.go::DeriveMinerID). Same key, same address, same identity. A lost key is a
# lost identity, with its stake and its record. The key lives in the Docker volume miner-keys
# (dendra-miner_miner-keys), and the keyring passphrase in its own directory (DENDRA_SECRETS_DIR, see
# encrypt-keys.sh): both on the system drive, so a reinstalled or reflashed system drive loses both.
#
# Verbs:
#   backup  [--to <dir>]   one archive of the volume and of the keyring passphrase file, encrypted with a
#                          passphrase you type. Written 0600 into <dir> (default: ~/.config/dendra/backups,
#                          outside the clone, or DENDRA_BACKUP_DIR), then read back, opened and compared with
#                          the volume before it is reported as written. Copy it off this machine.
#   verify  <archive>      opens the archive with your passphrase and checks it. Changes nothing.
#   restore <archive>      checks the archive first, in memory, and only then writes: the volume (created
#                          when absent) and the passphrase file, both read back byte for byte. It never writes
#                          over a key: a volume holding other files, or a passphrase file with other bytes, is
#                          refused. Restore before the miner first starts on a new system: a miner that starts
#                          on an empty volume creates a new key, and so a new identity.
#                          It prints the volume the archive was made from next to the target, and refuses when
#                          they differ: with one identity per card, that would move an identity to another
#                          card's slot. --accept-other-volume restores it into the target all the same.
#                          When Docker is missing, the archive is checked, nothing is written, and the restore
#                          is recorded as pending (~/.config/dendra/restore-pending): the HiveOS package
#                          installs nothing while that file is there. Install Docker, then restore again.
# Options:
#   --volume <name>        the keys volume (default dendra-miner_miner-keys; with one identity per card,
#                          bash deploy/testnet-miner/slots.sh list names the others)
#   --secrets-dir <dir>    the keyring passphrase directory (default: DENDRA_SECRETS_DIR of the kit's .env next
#                          to this file, else ~/.config/dendra/miner-secrets, where deploy/join.sh puts it)
#   --passphrase-fd <n>    read the archive passphrase from file descriptor <n> (its first line) instead of
#                          asking on the terminal. The passphrase is never taken from an argument or a file
#                          name, never written, never printed.
#   --via-image <image>    backup only: read the volume through a container of that image (an image of the
#                          kit already on this host, which carries tar; nothing is pulled), streamed into this
#                          process's memory, instead of its directory on the disk. For an account in the docker
#                          group that is not root, or a Docker daemon whose volumes are not on this file system.
#                          deploy/uninstall.sh backs the keys up this way.
#   --accept-other-volume  restore only: see restore above.
#
# Where it writes: the backups and the pending restore go under XDG_CONFIG_HOME/dendra when it is set, else
# under the home of the account running it, taken from the password database for root (getent passwd 0), so
# that a sudo that keeps the caller's HOME still writes where the HiveOS package, run as root, reads.
#
# The passphrase: at least BK_MIN_PASSPHRASE characters, asked twice for a backup. The archive leaves this
# machine, and whoever holds a copy can try passphrases offline; each try costs BK_KDF_ITER rounds of
# PBKDF2-SHA256, and the length of the passphrase does the rest. Forgotten, the archive opens for nobody.
# The archive holds the keys and the keyring passphrase together: its own passphrase is what protects them.
#
# Format: `openssl enc`, AES-256-CBC with a random salt and a key from PBKDF2-SHA256 (BK_KDF_ITER rounds).
# The plaintext is a line "DENDRA-KEYS-BACKUP 1", a line "sha256 <hex>", then a tar of `meta`, `volume/`
# and `secrets/keyring-passphrase`; the digest covers the tar. A wrong passphrase, or a byte altered in the
# archive, fails that digest, and nothing is written. The plaintext exists only in memory.
#
# Needs docker, python3 and openssl (1.1.1 or later, for -pbkdf2). The volume's directory is read and written
# in place (docker volume inspect: Mountpoint), which needs root where the Docker daemon runs as root (sudo).
#
# Exit codes -- three answers, never two:
#   0  done: backup written and read back; archive verified; keys restored and read back, or already there
#   1  a step was attempted and failed (what it wrote is removed; the message says what is left)
#   2  refused: a short or mismatched passphrase, an archive that does not open or was altered, a volume or a
#      passphrase file holding other bytes, an archive of another volume, no key to back up, or a restore
#      left pending (Docker missing)
#   3  not measurable: a tool missing, the Docker daemon not answering, the volume's directory out of reach
set -u
BK_MIN_PASSPHRASE=12
BK_KDF_ITER=600000

refuse(){ echo "  [refused] $*" >&2; exit 2; }
unmeasurable(){ echo "  [?] $*" >&2; exit 3; }

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
VERB="${1:-}"; [ $# -gt 0 ] && shift
ARCHIVE=""; OUTDIR=""; VOLUME=dendra-miner_miner-keys; SD=""; PFD=""; VIA=""; ACCEPT_OTHER=0
case "$VERB" in
  backup) : ;;
  verify|restore)
    ARCHIVE="${1:-}"
    case "$ARCHIVE" in ""|-*) refuse "$VERB needs the archive: bash $SELF $VERB <archive>" ;; esac
    shift ;;
  -h|--help) awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; exit 0 ;;
  *) refuse "name a verb first: backup, verify <archive> or restore <archive> (see --help)" ;;
esac
while [ $# -gt 0 ]; do
  if [ "$1" = --accept-other-volume ]; then
    [ "$VERB" = restore ] || refuse "--accept-other-volume belongs to restore"
    ACCEPT_OTHER=1; shift; continue
  fi
  [ $# -ge 2 ] || refuse "$1 needs a value (see --help)"
  case "$1" in
    --to) [ "$VERB" = backup ] || refuse "--to belongs to backup"; OUTDIR="$2" ;;
    --volume) VOLUME="$2" ;;
    --secrets-dir) SD="$2" ;;
    --passphrase-fd) PFD="$2" ;;
    --via-image) [ "$VERB" = backup ] || refuse "--via-image belongs to backup"; VIA="$2" ;;
    *) refuse "unknown argument: $1 (see --help)" ;;
  esac
  shift 2
done
case "$VOLUME" in ""|*[!A-Za-z0-9_.-]*) refuse "--volume takes a Docker volume name (letters, digits, _ . -)" ;; esac
case "$PFD" in *[!0-9]*) refuse "--passphrase-fd takes a file descriptor number" ;; esac
case "$VIA" in *[!A-Za-z0-9_.:/@-]*) refuse "--via-image takes an image reference (letters, digits, _ . : / @ -)" ;; esac

# The configuration home (see the header): root's is read from the password database, never from HOME.
if [ -n "${XDG_CONFIG_HOME:-}" ]; then
  CONF_HOME="$XDG_CONFIG_HOME/dendra"
elif [ "$EUID" = 0 ]; then
  _rh="$(getent passwd 0 2>/dev/null | cut -d: -f6)"
  case "$_rh" in /*) CONF_HOME="$_rh/.config/dendra" ;; *) unmeasurable "the home directory of root cannot be read (getent passwd 0): set XDG_CONFIG_HOME. Nothing was done." ;; esac
else
  CONF_HOME="$HOME/.config/dendra"
fi
PENDING="$CONF_HOME/restore-pending"
[ -n "$OUTDIR" ] || OUTDIR="${DENDRA_BACKUP_DIR:-$CONF_HOME/backups}"
# The passphrase directory as join.sh chooses it: the kit's .env (read, never sourced), else the default.
KIT="$(dirname "$SELF")"
if [ -z "$SD" ] && [ -f "$KIT/docker-compose.yml" ] && [ -r "$KIT/.env" ]; then
  SD="$(awk 'index($0, "DENDRA_SECRETS_DIR=") == 1 { print substr($0, 20); exit }' "$KIT/.env" | tr -d '[:cntrl:]')"
fi
[ -n "$SD" ] || SD="$CONF_HOME/miner-secrets"
case "$SD" in /*) : ;; *) refuse "the passphrase directory must be an absolute path: $SD" ;; esac
case "$OUTDIR" in /*) : ;; *) OUTDIR="$(pwd)/$OUTDIR" ;; esac

for _t in python3 openssl; do
  command -v "$_t" >/dev/null 2>&1 || unmeasurable "$_t is not on this machine: the archive is made and opened with it."
done
printf '' | openssl enc -aes-256-cbc -pbkdf2 -iter 1 -md sha256 -pass pass:probe >/dev/null 2>&1 || unmeasurable "this openssl does not offer -pbkdf2 (1.1.1 or later is needed): nothing was done."

DOCKER=0; MP=""; HAVE_VOL=0
if [ "$VERB" != verify ] && command -v docker >/dev/null 2>&1; then
  DOCKER=1
  docker info >/dev/null 2>&1 || unmeasurable "the Docker daemon does not answer from this account (run this as root, or with sudo)."
  if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    HAVE_VOL=1
    # Read through an image (--via-image), the volume's directory is never opened from here.
    if [ -z "$VIA" ]; then
      MP="$(docker volume inspect -f '{{.Mountpoint}}' "$VOLUME" 2>/dev/null)"
      [ -n "$MP" ] && [ -d "$MP" ] || unmeasurable "the directory of the volume $VOLUME (${MP:-none given}) is not on this machine's file system: this file reads it in place (or give --via-image <an image of the kit>)."
      [ -r "$MP" ] && [ -x "$MP" ] || unmeasurable "the directory of the volume $VOLUME ($MP) cannot be read from this account: run this as root (sudo)."
      if [ "$VERB" = restore ] && [ ! -w "$MP" ]; then
        unmeasurable "the directory of the volume $VOLUME ($MP) cannot be written from this account: run this as root (sudo)."
      fi
    fi
  fi
fi
if [ "$VERB" = backup ]; then
  [ "$DOCKER" = 1 ] || unmeasurable "docker is not on this machine: the keys live in a Docker volume."
  [ "$HAVE_VOL" = 1 ] || refuse "there is no Docker volume $VOLUME on this machine: no miner key to back up (docker volume ls names the volumes)."
fi

# Everything below runs in one process, so that the passphrase and the plaintext never leave its memory.
# shellcheck disable=SC2016
PY='
import errno, getpass, hashlib, io, os, stat, subprocess, sys, tarfile, time

nl = chr(10)
nlb = bytes([10])
magic = b"DENDRA-KEYS-BACKUP 1"
verb = sys.argv[1]
a = dict(zip(sys.argv[2::2], sys.argv[3::2]))
cipher = ["-aes-256-cbc", "-salt", "-pbkdf2", "-iter", a["iter"], "-md", "sha256"]
max_pp = 1024
# What link() answers on a file system that has no hard links.
no_link = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS, errno.EXDEV}


def say(s):
    print(s, flush=True)


def stop(code, s):
    print(s, file=sys.stderr, flush=True)
    sys.exit(code)


def refused(s):
    stop(2, "  [refused] " + s)


def failed(s):
    stop(1, "  [FAILED] " + s)


def unmeasurable(s):
    stop(3, "  [?] " + s)


def read_passphrase(confirm):
    fd = a["fd"]
    if fd:
        chunks = []
        try:
            while sum(len(c) for c in chunks) <= max_pp + 2:
                c = os.read(int(fd), 4096)
                if not c:
                    break
                chunks.append(c)
                if nlb in c:
                    break
        except OSError as e:
            refused("the passphrase could not be read from file descriptor " + fd + " (" + str(e.strerror) + ").")
        raw = b"".join(chunks).split(nlb)[0]
        if raw.endswith(bytes([13])):
            raw = raw[:-1]
        try:
            pp = raw.decode("utf-8")
        except UnicodeDecodeError:
            refused("the passphrase is not UTF-8 text.")
    else:
        try:
            os.close(os.open("/dev/tty", os.O_RDWR))
        except OSError:
            refused("there is no terminal to ask the passphrase on: run this from a terminal, or give --passphrase-fd <n>.")
        try:
            pp = getpass.getpass("Archive passphrase: ")
            if confirm and getpass.getpass("The same passphrase again: ") != pp:
                refused("the two passphrases differ. Nothing was written.")
        except (EOFError, KeyboardInterrupt):
            refused("no passphrase was given. Nothing was written.")
    if not pp:
        refused("the passphrase is empty. Nothing was written.")
    if len(pp) > max_pp:
        refused("the passphrase is longer than " + str(max_pp) + " characters. Nothing was written.")
    if confirm and len(pp) < int(a["min"]):
        refused("the passphrase has " + str(len(pp)) + " characters and needs at least " + a["min"]
                + " (BK_MIN_PASSPHRASE): whoever holds a copy of the archive can try passphrases offline. Nothing was written.")
    return pp


def ssl(direction, data, pp):
    # The passphrase goes to openssl through a pipe it inherits (-pass fd:N): never an argument, never a file.
    r, w = os.pipe()
    try:
        os.write(w, pp.encode("utf-8") + nlb)
    finally:
        os.close(w)
    try:
        p = subprocess.run(["openssl", "enc", direction] + cipher + ["-pass", "fd:" + str(r)], input=data,
                           pass_fds=(r,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        os.close(r)
    return p.returncode, p.stdout


def collect(mp):
    """The directory as (dirs, files, others): relative path -> (mode, uid, gid) and, for a file, its bytes."""
    dirs, files, others = {}, {}, []
    st = os.lstat(mp)
    dirs[""] = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
    for root, dnames, fnames in os.walk(mp):
        dnames.sort()
        for n in sorted(dnames + fnames):
            p = os.path.join(root, n)
            rel = os.path.relpath(p, mp)
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                dirs[rel] = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
            elif stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    files[rel] = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, f.read())
            else:
                others.append(rel)
    return dirs, files, others


def collect_via(vol, image):
    """The volume as collect() gives it, read through a container of the image: its tar comes into this
    process through a pipe and is parsed in memory, so no plaintext copy reaches a disk."""
    p = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", vol + ":/k:ro", "--entrypoint", "tar",
                        image, "-C", "/k", "-cf", "-", "."], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 or not p.stdout:
        unmeasurable("the volume " + vol + " could not be read through the image " + image + " (docker run exited "
                     + str(p.returncode) + "). Nothing was written.")
    dirs, files, others = {}, {}, []
    try:
        with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r:") as t:
            for m in t.getmembers():
                n = m.name
                while n.startswith("./"):
                    n = n[2:]
                rel = "" if n in ("", ".") else n
                if rel.startswith("/") or any(x in ("", ".", "..") for x in (rel.split("/") if rel else [])):
                    unmeasurable("the tar of the volume " + vol + " names " + m.name + ", which is not a plain relative path. Nothing was written.")
                if m.isdir():
                    dirs[rel] = (m.mode & 0o7777, m.uid, m.gid)
                elif m.isreg():
                    files[rel] = (m.mode & 0o7777, m.uid, m.gid, t.extractfile(m).read())
                else:
                    others.append(rel)
    except (tarfile.TarError, EOFError) as e:
        unmeasurable("what the image " + image + " returned for the volume " + vol + " is not a tar (" + str(e) + "). Nothing was written.")
    if "" not in dirs:
        unmeasurable("the tar of the volume " + vol + " has no entry for the volume itself. Nothing was written.")
    return dirs, files, others


def tar_of(volume, dirs, files, secret):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        def add(name, kind, mode, uid, gid, data=b""):
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.uid, ti.gid, ti.mtime = kind, mode, uid, gid, 0
            ti.size = len(data) if kind == tarfile.REGTYPE else 0
            t.addfile(ti, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        meta = "format=1" + nl + "volume=" + volume + nl + "created=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + nl
        add("meta", tarfile.REGTYPE, 0o600, 0, 0, meta.encode())
        for rel in sorted(dirs):
            add("volume/" + rel if rel else "volume", tarfile.DIRTYPE, *dirs[rel])
        for rel in sorted(files):
            add("volume/" + rel, tarfile.REGTYPE, *files[rel])
        if secret is not None:
            add("secrets/keyring-passphrase", tarfile.REGTYPE, 0o600, 0, 0, secret)
    return buf.getvalue()


def seal(tb):
    return magic + nlb + b"sha256 " + hashlib.sha256(tb).hexdigest().encode() + nlb + tb


def unseal(plain):
    parts = plain.split(nlb, 2)
    if len(parts) != 3 or parts[0] != magic or not parts[1].startswith(b"sha256 "):
        return None
    if parts[1][7:] != hashlib.sha256(parts[2]).hexdigest().encode():
        return None
    return parts[2]


def members(tb):
    """meta, dirs, files, secret -- or ValueError. Only regular files and directories, under volume/, plus
    meta and secrets/keyring-passphrase; a name that climbs (..), starts at / or repeats is refused."""
    meta, dirs, files, secret = {}, {}, {}, None
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(tb), mode="r:") as t:
        for m in t.getmembers():
            n = m.name
            if n in seen:
                raise ValueError("the name " + n + " appears twice")
            seen.add(n)
            parts = n.split("/")
            if n.startswith("/") or any(p in ("", ".", "..") for p in parts):
                raise ValueError("the name " + n + " is not a plain relative path")
            if n in ("meta", "secrets/keyring-passphrase"):
                if not m.isreg():
                    raise ValueError(n + " is not a regular file")
                data = t.extractfile(m).read()
                if n == "meta":
                    for line in data.decode("utf-8").split(nl):
                        k, _, v = line.partition("=")
                        if k:
                            meta[k] = v
                else:
                    secret = data
            elif parts[0] == "volume":
                rel = "/".join(parts[1:])
                if m.isdir():
                    dirs[rel] = (m.mode & 0o7777, m.uid, m.gid)
                elif m.isreg():
                    if not rel:
                        raise ValueError("volume is a file")
                    files[rel] = (m.mode & 0o7777, m.uid, m.gid, t.extractfile(m).read())
                else:
                    raise ValueError(n + " is neither a regular file nor a directory")
            else:
                raise ValueError("the name " + n + " is outside meta, volume/ and secrets/")
    if meta.get("format") != "1" or "" not in dirs:
        raise ValueError("no meta of format 1, or no volume directory")
    for rel in list(files) + list(dirs):
        parent = os.path.dirname(rel)
        if rel and parent not in dirs:
            raise ValueError("the directory of " + rel + " is not in the archive")
    if not any(rel.startswith("cosmos/") for rel in files):
        raise ValueError("no keyring (cosmos/) in the volume")
    return meta, dirs, files, secret


def same_tree(d1, f1, d2, f2):
    return (set(d1) == set(d2) and set(f1) == set(f2)
            and all(f1[r][3] == f2[r][3] and f1[r][0] == f2[r][0] for r in f1))


def open_archive(path, confirm=False):
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        refused("the archive " + path + " cannot be read: " + str(e.strerror) + ".")
    pp = read_passphrase(confirm)
    rc, plain = ssl("-d", blob, pp)
    tb = unseal(plain) if rc == 0 else None
    why = "it does not open with this passphrase, or it was altered or damaged after it was written"
    if tb is not None:
        try:
            return blob, members(tb)
        except (tarfile.TarError, ValueError, UnicodeDecodeError, EOFError) as e:
            why = "it opens, and its content is refused: " + str(e)
    refused("the archive " + path + " was not accepted: " + why + ". Nothing was touched.")


def summary(meta, files, secret):
    size = sum(len(v[3]) for v in files.values())
    return (str(len(files)) + " files, " + str(size) + " bytes, from the volume " + meta.get("volume", "?")
            + ", made " + meta.get("created", "?") + "; "
            + ("with the keyring passphrase file" if secret is not None else "no keyring passphrase file (an unencrypted keyring)"))


def write_new(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def backup():
    mp, vol, pf, outdir = a["mp"], a["volume"], a["pf"], a["outdir"]
    if a["via"]:
        dirs, files, others = collect_via(vol, a["via"])
    else:
        try:
            dirs, files, others = collect(mp)
        except OSError as e:
            unmeasurable("the volume directory " + mp + " cannot be read (" + str(e.strerror) + "): run this as root (sudo).")
    if others:
        refused("the volume holds entries that are neither files nor directories (" + ", ".join(others[:5])
                + "): the archive copies files and directories only, and these would be missing. Nothing was written.")
    # The keyring is what holds the address: a volume without cosmos/ holds no miner key.
    if not any(rel.startswith("cosmos/") for rel in files):
        refused("the volume " + vol + " holds no keyring (cosmos/): there is no miner key to back up. Nothing was written.")
    secret = None
    if os.path.lexists(pf):
        try:
            with open(pf, "rb") as f:
                secret = f.read()
        except OSError as e:
            unmeasurable(pf + " cannot be read (" + str(e.strerror) + "): run this as root (sudo).")
    pp = read_passphrase(True)
    plain = seal(tar_of(vol, dirs, files, secret))
    rc, blob = ssl("-e", plain, pp)
    if rc != 0 or not blob:
        failed("openssl could not encrypt the archive. Nothing was written.")
    old = os.umask(0o077)
    try:
        os.makedirs(outdir, mode=0o700, exist_ok=True)
    except OSError as e:
        failed("the directory " + outdir + " cannot be created (" + str(e.strerror) + "). Nothing was written.")
    finally:
        os.umask(old)
    name = os.path.join(outdir, "dendra-keys-" + vol + "-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".dkb")
    tmp = os.path.join(outdir, ".dendra-keys.partial." + str(os.getpid()))
    try:
        write_new(tmp, blob, 0o600)
        try:
            os.link(tmp, name)
            linked = True
        except FileExistsError:
            raise
        except OSError as e:
            if e.errno not in no_link:
                raise
            linked = False
        if not linked:
            # A file system without hard links (vfat or exFAT, the usual USB drive): the archive is written
            # under its own name, still never over another file (O_EXCL); a partial one is removed.
            try:
                write_new(name, blob, 0o600)
            except FileExistsError:
                raise
            except OSError:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
    except FileExistsError:
        failed(name + " already exists: run this again in a second. Nothing was replaced.")
    except OSError as e:
        failed("the archive could not be written into " + outdir + " (" + str(e.strerror) + ").")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    # Read back from the disk, opened, and compared with the volume, before it is called written.
    with open(name, "rb") as f:
        back = f.read()
    rc, plain2 = ssl("-d", back, pp)
    tb = unseal(plain2) if rc == 0 else None
    ok = False
    if tb is not None:
        try:
            meta, d2, f2, s2 = members(tb)
            ok = same_tree(dirs, files, d2, f2) and s2 == secret
        except (tarfile.TarError, ValueError, UnicodeDecodeError, EOFError):
            ok = False
    if not ok:
        os.unlink(name)
        failed("the archive written to " + name + " does not read back as the volume: it was removed.")
    say("  [OK] archive written, read back and opened: " + name)
    say("       " + summary(meta, files, secret))
    say("  It is encrypted with your passphrase: without it nobody can open it, you included.")
    say("  Copy it off this machine now (another computer, a drive you unplug): a reinstall or a reflash of this")
    say("  system drive erases " + outdir + " together with the keys.")
    say("  To restore it on a new system, before the miner first starts there: bash " + a["self"] + " restore <archive>")


def write_pending(blob, path):
    d = os.path.dirname(a["pending"])
    old = os.umask(0o077)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = a["pending"] + ".tmp." + str(os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("archive=" + os.path.abspath(path) + nl + "sha256=" + hashlib.sha256(blob).hexdigest() + nl
                    + "volume=" + a["volume"] + nl)
        os.replace(tmp, a["pending"])
    finally:
        os.umask(old)


def restore():
    path, vol, pf = a["archive"], a["volume"], a["pf"]
    blob, (meta, dirs, files, secret) = open_archive(path)
    say("  [OK] archive verified: " + summary(meta, files, secret))
    src = meta.get("volume", "")
    say("  archive of the volume " + (src or "(not named)") + ", restored into the volume " + vol)
    if src != vol and a["accept"] != "1":
        refused("this archive was made from the volume " + (src or "(not named)") + ", and the target is " + vol
                + ": with one identity per card, that would move an identity into the slot of another card. Name the target"
                + " with --volume " + (src or "<name>") + ", or give --accept-other-volume to restore it into " + vol
                + " all the same. Nothing was touched.")
    if a["docker"] != "1":
        try:
            write_pending(blob, path)
        except OSError as e:
            failed("Docker is missing, and the pending restore could not be recorded in " + a["pending"] + " (" + str(e.strerror) + ").")
        refused("Docker is not on this machine, and the keys live in a Docker volume: nothing was written. The restore is"
                + " recorded as pending in " + a["pending"] + ", and the HiveOS package installs nothing while that file is there."
                + " Install Docker Engine, then run this restore again; it removes that file once the keys are back.")
    # Every check before the first write: the passphrase file, then the volume.
    keep_pf = False
    if secret is not None and os.path.lexists(pf):
        try:
            with open(pf, "rb") as f:
                cur = f.read()
        except OSError as e:
            unmeasurable(pf + " cannot be read (" + str(e.strerror) + "): run this as root (sudo).")
        if cur != secret:
            refused(pf + " holds another passphrase: the keyring it opens is not the one in this archive, and a restore never"
                    + " replaces it. Move it away first if it belongs to nothing you keep. Nothing was touched.")
        keep_pf = True
    mp = a["mp"]
    already = False
    if mp:
        try:
            cd, cf, co = collect(mp)
        except OSError as e:
            unmeasurable("the volume directory " + mp + " cannot be read (" + str(e.strerror) + "): run this as root (sudo).")
        if cf or co or len(cd) > 1:
            if not co and same_tree(cd, cf, dirs, files):
                already = True
            else:
                refused("the volume " + vol + " already holds files: a key, perhaps of another identity. A restore never writes over a"
                        + " key. Back that volume up (bash " + a["self"] + " backup) and remove it yourself if it is to go. Nothing was touched.")
    created_volume = False
    if not mp:
        cmd = ["docker", "volume", "create"]
        if vol.endswith("_miner-keys"):
            # The labels docker compose gives its own volumes, so that the kit adopts this one as its miner-keys.
            cmd += ["--label", "com.docker.compose.project=" + vol[:-len("_miner-keys")],
                    "--label", "com.docker.compose.volume=miner-keys"]
        p = subprocess.run(cmd + [vol], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            failed("docker volume create " + vol + " failed (" + (p.stderr.strip().splitlines() or ["no reason given"])[-1] + "). Nothing was written.")
        created_volume = True
        p = subprocess.run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", vol], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        mp = p.stdout.strip()
        if p.returncode != 0 or not mp or not os.path.isdir(mp):
            subprocess.run(["docker", "volume", "rm", vol], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            failed("the new volume " + vol + " has no directory on this machine: it was removed. Nothing was written.")
    created = []
    if not already:
        try:
            for rel in sorted(dirs):
                if rel:
                    p = os.path.join(mp, rel)
                    os.mkdir(p, 0o700)
                    created.append(p)
            for rel in sorted(files):
                p = os.path.join(mp, rel)
                write_new(p, files[rel][3], 0o600)
                created.append(p)
            root = os.geteuid() == 0
            for rel in sorted(files):
                p = os.path.join(mp, rel)
                os.chmod(p, files[rel][0])
                if root:
                    os.lchown(p, files[rel][1], files[rel][2])
            for rel in sorted(dirs, reverse=True):
                p = os.path.join(mp, rel) if rel else mp
                os.chmod(p, dirs[rel][0])
                if root:
                    os.lchown(p, dirs[rel][1], dirs[rel][2])
        except OSError as e:
            for p in reversed(created):
                try:
                    os.rmdir(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
                except OSError:
                    pass
            if created_volume:
                subprocess.run(["docker", "volume", "rm", vol], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            failed("writing the keys into " + mp + " failed (" + str(e.strerror) + "): what was written is removed"
                   + (", and so is the volume this restore created" if created_volume else "") + ".")
    if secret is not None and not keep_pf:
        d = os.path.dirname(pf)
        old = os.umask(0o077)
        tmp = os.path.join(d, ".keyring-passphrase.restore." + str(os.getpid()))
        try:
            if not os.path.isdir(d):
                os.makedirs(d, mode=0o700)
            write_new(tmp, secret, 0o600)
            os.link(tmp, pf)
        except FileExistsError:
            failed(pf + " appeared while this restore ran: the keys are in the volume; run the restore again to compare it.")
        except OSError as e:
            failed("the keys are in the volume, and the passphrase file " + pf + " could not be written (" + str(e.strerror)
                   + "): run the restore again, it finds the keys in place and writes that file.")
        finally:
            os.umask(old)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    cd, cf, co = collect(mp)
    pf_ok = True
    if secret is not None:
        with open(pf, "rb") as f:
            pf_ok = f.read() == secret
    if co or not same_tree(cd, cf, dirs, files) or not pf_ok:
        failed("read back: the volume " + vol + " or " + pf + " differs from the archive. Look before starting the miner.")
    try:
        os.unlink(a["pending"])
        say("  [OK] the pending restore (" + a["pending"] + ") is done and removed.")
    except FileNotFoundError:
        pass
    if already:
        say("  [OK] the volume " + vol + " already holds exactly these keys: nothing was written to it.")
    else:
        say("  [OK] keys restored into the volume " + vol + " and read back byte for byte (" + str(len(files)) + " files).")
    if secret is not None:
        say("  [OK] keyring passphrase: " + pf + (" (already in place, identical)" if keep_pf else " (written, 0600)"))
    say("  Same key, same address, same identity. Start the miner now (HiveOS: YES=1 in the flight sheet; elsewhere:")
    say("  deploy/join.sh). Before the miner starts, the keyring passphrase directory must be the one its .env names.")


if verb == "backup":
    backup()
elif verb == "verify":
    _, (meta, dirs, files, secret) = open_archive(a["archive"])
    say("  [OK] archive verified: " + summary(meta, files, secret))
else:
    restore()
'
python3 -I -c "$PY" "$VERB" archive "$ARCHIVE" outdir "$OUTDIR" volume "$VOLUME" mp "$MP" docker "$DOCKER" pf "$SD/keyring-passphrase" pending "$PENDING" fd "$PFD" min "$BK_MIN_PASSPHRASE" iter "$BK_KDF_ITER" self "$SELF" via "$VIA" accept "$ACCEPT_OTHER"
