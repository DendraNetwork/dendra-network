import concurrent.futures
import http.client
import json
import threading

from test_dendra_app import app, call, session_cookie, session_headers


def request(A, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', A.APP.port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        conn.close()


def test_cross_site_and_rebinding_cannot_read_or_act(app, monkeypatch):
    A, _ = app
    calls = []
    monkeypatch.setattr(A, 'compose', lambda *a, **kw: calls.append(a) or (0, ''))
    host = f'127.0.0.1:{A.APP.port}'
    status, headers, _ = request(A, 'OPTIONS', '/api/start', headers={'Host': host, 'Origin': 'https://attacker.invalid', 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'x-dendra-app'})
    assert status >= 400 and 'Access-Control-Allow-Origin' not in headers
    assert request(A, 'GET', '/api/overview', headers={'Host': host, 'Origin': 'https://attacker.invalid'})[0] == 401
    assert request(A, 'POST', '/api/start', body='{}', headers={'Host': host, 'Origin': 'https://attacker.invalid', 'Content-Type': 'text/plain'})[0] == 401
    assert request(A, 'GET', '/api/overview', headers={'Host': f'attacker.invalid:{A.APP.port}', 'Cookie': session_cookie(A)})[0] == 403
    assert not calls


def test_slot_and_address_injection_are_rejected_before_commands(app, monkeypatch):
    A, _ = app
    calls = []
    monkeypatch.setattr(A, 'compose', lambda *a, **kw: calls.append(a) or (0, ''))
    monkeypatch.setattr(A, 'miner_exec', lambda *a, **kw: calls.append(a) or (0, ''))
    for value in ('../0', '0;touch /tmp/no', '--help', '0/../../x'):
        assert call(A, '/api/start', {'slot': value})[0] == 400
    assert call(A, '/api/payout', {'address': 'dendra1abc;touch x'})[0] == 400
    assert not calls


def test_handoff_token_is_single_use_under_race(app):
    A, _ = app
    A.APP.once = 'A' * 43
    host = f'127.0.0.1:{A.APP.port}'
    def attempt():
        return request(A, 'POST', '/handoff', body='t=' + 'A' * 43, headers={'Host': host, 'Content-Type': 'application/x-www-form-urlencoded'})[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [200, 401]


def test_conflicting_container_actions_do_not_overlap(app, monkeypatch):
    A, _ = app
    entered, release = threading.Event(), threading.Event()
    calls = []
    def compose(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            release.wait(5)
        return 0, ''
    monkeypatch.setattr(A, 'compose', compose)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, A, '/api/start', {})
        assert entered.wait(3)
        try:
            # Restart, not Stop: Stop interrupts the action instead of waiting (R74, test_dendra_app_stop_and_code.py)
            second = pool.submit(call, A, '/api/restart', {}).result(timeout=3)
        finally:
            release.set()
        assert first.result(timeout=3)[0] == 200
    assert second[0] == 409
    assert len(calls) == 1


def test_recovery_confirmation_cannot_run_twice(app, monkeypatch):
    A, _ = app
    entered, release = threading.Event(), threading.Event()
    words = ['word'] * 12
    A.APP.challenge = (0, 'address', [1, 2, 3])
    monkeypatch.setattr(A, 'recovery_phrase', lambda: {'present': True, 'address': 'address', 'words': words})
    monkeypatch.setattr(A, 'settings', lambda: {})
    calls = []
    def save(value):
        calls.append('save')
        if len(calls) == 1:
            entered.set()
            release.wait(5)
    monkeypatch.setattr(A, 'save_settings', save)
    monkeypatch.setattr(A, 'forget_recovery', lambda address: (True, ''))
    body = {'words': {'1': 'word', '2': 'word', '3': 'word'}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, A, '/api/recovery/confirm', body)
        assert entered.wait(3)
        try:
            second = pool.submit(call, A, '/api/recovery/confirm', body).result(timeout=3)
        finally:
            release.set()
        assert first.result(timeout=3)[0] == 200
    assert second[0] == 409
    assert calls == ['save']


def test_unauthenticated_local_client_cannot_read_the_recovery_phrase(app, monkeypatch):
    A, _ = app
    calls = []
    monkeypatch.setattr(A, 'recovery_phrase', lambda: calls.append(True) or {'present': True, 'address': 'example', 'words': ['sample'] * 12})
    assert call(A, '/api/recovery', token=False)[0] == 401
    assert call(A, '/api/recovery', token='wrong')[0] == 401
    assert calls == []
    status, data = call(A, '/api/recovery')
    assert status == 200 and data['words'] == ['sample'] * 12
    assert calls == [True]


def test_session_token_is_not_echoed_by_refusals_or_in_logs(app, capsys):
    A, _ = app
    host = f'127.0.0.1:{A.APP.port}'
    status, headers, body = request(A, 'GET', '/api/overview', headers={'Host': host, 'Cookie': session_cookie(A, 'incorrect')})
    assert status == 401 and A.APP.token.encode() not in body
    assert headers['Cache-Control'] == 'no-store'
    assert A.APP.token not in capsys.readouterr().out
    assert A.APP.token not in capsys.readouterr().err
    assert len(A.APP.token) == 43


def test_a_non_ascii_token_header_is_refused_with_401(app, capsys):
    A, _ = app
    host = f'127.0.0.1:{A.APP.port}'
    status, _, body = request(A, 'GET', '/api/overview', headers={'Host': host, 'Cookie': session_cookie(A, '\xe9\xe9')})
    assert status == 401 and body == b'{"error": "token"}'
    assert request(A, 'POST', '/api/stop', body='{}', headers={'Host': host, 'Cookie': session_cookie(A, '\xe9')})[0] == 401
    # the confirmation code's header is compared as bytes too: a wrong code, never a crash
    assert call(A, '/api/recovery', code='\xe9\xe9')[0] == 403
    assert 'Traceback' not in capsys.readouterr().err


def test_a_content_length_that_is_not_a_number_is_a_400(app, monkeypatch, capsys):
    A, _ = app
    calls = []
    monkeypatch.setattr(A, 'compose', lambda *a, **kw: calls.append(a) or (0, ''))
    host = f'127.0.0.1:{A.APP.port}'
    for bad in ('abc', '-1', '65537', '\xb2'):
        status, _, body = request(A, 'POST', '/api/stop', headers=dict({'Host': host, 'Content-Length': bad},
                                                                       **session_headers(A)))
        assert status == 400 and b'Content-Length' in body and calls == [], (bad, status, body)
    # a body that is not a JSON object is a 400 too, before any command (sent as JSON, as the page sends it: any
    # other Content-Type is a 415, test_dendra_app_stop_and_code.py)
    for raw in ('not json', '[1]'):
        status, _, body = request(A, 'POST', '/api/start', body=raw, headers=dict({'Host': host, 'Content-Type': 'application/json'},
                                                                                 **session_headers(A)))
        assert status == 400 and calls == [], (raw, status, body)
    # the handoff's own length check: not a number is a 400, an empty body a 401 (no token)
    form = {'Host': host, 'Content-Type': 'application/x-www-form-urlencoded'}
    assert request(A, 'POST', '/handoff', headers=dict(form, **{'Content-Length': 'x'}))[0] == 400
    assert request(A, 'POST', '/handoff', body='', headers=form)[0] == 401
    assert 'Traceback' not in capsys.readouterr().err


def test_the_page_runs_only_its_own_script_and_cannot_be_framed(app):
    import base64
    import hashlib
    import os
    A, _ = app
    host = f'127.0.0.1:{A.APP.port}'
    status, headers, page = request(A, 'GET', '/', headers={'Host': host})
    csp = headers['Content-Security-Policy']
    with open(os.path.join(os.path.dirname(A.__file__), 'ui', 'index.html'), 'rb') as f:
        shipped = f.read()
    script = shipped[shipped.index(b'<script>') + 8:shipped.rindex(b'</script>')]
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
    assert status == 200 and page == shipped
    assert f"script-src 'sha256-{digest}';" in csp, csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert 'unsafe-inline' not in csp.split('script-src')[1].split(';')[0]
    # the API's answers are JSON: no page policy is needed there
    assert 'Content-Security-Policy' not in request(A, 'GET', '/api/overview', headers={'Host': host})[1]


def test_the_refusal_names_the_action_that_runs(app, monkeypatch):
    A, _ = app
    entered, release = threading.Event(), threading.Event()

    def compose(*args, **kwargs):
        entered.set()
        release.wait(5)
        return 0, ''
    monkeypatch.setattr(A, 'compose', compose)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, A, '/api/start', {})
        assert entered.wait(3)
        try:
            second = pool.submit(call, A, '/api/restart', {}).result(timeout=3)
        finally:
            release.set()
        first.result(timeout=3)
    assert second[0] == 409 and second[1]['running'] == '/api/start'
    assert 'another action is already running (Start, started' in second[1]['error'], second


def test_another_copy_of_the_application_on_the_kit_holds_the_actions_back(app, monkeypatch, tmp_path):
    """A second process holds the kit's action lock as dendra_app.py::kit_action_lock takes it (the reviewers'
    case: one copy started from Windows, one from Linux). This copy refuses, names the action, and runs no
    compose; once the other process ends, it acts."""
    import os
    import subprocess
    import sys
    A, _ = app
    calls = []
    monkeypatch.setattr(A, 'compose', lambda *a, **kw: calls.append(a) or (0, ''))
    lock = A.action_lock_path()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    holder = subprocess.Popen([sys.executable, '-c', (
        'import fcntl, json, os, sys, time\n'
        'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n'
        'fcntl.flock(fd, fcntl.LOCK_EX)\n'
        'os.ftruncate(fd, 0)\n'
        'os.write(fd, json.dumps({"path": "/api/judge", "since": int(time.time()) - 120}).encode())\n'
        'print("held", flush=True)\n'
        'sys.stdin.read()\n'), lock], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        status, body = call(A, '/api/start', {})
        assert status == 409 and body['running'] == '/api/judge', body
        assert 'the Judge switch, started 2 min ago, in another copy of this application' in body['error'], body
        assert calls == []
    finally:
        holder.stdin.close()
        holder.wait(10)
    status, body = call(A, '/api/stop', {})
    assert status == 200 and len(calls) == 1


def test_session_bearer_token_can_be_reused_from_another_local_connection(app):
    A, _ = app
    host = f'127.0.0.1:{A.APP.port}'
    headers = dict({'Host': host}, **session_headers(A))
    assert request(A, 'GET', '/api/overview', headers=headers)[0] == 200
    assert request(A, 'GET', '/api/overview', headers=headers)[0] == 200
