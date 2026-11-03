# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Steward tools in the app: review detail panels, definitions, issues, automatic review, path finder."""
import pytest
import binggio
from binggio import issues
from binggio.ui.server import App, ApiError, api

BAD_PLAN = {'entity': 'csv_clean::customer_master', 'select': ['csv_clean::customer_master::CHURN_RISK'], 'filters': [], 'where': None,
            'exists': [], 'aggregate': [], 'group_by': [], 'sort': [], 'limit': 50, 'cohort': None, 'latest': None, 'terms': []}


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    return App(binggio.Workspace.demo(tmp_path_factory.mktemp('stw') / 'bank'))


def call(app, method, path, q=None, body=None):
    return api(app, method, path, q or {}, body or {})


def test_relationship_detail_and_single_decision(app):
    rel = call(app, 'GET', '/api/review/relationships', {'status': 'pending'})['items'][0]
    d = call(app, 'GET', '/api/review/relationship', {'edge': rel['edge_id']})
    assert d['status'] == 'pending' and d['revision'] == 0 and d['evidence']['coverage_pct'] is not None
    assert d['child']['column'] == rel['child_column'] and d['risks'] and isinstance(d['child']['samples'], list)
    with pytest.raises(ApiError, match='rationale'):
        call(app, 'POST', '/api/review/relationship', body={'edge_id': rel['edge_id'], 'action': 'approve', 'by': 'Steward', 'note': ' '})
    call(app, 'POST', '/api/review/relationship', body={'edge_id': rel['edge_id'], 'action': 'approve', 'by': 'Steward', 'note': 'Checked',
                                                         'expected_revision': 0, 'cardinality': 'many_to_one', 'join_note': 'each row has one parent'})
    d = call(app, 'GET', '/api/review/relationship', {'edge': rel['edge_id']})
    assert d['status'] == 'approved' and d['revision'] == 1 and d['decision']['approved']['join_note'] == 'each row has one parent'
    assert d['history'] and d['history'][0]['actor'] == 'Steward'
    with pytest.raises(Exception):   # stale revision is refused, never overwritten
        call(app, 'POST', '/api/review/relationship', body={'edge_id': rel['edge_id'], 'action': 'revoke', 'by': 'S', 'note': 'x', 'expected_revision': 0})


def test_role_detail(app):
    r = call(app, 'GET', '/api/review/roles', {'status': 'pending', 'limit': 1})['items'][0]
    d = call(app, 'GET', '/api/review/role', {'connection': r['connection'], 'table': r['table'], 'column': r['column']})
    assert d['column'] == r['column'] and d['suggested_role'] and d['checks'] and 'MEASURE' in d['roles']


def test_definitions_draft_then_approve(app):
    d = call(app, 'GET', '/api/review/definitions')
    assert d['measures'] and not any(x['name'] == 'Revenue total' for x in d['items'])   # junk sample entry removed
    assert all(x['status'] == 'approved' for x in d['items'] if x['name'] in ('Outstanding exposure', 'Transaction Count'))
    m = d['measures'][0]
    r = call(app, 'POST', '/api/review/definitions', body={'action': 'create', 'by': 'Steward', 'note': 'drafted',
              'definition': {'name': 'Test total', 'description': 'A test', 'aggregation': 'SUM', 'connection': m['connection'], 'table': m['table'], 'column': m['column']}})
    call(app, 'POST', '/api/review/definitions', body={'id': r['id'], 'action': 'approve', 'by': 'Steward', 'note': 'checked'})
    assert next(x for x in call(app, 'GET', '/api/review/definitions')['items'] if x['id'] == r['id'])['status'] == 'approved'


def test_refusals_become_issues(app):
    ws = app.ws
    for _ in range(2):
        a = ws.ask('Which customers are likely to churn?', plan=BAD_PLAN)
        assert a['status'] == 'refused' and a['issue']
    open_ = call(app, 'GET', '/api/issues')
    assert open_['counts']['open'] == 1 and open_['items'][0]['times'] == 2
    assert call(app, 'GET', '/api/overview')['issues'] == 1
    with pytest.raises(ApiError, match='note'):
        call(app, 'POST', '/api/issues', body={'id': a['issue'], 'action': 'resolve', 'by': 'Steward', 'note': ''})
    call(app, 'POST', '/api/issues', body={'id': a['issue'], 'action': 'dismiss', 'by': 'Steward', 'note': 'Not a supported question'})
    assert call(app, 'GET', '/api/issues')['counts'] == {'open': 0, 'resolved': 0, 'dismissed': 1}
    assert issues.record(ws.root, 'q', {'message': 'Canonical interpretation unavailable: no API key'}) is None


def test_automatic_review_starts_off(app):
    s = call(app, 'GET', '/api/autonomy')['status']
    assert s['enabled'] is False
    with pytest.raises(Exception):
        call(app, 'POST', '/api/autonomy', body={'action': 'run'})
    with pytest.raises(Exception):
        call(app, 'POST', '/api/autonomy', body={'action': 'enable', 'confirmation': 'yes'})
    p = call(app, 'GET', '/api/autonomy/preview')
    assert 'eligible_roles' in p['counts'] and all(not x['source'].startswith('conn_') for x in p['eligible_examples'])


def test_path_finder(app):
    g = call(app, 'GET', '/api/graph')
    e = next(x for x in g['edges'] if x['status'] == 'approved')
    r = call(app, 'GET', '/api/paths', {'from': e['source'], 'to': e['target']})
    assert r['paths'] and r['paths'][0]['hops'] == 1 and r['paths'][0]['all_approved']
    with pytest.raises(ApiError):
        call(app, 'GET', '/api/paths', {'from': e['source'], 'to': e['source']})
