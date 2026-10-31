import os
import sys
from pathlib import Path

# DENDRA_REPO names the tree that carries the files read at its root, and DENDRA_MODEA_DIR a copy of services,
# when pytest runs from a copy of prototype/ (the battery does).
REPO = Path(os.environ.get('DENDRA_REPO') or Path(__file__).resolve().parents[3])
MODEA = Path(os.environ.get('DENDRA_MODEA_DIR') or Path(__file__).resolve().parents[1])

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway as gateway
import faucet as faucet


def test_gateway_refuses_reservation_when_persistence_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, 'QUOTA_FILE', str(tmp_path / 'quota.json'))
    monkeypatch.setattr(gateway, '_QUOTA', {'day': gateway.datetime.date.today().isoformat(), 'glob': 0, 'ip': {}})
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(gateway.os, 'replace', fail)
    ok, _ = gateway.quota_reserve('client', 10, False)
    assert not ok


def test_gateway_failed_quota_save_is_a_503_and_leaves_memory_as_the_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, 'QUOTA_FILE', str(tmp_path / 'quota.json'))
    monkeypatch.setattr(gateway, '_QUOTA', {'day': gateway.datetime.date.today().isoformat(), 'glob': 0, 'ip': {},
                                            'open': {}})
    monkeypatch.setattr(gateway, 'FREE_TIER', True)

    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(gateway.os, 'replace', fail)

    def no_job(*a, **k):
        raise AssertionError('no job runs without a recorded reservation')
    monkeypatch.setattr(gateway.dc, 'quick', no_job)
    monkeypatch.setattr(gateway.dc, 'quick_metered', no_job)
    assert gateway.quota_reserve('client', 10, True) == (False, gateway.QUOTA_UNSAVED)
    answer, meta = gateway.run_dendra([{'role': 'user', 'content': 'hi'}], ip='203.0.113.9')
    assert meta['dendra_failure'] == {'http': 503, 'code': 'quota_unavailable'}, meta
    assert 'quota reached' not in answer and 'tomorrow' not in answer, answer
    assert gateway._QUOTA['glob'] == 0 and gateway._QUOTA['ip'] == {} and gateway._QUOTA['open'] == {}, gateway._QUOTA


