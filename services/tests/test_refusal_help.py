"""A refusal that a person reads points to the section of the public site that settles it.

The faucet and the relay put the address in a field of their own, `help`, and leave every machine-read text as it
was: the miner daemon compares the `info` of a 429 with the faucet's REFUSAL_* constants, and an older miner
compares its own copy, so a link added to that text would turn a quota into `unknown`. These cases run the shipped
handlers and read what they send:

  * each of the five faucet refusals carries `help`, before `info`, and `info` is the unchanged text;
  * the miner daemon still classes the refusals it classed before;
  * every reason the relay can return is listed with a link (the list is read from the modules, not retyped), and
    a reason that is not listed gets none: no address is invented;
  * every address a refusal can carry leads, through the redirects of sites/main/_redirects, to a page of sites/
    that holds its id.

The relay's own 401 body, over a real socket, is checked in test_relay_access.py (Part 3, the kit floor).
"""
import io
import json
import os
import re
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest

MODEA = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(MODEA))

ADDR = "dendra1" + "q" * 38
KIT_MIN = 5


class _Req(object):
    def __init__(self, FD, body):
        b = json.dumps(body).encode()
        self.headers = {"Content-Length": str(len(b))}
        self.rfile = io.BytesIO(b)
        self.client_address = ("203.0.113.4", 0)
        self.out = []
        self.do_POST = types.MethodType(FD.H.do_POST, self)

    def _send(self, code, obj):
        self.out.append((code, obj))


@pytest.fixture()
def faucet(monkeypatch):
    import faucet as FD
    monkeypatch.setattr(FD, "POW_BITS", 0)
    monkeypatch.setattr(FD, "STATE_FILE", "")
    monkeypatch.setattr(FD, "drip_amount", lambda read=True, now=None: (1_000_000, "bench"))
    monkeypatch.setattr(FD, "fund", lambda addr, amount=None: (True, "sent"))
    monkeypatch.setattr(FD, "_rate_refund", lambda addr: None)
    monkeypatch.setattr(FD, "_save_state", lambda: None)
    monkeypatch.setattr(FD, "KIT_FLOOR", KIT_MIN)
    monkeypatch.setattr(FD, "KIT_DEADLINE_TS", 1.0)       # long past: the floor is armed
    return FD


def _post(FD, body):
    h = _Req(FD, dict(body, address=ADDR))
    h.do_POST()
    return h.out[0]


# The texts an older miner compares its own copy with. They are written out here on purpose: the constants are
# shared by the faucet and the miner daemon in one tree, so only a literal can see a reworded text that a miner of
# an earlier kit would read as `unknown`.
WIRE_TEXTS = ("address funded recently (cooldown)", "too many requests from this IP (daily quota)",
              "global daily cap reached (anti-drain)")


def _caps(FD):
    caps = [FD.REFUSAL_ADDR_COOLDOWN, FD.REFUSAL_IP_QUOTA, FD.REFUSAL_GLOBAL_CAP]
    assert sorted(caps) == sorted(WIRE_TEXTS), "a refusal text changed: miners of an earlier kit read it as unknown"
    return caps


def test_each_cap_refusal_carries_help_before_an_unchanged_info(faucet, monkeypatch):
    FD = faucet
    for text in _caps(FD):
        monkeypatch.setattr(FD, "_rate_ok", lambda addr, ip, t=text: (False, t))
        code, reply = _post(FD, {})
        assert code == 429 and reply["error"] == "rate limited" and reply["info"] == text, (text, code, reply)
        assert reply[FD.HELP_FIELD] == FD.REFUSAL_HELP[text] == FD.HELP_FAUCET_LIMITS, (text, reply)
        keys = list(reply)
        assert keys.index(FD.HELP_FIELD) < keys.index("info"), keys


def test_the_kit_refusals_carry_help_and_keep_their_fields(faucet, monkeypatch):
    FD = faucet
    monkeypatch.setattr(FD, "_rate_ok", lambda addr, ip: (True, ""))
    monkeypatch.setattr(FD, "_pow_ok", lambda addr, pow_str: True)
    code, reply = _post(FD, {"kit_version": KIT_MIN - 1})
    assert code == 403 and reply["error"] == "stale kit", (code, reply)
    assert reply["info"] == FD.REFUSAL_STALE_KIT % KIT_MIN and reply[FD.KIT_MIN_FIELD] == KIT_MIN, reply
    assert reply["declared"] == KIT_MIN - 1 and reply["address"] == ADDR, reply
    assert reply[FD.HELP_FIELD] == FD.HELP_UPDATE_KIT == FD.REFUSAL_HELP[FD.REFUSAL_STALE_KIT], reply
    # The miner daemon logs the first 300 bytes of a refusal: the link has to be inside them.
    raw = json.dumps(reply)
    assert FD.HELP_UPDATE_KIT in raw[:300], raw[:300]
    code, reply = _post(FD, {"kit_version": "3"})
    assert code == 400 and reply["error"] == "invalid kit_version" and reply["info"] == FD.REFUSAL_BAD_KIT_VERSION, reply
    assert reply[FD.HELP_FIELD] == FD.HELP_UPDATE_KIT, reply


