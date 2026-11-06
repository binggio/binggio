# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Large CSV files stay fast: a fast scan at add time, previews read only the first rows,
fingerprints are remembered between runs, and the data story says when it read part of a table."""
import time
from pathlib import Path
import binggio
from binggio.catalog import connectors
from binggio.profiling import profiler
from binggio import story, onboard


def _write(path, n):
    with open(path, 'w') as f:
        f.write('txn_id,customer_id,amount,channel\n')
        for i in range(n): f.write('T%08d,C%05d,%.2f,%s\n' % (i, i % 5000, (i * 37 % 10000) / 3, ('UPI', 'NEFT', 'CARD')[i % 3]))


def test_large_file_fast_scan_and_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(connectors, 'FULL_SCAN_BYTES', 1000)       # treat small test files as large
    monkeypatch.setattr(connectors, 'SCAN_ROWS_LARGE', 500)
    src = tmp_path / 'src'; src.mkdir(); _write(src / 'txns.csv', 3000)
    meta = connectors.read_csv_metadata(src / 'txns.csv', preview=0)
    assert meta['row_count'] == 3000 and any('estimated from line breaks' in w for w in meta['warnings'])
    ws = binggio.Workspace.create(tmp_path / 'ws'); c = ws.add_csv_folder('Big', str(src))
    from binggio.catalog import catalog
    d = catalog.table_detail(ws.root, c['id'], 'txns', preview=5)
    assert d['row_count'] == 3000 and len(d['preview']) == 5 and d['preview'][0]['txn_id'] == 'T00000000'


def test_fingerprints_remembered_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path)); monkeypatch.setenv('USERPROFILE', str(tmp_path))
    f = tmp_path / 'a.csv'; _write(f, 100)
    first = profiler._content_sha256(f, f.stat())
    profiler._HASH_CACHE.clear()
    reads = []
    real_open = open
    monkeypatch.setattr('builtins.open', lambda *a, **k: (reads.append(a[0]), real_open(*a, **k))[1])
    assert profiler._content_sha256(f, f.stat()) == first and str(f) not in [str(x) for x in reads]
    monkeypatch.undo()
    f.write_text(f.read_text() + 'T9,C1,1.00,UPI\n')
    assert profiler._content_sha256(f, f.stat()) != first           # a changed file is hashed again


def test_story_says_when_it_read_part_of_a_table(tmp_path, monkeypatch):
    monkeypatch.setattr(story, 'STORY_ROWS', 1000)
    src = tmp_path / 'src'; src.mkdir(); _write(src / 'txns.csv', 4000)
    ws = binggio.Workspace.create(tmp_path / 'ws'); c = ws.add_csv_folder('Big', str(src)); ws.profile(); ws.discover()
    onboard.apply(ws.root, onboard.proposals(ws.root, c['id']), 'Tester')
    s = story.build(ws.root, c['id'])
    assert s['facts']
    assert all(f.get('partial') and 'first 1,000 rows' in f['detail'] for f in s['facts'])