def test_faucet_refuses_to_authorize_when_persistence_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(faucet, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(faucet, '_addr_last', {})
    monkeypatch.setattr(faucet, '_ip_hits', {})
    monkeypatch.setattr(faucet, '_global_hits', [])
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(faucet.os, 'replace', fail)
    with pytest.raises(OSError):
        faucet._rate_ok('address', 'client')
    # Memory stays what the disk holds: nothing was granted, so nothing is counted.
    assert (faucet._addr_last, faucet._ip_hits, faucet._global_hits) == ({}, {}, [])


def test_a_faucet_refund_survives_a_restart(monkeypatch, tmp_path):
    """The drip is saved before the transfer, so a refund after a failed transfer is saved too: a restart must not
    lock the address for a drip it never received."""
    monkeypatch.setattr(faucet, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(faucet, '_addr_last', {})
    monkeypatch.setattr(faucet, '_ip_hits', {})
    monkeypatch.setattr(faucet, '_global_hits', [])
    assert faucet._rate_ok('address', 'client')[0]
    faucet._rate_refund('address')
    monkeypatch.setattr(faucet, '_addr_last', {})
    monkeypatch.setattr(faucet, '_ip_hits', {})
    monkeypatch.setattr(faucet, '_global_hits', [])
    faucet._load_state()
    assert 'address' not in faucet._addr_last
    assert len(faucet._global_hits) == 1 and len(faucet._ip_hits['client']) == 1


def test_the_faucet_s_exposure_check_refuses_public_and_only_warns_beyond_loopback():
    assert faucet.exposure_check('127.0.0.1', None, 0) == (None, None)
    fatal, warning = faucet.exposure_check('0.0.0.0', '0', 0)
    assert fatal is None and 'DENDRA_FAUCET_POW_BITS' in warning and '0.0.0.0' in warning, warning
    fatal, warning = faucet.exposure_check('127.0.0.1', '1', 0)
    assert 'Refusing to boot' in fatal and warning is None
    # Only the exact value "1" arms the refusal, as the running faucet does: another word must not stop it.
    for flag in ('true', 'yes', 'on', 'ture', '2', ''):
        assert faucet.exposure_check('127.0.0.1', flag, 0) == (None, None), flag
    assert faucet.exposure_check('0.0.0.0', '1', 20) == (None, None)


def _compose_default(value):
    """A compose value read with no .env behind it: `${NAME:-default}` is its default."""
    if value.startswith('${') and ':-' in value and value.endswith('}'):
        return value[value.index(':-') + 2:-1]
    return value


def test_the_faucet_starts_with_the_compose_settings_and_warns_without_pow(monkeypatch, capsys):
    """The testnet's faucet runs with the compose's settings: 0.0.0.0 and, without a .env value, no PoW. It starts
    there, as before the static review, and its log names the setting that arms the PoW."""
    import http.server
    import runpy
    env = {k: _compose_default(v) for k, v in _compose_faucet_env().items()}
    assert (env['DENDRA_FAUCET_HOST'], env['DENDRA_FAUCET_POW_BITS'], env['DENDRA_PUBLIC']) == ('0.0.0.0', '0', '0'), env
    for name in ('DENDRA_FAUCET_HOST', 'DENDRA_FAUCET_POW_BITS', 'DENDRA_PUBLIC'):
        monkeypatch.setenv(name, env[name])
    monkeypatch.setenv('DENDRA_FAUCET_STATE', '')
    addresses = []

    class FakeServer:
        def __init__(self, address, handler):
            addresses.append(address)

        def serve_forever(self):
            pass
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', FakeServer)
    runpy.run_path(str(Path(faucet.__file__)), run_name='__main__')
    assert addresses and addresses[0][0] == '0.0.0.0'
    out = capsys.readouterr().out
    assert 'beyond loopback, with DENDRA_FAUCET_POW_BITS=0' in out, out


def test_faucet_defaults_to_loopback(monkeypatch):
    import http.server
    import runpy
    import os
    monkeypatch.delenv('DENDRA_FAUCET_HOST', raising=False)
    monkeypatch.delenv('DENDRA_PUBLIC', raising=False)
    monkeypatch.setenv('DENDRA_FAUCET_STATE', '')
    monkeypatch.setenv('DENDRA_FAUCET_POW_BITS', '0')
    addresses = []
    class FakeServer:
        def __init__(self, address, handler):
            addresses.append(address)
        def serve_forever(self):
            pass
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', FakeServer)
    runpy.run_path(str(Path(faucet.__file__)), run_name='__main__')
    assert addresses[0][0] == '127.0.0.1'


def _compose_faucet_env():
    """The faucet service's environment in docker-compose.yml, each value as compose writes it."""
    import re
    text = (REPO / 'docker-compose.yml').read_text(encoding='utf8')
    block = text[text.index('\n  faucet:'):text.index('\n  relay:')]
    line = next(ln for ln in block.splitlines() if ln.strip().startswith('environment:'))
    return dict(re.findall(r'([A-Z_]+): "([^"]*)"', line))


def test_compose_faucet_listens_where_the_gateway_and_the_relay_reach_it(monkeypatch):
    """The gateway (DENDRA_FAUCET=http://faucet:4500) and the relay reach the faucet over the compose network,
    and the daemon's own default is loopback: compose names the host, or the service answers nobody."""
    import http.server
    import runpy
    env = _compose_faucet_env()
    assert env.get('DENDRA_FAUCET_HOST') == '0.0.0.0', env
    monkeypatch.setenv('DENDRA_FAUCET_HOST', env['DENDRA_FAUCET_HOST'])
    monkeypatch.delenv('DENDRA_PUBLIC', raising=False)
    monkeypatch.setenv('DENDRA_FAUCET_STATE', '')
    monkeypatch.setenv('DENDRA_FAUCET_POW_BITS', '20')
    addresses = []

    class FakeServer:
        def __init__(self, address, handler):
            addresses.append(address)

        def serve_forever(self):
            pass
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', FakeServer)
    runpy.run_path(str(Path(faucet.__file__)), run_name='__main__')
    assert addresses[0][0] == '0.0.0.0'


def test_faucet_public_flag_one_requires_pow_at_boot(monkeypatch):
    import runpy
    import http.server
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', lambda *a: (_ for _ in ()).throw(AssertionError('server started')))
    monkeypatch.setenv('DENDRA_FAUCET_STATE', '')
    monkeypatch.setenv('DENDRA_PUBLIC', '1')
    monkeypatch.setenv('DENDRA_FAUCET_POW_BITS', '0')
    with pytest.raises(SystemExit):
        runpy.run_path(str(Path(faucet.__file__)), run_name='__main__')


def test_journal_rejects_corrupt_existing_file(tmp_path):
    import miner as miner
    path = tmp_path / miner._JOURNAL
    path.write_text('{bad', encoding='utf8')
    with pytest.raises((OSError, ValueError)):
        miner._journal_commitment(tmp_path, 'job', 'commit', 'proof')
    assert path.read_text(encoding='utf8') == '{bad'


def test_an_unreadable_journal_stops_the_pass_and_is_named_in_the_heartbeat(monkeypatch, tmp_path):
    import miner as miner
    path = tmp_path / miner._JOURNAL
    path.write_text('{bad', encoding='utf8')
    written = {}
    monkeypatch.setattr(miner, '_status_write', lambda _touch=True, **f: written.update(f) or True)
    with pytest.raises(miner.JournalUnreadable):
        miner._journal_for_pass(tmp_path)
    assert written['journal_error']['path'] == str(path), written
    assert path.read_text(encoding='utf8') == '{bad'
    path.unlink()                                  # moved aside by the operator: the next pass reads it as empty
    noted = {}
    monkeypatch.setattr(miner, '_status_note', lambda **f: noted.update(f))
    assert miner._journal_for_pass(tmp_path) == {}
    assert noted == {'journal_error': None}


def test_journal_write_failure_stops_caller(monkeypatch, tmp_path):
    import miner as miner
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(miner.os, 'replace', fail)
    with pytest.raises(OSError):
        miner._journal_commitment(tmp_path, 'job', 'commit', 'proof')


def test_a_journal_that_cannot_be_written_is_named_in_the_heartbeat_and_said_truly(monkeypatch, tmp_path, capsys):
    """The pass stops on it (nothing is deposited without its commitment journalled): the heartbeat says so with
    the path and the cause, which C9 of miner_selftest.py reads, and the log says what happens to the job."""
    import miner as miner
    written, noted = {}, {}
    monkeypatch.setattr(miner, '_status_write', lambda _touch=True, **f: written.update(f) or True)

    def fail(*args):
        raise OSError('No space left on device')
    monkeypatch.setattr(miner.os, 'replace', fail)
    with pytest.raises(OSError):
        miner._journal_commitment(tmp_path, 'job', 'commit', 'proof')
    je = written['journal_error']
    assert (je['kind'], je['path']) == ('write', str(tmp_path / miner._JOURNAL)) and 'No space' in je['why'], je
    out = capsys.readouterr().out
    assert 'would become permanent again' not in out and 'not deposited' in out and 'this pass stops' in out, out
    # A pass that reads the journal says nothing of the write: the error stays until a write succeeds.
    monkeypatch.setitem(miner._STATUS, 'journal_error', je)
    monkeypatch.setattr(miner, '_status_note', lambda **f: noted.update(f))
    assert miner._journal_for_pass(tmp_path) == {}
    assert noted == {}


def test_points_rejects_failed_verdict_query(monkeypatch):
    from types import SimpleNamespace
    import points_indexer as points
    monkeypatch.setattr(points.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='{"commit":{"result_commit":"1"}}', stderr=''))
    jobs = [{'id': 'j', 'state': 'open+paid+disputed+resolved', 'miner_id': 'm'}]
    with pytest.raises(points.Unmeasured):
        points._fetch_verdicts(jobs, [{'id': 'judge'}])


# What dendrad prints for a juror who has no commit (x/jobs/keeper/query_commit.go::GetCommit), exit code 1.
NOT_FOUND = 'Error: rpc error: code = NotFound desc = not found'


def _one_voter_one_absent(*a, **k):
    from types import SimpleNamespace
    key = a[0][4]
    if key.endswith('__verdict__voter'):
        return SimpleNamespace(returncode=0, stdout='{"commit":{"result_commit":"1","model_id":"m1"}}', stderr='')
    return SimpleNamespace(returncode=1, stdout='', stderr=NOT_FOUND)


def test_points_reads_a_juror_who_did_not_vote_as_no_vote(monkeypatch):
    import points_indexer as points
    monkeypatch.setattr(points.subprocess, 'run', _one_voter_one_absent)
    jobs = [{'id': 'j', 'state': 'open+paid+disputed+resolved', 'miner_id': 'm'}]
    assert points._fetch_verdicts(jobs, [{'id': 'voter'}, {'id': 'silent'}]) == {'j': {'voter': '1'}}


def test_points_still_rejects_a_failure_that_is_not_notfound(monkeypatch):
    from types import SimpleNamespace
    import points_indexer as points
    monkeypatch.setattr(points.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stdout='', stderr='Error: rpc error: code = Unavailable desc = connection refused'))
    jobs = [{'id': 'j', 'state': 'open+paid+disputed+resolved', 'miner_id': 'm'}]
    with pytest.raises(points.Unmeasured):
        points._fetch_verdicts(jobs, [{'id': 'voter'}])


