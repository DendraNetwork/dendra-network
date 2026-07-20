"""Tests du transport reseau Mode A (serveur HTTP + client)."""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modea.miner import Miner
from modea.net_client import NetClient
from modea.server import make_server

SECRET = "secret reseau: dossier 4471-A IBAN confidentiel"


def _start_server():
    miner = Miner("miner-test", backend="mock")
    srv = make_server(miner, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port, miner


def test_network_roundtrip():
    srv, port, miner = _start_server()
    try:
        client = NetClient(f"http://127.0.0.1:{port}")
        mid, pub = client.fetch_pubkey()
        assert mid == "miner-test" and len(pub) == 32
        res = client.submit("j1", SECRET)
        assert isinstance(res.output, str) and res.output
    finally:
        srv.shutdown()


def test_no_plaintext_on_wire():
    srv, port, _ = _start_server()
    try:
        client = NetClient(f"http://127.0.0.1:{port}")
        res = client.submit("j1", SECRET)
        # les octets reellement transmis ne contiennent pas le clair
        assert b"4471-A" not in res.request_bytes
        assert b"IBAN" not in res.request_bytes
        # uniquement des champs opaques
        sent = json.loads(res.request_bytes.decode())
        assert set(sent.keys()) == {"job_id", "client_eph_pk", "nonce", "ct"}
    finally:
        srv.shutdown()


def test_ephemeral_unique_on_wire():
    srv, port, _ = _start_server()
    try:
        client = NetClient(f"http://127.0.0.1:{port}")
        r1 = client.submit("a", "x")
        r2 = client.submit("b", "x")
        e1 = json.loads(r1.request_bytes.decode())["client_eph_pk"]
        e2 = json.loads(r2.request_bytes.decode())["client_eph_pk"]
        assert e1 != e2
    finally:
        srv.shutdown()
