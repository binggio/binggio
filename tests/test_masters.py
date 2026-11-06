# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""0.1.7: enterprise extracts (references stored as domain/id/version, monthly snapshots, people
stored as column blocks) and the masters a person approves for them. Random data only."""
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
import trading_schema  # noqa: E402

CORE, ERDS, MTA, ASSO, INSTR = 'BRDS_BK_CORE-YTD_2026', 'ERDS-YTD_2026', 'ERDS_MTA-YTD_2026', 'BRDS_BK_ASSO-YTD_2026', 'IRDS_INSTR-YTD_2026'


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    base = tmp_path_factory.mktemp('work')
    planted = trading_schema.generate(base / 'src', scale=1)
    ws = binggio.Workspace.create(base / 'ws')
    cid = ws.add_csv_folder('Work', base / 'src')['id']
    ws.profile(); ws.discover()
    return ws, cid, planted


def _cols(ws, cid, table):
    return {c['name']: c for c in profiles.get_table(ws.root, cid, table)['columns']}


def test_names_decide_what_is_never_a_measure(work):
    ws, cid, _ = work
    core, erds, hire = _cols(ws, cid, CORE), _cols(ws, cid, ERDS), _cols(ws, cid, 'BRDS_BK_HIRE-YTD_2026')
    for name in ('identifierversion', 'traderidentifierversion', 'accountingcategorycode'):
        assert core[name]['semantic_role_candidate'] != 'MEASURE', name
    assert hire['level']['semantic_role_candidate'] == 'CLASSIFICATION'           # a depth, not a quantity
    assert hire['isleaf']['semantic_role_candidate'] == 'CLASSIFICATION'          # a flag
    assert core['middleofficegroupemailid']['semantic_role_candidate'] == 'TEXT'  # an email, not a key
    assert erds['emailid']['semantic_role_candidate'] == 'TEXT'
    assert erds['state']['semantic_role_candidate'] != 'STATUS'                   # a place
    assert erds['reportingmonth']['semantic_role_candidate'] == 'DATE'
    assert core['bookcreateddatetime']['observed_type'] == 'date'                 # 2026-01-05T10:22:33.123Z
    assert erds['hiredate']['observed_type'] == 'date'                            # 05-Jan-2019
    assert any(f['code'] == 'date_as_text' for f in erds['hiredate']['findings'])
    # names where some values are numbers are text, without a parsing warning
    assert core['traderbookname']['observed_type'] == 'text'
    assert not any(f['code'] == 'mixed_types' for f in core['traderbookname']['findings'])
    # no outlier warnings on identifiers
    assert not any(f['code'] == 'numeric_outlier_candidate' for f in core['traderidentifierid']['findings'])


def test_groups_no_information_and_the_repeating_identifier(work):
    ws, cid, _ = work
    p = profiles.get_table(ws.root, cid, CORE)
    kinds = {}
    for g in p['groups']: kinds.setdefault(g['kind'], []).append(g['name'])
    assert {'trader', 'bookrunner', 'financialcontroller'} <= set(kinds['person'])
    assert 'own' in kinds['reference'] and 'portfolio' in kinds['reference']
    assert 'businessowner' in kinds['code_name']
    cols = {c['name']: c for c in p['columns']}
    assert cols['frtbirtbooktype']['no_information'] == 'empty'
    assert cols['hubbingcomment']['no_information'] == 'empty'
    own = cols['identifierid']
    assert own['own_identifier'] and own['semantic_role_candidate'] == 'IDENTIFIER'
    assert own['unique_with'] == ['identifierid', 'reportingmonth']
    assert p['counts_scope'] == 'all_rows'
    hire = profiles.get_table(ws.root, cid, 'BRDS_BK_HIRE-YTD_2026')
    assert any(g['kind'] == 'hierarchy' and len(g['columns']) == 39 for g in hire['groups'])
    assert _cols(ws, cid, INSTR)['instrumentidentifierid']['semantic_role_candidate'] == 'PRIMARY_KEY_CANDIDATE'


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
    emp = by['Employee']['spec']
    assert (emp['system_of_record']['table'], emp['system_of_record']['column']) == (ERDS, 'employeeid')
    members = {(m['table'], m['column']): m for m in emp['members']}
    for col in ('traderidentifierid', 'bookrunneridentifierid', 'financialcontrolleridentifierid', 'lastactionbyidentifierid'):
        assert members[(CORE, col)]['coverage'] == 1.0 and members[(CORE, col)]['include']
    assert members[(ASSO, 'delegatecontactworkeridentifierid')]['include']
    assert members[(ERDS, 'managerid')]['include']
    padded = members[(MTA, 'employeeid')]                     # 01234567 vs 1234567
    assert padded['needs_fix'] == 'digits' and not padded['include']
    book = by['Book']['spec']
    assert book['system_of_record']['table'] == CORE           # the table describing books, not the associations
    assert (ASSO, 'associatedbookidentifierid') in {(m['table'], m['column']) for m in book['members']}
    assert 'Legalentity' in by and by['Legalentity']['spec'].get('system_of_record') is None
    # versions, domains and emails are never proposed as members
    for p in r['masters']:
        for m in p['spec']['members']:
            assert not m['column'].endswith(('version', 'domain')) and 'email' not in m['column']
    assert r['calendar'] and {s['table'] for s in r['snapshots']} >= {CORE, ERDS}


def test_approve_build_link_and_answer(work):
    ws, cid, planted = work
    r = masters.recommend(ws.root)
    emp = next(p for p in r['masters'] if p['name'] == 'Employee')
    try:
        masters.decide(ws.root, 'approve', '', 'x', spec=emp['spec'])
        raise AssertionError('approved without a name')
    except masters.MasterError:
        pass
    m = masters.decide(ws.root, 'approve', 'Asha', 'Trader and runner ids are ERDS employee ids', spec=emp['spec'])
    built = masters.build(ws.root)
    assert built['built'][0]['table'] == 'MASTER_EMPLOYEE'
    mc = masters.master_connection(ws.root, create=False)
    path = Path(ws.root) / 'data' / mc['source_path'] / 'MASTER_EMPLOYEE.csv'
    with open(path, encoding='utf-8') as f:
        rows = list(csv.reader(f))
    keys = [x[0] for x in rows[1:]]
    assert len(keys) == len(set(keys))                          # unique by construction
    assert rows[0][:3] == ['employee_id', 'in_system_of_record', 'referenced_by_columns']
    ws.profile()
    res = masters.link_members(ws.root, m, 'Asha', 'approved with the master', approve=True)
    assert not res['failed'] and len(res['added']) >= 10
    rels = [x for x in store.list_relationships(ws.root, limit=10 ** 6)['items'] if x['parent_table'] == 'MASTER_EMPLOYEE']
    assert rels and all(x['effective_status'] == 'approved' for x in rels)
    # unchanged data: building again leaves the file (and approvals) as they are
    before = path.stat().st_mtime_ns
    masters.build(ws.root)
    assert path.stat().st_mtime_ns == before
    assert all(x['effective_status'] == 'approved' for x in store.list_relationships(ws.root, limit=10 ** 6)['items'] if x['parent_table'] == 'MASTER_EMPLOYEE')
    assert masters.recommend(ws.root)['masters'] and all(p['name'] != 'Employee' for p in masters.recommend(ws.root)['masters'])


def _approve_table(ws, cid, table, roles):
    for r in store.list_roles(ws.root, connection_id=cid, table=table, limit=1000)['items']:
        if r['column'] in roles:
            store.decide_column(ws.root, cid, table, r['column'], action='approve', actor='T', note='ok', expected_revision=0, role=roles[r['column']])


def test_snapshot_tables_are_not_added_up_across_months(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    with open(src / 'positions.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['reportingmonth', 'desk_id', 'notional_amount'])
        for m in ('2026-01-31', '2026-02-28', '2026-03-31'):
            for d in range(5): w.writerow([m, 'D%d' % d, 100])
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('S', src)['id']; ws.profile(); ws.discover()
    _approve_table(ws, cid, 'positions', {'reportingmonth': 'DATE', 'desk_id': 'IDENTIFIER', 'notional_amount': 'MEASURE'})
    T = cid + '::positions'
    total = full_plan({'entity': T, 'aggregate': [{'fn': 'sum', 'field': T + '::notional_amount', 'alias': 'total'}]})
    assert ws.ask('total notional', plan=total)['rows'][0][0] == 1500       # before: added across months
    snap = next(s for s in masters.recommend(ws.root)['snapshots'] if s['table'] == 'positions')
    assert snap['period_column'] == 'reportingmonth'
    masters.decide_snapshot(ws.root, cid, 'positions', 'reportingmonth', 'approve', 'T', 'one copy per month')
    a = ws.ask('total notional', plan=total)
    assert a['status'] != 'answered' and 'snapshot' in str(a).lower()
    one = full_plan({**total, 'filters': [{'field': T + '::reportingmonth', 'op': 'eq', 'value': '2026-03-31'}]})
    a = ws.ask('total notional in March', plan=one)
    assert a['status'] == 'answered' and a['rows'][0][0] == 500, a
    per = full_plan({**total, 'group_by': [T + '::reportingmonth']})
    assert ws.ask('total notional by month', plan=per)['status'] == 'answered'


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
    planted = trading_schema.generate(tmp_path / 'src', scale=1)
    ws = binggio.Workspace.create(tmp_path / 'ws')
    cid = ws.add_csv_folder('Work', tmp_path / 'src')['id']
    ws.profile(); ws.discover()
    app = App(ws)
    d = api(app, 'GET', '/api/masters', {}, {})
    desk = next(p for p in d['proposals'] if p['name'] == 'Desk')
    r = api(app, 'POST', '/api/masters', {}, {'action': 'approve', 'kind': 'entity', 'spec': desk['spec'], 'by': 'Asha',
                                               'note': 'desk ids', 'approve_links': True})
    for _ in range(200):
        j = api(app, 'GET', '/api/jobs/' + r['job']['id'], {}, {})
        if j['status'] != 'running': break
        time.sleep(0.05)
    assert j['status'] == 'done', j
    assert not j['result']['links']['failed']
    d = api(app, 'GET', '/api/masters', {}, {})
    m = d['approved'][0]
    assert m['table'] == 'MASTER_DESK' and m['links']['approved'] == m['links']['total'] >= 2
    t = api(app, 'GET', '/api/table', {'connection': m['connection'], 'table': 'MASTER_DESK'}, {})
    assert t['master']['name'] == 'Desk'
    g = api(app, 'GET', '/api/graph', {}, {})
    assert any(n['master'] for n in g['nodes'])
    roles = api(app, 'GET', '/api/review/roles', {}, {})
    assert roles['hidden_no_information'] > 0
    chk = api(app, 'POST', '/api/masters/check', {}, {'member': {'connection': cid, 'table': MTA, 'column': 'employeeid'},
                                                     'against': {'connection': cid, 'table': ERDS, 'column': 'employeeid'}})
    assert chk['coverage_pct'] == 0 and chk['digits_coverage_pct'] == 100 and 'Keep digits only' in chk['hint']
    api(app, 'POST', '/api/masters', {}, {'action': 'withdraw', 'id': m['id'], 'by': 'Asha', 'note': 'test'})
    rels = [x for x in store.list_relationships(ws.root, limit=10 ** 6)['items'] if x['parent_table'] == 'MASTER_DESK']
    assert rels and not any(x['effective_status'] == 'approved' for x in rels)