def _exporter(monkeypatch):
    import importlib
    from types import ModuleType
    fake = ModuleType('prometheus_client')

    class Gauge:
        def __init__(self, *args, **kwargs): pass
        def labels(self, **kwargs): return self
        def set(self, value): pass
        def clear(self): pass
    fake.Gauge = Gauge
    fake.start_http_server = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, 'prometheus_client', fake)
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)
    return importlib.import_module('exporter')


def test_exporter_reads_a_juror_who_did_not_vote_as_no_vote(monkeypatch):
    exporter = _exporter(monkeypatch)
    monkeypatch.setattr(exporter.subprocess, 'run', _one_voter_one_absent)
    assert exporter._verdicts_of('j', ['voter', 'silent']) == ({'voter': '1'}, {'m1'})
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)


def test_exporter_rejects_failed_verdict_query(monkeypatch):
    import importlib
    from types import ModuleType, SimpleNamespace
    fake = ModuleType('prometheus_client')
    class Gauge:
        def __init__(self, *args, **kwargs): pass
        def labels(self, **kwargs): return self
        def set(self, value): pass
        def clear(self): pass
    fake.Gauge = Gauge
    fake.start_http_server = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, 'prometheus_client', fake)
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)
    exporter = importlib.import_module('exporter')
    monkeypatch.setattr(exporter.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='{"commit":{"result_commit":"1"}}', stderr=''))
    with pytest.raises(ValueError):
        exporter._verdicts_of('j', ['judge'])
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)


