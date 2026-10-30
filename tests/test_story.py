# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Data story prototype on the automotive sample: facts from approved meanings only."""
import sys
from pathlib import Path
import binggio
from binggio import onboard, story

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'samples' / 'automotive'))
import make_automotive as M  # noqa: E402


def test_story_finds_the_known_facts(tmp_path):
    t = M.build(); M.write(t, tmp_path)
    ws = binggio.Workspace.create(tmp_path / 'ws')
    c = ws.add_csv_folder('After-sales', tmp_path / 'aftersales_quality'); ws.profile(); ws.discover()
    assert story.build(ws.root, c['id']).get('empty')            # nothing approved yet: no story
    onboard.apply(ws.root, onboard.proposals(ws.root, c['id']), 'Tester')
    s = story.build(ws.root, c['id'])
    kinds = [f['kind'] for f in s['facts']]
    assert kinds[0] == 'headline' and 'driver' in kinds and len(s['facts']) <= 6
    claims = next(f for f in s['facts'] if f['kind'] == 'driver' and 'claim amount' in f['title'])
    paid = {}
    part = {p['part_number']: p['category'] for p in t['parts']}
    for x in t['warranty_claims']: paid[part[x['part_no']]] = paid.get(part[x['part_no']], 0) + x['claim_amount']
    assert claims['chart']['labels'][0] == max(paid, key=paid.get)            # matches an independent count
    assert abs(claims['chart']['values'][0] - round(max(paid.values()), 2)) < 0.01
    assert not any('Total price per litre' in f['title'] for f in s['facts'])  # rates are averaged, not summed
