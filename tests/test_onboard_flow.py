# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""First-run flow: add a source, accept the suggestions, add a term for a coded value, ask."""
from pathlib import Path
import binggio
from binggio import onboard
from binggio.registry import store
from conftest import full_plan

FIX = Path(__file__).parent / 'fixtures' / 'collections'


def test_onboard_then_ask_with_new_term(tmp_path):
    ws = binggio.Workspace.demo(tmp_path / 'bank', locked=False)
    cid = ws.add_csv_folder('Collections system', FIX)['id']
    ws.profile(); ws.discover()
    props = onboard.proposals(ws.root, cid)
    assert 'borrower_profile' not in props['roles']                       # the duplicate copy is not proposed
    out = onboard.apply(ws.root, props, 'Test Steward')
    assert out['roles'] >= 40 and out['links'] >= 10 and out['concepts'] >= 7
    assert any('customer_kyc_profiles' in x for x in out['left_pending'])  # the weak alternative stays pending
    assert store.summary(ws.root)['registry_status']['stale'] == 0
    onboard.create_term(ws.root, cid, 'promises_to_pay', 'kept', 'N', ['broken promise', 'promise not kept'], 'Test Steward', 'test')
    st = ws.status()['answerable']
    assert st['terms'] == 6
    from binggio.context.context import approved_context
    ctx = approved_context(ws.root)
    broken = next(k for k, v in ctx['governed_terms'].items() if v['name'] == 'Broken promise')
    delinquent = next(k for k, v in ctx['governed_terms'].items() if v['name'] == 'Delinquent loan')
    LA = 'conn_31ca3d841ed240::loan_accounts'
    ans = ws.ask('Which delinquent loans have an OPEN collection case and a broken promise?', plan=full_plan({
        'entity': LA, 'select': [LA + '::loan_id'],
        'terms': [{'term': delinquent, 'negate': False}, {'term': broken, 'negate': False}],
        'exists': [{'table': cid + '::collection_cases', 'filters': [{'field': cid + '::collection_cases::case_status', 'op': 'eq', 'value': 'OPEN'}], 'negate': False}]}))
    assert ans.get('status') != 'refused', ans
    assert sorted(r[0] for r in ans['rows'])[:5] == ['LN0015', 'LN0022', 'LN0026', 'LN0040', 'LN0041'] and len(ans['rows']) == 11
    assert {t['term'] for t in ans['business_terms']} == {'Broken promise', 'Delinquent loan'}
