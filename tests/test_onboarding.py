# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Adding a 10-table collections system to the demo bank (the scenario that found the v19.4 gaps).

The source has: cases, actions, agents, promises to pay, recoveries, settlements, legal notices,
collateral, a 1:1 contacts extension of the customer table, a full copy of the customer table
under other column names, and some messy loan IDs (" ln0005 ")."""
from pathlib import Path
import pytest
import binggio
from binggio.registry import store, autonomy
from binggio.discovery import store as graph
from binggio import impact

FIX = Path(__file__).parent / 'fixtures' / 'collections'


@pytest.fixture(scope='module')
def onboarded(tmp_path_factory):
    ws = binggio.Workspace.demo(tmp_path_factory.mktemp('onb') / 'bank', locked=False)
    before = store.summary(ws.root)['registry_status']
    c = ws.add_csv_folder('Collections system', FIX)
    assert ws.profile()['failures'] == []
    ws.discover()
    return ws, c['id'], before


def edges_of(ws, cid):
    return {(e['child_table'], e['child_column'], e['parent_table'], e['parent_column']): e
            for e in graph.all_edges(ws.root) if cid in (e['child_connection_id'], e['parent_connection_id'])}


def test_existing_approvals_untouched(onboarded):
    ws, cid, before = onboarded
    after = store.summary(ws.root)['registry_status']
    assert after == before and after['stale'] == 0


def test_every_real_link_found_and_no_noise(onboarded):
    ws, cid, _ = onboarded
    got = set(edges_of(ws, cid))
    must = {('collection_cases', 'loan_ref', 'loan_accounts', 'loan_id'),
            ('collection_cases', 'cust_no', 'customer_master', 'CUSTOMER_ID'),
            ('collection_cases', 'assigned_agent', 'collection_agents', 'agent_id'),
            ('promises_to_pay', 'case_id', 'collection_cases', 'case_id'),
            ('collection_actions', 'case_id', 'collection_cases', 'case_id'),
            ('legal_notices', 'case_id', 'collection_cases', 'case_id'),
            ('recoveries', 'loan_id', 'loan_accounts', 'loan_id'),
            ('settlements', 'loan_id', 'loan_accounts', 'loan_id'),
            ('loan_collateral', 'loan_id', 'loan_accounts', 'loan_id'),
            ('customer_contacts', 'customer_id', 'customer_master', 'CUSTOMER_ID'),
            ('borrower_profile', 'CUST_NO', 'customer_master', 'CUSTOMER_ID')}
    assert must <= got, must - got
    assert not any(p == 'customer_contacts' for _, _, p, _ in got), 'extension table offered as a parent'
    assert not any(p == 'loan_collateral' for _, _, p, _ in got), 'collateral offered as the loan table'
    assert len(got) <= len(must) + 2
    assert edges_of(ws, cid)[('customer_contacts', 'customer_id', 'customer_master', 'CUSTOMER_ID')]['evidence']['cardinality_hint'] == 'one_to_one'


def test_key_suggestions(onboarded):
    ws, cid, _ = onboarded
    roles = {(r['table'], r['column']): r['candidate_role'] for r in store.list_roles(ws.root, connection_id=cid, limit=200)['items']}
    pk = {t: c for (t, c), r in roles.items() if r == 'PRIMARY_KEY_CANDIDATE'}
    assert pk == {'collection_cases': 'case_id', 'collection_actions': 'action_id', 'collection_agents': 'agent_id',
                  'promises_to_pay': 'ptp_id', 'recoveries': 'recovery_id', 'settlements': 'settlement_id',
                  'legal_notices': 'notice_id', 'loan_collateral': 'collateral_id', 'customer_contacts': 'customer_id',
                  'borrower_profile': 'CUST_NO'}
    assert roles[('collection_cases', 'cust_no')] == 'FOREIGN_KEY_CANDIDATE'
    assert roles[('loan_collateral', 'loan_id')] == 'FOREIGN_KEY_CANDIDATE'


def test_review_list_shows_every_candidate(onboarded):
    ws, cid, _ = onboarded
    assert store.list_relationships(ws.root, limit=1)['total'] == len(graph.all_edges(ws.root))
    assert store.summary(ws.root)['candidate_relationships'] == len(graph.all_edges(ws.root))


def test_auto_approval_never_takes_one_to_one(onboarded):
    ws, cid, _ = onboarded
    e = edges_of(ws, cid)[('customer_contacts', 'customer_id', 'customer_master', 'CUSTOMER_ID')]
    from binggio.profiling import store as profiles
    cp = profiles.get_table(ws.root, e['child_connection_id'], e['child_table'])
    pp = profiles.get_table(ws.root, e['parent_connection_id'], e['parent_table'])
    reasons = autonomy._edge_check({**e, 'freshness': 'current'}, cp, pp, False)
    assert any('One-to-one' in r for r in reasons)


def test_impact_report(onboarded):
    ws, cid, _ = onboarded
    r = impact.report(ws.root, cid)
    kinds = {t['table']: t['kind'] for t in r['tables']}
    assert kinds['borrower_profile'] == 'duplicate' and kinds['customer_contacts'] == 'extends'
    assert kinds['collection_agents'] == 'reference'
    assert {kinds[t] for t in ('collection_cases', 'recoveries', 'settlements', 'loan_collateral', 'promises_to_pay')} == {'entity'}
    kept = next(t for t in r['tables'] if t['table'] == 'promises_to_pay')['coded_columns']
    assert {'column': 'kept', 'values': ['N', 'Y']} in kept
    assert next(t for t in r['tables'] if t['table'] == 'customer_contacts')['suggested_concept']['as'] == 'secondary (enrichment)'
    assert 'DUPLICATE' in impact.format_report(r)


def test_discovery_handles_many_tables(tmp_path):
    import csv
    src = tmp_path / 'wide'
    src.mkdir()
    for i in range(130):
        with open(src / ('t%03d.csv' % i), 'w', newline='') as f:
            w = csv.writer(f); w.writerow(['t%03d_id' % i, 'customer_id', 'amount'])
            for r in range(5): w.writerow(['%d-%d' % (i, r), 'C%03d' % r, r])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    ws.add_csv_folder('Wide', src)
    assert ws.profile()['failures'] == []
    ws.discover()  # previously refused above 100 tables
    assert ws.status()['tables'] == 130
