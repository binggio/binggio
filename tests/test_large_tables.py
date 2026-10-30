# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Answers read whole tables, not the 2,500-row profiling sample."""
import csv
import pytest
import binggio
from binggio.registry import store
from conftest import full_plan

N_LOANS, N_CUST = 60_000, 3_000


@pytest.fixture(scope='module')
def big(tmp_path_factory):
    root = tmp_path_factory.mktemp('big')
    src = root / 'export'; src.mkdir()
    with open(src / 'customers.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['customer_id', 'segment'])
        for i in range(N_CUST): w.writerow(['C%05d' % i, ['RETAIL', 'PREMIUM', 'CORPORATE'][i % 3]])
    with open(src / 'loans.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['loan_id', 'customer_id', 'balance', 'status'])
        for i in range(N_LOANS): w.writerow(['L%06d' % i, 'C%05d' % (i % N_CUST), i % 1000, ['OPEN', 'CLOSED'][i % 4 == 0]])
    ws = binggio.Workspace.create(root / 'ws')
    cid = ws.add_csv_folder('Big', src)['id']
    ws.profile(); ws.discover()
    for t, c, r in [('customers', 'customer_id', 'PRIMARY_KEY_CANDIDATE'), ('customers', 'segment', 'CLASSIFICATION'),
                    ('loans', 'loan_id', 'PRIMARY_KEY_CANDIDATE'), ('loans', 'customer_id', 'FOREIGN_KEY_CANDIDATE'),
                    ('loans', 'balance', 'MEASURE'), ('loans', 'status', 'STATUS')]:
        store.decide_column(ws.root, cid, t, c, action='approve', actor='T', note='t', expected_revision=0, role=r)
    e = next(x for x in store.list_relationships(ws.root, limit=200)['items'] if x['child_table'] == 'loans')
    store.decide_relationship(ws.root, e['edge_id'], action='approve', actor='T', note='t', expected_revision=0, cardinality='many_to_one')
    return ws, (lambda t: cid + '::' + t)


def test_link_found_beyond_profile_sample(big):
    ws, T = big
    e = [x for x in store.list_relationships(ws.root, limit=200)['items'] if x['child_table'] == 'loans' and x['parent_table'] == 'customers']
    assert e and e[0]['evidence']['distinct_coverage_pct'] == 100.0  # all 3,000 customers seen, not the first 2,500 rows


def test_aggregates_cover_every_row(big):
    ws, T = big
    ans = ws.ask('count and total balance of OPEN loans by segment', plan=full_plan({
        'entity': T('loans'), 'filters': [{'field': T('loans') + '::status', 'op': 'eq', 'value': 'OPEN'}],
        'aggregate': [{'fn': 'count', 'field': None, 'alias': 'n'}, {'fn': 'sum', 'field': T('loans') + '::balance', 'alias': 'total'}],
        'group_by': [T('customers') + '::segment']}))
    got = {r[0]: (r[1], r[2]) for r in ans['rows']}
    exp = {}
    for i in range(N_LOANS):
        if i % 4:
            s = ['RETAIL', 'PREMIUM', 'CORPORATE'][(i % N_CUST) % 3]
            n, t = exp.get(s, (0, 0)); exp[s] = (n + 1, t + i % 1000)
    assert {k: (v[0], float(v[1])) for k, v in got.items()} == {k: (v[0], float(v[1])) for k, v in exp.items()}


def test_long_lists_show_200_and_say_how_many(big):
    ws, T = big
    ans = ws.ask('Which loans are OPEN?', plan=full_plan({'entity': T('loans'), 'select': [T('loans') + '::loan_id'],
        'filters': [{'field': T('loans') + '::status', 'op': 'eq', 'value': 'OPEN'}]}))
    assert len(ans['rows']) == 200 and ans['row_count'] == N_LOANS * 3 // 4 and ans['truncated_for_display']
    assert 'Showing the first 200 of 45000 rows' in ans['note']
    follow = ws.ask('Which of these have a balance above 900?', plan=full_plan({'entity': T('loans'), 'select': [T('loans') + '::loan_id'], 'cohort': 'active'}))
    assert follow['status'] == 'refused'  # the partial list is never reused as if it were complete
