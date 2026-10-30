# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Test data for the simulator, with answers computed here, independently of Binggio.

The "retail bank" is deliberately messy, the way real exports are:
  * a UTF-8 byte-order mark, Windows line endings, one file with semicolons;
  * names with accents and Indian scripts, thousands separators in amounts ("1,234.50");
  * customer IDs written " c0007 " in some loan rows;
  * a column named cust_ref (not *_id) that points to customers.
The same rows are written as CSV, as an Excel workbook (with a title row above the header), and
as a SQLite database, so the three can be compared.
"""
from __future__ import annotations
import csv
import random
import sqlite3
from pathlib import Path

SEGMENTS = ['RETAIL', 'PREMIUM', 'CORPORATE']
CITIES = ['Mumbai', 'Pune', 'Bengaluru', 'Chennai', 'Kolkata']
NAMES = ['Aarav', 'Zoë', 'José', 'अनन्या', 'Rohan', 'Meera', 'Kabir', 'Élodie', 'ராஜ்', 'Ishaan']


def build(seed=7, customers=60, accounts=150, loans=80, repayments=400):
    r = random.Random(seed)
    cust = [{'customer_id': 'C%04d' % i, 'customer_name': '%s %d' % (r.choice(NAMES), i),
             'segment': SEGMENTS[i % 3], 'city': CITIES[i % 5],
             'joined': '%02d/%02d/20%02d' % (1 + i % 28, 1 + i % 12, 15 + i % 10)} for i in range(1, customers + 1)]
    # every 6th customer has no account (for "customers with no account")
    holders = [c['customer_id'] for i, c in enumerate(cust, 1) if i % 6]
    acc = [{'account_id': 'A%05d' % i, 'customer_id': holders[i % len(holders)],
            'product': r.choice(['SAVINGS', 'CURRENT', 'FD']), 'status': ['OPEN', 'OPEN', 'CLOSED', 'DORMANT'][i % 4],
            'balance': round(r.uniform(100, 250000), 2), 'opened_on': '2024-%02d-%02d' % (1 + i % 12, 1 + i % 28)}
           for i in range(1, accounts + 1)]
    lns = [{'loan_id': 'L%05d' % i, 'cust_ref': 'C%04d' % (1 + (i * 7) % customers), 'loan_type': r.choice(['HOME', 'AUTO', 'PERSONAL']),
            'amount': 50000 * (1 + i % 40), 'status': ['ACTIVE', 'ACTIVE', 'CLOSED'][i % 3]} for i in range(1, loans + 1)]
    rep = [{'repayment_id': 'R%06d' % i, 'loan_id': 'L%05d' % (1 + (i * 3) % loans), 'due_date': '2026-%02d-15' % (1 + i % 9),
            'amount_due': 2500 + (i % 7) * 500, 'payment_status': ['PAID', 'PAID', 'PAID', 'MISSED', 'PARTIAL'][i % 5]}
           for i in range(1, repayments + 1)]
    return {'customers': cust, 'accounts': acc, 'loans': lns, 'repayments': rep}


HEADERS = {'customers': ['customer_id', 'customer_name', 'segment', 'city', 'joined'],
           'accounts': ['account_id', 'customer_id', 'product', 'status', 'balance', 'opened_on'],
           'loans': ['loan_id', 'cust_ref', 'loan_type', 'amount', 'status'],
           'repayments': ['repayment_id', 'loan_id', 'due_date', 'amount_due', 'payment_status']}


def _messy_value(table, col, v, i):
    if table == 'accounts' and col == 'balance':
        return '{:,.2f}'.format(v)                        # thousands separators
    if table == 'loans' and col == 'cust_ref' and i % 9 == 0:
        return ' %s ' % v.lower()                         # " c0007 "
    return v


def write_csv(data, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    for t, rows in data.items():
        delim = ';' if t == 'repayments' else ','
        with open(folder / (t + '.csv'), 'w', newline='', encoding='utf-8-sig') as f:   # BOM
            w = csv.writer(f, delimiter=delim, lineterminator='\r\n')                    # Windows line endings
            w.writerow(HEADERS[t])
            for i, row in enumerate(rows):
                w.writerow([_messy_value(t, c, row[c], i) for c in HEADERS[t]])
    return folder


def write_excel(data, path: Path):
    import openpyxl
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for t, rows in data.items():
        ws = wb.create_sheet(t)
        ws.append(['Export of %s, generated for testing' % t]); ws.append([])
        ws.append(HEADERS[t])
        for row in rows: ws.append([row[c] for c in HEADERS[t]])
    wb.save(path)
    return path


def write_sqlite(data, path: Path):
    con = sqlite3.connect(path)
    pk = {'customers': 'customer_id', 'accounts': 'account_id', 'loans': 'loan_id', 'repayments': 'repayment_id'}
    for t, rows in data.items():
        cols = ', '.join('%s %s' % (c, 'TEXT PRIMARY KEY' if c == pk[t] else ('REAL' if c in ('balance', 'amount', 'amount_due') else 'TEXT'))
                         for c in HEADERS[t])
        con.execute('CREATE TABLE %s (%s)' % (t, cols))
        con.executemany('INSERT INTO %s VALUES (%s)' % (t, ','.join('?' * len(HEADERS[t]))), [[row[c] for c in HEADERS[t]] for row in rows])
    con.commit(); con.close()
    return path


# ------------------------------------------------------------------ independent answers

def oracle(data):
    cust = {c['customer_id']: c for c in data['customers']}
    open_by_seg = {}
    for a in data['accounts']:
        if a['status'] == 'OPEN':
            s = cust[a['customer_id']]['segment']
            open_by_seg[s] = round(open_by_seg.get(s, 0) + a['balance'], 2)
    loan_cust = {l['loan_id']: l['cust_ref'] for l in data['loans']}
    missed = sorted({loan_cust[p['loan_id']] for p in data['repayments'] if p['payment_status'] == 'MISSED'})
    holders = {a['customer_id'] for a in data['accounts']}
    no_account = sorted(c for c in cust if c not in holders)
    top_loans = [l['loan_id'] for l in sorted(data['loans'], key=lambda l: (-l['amount'], l['loan_id']))[:5]]
    balance = {}
    for a in data['accounts']:
        balance[a['customer_id']] = balance.get(a['customer_id'], 0) + a['balance']
    top10 = [c for c, _ in sorted(balance.items(), key=lambda kv: -kv[1])[:10]]
    top10_missed = sorted(set(top10) & set(missed))
    mumbai_partial = sorted({loan_cust[p['loan_id']] for p in data['repayments'] if p['payment_status'] == 'PARTIAL'}
                            & {c for c, v in cust.items() if v['city'] == 'Mumbai'})
    return {'open balance by segment': open_by_seg, 'customers with a MISSED repayment': missed,
            'customers with no account': no_account, 'top 5 loans by amount': top_loans,
            'top 10 by balance': top10, 'top 10 then MISSED': top10_missed, 'Mumbai with PARTIAL': mumbai_partial}


def plans(conn_id, names):
    """Canonical plans for the questions above; `names` maps lower-case names to spellings."""
    T = lambda t, c=None: conn_id + '::' + names[t] + ('::' + names[(t, c)] if c else '')
    D = {'select': [], 'filters': [], 'where': None, 'exists': [], 'aggregate': [], 'group_by': [], 'sort': [],
         'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
    P = lambda **k: {**D, **k}
    return {
        'open balance by segment': P(entity=T('accounts'), filters=[{'field': T('accounts', 'status'), 'op': 'eq', 'value': 'OPEN'}],
                                     aggregate=[{'fn': 'sum', 'field': T('accounts', 'balance'), 'alias': 'total'}],
                                     group_by=[T('customers', 'segment')]),
        'customers with a MISSED repayment': P(entity=T('customers'), select=[T('customers', 'customer_id')],
                                               exists=[{'table': T('repayments'), 'negate': False,
                                                        'filters': [{'field': T('repayments', 'payment_status'), 'op': 'eq', 'value': 'MISSED'}]}]),
        'customers with no account': P(entity=T('customers'), select=[T('customers', 'customer_id')],
                                       exists=[{'table': T('accounts'), 'filters': [], 'negate': True}]),
        'top 5 loans by amount': P(entity=T('loans'), select=[T('loans', 'loan_id')],
                                   sort=[{'field': T('loans', 'amount'), 'direction': 'desc'}, {'field': T('loans', 'loan_id'), 'direction': 'asc'}], limit=5),
        'Mumbai with PARTIAL': P(entity=T('customers'), select=[T('customers', 'customer_id')],
                                 filters=[{'field': T('customers', 'city'), 'op': 'eq', 'value': 'Mumbai'}],
                                 exists=[{'table': T('repayments'), 'negate': False,
                                          'filters': [{'field': T('repayments', 'payment_status'), 'op': 'eq', 'value': 'PARTIAL'}]}]),
        'conversation': [
            {'question': 'Who are the top 10 customers by total balance?',
             'plan': P(entity=T('accounts'), aggregate=[{'fn': 'sum', 'field': T('accounts', 'balance'), 'alias': 'total'}],
                       group_by=[T('customers', 'customer_id')], sort=[{'field': 'total', 'direction': 'desc'}], limit=10)},
            {'question': 'Which of these customers have a MISSED repayment?',
             'plan': P(entity=T('customers'), select=[T('customers', 'customer_id')], cohort='active',
                       exists=[{'table': T('repayments'), 'negate': False,
                                'filters': [{'field': T('repayments', 'payment_status'), 'op': 'eq', 'value': 'MISSED'}]}])}],
    }


def big_csv(folder: Path, loans=300_000, customers=20_000):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / 'customers.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['customer_id', 'segment'])
        for i in range(customers): w.writerow(['C%06d' % i, SEGMENTS[i % 3]])
    with open(folder / 'loans.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['loan_id', 'customer_id', 'amount', 'status'])
        for i in range(loans): w.writerow(['L%07d' % i, 'C%06d' % (i % customers), i % 5000, ['ACTIVE', 'CLOSED'][i % 4 == 0]])
    exp = {}
    for i in range(loans):
        if i % 4:
            s = SEGMENTS[(i % customers) % 3]; exp[s] = exp.get(s, 0) + i % 5000
    return folder, exp
