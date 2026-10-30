# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""A new workspace: add sources, profile, discover, review lists, approve, ask by plan."""
import csv
import pytest
import binggio
from binggio.registry import store
from conftest import full_plan


def write(folder, name, header, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / (name + '.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)


@pytest.fixture
def ws(tmp_path):
    src = tmp_path / 'export'
    write(src, 'customers', ['customer_id', 'customer_name', 'segment'],
          [('C%03d' % i, 'Customer %d' % i, ['RETAIL', 'PREMIUM'][i % 2]) for i in range(1, 41)])
    write(src, 'loans', ['loan_id', 'customer_id', 'balance', 'status'],
          [('L%03d' % i, 'C%03d' % (i % 40 + 1), 1000 * i, ['OPEN', 'CLOSED'][i % 3 == 0]) for i in range(1, 61)])
    w = binggio.Workspace.create(tmp_path / 'ws')
    w.add_csv_folder('Core export', src)
    return w


def test_pipeline_from_empty_workspace(ws):
    assert ws.status()['tables'] == 2
    assert ws.profile()['failures'] == []
    g = ws.discover()
    assert g['candidate_edges'] >= 1
    roles = store.list_roles(ws.root, limit=200)['items']
    assert len(roles) == 7
    edges = store.list_relationships(ws.root, limit=200)['items']
    link = next(e for e in edges if e['child_table'] == 'loans' and e['parent_table'] == 'customers')
    cid = roles[0]['connection_id']
    for t, c, r in [('customers', 'customer_id', 'PRIMARY_KEY_CANDIDATE'), ('customers', 'segment', 'CLASSIFICATION'),
                    ('loans', 'loan_id', 'PRIMARY_KEY_CANDIDATE'), ('loans', 'customer_id', 'FOREIGN_KEY_CANDIDATE'),
                    ('loans', 'balance', 'MEASURE'), ('loans', 'status', 'STATUS')]:
        store.decide_column(ws.root, cid, t, c, action='approve', actor='Tester', note='test', expected_revision=0, role=r)
    store.decide_relationship(ws.root, link['edge_id'], action='approve', actor='Tester', note='test',
                              expected_revision=0, cardinality='many_to_one')
    st = ws.status()['answerable']
    assert st['tables'] == 2 and st['relationships'] == 1
    T = lambda t: cid + '::' + t
    plan = full_plan({'entity': T('loans'), 'aggregate': [{'fn': 'sum', 'field': T('loans') + '::balance', 'alias': 'total'}],
                      'group_by': [T('customers') + '::segment'],
                      'filters': [{'field': T('loans') + '::status', 'op': 'eq', 'value': 'OPEN'}]})
    ans = ws.ask('Total balance of OPEN loans by segment', plan=plan)
    got = {r[0]: float(r[1]) for r in ans['rows']}
    exp = {}
    for i in range(1, 61):
        if i % 3 != 0:
            seg = ['RETAIL', 'PREMIUM'][(i % 40 + 1) % 2]
            exp[seg] = exp.get(seg, 0) + 1000 * i
    assert got == exp


def test_existing_workspace_detected_and_refuses_overwrite(ws, tmp_path):
    assert binggio.Workspace.open(ws.root / 'data').root == ws.root
    with pytest.raises(binggio.WorkspaceError):
        binggio.Workspace.create(ws.root)


def test_ask_on_empty_workspace_explains_next_steps(ws, monkeypatch):
    for k in ('BINGGIO_LLM_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY', 'OPENAI_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    ans = ws.ask('How many loans are there?')
    assert ans['status'] == 'refused' and ans['code'] == 'NOTHING_APPROVED'  # nothing approved yet


def test_ask_without_key_on_approved_workspace(demo, monkeypatch):
    for k in ('BINGGIO_LLM_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY', 'OPENAI_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    ans = demo.ask('How many loans are there?')
    assert ans['status'] == 'refused' and ans['code'] == 'MODEL_NOT_CONFIGURED'


def test_add_csv_accepts_a_single_file_and_explains_bad_paths(tmp_path):
    import pytest
    src = tmp_path / 'export'; src.mkdir()
    (src / 'dealers.csv').write_text('dealer_id,name\nD1,a\nD2,b\n')
    (src / 'other.csv').write_text('x\n1\n')
    ws = binggio.Workspace.create(tmp_path / 'ws')
    c = ws.add_csv_folder('One file', '"%s"' % (src / 'dealers.csv'))   # pasted with quotes, as Windows "Copy as path" does
    assert c['table_count'] == 1
    with pytest.raises(binggio.WorkspaceError, match='Not a .csv file'):
        (src / 'notes.txt').write_text('hi'); ws.add_csv_folder('Text', src / 'notes.txt')
    with pytest.raises(binggio.WorkspaceError, match='not found'):
        ws.add_csv_folder('Missing', tmp_path / 'nope')


def test_add_csv_accepts_several_files(tmp_path):
    import pytest
    src = tmp_path / 'export'; src.mkdir()
    for n in ('a', 'b', 'c'): (src / (n + '.csv')).write_text('k_id,v\n1,x\n2,y\n')
    ws = binggio.Workspace.create(tmp_path / 'ws')
    assert ws.add_csv_folder('Two', [src / 'a.csv', src / 'b.csv'])['table_count'] == 2
    assert ws.add_csv_folder('Two again', '%s; "%s"' % (src / 'b.csv', src / 'c.csv'))['table_count'] == 2
    other = tmp_path / 'other'; other.mkdir(); (other / 'a.csv').write_text('z\n1\n')
    with pytest.raises(binggio.WorkspaceError, match='Two files are called a.csv'):
        ws.add_csv_folder('Clash', [src / 'a.csv', other / 'a.csv'])


def test_sample_bank_data_is_fixed(tmp_path):
    from binggio.workspace import Workspace, WorkspaceError
    ws = Workspace.demo(tmp_path / 'sample')
    assert ws.is_demo
    csv = tmp_path / 'noise.csv'; csv.write_text('a,b\n1,2\n')
    before = len(ws.connections())
    for attempt in (lambda: ws.add_csv_folder('Noise', str(csv)), lambda: ws.refresh(ws.connections()[0]['id'])):
        with pytest.raises(WorkspaceError, match='sample bank is read-only'):
            attempt()
    assert len(ws.connections()) == before
    own = Workspace.create(tmp_path / 'mine')
    assert not own.is_demo and own.add_csv_folder('Mine', str(csv))['table_count'] == 1
