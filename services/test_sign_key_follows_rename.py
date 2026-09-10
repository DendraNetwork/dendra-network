# -*- coding: utf-8 -*-
"""The signing identity must FOLLOW the keyring rename, or every deposit goes out unsigned.

`DENDRA_SIGN_KEY` defaults to `MINER_ID` (deploy/testnet-miner/docker-compose.yml). `relay_client`
reads it ONCE, at import. The daemon then calls `align_identity`, which RENAMES the keyring entry to
the derived `dm1...` identity. From that point the name held by the client designates nothing:
`address_from_key` fails, no signature is attached, and nothing says so while the relay runs in
`observe`. The day `DENDRA_RELAY_SIGN=enforce` is armed, every write from that miner is refused and
no log line on either side explains why.

This bench drives the SHIPPED entry point rather than restating the rule: re-exporting the variable
would not help, because the module has already read it.
"""
import os, sys, importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAIL = 0


def ok(m):
    print("  [ok] " + m)


def ko(m):
    global FAIL
    FAIL = 1
    print("  [KO] " + m)


def main():
    os.environ["DENDRA_SIGN_KEY"] = "miner-original-name"
    import relay_client
    importlib.reload(relay_client)

    if relay_client._SIGN_KEY == "miner-original-name":
        ok("at import the client signs for the name the compose gave it")
    else:
        ko("unexpected initial signing name: %r" % relay_client._SIGN_KEY)

    # Re-exporting does NOT reach the module: this is the whole reason the entry point exists.
    os.environ["DENDRA_SIGN_KEY"] = "dm1derived"
    if relay_client._SIGN_KEY == "miner-original-name":
        ok("re-exporting the variable changes nothing (the module read it at import)")
    else:
        ko("the environment reached an already-imported module, which it cannot")

    # A cache entry for the OLD name must not survive the switch: signing for a stale address is the
    # same failure one step later, and it is attributed to the wrong operator instead of refused.
    relay_client._SIGN_CACHE["adresse"] = "dendra1stale"
    relay_client.set_sign_key("dm1derived")
    if relay_client._SIGN_KEY == "dm1derived":
        ok("set_sign_key re-points the signing identity after the rename")
    else:
        ko("set_sign_key did not re-point: %r" % relay_client._SIGN_KEY)
    if relay_client._SIGN_CACHE == {}:
        ok("the cache of the old name is dropped")
    else:
        ko("stale cache survived: %r" % relay_client._SIGN_CACHE)

    # And the daemon actually calls it. Read from the shipped file rather than trusted: a fix nobody
    # invokes is an intention, and this project has already shipped one of those.
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "miner.py"),
               encoding="utf-8").read()
    if "relay.set_sign_key(a.id)" in src:
        ok("miner calls set_sign_key with the aligned identity")
    else:
        ko("miner never calls set_sign_key -- the entry point would be dead code")
    i_align = src.find("a.id = align_identity(a.id, addr)")
    i_set = src.find("relay.set_sign_key(a.id)")
    if 0 <= i_align < i_set:
        ok("it calls it AFTER the rename, which is the only moment that helps")
    else:
        ko("the call does not sit after align_identity (align=%d, set=%d)" % (i_align, i_set))


if __name__ == "__main__":
    main()
    print("SIGN KEY BENCH", "GREEN" if FAIL == 0 else "RED")
    sys.exit(FAIL)