def test_capacity_rejects_corrupt_store(monkeypatch, tmp_path):
    import capacity_server as capacity
    path = tmp_path / 'capacity.json'
    path.write_text('{bad', encoding='utf8')
    monkeypatch.setattr(capacity, 'DB', path)
    with pytest.raises(ValueError):
        capacity._load()
    assert path.read_text(encoding='utf8') == '{bad'


def test_capacity_surfaces_failed_save(monkeypatch, tmp_path):
    import capacity_server as capacity
    monkeypatch.setattr(capacity, 'DB', tmp_path / 'capacity.json')
    def fail(*args): raise OSError('write failed')
    monkeypatch.setattr(capacity.os, 'replace', fail)
    with pytest.raises(OSError):
        capacity._save({'entry': {}})


def test_capacity_keeps_registry_after_failed_query(monkeypatch):
    from types import SimpleNamespace
    import capacity_server as capacity
    old = {'t': 0.0, 'ids': {'miner'}, 'ops': {'miner': 'op'}}
    monkeypatch.setattr(capacity, '_MINERS', old)
    monkeypatch.setattr(capacity.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='{"miner":[]}'))
    assert capacity._onchain_miner_ids() == {'miner'}
    assert old['t'] == 0.0


def test_capacity_post_reports_failed_save(monkeypatch, tmp_path):
    import io
    from types import SimpleNamespace
    import capacity_server as capacity
    monkeypatch.setattr(capacity, 'DB', tmp_path / 'capacity.json')
    monkeypatch.setattr(capacity, '_rate_ok', lambda ip: True)
    monkeypatch.setattr(capacity, '_clean', lambda body: {'node_id': 'node', 'machine': 'm', 'ts': capacity.time.time(), 'tier': '', 'model': '', 'vram_mb': 0, 'can_judge': False, 'judge_role': '', 'judge_mode': ''})
    monkeypatch.setattr(capacity, '_proven_miner_of', lambda *a: None)
    def fail(*args): raise OSError('write failed')
    monkeypatch.setattr(capacity.os, 'replace', fail)
    sent = []
    handler = SimpleNamespace(path='/capacity', client_address=('127.0.0.1', 1), headers={'Content-Length': '2'},
                              rfile=io.BytesIO(b'{}'), _send=lambda code, body: sent.append(code))
    capacity.Handler.do_POST(handler)
    assert sent == [503]


