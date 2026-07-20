#!/usr/bin/env python3
"""Persistent LOCAL miner (the real Dendra miner client).

At startup: creates its encryption key (X25519, local) + publishes its pub to the relay; creates its
CHAIN key, funds itself at the FAUCET, and registers on-chain by SIGNING itself.
Then LOOPS: fetches from the relay the jobs assigned to it -> decrypts in locked memory
-> infers on OLLAMA (GPU) -> returns the SEALED response -> anchors its content_commit on-chain.
Content stays encrypted; only metadata + hash go on-chain. It earns `token` on each
honest verdict (payout from the client's escrow).

Usage: python3 miner.py --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea.crypto import Sealed
from modea.miner import Miner
import relay_client as relay

CHAIN = "dendra"
NODE = os.environ.get("DENDRA_NODE", "")  # e.g. "tcp://chain:26657" in a container; "" = local node
# Model registry: if set, the miner DECLARES this model_id in its commits (--model-id flag).
# REQUIRED when enforce_model_registry=ON; empty (default) = no flag -> historical behavior intact.
MODEL_ID = os.environ.get("DENDRA_MODEL_ID", "")
_RE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # sane relay key (anti dendrad injection). Dash allowed (IDs like "miner-A") but NEVER leading -> a key cannot be taken for a dendrad flag.


def _node():
    return ["--node", NODE] if NODE else []


def model_weights_hash():
    """Digest of the served model's weights (Ollama manifest) -> binds the commit to
    the artifact. Best-effort: "" if Ollama unreachable/model absent (the on-chain check only bites if
    enforce_model_registry=ON AND the registry has a non-empty weights anchor)."""
    if not MODEL_ID:
        return ""
    ep = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(ep + "/api/tags", timeout=10) as r:
            data = json.loads(r.read())
        for m in data.get("models", []):
            if m.get("name") == MODEL_ID or m.get("model") == MODEL_ID:
                dg = m.get("digest", "")
                return dg.split(":")[-1] if dg else ""
    except Exception:
        return ""
    return ""


def run(c, t=600):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t)
    return (r.stdout or "") + (r.stderr or "")


def tx_from(frm, *a):
    # robust NONCE: a miner-judge shares 1 account across 3 processes (commit/reveal/verdict)
    # -> 2 close tx pull the SAME sequence -> "account sequence mismatch". We RETRY: dendrad re-fetches the
    # sequence each attempt (online), so once the in-flight tx is included, the retry passes. Bounded backoff (~14 s max).
    cmd = ["dendrad", "tx", "jobs", *a, "--from", frm, "--keyring-backend", "test",
           "--chain-id", CHAIN, "--gas", "auto", "--gas-adjustment", "1.6", "--yes", *_node()]
    o = ""
    for attempt in range(6):
        o = run(cmd)
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    return o


def _tx_err(o):
    """Short error message from a tx output (to log a failing create-commit)."""
    m = re.search(r'raw_log:\s*"?([^\n"]+)', o) or re.search(r'(code:\s*\d+[^\n]*)', o)
    return (m.group(1) if m else o[-200:]).strip()[:200]


def query(*a):
    return run(["dendrad", "query", "jobs", *a, *_node()])


def _ok(t):
    m = re.search(r'(^|\n)code: (\d+)', t)
    return bool(m) and m.group(2) == "0"


def wait_tx(o, timeout=24):
    if not _ok(o):
        return False
    h = re.search(r'txhash:\s*([A-Fa-f0-9]{64})', o)
    if not h:
        return False
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return _ok(q)
        time.sleep(2)
    return False


def keys_addr(name):
    show = run(["dendrad", "keys", "show", name, "-a", "--keyring-backend", "test"])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    if m:
        return m.group(1)
    run(["dendrad", "keys", "add", name, "--keyring-backend", "test"])
    show = run(["dendrad", "keys", "show", name, "-a", "--keyring-backend", "test"])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    return m.group(1) if m else ""


def bal_token(addr):
    try:
        d = json.loads(run(["dendrad", "query", "bank", "balances", addr, "--output", "json", *_node()]))
        for c in d.get("balances", []):
            if c.get("denom") == "udndr":
                return int(c["amount"])
    except Exception:
        return 0
    return 0


def stake_of(mid):
    m = re.search(r'stake:\s*"?(\d+)"?', query("get-miner", mid))
    return int(m.group(1)) if m else -1


def faucet_fund(faucet, addr):
    try:
        req = urllib.request.Request(faucet, data=json.dumps({"address": addr}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception:
        return False


def pick_backend(want):
    """Chooses the backend. We NEVER serve a mock SILENTLY in prod.
    A miner without a GPU would collect rewards for bogus text, anchored on-chain as real
    (and N deterministic mocks agree -> majority -> evict/slash the real miners).
    Mock fallback/use is allowed ONLY if DENDRA_ALLOW_MOCK=1 (tests); otherwise HARD FAILURE."""
    allow_mock = os.environ.get("DENDRA_ALLOW_MOCK", "0") == "1"
    if want == "mock":
        if not allow_mock:
            print("[daemon] FATAL : backend 'mock' demande mais DENDRA_ALLOW_MOCK!=1 (anti-faux-mineur).")
            sys.exit(3)
        print("[daemon] backend MOCK explicite (tests) -> NON destine a la prod")
        return "mock"
    if want == "ollama":
        try:
            m = Miner("probe", backend="ollama")
            m.backend.generate("ok")
            print(f"[daemon] Ollama joignable ({m.backend.endpoint}) -> VRAI LLM (GPU)")
            return "ollama"
        except Exception as e:
            if allow_mock:
                print(f"[daemon] Ollama injoignable ({type(e).__name__}) -> repli MOCK (DENDRA_ALLOW_MOCK=1)")
                return "mock"
            print(f"[daemon] FATAL : Ollama injoignable ({type(e).__name__}) et mock interdit en prod. "
                  f"Demarre Ollama (ou DENDRA_ALLOW_MOCK=1 pour des tests).")
            sys.exit(3)
    print(f"[daemon] FATAL : backend inconnu '{want}'.")
    sys.exit(3)


def _vrf_bin():
    """Path of the dendra-vrf binary (built by dendra_modea_vrf_avail.sh). "" if absent."""
    for c in (os.path.expanduser("~/go/bin/dendra-vrf"), "/usr/local/bin/dendra-vrf"):
        if os.path.exists(c):
            return c
    return "dendra-vrf"  # otherwise, let PATH resolve it (subprocess will fail cleanly if absent)


def vrf_identity(keydir, mid):
    """Loads or creates the miner's Ed25519 VRF key via dendra-vrf. Returns (sk_hex, pk_hex);
    ("","") if the binary is absent -> the miner falls back to the legacy 'echo' availability (backward-compatible)."""
    vbin = _vrf_bin()
    vpath = Path(keydir) / f"{mid}.vrf"
    try:
        if vpath.exists():
            sk = vpath.read_text().strip()
            pk = subprocess.run([vbin, "pubkey", sk], capture_output=True, text=True, timeout=10).stdout.strip()
        else:
            out = subprocess.run([vbin, "keygen"], capture_output=True, text=True, timeout=10).stdout.strip()
            sk, pk = out.split()
            vpath.write_text(sk)
            try:
                os.chmod(vpath, 0o600)
            except Exception:
                pass
        if len(sk) == 128 and len(pk) == 64:   # 64o sk / 32o pk en hex
            return sk, pk
    except Exception as e:
        print(f"[daemon] VRF indisponible ({type(e).__name__}) -> dispo en echo legacy")
    return "", ""


def prove_availability_once(mid, vsk, last_chal=""):
    """Best-effort: if availability is ON (challenge present) and new, proves presence with a VRF PROOF.
    Never raises (must not disturb the inference loop). Returns the proven challenge (or last_chal)."""
    if not vsk:
        return last_chal
    try:
        out = query("get-avail-challenge", "--output", "json")
        m = re.search(r'"challenge"\s*:\s*"([0-9a-fA-F]*)"', out)
        chal = m.group(1) if m else ""
        if not chal or chal == last_chal:
            return last_chal   # availability OFF, or challenge already proven
        pi = subprocess.run([_vrf_bin(), "prove", vsk, chal], capture_output=True, text=True, timeout=10).stdout.strip()
        if pi:
            tx_from(mid, "prove-availability", mid, chal, pi)
            print(f"[daemon] {mid} disponibilité prouvée (VRF) pour le défi {chal[:12]}…")
            return chal
    except Exception as e:
        print(f"[daemon] {mid} preuve de dispo ignorée ({type(e).__name__})")
    return last_chal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--faucet", default="http://127.0.0.1:4500")
    ap.add_argument("--backend", default="ollama", choices=["ollama", "mock"])
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    # PROCESS confinement at startup (anti same-user debugger, no core
    # dump, no-new-privs; mlockall opt-in). Best-effort, NEVER fatal. OFF via DENDRA_CONFINE=0.
    # STRONG OS confinement (routeless netns, seccomp, egress) is applied via modea_confine.sh.
    if os.environ.get("DENDRA_CONFINE", "1") != "0":
        try:
            from modea import confine
            cr = confine.apply_process_confinement_report_cached()
            print(f"[daemon] confinement dumpable_off={cr['non_dumpable']} no_new_privs={cr['no_new_privs']} "
                  f"core_off={cr['core_dumps_disabled']} mlockall={cr['mlockall']} (residuel root: oui)")
        except Exception as e:
            print(f"[daemon] confinement indisponible ({type(e).__name__}) -> on continue (best-effort)")

    Path(a.keydir).mkdir(parents=True, exist_ok=True)
    skpath = Path(a.keydir) / f"{a.id}.sk"

    # --- encryption identity (X25519): persisted ENCRYPTED at rest ---
    passphrase = os.environ.get("DENDRA_MINER_PASSPHRASE", "")
    if not passphrase:
        print("[daemon] ATTENTION : DENDRA_MINER_PASSPHRASE non defini -> cle stockee EN CLAIR (perms 0600). "
              "Definis-le pour chiffrer la cle au repos (compromission disque = vol des prompts + fonds).")
    if skpath.exists():
        sk = crypto.load_sk(str(skpath), passphrase)
    else:
        sk, _ = crypto.gen_keypair()
        crypto.save_sk(sk, str(skpath), passphrase)
    mypub = crypto.pub_bytes(sk).hex()
    relay.put(a.relay, "pub", a.id, {"pub": mypub})

    # SIGNED software attestation (measured hash = code + model_id + weights_hash + confinement),
    # published to the relay. A relay with the gate active (DENDRA_ATTEST_REQUIRE=1 + allow-list) only assigns
    # a confidential job to attested miners. Best-effort (never fatal). HONEST: deterrence, not
    # a proof of execution (cf. modea/confine.py). The measured hash is printed -> to add to the allow-list.
    try:
        from modea import confine as _confine
        _wh = model_weights_hash()
        _ask, _apub = _confine.load_or_create_attest_key(a.keydir, a.id)
        _att = _confine.signed_attestation(_ask, miner_id=a.id, model_id=MODEL_ID,
                                           weights_hash=_wh, enc_sk=sk)
        relay.put(a.relay, "attest", a.id, _att)
        print(f"[daemon] attestation signee publiee  measured_hash={_att['measured_hash'][:16]}…  "
              f"attest_pub={_apub[:16]}… (allow-list relais = DENDRA_ATTEST_ALLOW)")
    except Exception as e:
        print(f"[daemon] attestation indisponible ({type(e).__name__}) -> on continue (best-effort)")

    # --- chain identity: key + faucet + self-signed registration ---
    addr = keys_addr(a.id)
    vsk, vpk = vrf_identity(a.keydir, a.id)   # VRF identity (proof of availability)
    if stake_of(a.id) <= 0:
        faucet_fund(a.faucet, addr)
        for _ in range(20):
            if bal_token(addr) > 0:
                break
            time.sleep(2)
        for _ in range(3):
            if stake_of(a.id) > 0:
                break
            # we ANCHOR the X25519 pub on-chain (5th arg), signed by the miner's
            # Cosmos key -> the client will encrypt to THIS pub (anti relay-MITM).
            reg = ["create-miner", a.id, addr, "eu", os.environ.get("DENDRA_MINER_STAKE", "2000"), mypub]
            if vpk:
                reg += ["--vrf-pubkey", vpk]   # vrf_pubkey as a FLAG (not a 2nd optional positional)
            wait_tx(tx_from(a.id, *reg))
            time.sleep(2)
    backend = pick_backend(a.backend)
    miner = Miner(a.id, backend=backend, hardened=True, sk=sk)
    whash = model_weights_hash()
    if MODEL_ID:
        print(f"[daemon] model_id={MODEL_ID}  weights_hash={(whash[:16]+'...') if whash else '<absent>'} (NEW-MR-03)")
    print(f"[daemon] mineur {a.id} pret  addr={addr}  stake={stake_of(a.id)}  backend={backend}")
    print(f"[daemon] en attente de jobs au relais {a.relay} ...")

    done = set()
    suffix = "__" + a.id
    _avail_tick = 0
    _last_chal = ""
    while True:
        try:
            lst = relay.listing(a.relay)
            ress = set(lst.get("res", []))
            for key in lst.get("req", []):
                if not key.endswith(suffix) or key in ress or key in done:
                    continue
                if not _RE_KEY.match(key):       # unsafe relay key -> ignore (anti dendrad injection)
                    continue
                jid = key[: -len(suffix)]
                req = relay.get(a.relay, "req", key)
                if not req:
                    continue
                eph = bytes.fromhex(req["client_eph_pk"])
                sealed = Sealed(bytes.fromhex(req["nonce"]), bytes.fromhex(req["ct"]))
                try:
                    res = miner.handle_job(jid, eph, sealed, max_out=int(req.get("max_out", 0)))  # requested cap
                except Exception as e:
                    print(f"[daemon] {a.id} echec inference job {jid}: {type(e).__name__}")
                    continue
                relay.put(a.relay, "res", key,
                          {"nonce": res.sealed_result.nonce.hex(), "ct": res.sealed_result.ct.hex(),
                           "in_tok": res.in_tok, "out_tok": res.out_tok})   # real tokens (per-token pricing)
                commit = res.content_embed   # embedding (semantic mode: robust to free-form LLM)
                flags = (["--model-id", MODEL_ID] if MODEL_ID else []) + (["--weights-hash", whash] if whash else [])
                anchored = bool(commit) and (commit in query("get-commit", key))
                for _ in range(3):
                    if anchored:
                        break
                    out = tx_from(a.id, "create-commit", key, commit, commit, "infer", *flags)
                    if not wait_tx(out):
                        _nc = (commit.count(",") + 1) if commit else 0
                        print(f"[daemon] {a.id} create-commit job {jid} ECHEC ({_nc}c) : {_tx_err(out)}")
                        print(f"[daemon]   commit[:90]={commit[:90]!r}")
                        print(f"[daemon]   out[:450]={out[:450]!r}")
                    time.sleep(2)
                    anchored = commit in query("get-commit", key)
                if anchored:
                    done.add(key)
                    print(f"[daemon] {a.id} a traite le job {jid} -> reponse scellee + preuve ANCREE "
                          f"(solde: {bal_token(addr)} token)")
                else:
                    print(f"[daemon] {a.id} job {jid} : reponse postee mais COMMIT NON ancre -> reessai au prochain tour")
        except Exception as e:
            print(f"[daemon] {a.id} boucle: {type(e).__name__}: {e}")
        # prove availability periodically (best-effort; no-op if availability OFF).
        _avail_tick += 1
        if vsk and _avail_tick % 4 == 0:
            _last_chal = prove_availability_once(a.id, vsk, _last_chal)
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
