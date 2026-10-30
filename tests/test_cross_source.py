# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""The same bank loaded as CSV, Excel and SQLite must give the same answers (part 5).

Each copy goes through the whole first-run path: add the source, profile, discover, approve the
suggested roles and links, then ask. SQL Server and Oracle run the same check on a real machine
with tools/cross_source_check.py."""
import csv
import datetime as dt
import sqlite3
import pytest
import binggio
from binggio.registry import store
from conftest import full_plan

CUSTOMERS = [('C%03d' % i, 'Customer %d' % i, ['RETAIL', 'PREMIUM', 'CORPORATE'][i % 3]) for i in range(1, 41)]
LOANS = [('L%03d' % i, 'C%03d' % (i % 40 + 1), 5000 * i, ['OPEN', 'CLOSED'][i % 5 == 0], dt.date(2025, i % 12 + 1, 1)) for i in range(1, 61)]
REPAY = [('R%04d' % i, 'L%03d' % (i % 60 + 1), ['PAID', 'MISSED', 'PAID', 'PARTIAL'][i % 4]) for i in range(1, 201)]
TABLES = {'customers': (['customer_id', 'customer_name', 'segment'], CUSTOMERS),
          'loans': (['loan_id', 'customer_id', 'balance', 'status', 'opened_on'], LOANS),
          'repayments': (['repayment_id', 'loan_id', 'payment_status'], REPAY)}
ROLES = {('customers', 'customer_id'): 'PRIMARY_KEY_CANDIDATE', ('customers', 'segment'): 'CLASSIFICATION',
         ('loans', 'loan_id'): 'PRIMARY_KEY_CANDIDATE', ('loans', 'customer_id'): 'FOREIGN_KEY_CANDIDATE',
         ('loans', 'balance'): 'MEASURE', ('loans', 'status'): 'STATUS', ('loans', 'opened_on'): 'DATE',
         ('repayments', 'repayment_id'): 'PRIMARY_KEY_CANDIDATE', ('repayments', 'loan_id'): 'FOREIGN_KEY_CANDIDATE',
         ('repayments', 'payment_status'): 'STATUS'}
LINKS = {('loans', 'customers'), ('repayments', 'loans')}


def as_csv(folder):
    folder.mkdir()
    for t, (h, rows) in TABLES.items():
        with open(folder / (t + '.csv'), 'w', newline='') as f:
            w = csv.writer(f); w.writerow(h); w.writerows([[x.isoformat() if isinstance(x, dt.date) else x for x in r] for r in rows])
    return folder


def as_excel(path):
    openpyxl = pytest.importorskip('openpyxl')
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for t, (h, rows) in TABLES.items():
        ws = wb.create_sheet(t); ws.append(h)
        for r in rows: ws.append(list(r))
    wb.save(path); return path


def as_sqlite(path):
    con = sqlite3.connect(path)
    for t, (h, rows) in TABLES.items():
        con.execute('CREATE TABLE %s (%s)' % (t, ', '.join(h)))
        con.executemany('INSERT INTO %s VALUES (%s)' % (t, ','.join('?' * len(h))), [[x.isoformat() if isinstance(x, dt.date) else x for x in r] for r in rows])
    con.commit(); con.close(); return path


def names(ws, cid):
    """lower-case name -> name as the source spells it (Oracle upper-cases unquoted names)."""
    out = {}
    for i in store.list_roles(ws.root, connection_id=cid, limit=200)['items']:
        out[i['table'].lower()] = i['table']; out[(i['table'].lower(), i['column'].lower())] = i['column']
    return out


def onboard(ws, cid):
    assert ws.profile()['failures'] == []
    ws.discover()
    items = store.list_roles(ws.root, connection_id=cid, limit=200)['items']
    for (t, c), role in ROLES.items():
        x = next(i for i in items if i['table'].lower() == t and i['column'].lower() == c)
        assert x['candidate_role'] == role, (t, c, x['candidate_role'])   # the suggestion was already right
        store.decide_column(ws.root, cid, x['table'], x['column'], action='approve', actor='T', note='t', expected_revision=0, role=role)
    edges = store.list_relationships(ws.root, limit=200)['items']
    for child, parent in LINKS:
        e = next(e for e in edges if e['child_table'].lower() == child and e['parent_table'].lower() == parent)
        store.decide_relationship(ws.root, e['edge_id'], action='approve', actor='T', note='t', expected_revision=0, cardinality='many_to_one')


def answers(ws, cid):
    n = names(ws, cid)
    T = lambda t, c=None: cid + '::' + n[t] + ('::' + n[(t, c)] if c else '')
    plans = {
        'open balance by segment': {'entity': T('loans'), 'filters': [{'field': T('loans', 'status'), 'op': 'eq', 'value': 'OPEN'}],
                                    'aggregate': [{'fn': 'sum', 'field': T('loans', 'balance'), 'alias': 't'}], 'group_by': [T('customers', 'segment')]},
        'customers with a MISSED repayment': {'entity': T('customers'), 'select': [T('customers', 'customer_id')],
                                              'exists': [{'table': T('repayments'), 'filters': [{'field': T('repayments', 'payment_status'), 'op': 'eq', 'value': 'MISSED'}], 'negate': False}]},
        'loans with no PARTIAL repayment': {'entity': T('loans'), 'select': [T('loans', 'loan_id')],
                                            'exists': [{'table': T('repayments'), 'filters': [{'field': T('repayments', 'payment_status'), 'op': 'eq', 'value': 'PARTIAL'}], 'negate': True}]},
        'top 5 loans by balance': {'entity': T('loans'), 'select': [T('loans', 'loan_id')], 'sort': [{'field': T('loans', 'balance'), 'direction': 'desc'}], 'limit': 5},
        'loans opened in 2025-06': {'entity': T('loans'), 'aggregate': [{'fn': 'count', 'field': None, 'alias': 'n'}],
                                    'filters': [{'field': T('loans', 'opened_on'), 'op': 'eq', 'value': '2025-06-01'}]},
    }
    out = {}
    for name, p in plans.items():
        a = ws.ask(name, plan=full_plan(p))
        assert a.get('status') != 'refused', (name, a)
        out[name] = sorted((r[0], round(float(r[1]), 2)) for r in a['rows']) if a['rows'] and len(a['rows'][0]) == 2 \
            else ([tuple(r) for r in a['rows']] if name.startswith('top') else sorted(tuple(r) for r in a['rows']))
    return out


def test_same_answers_from_csv_excel_and_sqlite(tmp_path):
    results = {}
    for kind in ('csv', 'excel', 'sqlite'):
        ws = binggio.Workspace.create(tmp_path / ('ws_' + kind))
        if kind == 'csv': c = ws.add_csv_folder('Bank', as_csv(tmp_path / 'csv'))
        elif kind == 'excel': c = ws.add_excel('Bank', as_excel(tmp_path / 'bank.xlsx'))
        else: c = ws.add_database('Bank', 'sqlite', database=str(as_sqlite(tmp_path / 'bank.db')))
        onboard(ws, c['id'])
        results[kind] = answers(ws, c['id'])
    assert results['csv'] == results['excel'] == results['sqlite']
    r = results['csv']
    missed = sorted({(l[1],) for l in LOANS for p in REPAY if p[1] == l[0] and p[2] == 'MISSED'})
    assert r['customers with a MISSED repayment'] == missed
    assert r['top 5 loans by balance'] == [('L060',), ('L059',), ('L058',), ('L057',), ('L056',)]
    assert r['loans opened in 2025-06'] == [(5,)]