def test_capacity_corrupt_store_refusal_names_the_way_out_in_the_log(monkeypatch, tmp_path, capsys):
    import io
    from types import SimpleNamespace
    import capacity_server as capacity
    path = tmp_path / 'capacity.json'
    path.write_text('{bad', encoding='utf8')
    monkeypatch.setattr(capacity, 'DB', path)
    monkeypatch.setattr(capacity, '_rate_ok', lambda ip: True)
    monkeypatch.setattr(capacity, '_clean', lambda body: {'node_id': 'node', 'machine': 'm', 'ts': capacity.time.time(), 'tier': '', 'model': '', 'vram_mb': 0, 'can_judge': False, 'judge_role': '', 'judge_mode': ''})
    monkeypatch.setattr(capacity, '_proven_miner_of', lambda *a: None)
    sent = []
    handler = SimpleNamespace(path='/capacity', client_address=('127.0.0.1', 1), headers={'Content-Length': '2'},
                              rfile=io.BytesIO(b'{}'), _send=lambda code, body: sent.append(code))
    capacity.Handler.do_POST(handler)
    assert sent == [503]
    out = capsys.readouterr().out
    assert f'mv {path} {path}.unreadable' in out and 'every report is refused' in out, out
    assert path.read_text(encoding='utf8') == '{bad'


def test_capacity_aggregation_failure_is_not_presented_as_an_unreadable_store(monkeypatch, tmp_path, capsys):
    """A defect of aggregate() is not the store's: told to move the store aside, the operator would lose every
    report for it."""
    from types import SimpleNamespace
    import capacity_server as capacity
    path = tmp_path / 'capacity.json'
    path.write_text('{}', encoding='utf8')
    monkeypatch.setattr(capacity, 'DB', path)
    monkeypatch.setattr(capacity, 'onchain_block', lambda: None)

    def broken(store, chain):
        raise KeyError('tier')
    monkeypatch.setattr(capacity, 'aggregate', broken)
    for route in ('/capacity', '/metrics'):
        sent = []
        handler = SimpleNamespace(path=route, _send=lambda code, body, *a, **k: sent.append((code, body)))
        capacity.Handler.do_GET(handler)
        assert sent == [(500, b'{"error":"capacity aggregation failed"}')], (route, sent)
    out = capsys.readouterr().out
    assert 'could not be aggregated' in out and 'unavailable' not in out and '.unreadable' not in out, out




def test_failed_balance_read_is_unknown(monkeypatch):
    import client as client
    monkeypatch.setattr(client, '_use_rest', lambda: False)
    monkeypatch.setattr(client, 'out_json', lambda *a, **k: {})
    monkeypatch.setattr(client, 'read_json', lambda *a, **k: None)
    assert client.balance('address') is None


