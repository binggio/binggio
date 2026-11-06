# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""0.1.9: tables are prepared in worker processes and profiled in threads; the results are exactly
those of one table at a time."""
import sqlite3
import sys
from pathlib import Path

import binggio
from binggio import prepared
from binggio.profiling import profiler, store as profiles

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import trading_schema  # noqa: E402

VOLATILE = ('profiled_at', 'cache_hit', 'stale')


def _profiles(ws):
    out = {}
    for t in profiles.list_profiles(ws.root):
        p = profiles.get_table(ws.root, t['connection_id'], t['table'])
        out[t['table']] = {k: v for k, v in p.items() if k not in VOLATILE}
    return out


def _copies(ws):
    out = {}
    for c in ws.connections():
        for t in profiles.list_profiles(ws.root, connection_id=c['id']):
            with sqlite3.connect(prepared.ensure(ws.root, c, t['table'])) as db:
                out[t['table']] = db.execute('SELECT * FROM t ORDER BY rowid').fetchall()
    return out


def _run(tmp, src, workers, monkeypatch):
    monkeypatch.setenv('BINGGIO_WORKERS', str(workers))
    monkeypatch.setenv('BINGGIO_PROCESSES', '1')
    ws = binggio.Workspace.create(tmp)
    ws.add_csv_folder('Work', src)
    seen = []
    r = prepared.prepare_all(ws.root, progress=lambda k, n, t: seen.append(t))
    assert r['prepared'] == 9 and not r['failures'] and len(seen) == 9
    ws.profile(progress=lambda k, n, t: seen.append(t))
    return ws


def test_parallel_results_match_one_at_a_time(tmp_path, monkeypatch):
    src = tmp_path / 'src'
    trading_schema.generate(src, scale=2)
    monkeypatch.setattr(profiler, 'SPLIT_ROWS', 500)       # also split the columns of each table between threads
    one = _run(tmp_path / 'one', src, 1, monkeypatch)
    many = _run(tmp_path / 'many', src, 4, monkeypatch)
    assert _copies(one) == _copies(many)
    a, b = _profiles(one), _profiles(many)
    assert a.keys() == b.keys()
    for t in a:
        a[t].pop('connection_id'); b[t].pop('connection_id')
        assert a[t] == b[t], t


def test_empty_rule_in_sql_matches_python():
    vals = [None, '', '  ', ' \t', 'null', 'NULL', ' Null ', 'nUlL', 'none', 'None ', 'N/A', 'n/a', ' na', 'NaN', 'nan', '?', ' ? ',
            'x', '0', 'NULLS', 'nan5', 'Na', 'A', '  NA  ', '\tnull\n', 'none\r', '\n', '\r\n?', 'nulls ', ' x ', 1, 0, 2.5]
    db = sqlite3.connect(':memory:')
    db.execute('create table t(c)'); db.executemany('insert into t values(?)', [(v,) for v in vals])
    got = {v: bool(r) for v, r in db.execute('select c, %s from t' % prepared.missing_sql('c'))}
    assert all(got[v] == prepared._is_missing(v) for v in vals)
