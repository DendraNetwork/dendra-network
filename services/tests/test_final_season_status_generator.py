"""The season status publishes the generator's address, and The Proof reads it from there.

`the_proof.py::origin_partition` splits the chain's jobs by who asked for them; the generator's share
needs the address whose jobs are the programme's work, which only the season service knows
(DENDRA_FINAL_SEASON_GENERATOR). The status carries it under `generator`, null while it is not set and
never "", because an empty string would match a job row whose client is empty.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)


def _service():
    import final_season_server as S
    return S


def _status_with(tmp_path, monkeypatch, generator):
    S = _service()
    st = S.State(str(tmp_path), 1)
    monkeypatch.setattr(S, "ST", st)
    monkeypatch.setattr(S, "GENERATOR", generator)
    st.height, st.latest_time = 1 + 17_280 + 5, S.END - 10 * 86400
    return S.Handler._status(None)


def test_the_status_names_the_generator_and_null_when_unset(tmp_path, monkeypatch):
    got = _status_with(tmp_path, monkeypatch, "dendra1generatoraddress")
    assert got["generator"] == "dendra1generatoraddress"
    unset = _status_with(tmp_path / "unset", monkeypatch, "")
    assert "generator" in unset and unset["generator"] is None
    # What leaves on the wire is JSON null, not an empty string a client could match.
    assert json.loads(json.dumps(unset))["generator"] is None


def _serve(payload):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            return
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/final-season/v1/status" % srv.server_address[1]


def test_the_proof_reads_the_address_from_the_status_and_null_as_unread(tmp_path, monkeypatch):
    import the_proof
    for generator, want in (("dendra1generatoraddress", "dendra1generatoraddress"), ("", None)):
        srv, url = _serve(_status_with(tmp_path / (generator or "none"), monkeypatch, generator))
        monkeypatch.setattr(the_proof, "SEASON_STATUS", url)
        try:
            assert the_proof._read_season_generator() == want
        finally:
            srv.shutdown()
    # The key is the one the partition's reader names: a status without it reads as unread, never as "".
    srv, url = _serve({"season": "x"})
    monkeypatch.setattr(the_proof, "SEASON_STATUS", url)
    try:
        assert the_proof._read_season_generator() is None
    finally:
        srv.shutdown()