def test_exporter_keeps_emission_gauges_when_balance_unknown(monkeypatch):
    import importlib
    from types import ModuleType
    fake = ModuleType('prometheus_client')
    class Gauge:
        def __init__(self, *args, **kwargs): self.values = []
        def labels(self, **kwargs): return self
        def set(self, value): self.values.append(value)
        def clear(self): pass
    fake.Gauge = Gauge
    fake.start_http_server = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, 'prometheus_client', fake)
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)
    exporter = importlib.import_module('exporter')
    monkeypatch.setattr(exporter.dc, 'network_state', lambda: {'miners': [], 'pools': {}, 'height': 1})
    monkeypatch.setattr(exporter.dc, 'balance', lambda *a: None)
    monkeypatch.setattr(exporter.dc, 'committee_seed_health', lambda: None)
    exporter.refresh()
    assert exporter.g_released.values == []
    assert exporter.g_r.values == []
    assert exporter.g_r2.values == []
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)


def test_embedder_retries_after_transient_load_failure(monkeypatch):
    from types import ModuleType
    import modea.miner as miner
    attempts = []
    class Model:
        def __init__(self, name):
            attempts.append(name)
            if len(attempts) == 1: raise OSError('temporary')
        def encode(self, text, normalize_embeddings=True): return [0.5]
    fake = ModuleType('sentence_transformers')
    fake.SentenceTransformer = Model
    monkeypatch.setitem(sys.modules, 'sentence_transformers', fake)
    monkeypatch.setattr(miner, '_ST_MODEL', None)
    monkeypatch.setattr(miner, '_ST_TRIED', False)
    assert miner._semantic_embed('one') is None
    assert miner._semantic_embed('two') == '500000'


def test_address_consistency_guard_survives_python_optimization():
    import ast
    from types import SimpleNamespace
    source = MODEA.joinpath('miner.py').read_text(encoding='utf8')
    tree = ast.parse(source)
    guards = [node for node in ast.walk(tree) if isinstance(node, (ast.Assert, ast.If)) and
              'keys_addr(a.id)' in ast.get_source_segment(source, node.test)]
    assert len(guards) == 1
    code = compile(ast.fix_missing_locations(ast.Module(body=guards, type_ignores=[])), '<guard>', 'exec', optimize=2)
    context = {'addr': 'first', 'a': SimpleNamespace(id='id'), 'keys_addr': lambda _: 'second'}
    with pytest.raises(Exception, match='keyring address changed'):
        exec(code, context)
    context['keys_addr'] = lambda _: 'first'
    exec(code, context)

@pytest.mark.parametrize('module,setting', [('modea.coherence_pll', 'DENDRA_PLL_REVISION'), ('modea.coherence_ppl', 'DENDRA_PPL_REVISION')])
def test_coherence_requires_and_uses_immutable_revision(monkeypatch, module, setting):
    import importlib
    from types import ModuleType
    mod = importlib.import_module(module)
    seen = []
    class Loader:
        @classmethod
        def from_pretrained(cls, model, **kw):
            seen.append(kw.get('revision'))
            return cls()
        def eval(self): pass
    fake = ModuleType('transformers')
    fake.AutoTokenizer = Loader
    fake.AutoModelForMaskedLM = Loader
    fake.AutoModelForCausalLM = Loader
    monkeypatch.setitem(sys.modules, 'transformers', fake)
    monkeypatch.setattr(mod, '_model', None)
    monkeypatch.setattr(mod, '_tok', None)
    monkeypatch.delenv(setting, raising=False)
    with pytest.raises(ValueError): mod._load()
    monkeypatch.setenv(setting, 'a' * 40)
    mod._load()
    assert seen == ['a' * 40, 'a' * 40]


def test_key_address_rejects_nonzero_command(monkeypatch):
    from types import SimpleNamespace
    import gateway_fund
    monkeypatch.setattr(gateway_fund.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='dendra1plausible'))
    assert gateway_fund.addr() == ''


def test_chain_status_rejects_nonzero_command(monkeypatch):
    from types import SimpleNamespace
    from modea import chain_id
    monkeypatch.setattr(chain_id.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='{"node_info":{"network":"test"}}', stderr=''))
    with pytest.raises(chain_id.ChainIdError):
        chain_id.resolve(env={})


