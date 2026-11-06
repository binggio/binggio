# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""0.1.4 steward tools: data fixes, key checks, links a person adds, what discovery tried,
whole-table parent checks and the answer cache."""
import csv
import hashlib
import random
from pathlib import Path

import binggio
from binggio import prepared, links
from binggio.discovery import store as graph
from binggio.discovery import discovery
from binggio.profiling import store as profiles
from binggio.registry import store
from conftest import full_plan


def _write(folder, name, header, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / (name + '.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)


def _bank(tmp_path, n_customers=400):
    random.seed(11)
    src = tmp_path / 'src'
    _write(src, 'customers', ['customer_id', 'segment'], [('C%05d' % i, random.choice(['RETAIL', 'SME'])) for i in range(n_customers)])
    _write(src, 'kyc_reviews', ['review_id', 'cust_ref', 'outcome'],
           [('R%05d' % i, 'CUST-%05d' % random.randrange(n_customers), random.choice(['PASS', 'FAIL'])) for i in range(300)])
    rows = [('L%05d' % i, 'C%05d' % random.randrange(n_customers), '$%s' % format(random.randint(100, 90000), ','), '' if i % 9 == 0 else 'OPEN') for i in range(500)]
    rows += [rows[3], rows[7]]                                    # two exact duplicate rows at the end
    _write(src, 'loan_book', ['loan_no', 'customer_id', 'principal', 'loan_status'], rows)
    _write(src, 'staff', ['staff_id', 'site'], [('S%03d' % i, 'C%05d' % random.randrange(n_customers)) for i in range(120)])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('Bank', src)['id']
    ws.profile(); ws.discover()
    return ws, cid, src


def _conn(ws, cid):
    return next(c for c in ws.connections() if c['id'] == cid)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_fixes_change_what_is_read_never_the_source(tmp_path):
    ws, cid, src = _bank(tmp_path)
    conn = _conn(ws, cid)
    before = _sha(ws.root / 'data' / conn['source_path'] / 'kyc_reviews.csv')
    fp0 = profiles.current_fingerprint(ws.root, conn, 'kyc_reviews')
    prepared.add_fix(ws.root, conn, 'kyc_reviews', 'strip_prefix', 'cust_ref', {'text': 'CUST-'}, 'Tester', 'export adds CUST-')
    prepared.add_fix(ws.root, conn, 'kyc_reviews', 'add_prefix', 'cust_ref', {'text': 'C'}, 'Tester', 'core banking keys start with C')
    h, rows, _ = prepared.read(ws.root, conn, 'kyc_reviews', 3)
    assert all(r[h.index('cust_ref')].startswith('C0') for r in rows)
    assert _sha(ws.root / 'data' / conn['source_path'] / 'kyc_reviews.csv') == before          # the file is untouched
    assert profiles.current_fingerprint(ws.root, conn, 'kyc_reviews') != fp0                    # approvals on it go stale
    assert profiles.get_table(ws.root, cid, 'kyc_reviews')['stale']
    profiles.run_table(ws.root, cid, 'kyc_reviews', force=True)
    ws.discover()
    assert any(e['child_table'] == 'kyc_reviews' and e['parent_table'] == 'customers' for e in graph.all_edges(ws.root))
    # removing every fix brings the original fingerprint back
    for f in prepared.list_fixes(ws.root, cid, 'kyc_reviews'):
        prepared.remove_fix(ws.root, f['id'], 'Tester', 'undo')
    assert profiles.current_fingerprint(ws.root, conn, 'kyc_reviews') == fp0
    hist = prepared.list_fixes(ws.root, cid, 'kyc_reviews', include_removed=True)
    assert len(hist) == 2 and all(f['status'] == 'removed' and f['removed_by'] == 'Tester' for f in hist)


def test_fix_validation_and_previews(tmp_path):
    ws, cid, src = _bank(tmp_path)
    conn = _conn(ws, cid)
    for bad in [dict(kind='trim', column='nope', params={}), dict(kind='combine', column='x y', params={'columns': ['loan_no', 'customer_id']}),
                dict(kind='missing', column='loan_status', params={'with': 'value', 'value': ''})]:
        try:
            prepared.add_fix(ws.root, conn, 'loan_book', bad['kind'], bad['column'], bad['params'], 'Tester', 'x')
        except prepared.FixError:
            pass
        else:
            raise AssertionError('accepted an invalid fix: %r' % bad)
    try:
        prepared.add_fix(ws.root, conn, 'loan_book', 'trim', 'loan_no', {}, 'Tester', '')
        raise AssertionError('a fix needs a reason')
    except prepared.FixError:
        pass
    p = prepared.preview_fix(ws.root, conn, 'loan_book', 'number', 'principal', {})
    assert p['changed'] > 0 and p['became_empty'] == 0 and p['examples'][0]['after'].isdigit()
    assert prepared.clean_number('$1,234.50') == '1234.5' and prepared.clean_number('(500)') == '-500' and prepared.clean_number('n/a') is None
    assert prepared.clean_number('12 kg') == '12' and prepared.clean_number('45%') == '45' and prepared.clean_number('1.2.3') is None


def test_key_check_dedupe_and_combined_key(tmp_path):
    ws, cid, src = _bank(tmp_path)
    conn = _conn(ws, cid)
    k = prepared.key_check(ws.root, conn, 'loan_book', ['loan_no'])
    assert not k['unique'] and k['duplicate_rows'] == 2 and k['rows'] == 502 and k['examples'][0]['rows'] == 2
    prepared.add_fix(ws.root, conn, 'loan_book', 'dedupe', '', {'columns': ['loan_no']}, 'Tester', 'exact duplicate rows from the export')
    assert prepared.key_check(ws.root, conn, 'loan_book', ['loan_no'])['unique']
    prepared.add_fix(ws.root, conn, 'loan_book', 'combine', 'loan_key', {'columns': ['loan_no', 'customer_id'], 'sep': '|'}, 'Tester', 'two-part key')
    h, rows, _ = prepared.read(ws.root, conn, 'loan_book', 2)
    assert h[-1] == 'loan_key' and rows[0][-1] == rows[0][0] + '|' + rows[0][1]
    prepared.add_fix(ws.root, conn, 'loan_book', 'missing', 'loan_status', {'with': 'value', 'value': 'UNKNOWN'}, 'Tester', 'blank means not recorded')
    h, rows, _ = prepared.read(ws.root, conn, 'loan_book', 20)
    assert 'UNKNOWN' in [r[h.index('loan_status')] for r in rows] and '' not in [r[h.index('loan_status')] for r in rows]
    try:
        prepared.remove_fix(ws.root, prepared.list_fixes(ws.root, cid, 'loan_book')[0]['id'], 'Tester', 'x')   # dedupe: nothing depends on it
    except prepared.FixError:
        raise AssertionError('removing a step nothing depends on should work')


def test_make_key_only_when_unique_on_every_row(tmp_path):
    from binggio.ui.server import App, api, ApiError
    ws, cid, src = _bank(tmp_path)
    app = App(ws)
    try:
        api(app, 'POST', '/api/keys/make', {}, {'connection': cid, 'table': 'loan_book', 'column': 'loan_no', 'note': 'loan number', 'by': 'Tester'})
        raise AssertionError('a repeated key was approved')
    except ApiError as e:
        assert 'repeat' in str(e)
    r = api(app, 'POST', '/api/keys/make', {}, {'connection': cid, 'table': 'customers', 'column': 'customer_id', 'note': 'customer number', 'by': 'Tester'})
    assert r['ok'] and r['check']['unique']
    role = next(x for x in store.list_roles(ws.root, connection_id=cid, table='customers', limit=100)['items'] if x['column'] == 'customer_id')
    assert role['effective_status'] == 'approved' and role['decision']['approved']['role'] == 'PRIMARY_KEY_CANDIDATE'


def test_discovery_says_where_it_failed(tmp_path):
    ws, cid, src = _bank(tmp_path)
    rep = graph.attempts(ws.root)
    kyc = next(c for c in rep['columns'] if c['table'] == 'kyc_reviews' and c['column'] == 'cust_ref')
    assert kyc['format_hint']['table'] == 'customers' and 'digits' in kyc['format_hint']['advice']
    loans = next(t for t in rep['tables'] if t['table'] == 'loan_book')
    assert loans['status'] == 'checked' and loans['linked']
    assert any('repeat' in n['why'] for n in loans['not_parent_keys'] if n['column'] == 'loan_no')   # the 2 duplicate rows
    staff = next(c for c in rep['columns'] if c['table'] == 'staff' and c['column'] == 'site')
    assert staff['best']['table'] == 'customers' and staff['best']['coverage_pct'] >= 45 and staff['best']['why']


def test_parent_keys_checked_on_all_rows(tmp_path, monkeypatch):
    # a parent table bigger than the rows discovery reads: its later keys are still found
    monkeypatch.setattr(discovery, 'DISCOVERY_ROW_LIMIT', 150)
    src = tmp_path / 'src'
    _write(src, 'accounts', ['account_id', 'product'], [('A%05d' % i, 'SAV') for i in range(600)])
    _write(src, 'payments', ['payment_id', 'account_id', 'amount'], [(i, 'A%05d' % (300 + i % 300), 5) for i in range(140)])
    ws = binggio.Workspace.create(tmp_path / 'ws'); ws.add_csv_folder('B', src); ws.profile(); ws.discover()
    e = [x for x in graph.all_edges(ws.root) if x['child_table'] == 'payments' and x['parent_table'] == 'accounts']
    assert e and e[0]['evidence']['distinct_coverage_pct'] == 100.0 and e[0]['evidence'].get('parent_checked_all_rows')


def test_links_a_person_adds(tmp_path):
    ws, cid, src = _bank(tmp_path)
    ev = links.check(ws.root, (cid, 'staff', 'site'), (cid, 'customers', 'customer_id'))
    assert ev['coverage_pct'] == 100.0 and ev['parent_unique'] and ev['suggested_cardinality'] == 'many_to_one'
    bad = links.check(ws.root, (cid, 'kyc_reviews', 'cust_ref'), (cid, 'customers', 'customer_id'))
    assert bad['matched_distinct'] == 0 and bad.get('format_hint')
    try:
        links.add(ws.root, (cid, 'kyc_reviews', 'cust_ref'), (cid, 'customers', 'customer_id'), 'Tester', 'about customers')
        raise AssertionError('a link that joins nothing was added')
    except links.LinkError:
        pass
    r = links.add(ws.root, (cid, 'staff', 'site'), (cid, 'customers', 'customer_id'), 'Tester', 'site is the customer they serve')
    eid = r['edge']['id']
    assert graph.get_edge(ws.root, eid)['evidence']['manual']
    ws.discover()                                                   # survives rediscovery
    assert graph.get_edge(ws.root, eid) is not None
    store.decide_relationship(ws.root, eid, action='approve', actor='Tester', note='checked with HR', expected_revision=0, cardinality='many_to_one')
    assert next(x for x in store.list_relationships(ws.root, limit=1000)['items'] if x['edge_id'] == eid)['effective_status'] == 'approved'
    from binggio.ui.server import App, api, ApiError
    app = App(ws)
    try:
        api(app, 'POST', '/api/links', {}, {'action': 'remove', 'edge_id': eid, 'by': 'Tester'})
        raise AssertionError('removed an approved link')
    except ApiError:
        pass
    store.decide_relationship(ws.root, eid, action='revoke', actor='Tester', note='wrong', expected_revision=1)
    api(app, 'POST', '/api/links', {}, {'action': 'remove', 'edge_id': eid, 'by': 'Tester'})
    assert graph.get_edge(ws.root, eid) is None
    ws.discover()
    assert graph.get_edge(ws.root, eid) is None                     # removed links stay removed


def test_answers_use_fixed_data_and_the_cache(tmp_path):
    ws, cid, src = _bank(tmp_path)
    conn = _conn(ws, cid)
    prepared.add_fix(ws.root, conn, 'loan_book', 'number', 'principal', {}, 'Tester', 'amounts exported with $ and commas')
    profiles.run_table(ws.root, cid, 'loan_book', force=True)
    for r in store.list_roles(ws.root, connection_id=cid, table='loan_book', limit=100)['items']:
        role = 'MEASURE' if r['column'] == 'principal' else r['candidate_role']
        store.decide_column(ws.root, cid, 'loan_book', r['column'], action='approve', actor='Tester', note='ok', expected_revision=0, role=role)
    T = cid + '::loan_book'
    plan = full_plan({'entity': T, 'aggregate': [{'fn': 'sum', 'field': T + '::principal', 'alias': 'total'}]})
    a1 = ws.ask('total principal', plan=plan)
    assert a1['status'] == 'answered', a1
    expected = 0.0
    with open(src / 'loan_book.csv') as f:
        for row in csv.DictReader(f): expected += float(row['principal'].replace('$', '').replace(',', ''))
    assert abs(a1['rows'][0][0] - expected) < 0.01
    from binggio.engine import answer_cache
    import sqlite3
    with sqlite3.connect(answer_cache.path(ws.root)) as db:
        assert db.execute('SELECT COUNT(*) FROM cached').fetchone()[0] >= 1
    assert ws.ask('total principal', plan=plan)['rows'] == a1['rows']


def test_demo_data_cannot_be_fixed(tmp_path):
    from binggio.ui.server import App, api
    from binggio.workspace import WorkspaceError
    ws = binggio.Workspace.demo(tmp_path / 'demo')
    app = App(ws)
    cid = ws.connections()[0]['id']
    t = profiles.list_profiles(ws.root, connection_id=cid)[0]['table']
    try:
        api(app, 'POST', '/api/fixes', {}, {'action': 'add', 'connection': cid, 'table': t, 'kind': 'trim', 'column': 'x', 'note': 'x', 'by': 'T'})
        raise AssertionError('the sample bank accepted a data fix')
    except WorkspaceError:
        pass
