# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""A table that cannot be profiled must not stop relationship discovery for the others."""
import binggio


def test_discovery_skips_tables_that_could_not_be_profiled(tmp_path):
    src = tmp_path / 'broken'; src.mkdir()
    (src / 'empty.csv').write_text('')
    (src / 'dupes.csv').write_text('id,name,name\n1,a,b\n')
    (src / 'customers.csv').write_text('customer_id,name\nC1,a\nC2,b\nC3,c\n')
    (src / 'orders.csv').write_text('order_id,customer_id\nO1,C1\nO2,C2\nO3,C3\nO4,C1\n')
    ws = binggio.Workspace.create(tmp_path / 'ws')
    ws.add_csv_folder('Broken', src)
    assert len(ws.profile()['failures']) == 2
    r = ws.discover()
    assert sorted(t.split('::')[1] for t in r['skipped_tables']) == ['dupes', 'empty']
    assert r['candidate_edges'] >= 1
