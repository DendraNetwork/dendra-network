import os
import sys
from pathlib import Path

# DENDRA_REPO names the tree that carries the files read at its root, and DENDRA_MODEA_DIR a copy of services,
# when pytest runs from a copy of prototype/ (the battery does).
REPO = Path(os.environ.get('DENDRA_REPO') or Path(__file__).resolve().parents[3])
MODEA = Path(os.environ.get('DENDRA_MODEA_DIR') or Path(__file__).resolve().parents[1])

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(not hasattr(os, 'O_NOFOLLOW'), reason='requires Unix no-follow semantics')


def test_judge_trace_does_not_follow_symlinks(tmp_path):
    import judge_worker
    target = tmp_path / 'target'
    target.write_text('original', encoding='utf8')
    link = tmp_path / 'trace'
    link.symlink_to(target)
    judge_worker._trace_write(str(link), {'stage': 'test'})
    assert target.read_text(encoding='utf8') == 'original'


def test_judge_trace_is_one_file_per_identity_in_its_key_directory(tmp_path):
    import judge_worker
    keydir = str(tmp_path / 'keys')
    one = judge_worker.trace_destination('1', keydir, 'judge1')
    two = judge_worker.trace_destination('1', keydir, 'judge2')
    # Two judges on one key directory never write the same file, and both write in that directory.
    assert one != two and Path(one).parent == Path(two).parent == Path(keydir)
    assert Path(one).name == 'judge-trace-judge1.jsonl'
    assert judge_worker.trace_destination('', keydir, 'judge1') == ''
    assert judge_worker.trace_destination('/x/trace.jsonl', keydir, 'judge1') == '/x/trace.jsonl'


def test_the_benches_read_the_judge_trace_where_the_judges_write_it():
    """Every internal script that reads the judges' traces reads the files they write with DENDRA_JUDGE_TRACE=1
    (judge_worker.py::trace_destination), in the key directory the judges are given, never under /tmp. The scripts
    are found by what they read, not by name: they are internal, and absent from a published copy, where this test
    has nothing to check."""
    root = REPO / 'dendra' / 'onchain-staging'
    if not root.is_dir():
        pytest.skip('no internal scripts in this copy')
    readers = {}
    # A bench (*_test.sh) may name the trace as a pattern to refuse, such as the publication leak filter's: it reads none.
    for path in sorted(p for p in root.glob('*.sh') if not p.name.endswith('_test.sh')):
        code = [ln for ln in path.read_text(encoding='utf8', errors='replace').splitlines()
                if 'judge-trace-' in ln and not ln.lstrip().startswith('#')]
        if code:
            readers[path.name] = code
    assert len(readers) >= 2, sorted(readers)
    for name, code in readers.items():
        assert all('/tmp/judge-trace' not in ln for ln in code), (name, code)
        assert all('"$KEYDIR"/judge-trace-' in ln for ln in code), (name, code)


def test_selftest_refuses_a_symlink_lock(monkeypatch, tmp_path):
    import miner_selftest
    target = tmp_path / 'target'
    target.write_text('original', encoding='utf8')
    link = tmp_path / 'lock'
    link.symlink_to(target)
    monkeypatch.setenv('DENDRA_SELFTEST_LOCK', str(link))
    # Refused as a lock (False: the run goes on unguarded), never followed, and never read as another run.
    assert miner_selftest._one_at_a_time() is False
    assert target.read_text(encoding='utf8') == 'original'


def test_selftest_lock_that_cannot_be_opened_is_not_read_as_busy(monkeypatch, tmp_path):
    import miner_selftest
    monkeypatch.delenv('DENDRA_SELFTEST_LOCK', raising=False)
    assert miner_selftest._one_at_a_time(str(tmp_path / 'no-such-keydir')) is False


def test_selftest_lock_held_by_another_process_is_busy(monkeypatch, tmp_path):
    import fcntl
    import miner_selftest
    path = tmp_path / 'held.lock'
    monkeypatch.setenv('DENDRA_SELFTEST_LOCK', str(path))
    with open(path, 'a+') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert miner_selftest._one_at_a_time() is None
    got = miner_selftest._one_at_a_time()
    assert got not in (None, False)
    got.close()


def test_selftest_main_runs_when_the_lock_cannot_be_opened(monkeypatch, tmp_path, capsys):
    import miner_selftest

    def ran(*a, **k):
        raise SystemExit(7)                 # the checks were reached: the run was not refused as busy
    monkeypatch.setenv('DENDRA_SELFTEST_LOCK', str(tmp_path / 'no-such-dir' / 'lock'))
    monkeypatch.setattr(miner_selftest, 'run_checks', ran)
    with pytest.raises(SystemExit) as e:
        miner_selftest.main(['--host'])
    assert e.value.code == 7
    assert 'DENDRA_SELFTEST_BUSY' not in capsys.readouterr().out