def test_vrf_keygen_nonzero_never_stores_output(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import miner as miner
    stored = []
    monkeypatch.setattr(miner, '_vrf_bin', lambda: 'test-vrf')
    monkeypatch.setattr(miner, '_keyring', lambda: SimpleNamespace(seal_with='', open_with=''))
    monkeypatch.setattr(miner.crypto, 'store_secret', lambda *a, **k: stored.append(True))
    monkeypatch.setattr(miner.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='a'*128 + ' ' + 'b'*64))
    assert miner.vrf_identity(tmp_path, 'miner') == ('', '')
    assert stored == []


def test_concurrent_gateway_reservations_survive_reload(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / 'quota.json')
    monkeypatch.setattr(gateway, 'QUOTA_FILE', path)
    monkeypatch.setattr(gateway, '_QUOTA', {'day': gateway.datetime.date.today().isoformat(), 'glob': 0, 'ip': {}})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: gateway.quota_reserve(str(n), 10, True), range(20)))
    assert all(ok for ok, _ in results)
    monkeypatch.setattr(gateway, '_QUOTA', {'day': '', 'glob': 0, 'ip': {}})
    gateway._quota_load()
    assert gateway._QUOTA['glob'] == 200
    assert len(gateway._QUOTA['ip']) == 20


def test_concurrent_faucet_reservations_survive_reload(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(faucet, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(faucet, '_addr_last', {})
    monkeypatch.setattr(faucet, '_ip_hits', {})
    monkeypatch.setattr(faucet, '_global_hits', [])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: faucet._rate_ok('address'+str(n), 'client'+str(n)), range(20)))
    assert all(ok for ok, _ in results)
    monkeypatch.setattr(faucet, '_addr_last', {})
    monkeypatch.setattr(faucet, '_ip_hits', {})
    monkeypatch.setattr(faucet, '_global_hits', [])
    faucet._load_state()
    assert len(faucet._global_hits) == 20
    assert len(faucet._addr_last) == 20


def test_exporter_ignores_failed_key_lookup(monkeypatch):
    import importlib
    from types import ModuleType, SimpleNamespace
    fake = ModuleType('prometheus_client')
    class Gauge:
        def __init__(self, *args, **kwargs): pass
    fake.Gauge = Gauge
    fake.start_http_server = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, 'prometheus_client', fake)
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)
    exporter = importlib.import_module('exporter')
    monkeypatch.setenv('DENDRA_R2_EXCLUDE_ADDRS', '')
    monkeypatch.setattr(exporter.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='dendra1plausible'))
    assert exporter._resolve_excluded() == set()
    monkeypatch.delitem(sys.modules, 'exporter', raising=False)


def test_vrf_pubkey_nonzero_is_not_accepted(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import miner as miner
    (tmp_path / 'miner.vrf').touch()
    monkeypatch.setattr(miner, '_keyring', lambda: SimpleNamespace(seal_with='', open_with=''))
    monkeypatch.setattr(miner.crypto, 'load_secret', lambda *a, **k: b'a' * 128)
    monkeypatch.setattr(miner.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='b' * 64))
    assert miner.vrf_identity(tmp_path, 'miner') == ('', '')


def test_failed_vrf_proof_is_not_sent(monkeypatch):
    from types import SimpleNamespace
    import json
    import miner as miner
    sent = []
    calls = []
    monkeypatch.setattr(miner, '_chain_height', lambda: 1)
    monkeypatch.setattr(miner, 'avail_window', lambda *a: ('open', None))
    monkeypatch.setattr(miner, 'query', lambda *a, **k: json.dumps({'challenge': 'c' * 64}))
    monkeypatch.setattr(miner, '_avail_deadline', lambda: None)
    monkeypatch.setattr(miner, '_avail_record', lambda *a: {})
    monkeypatch.setattr(miner, '_status_write', lambda **k: None)
    monkeypatch.setattr(miner, 'tx_from', lambda *a, **k: sent.append(a))
    monkeypatch.setattr(miner.subprocess, 'run', lambda *a, **k: calls.append(True) or SimpleNamespace(returncode=1, stdout='ab' * 80))
    monkeypatch.setattr(miner, '_PROBE_STATE', {'failed_at': 0.0, 'said_for': '', 'bad_bound_said': False})
    assert miner.prove_availability_once('miner', 'x', '', probe=lambda: (miner.PROBE_ANSWERED, '', 0.2)) == ''
    assert calls and not sent
