# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Database connector: read-only SELECTs with a row limit, declared keys kept, uniform values.

SQL Server and Oracle are exercised with a recording stand-in connection (the SQL each dialect
sends, and the value types their drivers return). Live servers are checked on a real machine
with tools/check_database.py."""
import datetime as dt
import decimal
import sqlite3
import pytest
import binggio
from binggio.connectors import database as db
from binggio.connectors import ConnectorError


class FakeCursor:
    def __init__(self, conn): self.conn, self.description, self._rows = conn, None, []
    def execute(self, sql, params=()):
        self.conn.log.append((sql, tuple(params)))
        for key, (desc, rows) in self.conn.answers.items():
            if key in sql:
                self.description = [(c,) for c in desc]; self._rows = rows; return
        raise AssertionError('unexpected SQL: ' + sql)
    def fetchall(self): return list(self._rows)


class FakeConn:
    def __init__(self, answers): self.answers, self.log = answers, []
    def cursor(self): return FakeCursor(self)
    def close(self): pass


def sqlserver_conn():
    return FakeConn({
        'INFORMATION_SCHEMA.TABLES': (['TABLE_SCHEMA', 'TABLE_NAME'], [('dbo', 'Customers'), ('dbo', 'Loans')]),
        'TABLE_CONSTRAINTS': (['TABLE_NAME', 'COLUMN_NAME'], [('Customers', 'CustomerID'), ('Loans', 'LoanID')]),
        '[dbo].[Customers]': (['CustomerID', 'Segment', 'Since'], [(i, 'RETAIL' if i % 2 else 'PREMIUM', dt.datetime(2020, 1, i % 28 + 1)) for i in range(1, 21)]),
        '[dbo].[Loans]': (['LoanID', 'CustomerID', 'Balance'], [(100 + i, i % 20 + 1, decimal.Decimal('1000.50') * i) for i in range(1, 31)]),
    })


def test_sqlserver_reads_with_top_and_keys(tmp_path):
    conn = sqlserver_conn()
    ws = binggio.Workspace.create(tmp_path / 'ws')
    c = ws.add_database('Core SQL Server', 'sqlserver', host='srv', database='core', user='u', schema='dbo',
                        row_limit=1000, connection=conn)
    sqls = [s for s, _ in conn.log]
    assert any(s.startswith('SELECT TOP (1001) * FROM [dbo].[Customers]') for s in sqls)
    assert all(s.lstrip().upper().startswith('SELECT') for s in sqls)       # read-only
    assert c['table_count'] == 2 and c['manifest']['tables']['Loans']['primary_key'] == ['LoanID']
    assert 'password' not in str(c['manifest']).lower()
    ws.profile(); ws.discover()
    from binggio.registry import store
    roles = {(r['table'], r['column']): r['candidate_role'] for r in store.list_roles(ws.root, limit=200)['items']}
    assert roles[('Customers', 'CustomerID')] == 'PRIMARY_KEY_CANDIDATE'   # declared in the database
    assert roles[('Customers', 'Since')] == 'DATE'
    edges = store.list_relationships(ws.root, limit=200)['items']
    assert any(e['child_table'] == 'Loans' and e['parent_table'] == 'Customers' for e in edges)
    text = (ws.root / 'data/sources/core_sql_server/Loans.csv').read_text()
    assert ',1000.5\n' in text.replace('\r', '')


def test_oracle_sql_shape():
    d = db.dialect('oracle')
    assert d.select_sql('bank', 'LOANS', 10) == 'SELECT * FROM "BANK"."LOANS" FETCH FIRST 10 ROWS ONLY'
    sql, params = d.list_tables_sql('bank')
    assert 'all_tables' in sql and params == ('BANK',)
    conn = FakeConn({'all_tables': (['OWNER', 'TABLE_NAME'], [('BANK', 'LOANS')]),
                     'all_constraints': (['TABLE_NAME', 'COLUMN_NAME'], [('LOANS', 'LOAN_ID')]),
                     '"BANK"."LOANS"': (['LOAN_ID', 'OPENED'], [(1, dt.datetime(2024, 5, 1)), (2, dt.datetime(2024, 5, 2, 13, 5))])})
    out = db.read_tables('oracle', {}, schema='bank', connection=conn)
    headers, rows, info = out['LOANS']
    assert info['primary_key'] == ['LOAN_ID'] and len(rows) == 2


def test_row_limit_flags_truncation(tmp_path):
    src = tmp_path / 'src.db'
    con = sqlite3.connect(src)
    con.execute('CREATE TABLE accounts (account_id TEXT PRIMARY KEY, balance REAL)')
    con.executemany('INSERT INTO accounts VALUES (?,?)', [('A%04d' % i, i) for i in range(50)])
    con.commit(); con.close()
    ws = binggio.Workspace.create(tmp_path / 'ws')
    c = ws.add_database('Small', 'sqlite', database=str(src), row_limit=20)
    assert c['manifest']['tables']['accounts']['truncated_at_row_limit'] is True
    assert any('cut at the row limit' in n for n in c['notes'])
    c2 = ws.add_database('Full', 'sqlite', database=str(src))
    assert c2['manifest']['tables']['accounts']['rows'] == 50 and c2['manifest']['tables']['accounts']['primary_key'] == ['account_id']


def test_errors_are_plain():
    with pytest.raises(ConnectorError, match='Unsupported database type'):
        db.dialect('db2')
    conn = FakeConn({'INFORMATION_SCHEMA.TABLES': (['A', 'B'], [('dbo', 'X')])})
    with pytest.raises(ConnectorError, match='Not found: y'):
        db.read_tables('sqlserver', {}, schema='dbo', tables=['Y'], connection=conn)


def test_refresh_keeps_unchanged_approvals(tmp_path):
    src = tmp_path / 'src.db'
    con = sqlite3.connect(src)
    con.execute('CREATE TABLE a (a_id TEXT PRIMARY KEY, v REAL)'); con.execute('CREATE TABLE b (b_id TEXT PRIMARY KEY, v REAL)')
    con.executemany('INSERT INTO a VALUES (?,?)', [('A%d' % i, i) for i in range(10)])
    con.executemany('INSERT INTO b VALUES (?,?)', [('B%d' % i, i) for i in range(10)])
    con.commit(); con.close()
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_database('Src', 'sqlite', database=str(src))['id']
    ws.profile()
    from binggio.registry import store
    for t in ('a', 'b'):
        store.decide_column(ws.root, cid, t, t + '_id', action='approve', actor='T', note='t', expected_revision=0, role='PRIMARY_KEY_CANDIDATE')
    con = sqlite3.connect(src); con.execute("INSERT INTO b VALUES ('B99', 99)"); con.commit(); con.close()
    r = ws.refresh(cid)
    assert r['changed_tables'] == ['b'] and r['unchanged_tables'] == ['a']
    st = {x['subject_key'].split('::')[1]: x['effective_status'] for x in store._rows(ws.root, 'column_role')}
    assert st == {'a': 'approved', 'b': 'stale'}
