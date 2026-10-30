# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Approvals are bound to content: copying or touching files keeps them; changing bytes makes them stale."""
import os
import time
from binggio.registry import store


def counts(ws):
    rows = store._rows(ws.root)
    return sum(r['effective_status'] == 'approved' for r in rows), sum(r['effective_status'] == 'stale' for r in rows)


def test_touch_keeps_and_edit_invalidates(tmp_path):
    import binggio
    ws = binggio.Workspace.demo(tmp_path / 'bank')
    approved, stale = counts(ws)
    assert approved == 429 and stale == 0
    now = time.time() + 100
    for dirpath, _, files in os.walk(ws.root / 'data'):
        for f in files: os.utime(os.path.join(dirpath, f), (now, now))
    assert counts(ws) == (429, 0)
    cm = ws.root / 'data/phase4_workbench/csv_clean/customer_master.csv'
    cm.write_text(cm.read_text() + 'C9999,Extra,RETAIL,2026-01-01,B001\n')
    a, s = counts(ws)
    assert s > 0 and a == 429 - s
