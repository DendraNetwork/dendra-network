"""Tests for the 3-party MPC core (Mode B) and its network endpoint."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modea import mpc
from modea.miner import Miner
from modea.net_client import NetClient  # noqa: F401 (ensures the package imports)
from modea.server import make_server


def test_share_reconstruct_roundtrip():
    v = mpc.encode_vec([1.0, -2.5, 3.25, 0.0])
    sh = mpc.share3(v)
    assert len(sh) == 3
    assert mpc.reconstruct(sh) == v


def test_single_share_hides_input():
    v = mpc.encode_vec([1.0, 2.0, 3.0])
    sh = mpc.share3(v)
    assert sh[0] != v                         # a single share is not the input
    assert mpc.reconstruct(sh[:2]) != v       # two shares are not enough


def test_linear_mpc_matches_plain():
    W = [[0.5, -0.2, 0.1], [0.0, 0.4, -0.3]]
    x = [1.0, 2.0, -1.0]
    W_int = mpc.encode_mat(W)
    shares = mpc.share3(mpc.encode_vec(x))
    res_shares = [mpc.linear_local(W_int, s) for s in shares]
    y = mpc.decode_linear(mpc.reconstruct(res_shares))
    y_ref = mpc.plain_linear(W, x)
    assert max(abs(a - b) for a, b in zip(y, y_ref)) < 1e-2


def test_partial_shares_do_not_leak():
    # 3-party additive sharing: even two parties together learn nothing about the input
    v = mpc.encode_vec([7.0])
    sh = mpc.share3(v)
    # the sum of two shares is uncorrelated with the value (the third one is random)
    assert mpc.reconstruct([sh[0], sh[1]]) != v


def test_mpc_endpoint_over_http():
    miner = Miner("p1", backend="mock")
    srv = make_server(miner, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import json, urllib.request
        W_int = mpc.encode_mat([[1.0, 0.0], [0.0, 1.0]])
        share = mpc.encode_vec([2.0, 3.0])
        body = json.dumps({"weight": W_int, "share": share}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/mpc_linear",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
        assert out["result"] == mpc.linear_local(W_int, share)
    finally:
        srv.shutdown()
