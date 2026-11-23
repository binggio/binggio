# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""0.1.7: enterprise-style extracts (a fictional retail chain: (references stored as domain/id/version, monthly snapshots, people
stored as column blocks)) and the masters a person approves for them. Random data only."""
import csv
import sys
from pathlib import Path

import pytest

import binggio
from binggio import masters, prepared
from binggio.profiling import store as profiles
from binggio.registry import store
from conftest import full_plan

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import retail_schema  # noqa: E402

STORES, STAFF, ROTA, LINKS, PRODUCTS = 'STORES-2026', 'STAFF-2026', 'STAFF_ROTA-2026', 'STORE_LINKS-2026', 'PRODUCTS-2026'
TREE = 'STORE_TREE-2026'


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    base = tmp_path_factory.mktemp('work')
    planted = retail_schema.generate(base / 'src', scale=1)
    ws = binggio.Workspace.create(base / 'ws')
    cid = ws.add_csv_folder('Work', base / 'src')['id']
    ws.profile(); ws.discover()
    return ws, cid, planted


def _cols(ws, cid, table):
    return {c['name']: c for c in profiles.get_table(ws.root, cid, table)['columns']}


def test_names_decide_what_is_never_a_measure(work):
    ws, cid, _ = work
    stores, staff, tree = _cols(ws, cid, STORES), _cols(ws, cid, STAFF), _cols(ws, cid, TREE)
    for name in ('identifierversion', 'buyeridentifierversion', 'taxcategorycode'):
        assert stores[name]['semantic_role_candidate'] != 'MEASURE', name
    assert tree['level']['semantic_role_candidate'] == 'CLASSIFICATION'           # a depth, not a quantity
    assert tree['isleaf']['semantic_role_candidate'] == 'CLASSIFICATION'          # a flag
    assert stores['supportgroupemailid']['semantic_role_candidate'] == 'TEXT'  # an email, not a key
    assert staff['emailid']['semantic_role_candidate'] == 'TEXT'
    assert staff['state']['semantic_role_candidate'] != 'STATUS'                   # a place
    assert staff['reportingmonth']['semantic_role_candidate'] == 'DATE'
    assert stores['storeopeneddatetime']['observed_type'] == 'date'                 # 2026-01-05T10:22:33.123Z
    assert staff['hiredate']['observed_type'] == 'date'                            # 05-Jan-2019
    assert any(f['code'] == 'date_as_text' for f in staff['hiredate']['findings'])
    # names where some values are numbers are text, without a parsing warning
    assert stores['storename']['observed_type'] == 'text'
    assert not any(f['code'] == 'mixed_types' for f in stores['storename']['findings'])
    # no outlier warnings on identifiers
    assert not any(f['code'] == 'numeric_outlier_candidate' for f in stores['buyeridentifierid']['findings'])


def test_groups_no_information_and_the_repeating_identifier(work):
    ws, cid, _ = work
    p = profiles.get_table(ws.root, cid, STORES)
    kinds = {}
    for g in p['groups']: kinds.setdefault(g['kind'], []).append(g['name'])
    assert {'buyer', 'storemanager', 'areamanager'} <= set(kinds['person'])
    assert 'own' in kinds['reference'] and 'assortment' in kinds['reference']
    assert 'format' in kinds['code_name']
    cols = {c['name']: c for c in p['columns']}
    assert cols['legacystoretype']['no_information'] == 'empty'
    assert cols['franchisecomment']['no_information'] == 'empty'
    own = cols['identifierid']
    assert own['own_identifier'] and own['semantic_role_candidate'] == 'IDENTIFIER'
    assert own['unique_with'] == ['identifierid', 'reportingmonth']
    assert p['counts_scope'] == 'all_rows'
    tree_p = profiles.get_table(ws.root, cid, TREE)
    assert any(g['kind'] == 'hierarchy' and len(g['columns']) == 39 for g in tree_p['groups'])
    assert _cols(ws, cid, PRODUCTS)['productidentifierid']['semantic_role_candidate'] == 'PRIMARY_KEY_CANDIDATE'


def test_whole_table_counts_on_a_large_table(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    with open(src / 'big.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['row_id', 'code'])
        for i in range(6000): w.writerow([i if i != 5999 else 3, 'C%d' % (i % 7)])   # one duplicate, past the first 2,500 rows
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('S', src)['id']; ws.profile()
    p = profiles.get_table(ws.root, cid, 'big')
    c = {x['name']: x for x in p['columns']}
    assert p['table_rows'] == 6000 and p['counts_scope'] == 'all_rows'
    assert c['row_id']['distinct_count'] == 5999 and c['row_id']['observed_rows'] == 6000
    assert c['row_id']['semantic_role_candidate'] != 'PRIMARY_KEY_CANDIDATE'        # the duplicate is seen


def test_recommended_masters(work):
    ws, cid, _ = work
    r = masters.recommend(ws.root)
    by = {p['name']: p for p in r['masters']}
    emp = by['Staff']['spec']
    assert (emp['system_of_record']['table'], emp['system_of_record']['column']) == (STAFF, 'staffid')
    members = {(m['table'], m['column']): m for m in emp['members']}
    for col in ('buyeridentifierid', 'storemanageridentifierid', 'areamanageridentifierid', 'lastactionbyidentifierid'):
        assert members[(STORES, col)]['coverage'] == 1.0 and members[(STORES, col)]['include']
    assert members[(LINKS, 'delegatestaffidentifierid')]['include']
    assert members[(STAFF, 'managerid')]['include']
    padded = members[(ROTA, 'staffid')]                     # 01234567 vs 1234567
    assert padded['needs_fix'] == 'digits' and not padded['include']
    store = by['Store']['spec']
    assert store['system_of_record']['table'] == STORES           # the table describing stores, not the links
    assert (LINKS, 'linkedstoreidentifierid') in {(m['table'], m['column']) for m in store['members']}
    assert 'Legalentity' in by and by['Legalentity']['spec'].get('system_of_record') is None
    # versions, domains and emails are never proposed as members
    for p in r['masters']:
        for m in p['spec']['members']:
            assert not m['column'].endswith(('version', 'domain')) and 'email' not in m['column']
    assert r['calendar'] and {s['table'] for s in r['snapshots']} >= {STORES, STAFF}


def test_approve_build_link_and_answer(work):
    ws, cid, planted = work
    r = masters.recommend(ws.root)
    emp = next(p for p in r['masters'] if p['name'] == 'Staff')
    try:
        masters.decide(ws.root, 'approve', '', 'x', spec=emp['spec'])
        raise AssertionError('approved without a name')
    except masters.MasterError:
        pass
    m = masters.decide(ws.root, 'approve', 'Asha', 'Buyer and store manager ids are staff ids', spec=emp['spec'])
    built = masters.build(ws.root)
    assert built['built'][0]['table'] == 'MASTER_STAFF'
    mc = masters.master_connection(ws.root, create=False)
    path = Path(ws.root) / 'data' / mc['source_path'] / 'MASTER_STAFF.csv'
    with open(path, encoding='utf-8') as f:
        rows = list(csv.reader(f))
    keys = [x[0] for x in rows[1:]]
    assert len(keys) == len(set(keys))                          # unique by construction
    assert rows[0][:3] == ['staff_id', 'in_system_of_record', 'referenced_by_columns']
    ws.profile()
    res = masters.link_members(ws.root, m, 'Asha', 'approved with the master', approve=True)
    assert not res['failed'] and len(res['added']) >= 10
    rels = [x for x in store.list_relationships(ws.root, limit=10 ** 6)['items'] if x['parent_table'] == 'MASTER_STAFF']
    assert rels and all(x['effective_status'] == 'approved' for x in rels)
    # unchanged data: building again leaves the file (and approvals) as they are
    before = path.stat().st_mtime_ns
    masters.build(ws.root)
    assert path.stat().st_mtime_ns == before
    assert all(x['effective_status'] == 'approved' for x in store.list_relationships(ws.root, limit=10 ** 6)['items'] if x['parent_table'] == 'MASTER_STAFF')
    assert masters.recommend(ws.root)['masters'] and all(p['name'] != 'Staff' for p in masters.recommend(ws.root)['masters'])


def _approve_table(ws, cid, table, roles):
    for r in store.list_roles(ws.root, connection_id=cid, table=table, limit=1000)['items']:
        if r['column'] in roles:
            store.decide_column(ws.root, cid, table, r['column'], action='approve', actor='T', note='ok', expected_revision=0, role=roles[r['column']])


def test_snapshot_tables_are_not_added_up_across_months(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    with open(src / 'stock.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['reportingmonth', 'store_id', 'stock_value'])
        for m in ('2026-01-31', '2026-02-28', '2026-03-31'):
            for d in range(5): w.writerow([m, 'S%d' % d, 100])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('S', src)['id']; ws.profile(); ws.discover()
    _approve_table(ws, cid, 'stock', {'reportingmonth': 'DATE', 'store_id': 'IDENTIFIER', 'stock_value': 'MEASURE'})
    T = cid + '::stock'
    total = full_plan({'entity': T, 'aggregate': [{'fn': 'sum', 'field': T + '::stock_value', 'alias': 'total'}]})
    assert ws.ask('total stock value', plan=total)['rows'][0][0] == 1500       # before: added across months
    snap = next(s for s in masters.recommend(ws.root)['snapshots'] if s['table'] == 'stock')
    assert snap['period_column'] == 'reportingmonth'
    masters.decide_snapshot(ws.root, cid, 'stock', 'reportingmonth', 'approve', 'T', 'one copy per month')
    a = ws.ask('total stock value', plan=total)
    assert a['status'] != 'answered' and 'snapshot' in str(a).lower()
    one = full_plan({**total, 'filters': [{'field': T + '::reportingmonth', 'op': 'eq', 'value': '2026-03-31'}]})
    a = ws.ask('total stock value in March', plan=one)
    assert a['status'] == 'answered' and a['rows'][0][0] == 500, a
    per = full_plan({**total, 'group_by': [T + '::reportingmonth']})
    assert ws.ask('total stock value by month', plan=per)['status'] == 'answered'


def test_add_date_fix_and_ambiguous_dates(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    with open(src / 'events.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['event_id', 'created_at', 'logged'])
        w.writerow(['1', '2026-01-05T10:22:33.123Z', '05/01/2026']); w.writerow(['2', '2026-02-07T00:00:00+05:30', '25/01/2026'])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('S', src)['id']
    conn = next(c for c in ws.connections() if c['id'] == cid)
    prepared.add_fix(ws.root, conn, 'events', 'add_date', 'created_at', {}, 'T', 'timestamps to dates')
    prepared.add_fix(ws.root, conn, 'events', 'add_date', 'logged', {}, 'T', 'try without a stated form')
    h, rows, _ = prepared.read(ws.root, conn, 'events', 10)
    got = [dict(zip(h, r)) for r in rows]
    assert [g['created_at_date'] for g in got] == ['2026-01-05', '2026-02-07']
    assert got[0]['created_at'] == '2026-01-05T10:22:33.123Z'             # the original column is kept
    assert [g['logged_date'] for g in got] == [None, '2026-01-25']          # 05/01/2026 is not guessed
    prepared.remove_fix(ws.root, prepared.list_fixes(ws.root, cid, 'events')[-1]['id'], 'T', 'state the form')
    pv = prepared.preview_fix(ws.root, conn, 'events', 'add_date', 'logged', {})
    assert pv['unreadable'] == 1 and 'day or month first' in pv['warning']
    pv = prepared.preview_fix(ws.root, conn, 'events', 'add_date', 'logged', {'format': '%d/%m/%Y'})
    assert pv['unreadable'] == 0 and pv['examples'][0]['after'] == '2026-01-05'


def test_older_approvals_survive_the_new_profiler(tmp_path):
    # the bundled bank was approved on the previous evidence rules; the move to 0.1.7 keeps every approval
    ws = binggio.Workspace.demo(tmp_path / 'bank', locked=False)
    before = store.summary(ws.root)['registry_status']
    ws.profile(force=True)
    after = store.summary(ws.root)['registry_status']
    assert after['approved'] == before['approved'] and after['stale'] == 0


def test_masters_through_the_app(tmp_path):
    import time
    from binggio.ui.server import App, api
    planted = retail_schema.generate(tmp_path / 'src', scale=1)
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('Work', tmp_path / 'src')['id']
    ws.profile(); ws.discover()
    app = App(ws)
    d = api(app, 'GET', '/api/masters', {}, {})
    assert d['computing']                                        # first visit: the analysis runs in the background
    for _ in range(300):
        if api(app, 'GET', '/api/jobs/' + d['job']['id'], {}, {})['status'] != 'running': break
        time.sleep(0.05)
    d = api(app, 'GET', '/api/masters', {}, {})
    district = next(p for p in d['proposals'] if p['name'] == 'District')
    r = api(app, 'POST', '/api/masters', {}, {'action': 'approve', 'kind': 'entity', 'spec': district['spec'], 'by': 'Asha',
                                               'note': 'district ids', 'approve_links': True})
    for _ in range(200):
        j = api(app, 'GET', '/api/jobs/' + r['job']['id'], {}, {})
        if j['status'] != 'running': break
        time.sleep(0.05)
    assert j['status'] == 'done', j
    assert not j['result']['links']['failed']
    d = api(app, 'GET', '/api/masters', {}, {})
    m = d['approved'][0]
    assert m['table'] == 'MASTER_DISTRICT' and m['links']['approved'] == m['links']['total'] >= 2
    t = api(app, 'GET', '/api/table', {'connection': m['connection'], 'table': 'MASTER_DISTRICT'}, {})
    assert t['master']['name'] == 'District'
    g = api(app, 'GET', '/api/graph', {}, {})
    assert any(n['master'] for n in g['nodes'])
    roles = api(app, 'GET', '/api/review/roles', {}, {})
    assert roles['hidden_no_information'] > 0
    chk = api(app, 'POST', '/api/masters/check', {}, {'member': {'connection': cid, 'table': ROTA, 'column': 'staffid'},
                                                     'against': {'connection': cid, 'table': STAFF, 'column': 'staffid'}})
    assert chk['coverage_pct'] == 0 and chk['digits_coverage_pct'] == 100 and 'Keep digits only' in chk['hint']
    api(app, 'POST', '/api/masters', {}, {'action': 'withdraw', 'id': m['id'], 'by': 'Asha', 'note': 'test'})
    rels = [x for x in store.list_relationships(ws.root, limit=10 ** 6)['items'] if x['parent_table'] == 'MASTER_DISTRICT']
    assert rels and not any(x['effective_status'] == 'approved' for x in rels)


def test_recommended_fixes_approved_by_a_person(tmp_path):
    import time
    from binggio import fix_advice
    from binggio.ui.server import App, api
    src = tmp_path / 'src'; src.mkdir()
    with open(src / 'sales.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['sale_id', 'region_code', 'amount', 'sale_date', 'customer'])
        for i in range(60):
            w.writerow(['S%03d' % i, ('north ' if i % 5 == 0 else 'NORTH') if i % 2 else 'SOUTH', '$%s' % format(1000 * (i + 1), ','),
                        '%02d/%02d/2026' % (i % 28 + 1, i % 12 + 1), '-' if i % 7 == 0 else 'C%d' % (i % 4)])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('S', src)['id']; ws.profile(); ws.discover()
    recs = {(r['column'], r['kind']): r for r in fix_advice.recommend(ws.root)}
    assert ('region_code', 'trim') in recs and ('amount', 'number') in recs and ('customer', 'replace') in recs
    d = recs[('sale_date', 'add_date')]
    assert d['params'] == {'format': '%d/%m/%Y'} and 'day first' in d['why']       # some days are above 12
    assert ('sale_id', 'number') not in recs                                        # never on identifiers
    app = App(ws)
    ids = [recs[('amount', 'number')]['id'], recs[('sale_date', 'add_date')]['id']]
    try:
        api(app, 'POST', '/api/fixes/recommended', {}, {'action': 'approve', 'ids': ids, 'by': 'Asha', 'note': ''})
        raise AssertionError('approved without a reason')
    except Exception as e:
        assert 'why' in str(e).lower()
    r = api(app, 'POST', '/api/fixes/recommended', {}, {'action': 'approve', 'ids': ids, 'by': 'Asha', 'note': 'checked the previews'})
    for _ in range(300):
        j = api(app, 'GET', '/api/jobs/' + r['job']['id'], {}, {})
        if j['status'] != 'running': break
        time.sleep(0.05)
    assert j['status'] == 'done', j
    conn = next(c for c in ws.connections() if c['id'] == cid)
    h, rows, _ = prepared.read(ws.root, conn, 'sales', 5)
    row = dict(zip(h, rows[1]))
    assert row['amount'] == '2000' and row['sale_date_date'] == '2026-02-02'
    fixes = prepared.list_fixes(ws.root, cid, 'sales')
    assert all('recommended by Binggio' in f['note'] and f['created_by'] == 'Asha' for f in fixes)
    left = {(x['column'], x['kind']) for x in fix_advice.recommend(ws.root)}
    assert ('amount', 'number') not in left and ('region_code', 'trim') in left
    fix_advice.dismiss(ws.root, [x['id'] for x in fix_advice.recommend(ws.root) if x['column'] == 'region_code'], 'Asha', 'spaces are meaningful here')
    assert not any(x['column'] == 'region_code' for x in fix_advice.recommend(ws.root))


def test_masters_analysis_is_saved_until_data_changes(work):
    ws, cid, _ = work
    masters.analysis(ws.root)
    assert masters.cached_analysis(ws.root) is not None
    conn = next(c for c in ws.connections() if c['id'] == cid)
    prepared.add_fix(ws.root, conn, STAFF, 'trim', 'lastname', {}, 'T', 'spaces')
    ws.profile()
    assert masters.cached_analysis(ws.root) is None            # the data Binggio reads changed


def test_a_file_sorted_by_month_keeps_its_months(tmp_path):
    """0.1.10: the first 2,500 rows are all January, so the rows read show one value; the column
    still has six months on all rows and must not be set aside as having no information."""
    from binggio.profiling import store as profiles
    src = tmp_path / 'src'; src.mkdir()
    with open(src / 'stock.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['reportingmonth', 'store_id', 'units'])
        for m in ('2026-01-31', '2026-02-28', '2026-03-31', '2026-04-30', '2026-05-31', '2026-06-30'):
            for s in range(3000): w.writerow([m, 'S%04d' % s, s % 7])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('S', src)['id']; ws.profile()
    col = {c['name']: c for c in profiles.get_table(ws.root, cid, 'stock')['columns']}['reportingmonth']
    assert col['distinct_count'] == 6 and col.get('distinct_scope') == 'all_rows'
    from binggio.ui.server import _no_information
    assert (cid, 'stock', 'reportingmonth') not in _no_information(ws.root)