def test_the_stale_kit_refusal_names_the_installer_and_no_update_line_that_moves_to_main(faucet):
    """A kit below the floor prints an update line that ends on `main`. That tree carries no signature and, during a
    governed upgrade, join.sh refuses it on the network rules. The refusal sends the kit to the installer instead, and
    repeats neither the line nor the old name of the rule that refuses it."""
    text = faucet.REFUSAL_STALE_KIT
    assert "install.sh" in text and "--yes" in text, text
    assert text.count("%") == 1 and "%d" in text, text
    for stale in ("checkout -B main", "reset --hard", "kit-before-update", "consensus epoch"):
        assert stale not in text, (stale, text)


def test_the_miner_still_classes_a_refusal_by_the_unchanged_info(faucet, monkeypatch):
    import miner
    FD = faucet
    for word, text in (("addr_cooldown", FD.REFUSAL_ADDR_COOLDOWN), ("ip_quota", FD.REFUSAL_IP_QUOTA),
                       ("global_cap", FD.REFUSAL_GLOBAL_CAP)):
        monkeypatch.setattr(FD, "_rate_ok", lambda addr, ip, t=text: (False, t))
        code, reply = _post(FD, {})
        assert miner.faucet_reason(code, json.dumps(reply)) == word, (word, reply)
    monkeypatch.setattr(FD, "_rate_ok", lambda addr, ip: (True, ""))
    monkeypatch.setattr(FD, "_pow_ok", lambda addr, pow_str: True)
    code, reply = _post(FD, {"kit_version": KIT_MIN - 1})
    assert miner.faucet_reason(code, json.dumps(reply)) == "stale_kit", reply


def test_every_refusal_text_of_the_faucet_is_listed(faucet):
    FD = faucet
    texts = sorted(v for k, v in vars(FD).items() if k.startswith("REFUSAL_") and isinstance(v, str))
    assert texts, "no REFUSAL_* constant found: the check read nothing"
    missing = [t for t in texts if t not in FD.REFUSAL_HELP]
    assert not missing, "a refusal without a link: %r" % (missing,)
    assert set(FD.REFUSAL_HELP) == set(texts), "REFUSAL_HELP lists a text that is no refusal"


# -- the relay ---------------------------------------------------------------------------------------------
@pytest.fixture()
def relay_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_RELAY_STORE", str(tmp_path / "relay-store"))
    monkeypatch.setenv("DENDRA_RELAY_SIGN", "off")
    for name in ("DENDRA_RELAY_TOKEN", "DENDRA_RELAY_REGISTRY", "DENDRA_KIT_MIN", "DENDRA_KIT_DEADLINE"):
        monkeypatch.delenv(name, raising=False)
    import relay
    from modea import relay_antireplay, relay_write
    return types.SimpleNamespace(server=relay, write=relay_write, replay=relay_antireplay)


def _reason_words(*modules):
    """The reason words a module defines: module-level strings whose value is their own name. The modes of the
    replay guard (NOMINAL, DEGRADED) are not refusals."""
    modes = {"NOMINAL", "DEGRADED"}
    words = set()
    for m in modules:
        for name, value in vars(m).items():
            if name.isupper() and isinstance(value, str) and value == name and name not in modes:
                words.add(name)
    return words


def test_every_reason_the_relay_can_return_is_listed(relay_modules):
    m = relay_modules
    words = _reason_words(m.write, m.replay, m.server)
    assert words, "no reason word found: the check read nothing"
    assert {"STALE_KIT", "REPLAY", "KEY_MISMATCH", "NOT_ATTESTED"} <= words, sorted(words)
    missing = sorted(w for w in words if m.server.help_for_why(w) is None)
    assert not missing, "a relay reason without a link: %r" % (missing,)
    # The stale kit goes to the update page, the rest to the relay refusals section.
    assert m.server.help_for_why("STALE_KIT") == m.write.HELP_UPDATE_KIT
    for w in sorted(words - {"STALE_KIT"}):
        assert m.server.help_for_why(w) == m.write.HELP_RELAY_REFUSALS, w


