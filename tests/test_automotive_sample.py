# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""The automotive sample (samples/automotive): two systems with keys not named *_id, an extension
table, a duplicate table and messy values. Binggio must find exactly the expected keys and links,
classify the tables, and keep approvals current when its heuristics change."""
import sqlite3
import sys
from pathlib import Path

import pytest
import binggio
from binggio import onboard
from binggio.registry import store
from binggio.discovery import store as graph

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'samples' / 'automotive'))
import make_automotive as M  # noqa: E402


@pytest.fixture(scope='module')
def auto(tmp_path_factory):
    base = tmp_path_factory.mktemp('auto')
    M.write(M.build(), base)
    ws = binggio.Workspace.create(base / 'ws')
    kinds = {}
    for system in ('plant_and_sales', 'aftersales_quality'):
        c = ws.add_csv_folder(system, base / system)
        assert ws.profile()['failures'] == []
        ws.discover()
        props = onboard.proposals(ws.root, c['id'])
        kinds.update({t['table']: t['kind'] for t in props['impact']['tables']})
        out = onboard.apply(ws.root, props, 'Tester')
        assert not [x for x in out['left_pending'] if not x.startswith('concept')], out
    return ws, kinds, base


def test_keys(auto):
    ws, _, _ = auto
    pk = {}
    for r in store.list_roles(ws.root, limit=10 ** 6)['items']:
        if r['candidate_role'] == 'PRIMARY_KEY_CANDIDATE': pk.setdefault(r['table'], []).append(r['column'])
    assert pk == {t: [k] for t, k in M.EXPECTED_KEYS.items()}


def test_links_exactly(auto):
    ws, _, _ = auto
    got = {(e['child_table'] + '.' + e['child_column'], e['parent_table'] + '.' + e['parent_column']) for e in graph.all_edges(ws.root)}
    assert got == {(a, b) for a, b, _ in M.EXPECTED_LINKS}


def test_classification(auto):
    _, kinds, _ = auto
    assert kinds['vehicle_telematics'] == 'extends'
    assert kinds['dealer_directory'] == 'duplicate'
    assert kinds['fuel_prices'] == 'standalone'
    assert kinds['suppliers'] == 'reference'


def test_duplicate_left_out_and_everything_else_answerable(auto):
    ws, _, _ = auto
    st = ws.status()
    assert st['answerable']['tables'] == 14 and st['answerable']['relationships'] == 14
    assert st['registry']['stale'] == 0


def test_heuristic_changes_do_not_stale_approvals_but_data_changes_do(auto):
    ws, _, base = auto
    db = sqlite3.connect(ws.root / 'data' / 'phase6_graph' / 'graph.sqlite3')
    import json
    rows = db.execute('SELECT id, payload_json FROM edges').fetchall()
    for eid, payload in rows:   # Binggio "improves" its scores and reasons
        e = json.loads(payload); e['score'] = 0.91; e['evidence']['reasons'] = ['rescored']
        db.execute('UPDATE edges SET payload_json=?, score=? WHERE id=?', (json.dumps(e), 0.91, eid))
    db.commit(); db.close()
    assert ws.status()['registry']['stale'] == 0
    src = next((ws.root / 'data' / 'sources').glob('aftersales*')) / 'recalls.csv'
    src.write_bytes(src.read_bytes().replace(b'HIGH', b'CRITICAL', 1))
    ws.profile()
    stale = [r for r in store._rows(ws.root) if r['effective_status'] == 'stale']
    assert stale and all('recall' in (r['subject_key'] + str(r['approved'])) for r in stale)


def test_aftersales_alone_never_preticks_partial_links(tmp_path):
    """Without the vehicles table, telematics is the only table keyed by vin. Service visits link to
    it fully; recall completions only 70% (unsold vehicles), so that link must not be pre-ticked."""
    M.write(M.build(), tmp_path)
    ws = binggio.Workspace.create(tmp_path / 'ws')
    c = ws.add_csv_folder('After-sales', tmp_path / 'aftersales_quality'); ws.profile(); ws.discover()
    links = {(l['child'], l['parent']): l for l in onboard.proposals(ws.root, c['id'])['links']}
    assert links[('service_visits.vehicle_vin', 'vehicle_telematics.vin')]['suggested']
    partial = links[('recall_completions.vin', 'vehicle_telematics.vin')]
    assert partial['coverage'] < 95 and not partial['suggested']
