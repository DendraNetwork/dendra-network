# -*- coding: utf-8 -*-
"""serve_guard.py: no mining model on the CPU, decided from where the engine holds the served model.

Three answers (0 on a GPU or the judge role on the CPU, 2 refused, 3 not measured), the judge role recognised
only when the served model is the judge model, a tag without a version read as `:latest`, and a `size_vram`
the entry omits read as the zero of its type (the CPU). The placement is injected: these cases pin the
decision; the CPU-rule shell bench of the development repository drives the real HTTP reading and the entrypoint's line.
The subject can be pointed at a mutated copy with DENDRA_SERVE_GUARD_FILE.
"""
import importlib.util
import os
from pathlib import Path

MODEA = Path(__file__).resolve().parents[1]
SUBJECT = os.environ.get("DENDRA_SERVE_GUARD_FILE", str(MODEA / "serve_guard.py"))
_spec = importlib.util.spec_from_file_location("serve_guard_under_test", SUBJECT)
sg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sg)

MOE = "qwen3:30b-a3b-instruct-2507-q4_K_M"
MINE = "llama3.1:8b-instruct-q4_K_M"


def measured(size, vram):
    def m(endpoint, model, wait_s):
        return size, vram
    return m


def unread(why="the engine could not be read (URLError)"):
    def m(endpoint, model, wait_s):
        return None, why
    return m


def test_gpu_held_model_passes():
    assert sg.decide({"OLLAMA_MODEL": MINE}, measured(5000, 5000)) == 0


def test_partly_held_model_passes_and_says_so(capsys):
    assert sg.decide({"OLLAMA_MODEL": MINE}, measured(5000, 2000)) == 0
    assert "Part of the model runs on the CPU" in capsys.readouterr().out


def test_mining_model_on_the_cpu_is_refused(capsys):
    assert sg.decide({"OLLAMA_MODEL": MINE}, measured(5000, 0)) == 2
    assert "SERVE REFUSED" in capsys.readouterr().out


def loads_nothing(endpoint, model, wait_s):
    raise AssertionError("the judge role on the CPU must not load the judge model to be decided")


def test_judge_role_on_the_cpu_passes_without_loading_the_model(capsys):
    env = {"OLLAMA_MODEL": MOE, "DENDRA_MINER_JUDGE": "1", "DENDRA_JUDGE_MODEL_ID": MOE}
    assert sg.decide(env, loads_nothing) == 0
    assert "Not loaded here" in capsys.readouterr().out


def test_judge_flag_with_another_served_model_is_refused():
    env = {"OLLAMA_MODEL": "llama3.2:1b", "DENDRA_MINER_JUDGE": "1", "DENDRA_JUDGE_MODEL_ID": MOE}
    assert sg.decide(env, measured(1300, 0)) == 2


def test_judge_model_without_the_flag_is_refused_on_the_cpu():
    env = {"OLLAMA_MODEL": MOE, "DENDRA_MINER_JUDGE": "0", "DENDRA_JUDGE_MODEL_ID": MOE}
    assert sg.decide(env, measured(19000, 0)) == 2


def test_unread_placement_is_not_a_pass(capsys):
    assert sg.decide({"OLLAMA_MODEL": MINE}, unread()) == 3
    assert "Not measured" in capsys.readouterr().out


def test_empty_served_model_is_not_measured():
    assert sg.decide({"OLLAMA_MODEL": ""}, measured(5000, 5000)) == 3


def test_other_backend_is_not_this_guards():
    assert sg.decide({"BACKEND": "mock", "OLLAMA_MODEL": MINE}, unread()) == 0


def test_a_bad_wait_bound_falls_back_to_the_default():
    seen = {}

    def m(endpoint, model, wait_s):
        seen["wait"] = wait_s
        return 5000, 5000
    assert sg.decide({"OLLAMA_MODEL": MINE, "DENDRA_SERVE_GUARD_WAIT_S": ""}, m) == 0
    assert seen["wait"] == 900.0


def test_the_shipped_guard_decides_the_cpu_judge_without_asking_the_engine_anything():
    """The delivered `decide` with its own reader against an engine on a socket: a machine without a card, whose
    miner serves the judge model, starts without the guard loading 19 GB (no POST /api/generate, no request at all)."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def _send(self, doc):
            out = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            seen.append(("GET", self.path))
            self._send({"models": [{"name": MOE, "model": MOE, "size": 19 * 1024 ** 3}]})

        def do_POST(self):
            seen.append(("POST", self.path))
            self._send({"response": "", "done": True})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {"OLLAMA_MODEL": MOE, "DENDRA_MINER_JUDGE": "1", "DENDRA_JUDGE_MODEL_ID": MOE,
               "OLLAMA_ENDPOINT": f"http://127.0.0.1:{srv.server_address[1]}", "DENDRA_SERVE_GUARD_WAIT_S": "2"}
        assert sg.decide(env) == 0
    finally:
        srv.shutdown()
        srv.server_close()
    assert seen == [], seen


def test_names_are_read_as_ollama_reports_them():
    assert sg.norm("mistral-nemo") == "mistral-nemo:latest"
    assert sg.norm(MOE) == MOE
    assert sg.norm("registry.example:5000/ns/model") == "registry.example:5000/ns/model:latest"
    assert sg.norm("") == ""