def test_the_words_of_an_unverified_write_are_listed_and_an_unlisted_reason_gets_no_link(relay_modules):
    s = relay_modules.server
    for why in ("off", "unsigned (missing: X-Dendra-Pubkey)", "malformed (ValueError)",
                "REPLAY (digest already accepted)", "STALE_KIT (kit_version 2 is below the floor 3)"):
        assert s.help_for_why(why), why
    for why in ("A_REASON_NOBODY_LISTED (x)", "", "signed"):
        assert s.help_for_why(why) is None, why
    body = json.loads(s.refusal_json("rate limited", help=s.HELP_RELAY_REFUSALS))
    assert body == {"error": "rate limited", "help": s.HELP_RELAY_REFUSALS}, body
    assert json.loads(s.refusal_json("x", help=None)) == {"error": "x"}


# -- the pages the links name ------------------------------------------------------------------------------
# The addresses are those of the single site, which installed kits carry. The main site answers them with the
# redirects of sites/main/_redirects, read here the way the host applies them, so the page judged is the one a
# person lands on: sites/<site>/<path>/index.html.
SITE_HOSTS = {"dendranetwork.com": "main", "www.dendranetwork.com": "main", "app.dendranetwork.com": "app",
              "miner.dendranetwork.com": "miner", "docs.dendranetwork.com": "docs",
              "explorer.dendranetwork.com": "explorer"}


def _sites_root():
    """The tree that carries sites/main/_redirects. DENDRA_REPO names it when this directory runs away from it (the
    battery runs pytest from a copy of prototype/). A DENDRA_REPO without it fails: a measurement was asked for and
    cannot be made."""
    named = os.environ.get("DENDRA_REPO") or ""
    if named:
        assert (Path(named) / "sites" / "main" / "_redirects").is_file(), "DENDRA_REPO=%s has no sites/" % named
        return Path(named)
    for d in [MODEA, *MODEA.parents]:
        if (d / "sites" / "main" / "_redirects").is_file():
            return d
    return None


def _landing_page(root, url):
    """(page file, fragment) that an address of the main site leads to: the first redirect that matches, in the
    order of the file, or the main site itself when none does."""
    parts = urlsplit(url)
    target = None
    for line in (root / "sites" / "main" / "_redirects").read_text(encoding="utf-8").splitlines():
        f = line.split()
        if len(f) != 3 or line.startswith("#"):
            continue
        src, dst = f[0], f[1]
        if src.endswith("/*") and parts.path.startswith(src[:-1]):
            target = dst.replace(":splat", parts.path[len(src) - 1:])
            break
        if parts.path == src:
            target = dst
            break
    dest = urlsplit(target) if target else parts
    site = SITE_HOSTS.get(dest.netloc)
    assert site, "%s leads to %s, which is not one of the five sites" % (url, dest.netloc)
    return root / "sites" / site / dest.path.strip("/") / "index.html", parts.fragment


def test_every_address_a_refusal_can_carry_names_a_page_and_an_id_that_exist(faucet, relay_modules):
    root = _sites_root()
    if root is None:
        pytest.skip("sites/main/_redirects is not found above %s: the anchors are not checked (declared, not passed); "
                    "DENDRA_REPO names the tree that carries them" % MODEA)
    FD, m = faucet, relay_modules
    urls = set(FD.REFUSAL_HELP.values()) | set(m.write.REFUSAL_HELP.values()) | set(m.server.REFUSAL_HELP.values())
    urls |= {m.server.HELP_RELAY_REFUSALS, FD.HELP_FAUCET_LIMITS, FD.HELP_UPDATE_KIT}
    assert urls, "no address found: the check read nothing"
    site = urlsplit(FD.SITE_URL)
    for url in sorted(urls):
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc) == (site.scheme, site.netloc), url
        assert parts.path.startswith("/") and parts.path.endswith("/") and parts.fragment, url
        page, fragment = _landing_page(root, url)
        assert page.is_file(), "%s: no page %s" % (url, page)
        html = page.read_bytes().decode("utf-8")
        found = re.findall(r"""\bid=["']%s["']""" % re.escape(fragment), html)
        assert len(found) == 1, "%s: id=%r appears %d time(s) in %s" % (url, fragment, len(found), page)


def test_every_word_the_relay_links_is_named_in_the_section_the_link_opens(relay_modules):
    """A link has to lead to a text that settles the refusal it came with. The words of the relay's table are read
    from the modules, and each one has to appear as a <code> item in the section its link names."""
    root = _sites_root()
    if root is None:
        pytest.skip("sites/main/_redirects is not found above %s: the section is not read (declared, not passed)" % MODEA)
    m = relay_modules
    page, fragment = _landing_page(root, m.server.HELP_RELAY_REFUSALS)
    html = page.read_bytes().decode("utf-8")
    start = html.index('id="%s"' % fragment)
    end = html.index("</section>", start)
    section = html[start:end]
    words = sorted(m.server.REFUSAL_HELP)
    assert words, "no word found: the check read nothing"
    missing = [w for w in words if "<code>%s</code>" % w not in section]
    assert not missing, "a relay reason that the section does not explain: %r" % (missing,)
