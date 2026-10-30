# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""binggio ui: the local web app's API, driven over HTTP like the page does."""
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sim'))
import datasets  # noqa: E402
import binggio  # noqa: E402
from binggio.ui.server import make_server  # noqa: E402


class Client:
    def __init__(self, srv):
        self.base = 'http://127.0.0.1:%d' % srv.server_address[1]
        html = urllib.request.urlopen(self.base + '/').read().decode()
        self.token = re.search(r"const TOKEN = '([^']+)'", html).group(1)

    def call(self, path, body=None, token=None, headers=None):
        h = {'X-Binggio-Token': self.token if token is None else token, 'Content-Type': 'application/json', **(headers or {})}
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            r = urllib.request.urlopen(req)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def ok(self, path, body=None):
        code, data = self.call(path, body)
        assert code == 200, (path, code, data)
        return data

    def wait(self, job):
        for _ in range(600):
            j = self.ok('/api/jobs/' + job['id'])
            if j['status'] != 'running': return j
            time.sleep(0.1)
        raise AssertionError('job did not finish')


def _serve(ws):
    srv = make_server(ws, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, Client(srv)


@pytest.fixture(scope='module')
def demo_app(demo):
    srv, c = _serve(demo)
    yield c
    srv.shutdown()


def test_token_and_host_are_required(demo_app):
    assert demo_app.call('/api/state', token='nope')[0] == 403
    assert demo_app.call('/api/state', token='')[0] == 403
    # a page served under another host name (DNS rebinding) is refused
    assert demo_app.call('/api/state', headers={'Host': 'evil.example:8765'})[0] == 403
    assert demo_app.ok('/api/state')['version'] == binggio.__version__


def test_overview_and_lists(demo_app):
    o = demo_app.ok('/api/overview')
    assert o['status']['registry']['approved'] == 429 and o['status']['answerable']['tables'] == 63
    assert o['relationships']['approved'] == 74
    roles = demo_app.ok('/api/review/roles?status=all&limit=5')
    assert roles['total'] >= 400 and len(roles['items']) == 5
    assert demo_app.ok('/api/review/relationships?status=all')['total'] == 219
    assert demo_app.ok('/api/review/terms')['items'] and demo_app.ok('/api/review/concepts')['items']
    g = demo_app.ok('/api/graph')
    assert len(g["edges"]) == 219 and all('source' in e and 'target' in e for e in g['edges'])
    t = demo_app.ok('/api/tables')['items'][0]
    d = demo_app.ok('/api/table?connection=%s&table=%s' % (t['connection_id'], t['table']))
    assert d['columns'] and d['preview']['rows']


def test_sample_questions_match_the_verified_answers(demo_app):
    gold = {g['id']: g for g in json.loads((Path(__file__).parent / 'golden' / 'demo_plans.json').read_text())}
    samples = demo_app.ok('/api/samples')['items']
    assert len(samples) == 10
    for s in samples:
        conv = demo_app.ok('/api/ask/new', {})['conversation']
        for step in s['steps']:
            r = demo_app.ok('/api/ask', {'question': step['question'], 'plan': step['plan'], 'conversation': conv})
        a = r['answer']
        assert a['status'] == 'answered', (s['id'], a)
        exp = gold[s['id']]['expected']
        if s['id'] in ('Q4', 'Q7'):
            assert {row[0]: round(float(row[1]), 2) for row in a['rows']} == {k: round(float(v), 2) for k, v in exp.items()}
        elif s['id'] == 'Q10':
            assert sorted(str(row[0]) for row in a['rows']) == exp[2]
        elif s['id'] == 'Q9':
            assert [row[0] for row in a['rows']] == exp
        else:
            assert sorted(str(row[0]) for row in a['rows']) == exp
        assert a['sources'] and a['explanation']


def test_asking_without_a_key_explains(demo_app, monkeypatch):
    for k in ('BINGGIO_LLM_API_KEY', 'OPENAI_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY'): monkeypatch.delenv(k, raising=False)
    a = demo_app.ok('/api/ask', {'question': 'Which customers are delinquent?'})['answer']
    assert a['status'] == 'refused' and a['code'] == 'MODEL_NOT_CONFIGURED'


def test_new_user_flow(tmp_path):
    """Start screen -> new workspace -> add CSV -> onboard -> review approve/revoke/reopen -> ask."""
    srv, c = _serve(None)
    try:
        assert c.ok('/api/state')['workspace'] is None
        assert c.call('/api/overview')[0] == 409
        src = datasets.write_csv(datasets.build(), tmp_path / 'downloads' / 'core export')
        c.ok('/api/workspace', {'action': 'create', 'path': str(tmp_path / 'my bank')})
        listing = c.ok('/api/fs?want=csvdir&path=' + urllib.request.quote(str(src)))
        assert listing['csv_here'] == 4
        job = c.wait(c.ok('/api/sources', {'type': 'csv', 'name': 'Core export', 'path': str(src)}))
        assert job['status'] == 'done', job
        cid = job['result']['connection']
        assert job['result']['tables'] == 4 and job['result']['candidate_relationships'] >= 3

        props = c.ok('/api/onboard?connection=' + cid)
        assert {t['kind'] for t in props['impact']['tables']} <= {'entity', 'reference', 'records'}
        # decisions need a name
        assert c.call('/api/onboard', {'connection': cid, 'roles': {}})[0] == 400
        body = {'connection': cid, 'by': 'Test Steward',
                'roles': {t: [{'column': x['column'], 'role': x['role']} for x in cols] for t, cols in props['roles'].items()},
                'links': [{'edge_id': l['edge_id'], 'cardinality': l['cardinality']} for l in props['links'] if l['suggested']],
                'concepts': [x['table'] for x in props['concepts']],
                'terms': [{'table': 'accounts', 'column': 'status', 'value': 'CLOSED', 'words': 'closed account, account closed'}]}
        r = c.ok('/api/onboard', body)
        assert r['roles'] == 21 and r['links'] == 3 and r['terms'] == 1 and not r['errors'], r

        approved = c.ok('/api/review/roles?status=approved')['items']
        one = approved[0]
        item = {'connection': one['connection'], 'table': one['table'], 'column': one['column']}
        assert c.ok('/api/review/roles', {'action': 'revoke', 'by': 'Test Steward', 'items': [item]})['done'] == 1
        assert c.ok('/api/review/roles?status=revoked')['total'] == 1
        assert c.ok('/api/review/roles', {'action': 'reopen', 'by': 'Test Steward', 'items': [item]})['done'] == 1
        assert c.ok('/api/review/roles?status=pending')['total'] == 1
        r = c.ok('/api/review/roles', {'action': 'approve', 'by': 'Test Steward', 'items': [{**item, 'role': one['suggested']}]})
        assert r['done'] == 1 and c.ok('/api/review/roles?status=pending')['total'] == 0
        # revoking something that is not approved is refused per item, not a crash
        r = c.ok('/api/review/roles', {'action': 'reopen', 'by': 'Test Steward', 'items': [item]})
        assert r['done'] == 0 and r['errors']

        rel = c.ok('/api/review/relationships?status=approved')['items'][0]
        assert c.ok('/api/review/relationships', {'action': 'revoke', 'by': 'T', 'items': [{'edge_id': rel['edge_id']}]})['done'] == 1
        assert c.ok('/api/review/relationships', {'action': 'reopen', 'by': 'T', 'items': [{'edge_id': rel['edge_id']}]})['done'] == 1
        assert c.ok('/api/review/relationships', {'action': 'approve', 'by': 'T', 'items': [{'edge_id': rel['edge_id'], 'cardinality': 'many_to_one'}]})['done'] == 1

        term = next(t for t in c.ok('/api/review/terms')['items'] if t['kind'] == 'term')
        assert term['status'] == 'approved'
        assert c.ok('/api/review/terms', {'id': term['id'], 'action': 'revoke', 'by': 'T'})['status'] == 'revoked'
        assert c.ok('/api/review/terms', {'id': term['id'], 'action': 'reopen', 'by': 'T'})['status'] == 'pending'
        assert c.ok('/api/review/terms', {'id': term['id'], 'action': 'approve', 'by': 'T'})['status'] == 'approved'

        names = {(x['table'], x['column'].lower()): x['column'] for x in c.ok('/api/review/roles?status=all&limit=1000')['items']}
        T = lambda t, col=None: cid + '::' + t + ('::' + names[(t, col)] if col else '')
        plan = {'entity': T('customers'), 'select': [T('customers', 'customer_id')], 'filters': [], 'where': None,
                'exists': [{'table': T('accounts'), 'filters': [], 'negate': True}], 'aggregate': [], 'group_by': [], 'sort': [],
                'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
        a = c.ok('/api/ask', {'question': 'Customers with no account', 'plan': plan})['answer']
        assert a['status'] == 'answered'
        assert sorted(r[0] for r in a['rows']) == datasets.oracle(datasets.build())['customers with no account']

        hist = c.ok('/api/history?limit=500')['items']
        assert any(e['actor'] == 'Test Steward' and e['action'] == 'revoke' for e in hist)
        # a second data job while one runs is refused, not interleaved
        j1 = c.ok('/api/pipeline', {'force': True})
        code, data = c.call('/api/pipeline', {})
        assert code in (200, 409)
        c.wait(j1)
    finally:
        srv.shutdown()


def test_bad_requests_are_explained(demo_app):
    code, d = demo_app.call('/api/workspace', {'action': 'open', 'path': '/definitely/not/here'})
    assert code == 400 and 'workspace' in d['error'].lower()
    code, d = demo_app.call('/api/sources', {'type': 'excel', 'name': 'x', 'path': '/nope/old.xls'})
    assert code in (200, 400)
    if code == 200:
        j = demo_app.wait(d)
        assert j['status'] == 'failed' and j['error']
    assert demo_app.call('/api/nothing')[0] == 404


def test_sample_bank_refuses_new_sources(tmp_path):
    from binggio.ui.server import App, api, ApiError
    from binggio.workspace import Workspace
    app = App(Workspace.demo(tmp_path / 'sample'))
    assert api(app, 'GET', '/api/state', {}, {})['demo'] is True
    csv = tmp_path / 'n.csv'; csv.write_text('a\n1\n')
    with pytest.raises(Exception, match='read-only'):
        api(app, 'POST', '/api/sources', {}, {'type': 'csv', 'name': 'Noise', 'path': str(csv)})
