# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Relationships a person adds: checked on the whole of both tables, then reviewed like any other.

    check(root, child, parent)   -> evidence (how many child keys are found, is the parent unique)
    add(root, child, parent, actor, note)  -> the candidate link, ready for review

`child` and `parent` are (connection_id, table, column). Discovery may have missed a link because
the key names give no hint, the values differ in format, or the match is weaker than its rules
allow. The check measures the link on every row (through the prepared copies, data fixes applied)
and says plainly what it found; it never approves anything. Approval is the same person-made
decision, with a rationale, as for a link Binggio proposed.
"""
from __future__ import annotations
import re
import sqlite3
from datetime import datetime, timezone

from binggio import prepared
from binggio.catalog import catalog
from binggio.catalog.connectors import ConnectionErrorSafe, _quote_identifier as q


class LinkError(ValueError):
    pass


def _norm(v):
    if prepared._is_missing(v): return None
    s = str(v).strip().casefold()
    return s or None


def _conn(root, cid):
    c = next((x for x in catalog.list_connections(root) if x['id'] == cid), None)
    if c is None: raise LinkError('Unknown source')
    return c


def _check_side(root, side, label):
    cid, table, column = side
    conn = _conn(root, cid)
    if not any(t['table_name'] == table for t in catalog.list_tables(root, connection_id=cid, limit=5000)):
        raise LinkError('%s table %s is not registered' % (label, table))
    header = prepared.header_with_fixes(root, conn, table)
    if column not in header: raise LinkError('%s column %s is not in %s' % (label, column, table))
    return conn


def check(root, child, parent):
    """Whole-table evidence for child -> parent. Values are matched ignoring spaces and case, as
    discovery and answers do."""
    if tuple(child[:2]) == tuple(parent[:2]): raise LinkError('Choose two different tables')
    cconn = _check_side(root, child, 'Child'); pconn = _check_side(root, parent, 'Parent')
    cpath = prepared.ensure(root, cconn, child[1]); ppath = prepared.ensure(root, pconn, parent[1])
    db = sqlite3.connect(':memory:')
    try:
        db.create_function('bn_norm', 1, _norm, deterministic=True)
        db.execute('ATTACH DATABASE ? AS c', (cpath.as_uri() + '?mode=ro',))
        db.execute('ATTACH DATABASE ? AS p', (ppath.as_uri() + '?mode=ro',))
        cc, pc = q(child[2]), q(parent[2])
        db.execute('CREATE TEMP TABLE cv AS SELECT bn_norm(%s) AS v, MIN(%s) AS raw, COUNT(*) AS n FROM c.t GROUP BY 1' % (cc, cc))
        db.execute('CREATE TEMP TABLE pv AS SELECT bn_norm(%s) AS v, MIN(%s) AS raw, COUNT(*) AS n FROM p.t GROUP BY 1' % (pc, pc))
        db.execute('CREATE INDEX temp.pv_v ON pv(v)')
        one = lambda sql: db.execute(sql).fetchone()[0] or 0
        c_rows, c_missing = one('SELECT SUM(n) FROM cv'), one('SELECT SUM(n) FROM cv WHERE v IS NULL')
        c_distinct = one('SELECT COUNT(*) FROM cv WHERE v IS NOT NULL')
        matched = one('SELECT COUNT(*) FROM cv WHERE v IS NOT NULL AND v IN (SELECT v FROM pv)')
        matched_rows = one('SELECT SUM(n) FROM cv WHERE v IS NOT NULL AND v IN (SELECT v FROM pv)')
        p_rows, p_missing = one('SELECT SUM(n) FROM pv'), one('SELECT SUM(n) FROM pv WHERE v IS NULL')
        p_distinct = one('SELECT COUNT(*) FROM pv WHERE v IS NOT NULL')
        p_repeated = one('SELECT SUM(n - 1) FROM pv WHERE v IS NOT NULL AND n > 1')
        c_repeated = one('SELECT SUM(n - 1) FROM cv WHERE v IS NOT NULL AND n > 1')
        unmatched = [r[0] for r in db.execute('SELECT raw FROM cv WHERE v IS NOT NULL AND v NOT IN (SELECT v FROM pv) ORDER BY n DESC, v LIMIT 5')]
        repeats = [{'value': r[0], 'rows': r[1]} for r in db.execute('SELECT raw, n FROM pv WHERE v IS NOT NULL AND n > 1 ORDER BY n DESC, v LIMIT 5')]
        cs = [r[0] for r in db.execute('SELECT v FROM cv WHERE v IS NOT NULL LIMIT 400')]
        ps = {r[0] for r in db.execute('SELECT v FROM pv WHERE v IS NOT NULL')} if p_distinct <= 2_000_000 else set()
        ex_c = [r[0] for r in db.execute('SELECT %s FROM c.t WHERE %s IS NOT NULL LIMIT 3' % (cc, cc))]
        ex_p = [r[0] for r in db.execute('SELECT %s FROM p.t WHERE %s IS NOT NULL LIMIT 3' % (pc, pc))]
    finally:
        db.close()
    coverage = matched / c_distinct if c_distinct else 0.0
    row_cov = matched_rows / (c_rows - c_missing) if c_rows - c_missing else 0.0
    parent_unique = p_repeated == 0 and p_missing == 0
    out = {'child': {'connection': child[0], 'table': child[1], 'column': child[2], 'rows': c_rows, 'missing': c_missing,
                     'distinct': c_distinct, 'examples': ex_c},
           'parent': {'connection': parent[0], 'table': parent[1], 'column': parent[2], 'rows': p_rows, 'missing': p_missing,
                      'distinct': p_distinct, 'repeated_rows': p_repeated, 'examples': ex_p, 'repeats': repeats},
           'matched_distinct': matched, 'coverage_pct': round(100 * coverage, 2), 'row_coverage_pct': round(100 * row_cov, 2),
           'unmatched_examples': unmatched, 'parent_unique': parent_unique, 'whole_table': True}
    if parent_unique:
        out['suggested_cardinality'] = 'one_to_one' if c_repeated == 0 and c_missing == 0 else 'many_to_one'
    else:
        out['suggested_cardinality'] = 'many_to_many'
    warn = []
    if c_distinct == 0: warn.append('The child column has no values.')
    elif matched == 0: warn.append('None of the child values are found in the parent column.')
    elif coverage < 0.45: warn.append('Only %s%% of the child values are found in the parent column.' % out['coverage_pct'])
    elif coverage < 0.95: warn.append('%s%% of the child values have no match in the parent (examples below).' % round(100 - out['coverage_pct'], 2))
    if p_missing: warn.append('The parent column has %s empty value(s).' % '{:,}'.format(p_missing))
    if p_repeated:
        warn.append('The parent column repeats values (%s extra rows), so each child row could join to more than one parent row and '
                    'totals would be counted more than once. Fix the parent key first (Key tools: keep one row per key, or combine columns).'
                    % '{:,}'.format(p_repeated))
    if matched < max(1, c_distinct) * 0.5 and cs and ps:
        dc = {re.sub(r'\D', '', v).lstrip('0') for v in cs} - {''}
        dp = {re.sub(r'\D', '', v).lstrip('0') for v in ps} - {''}
        if dc and len(dc & dp) / len(dc) >= 0.6:
            out['format_hint'] = ('The values look like the same keys written differently (%s here, %s there). Add the data fix '
                                  '"Keep digits only" to both columns (Explore tables > Data fixes), then check again.'
                                  % (ex_c[0] if ex_c else '', ex_p[0] if ex_p else ''))
            warn.append(out['format_hint'])
    out['warnings'] = warn
    out['strength'] = ('strong' if coverage >= 0.95 and parent_unique else 'moderate' if coverage >= 0.8 and parent_unique
                       else 'weak')
    return out


def add(root, child, parent, actor, note):
    """Check, then add the link as a candidate marked as added by a person."""
    from binggio.discovery import store as graph
    from binggio.discovery import VERSION
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise LinkError('Set your name first: the link is recorded with it')
    if not note: raise LinkError('Say what the link means and how you know (kept with the link)')
    ev = check(root, child, parent)
    if ev['matched_distinct'] == 0:
        raise LinkError('None of the child values are found in the parent column, so this link would join nothing. '
                        'Check the columns, or add a data fix so the keys are written the same way.')
    cov = ev['coverage_pct'] / 100
    uniq = 1.0 if ev['parent_unique'] else max(0.0, 1 - ev['parent']['repeated_rows'] / max(1, ev['parent']['rows']))
    score = round(min(0.99, 0.49 * cov + 0.23 * 1.0 + 0.20 * uniq + 0.08), 3)
    eid = graph.edge_id(child[0], child[1], child[2], parent[0], parent[1], parent[2])
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    edge = {'id': eid, 'child_connection_id': child[0], 'child_table': child[1], 'child_column': child[2],
            'parent_connection_id': parent[0], 'parent_table': parent[1], 'parent_column': parent[2],
            'child_schema': 'files' if _conn(root, child[0])['kind'] == 'csv_folder' else 'main',
            'parent_schema': 'files' if _conn(root, parent[0])['kind'] == 'csv_folder' else 'main',
            'status': 'candidate', 'score': score, 'score_label': 'added by a person', 'version': VERSION,
            'evidence': {'manual': True, 'added_by': actor, 'added_at': now, 'note': note[:600], 'whole_table': True,
                         'child_distinct_sampled': ev['child']['distinct'], 'parent_distinct_sampled': ev['parent']['distinct'],
                         'overlapping_distinct': ev['matched_distinct'], 'distinct_coverage_pct': ev['coverage_pct'],
                         'row_coverage_pct': ev['row_coverage_pct'],
                         'parent_uniqueness_pct': round(100 * ev['parent']['distinct'] / max(1, ev['parent']['rows'] - ev['parent']['missing']), 2),
                         'child_missing_sampled': ev['child']['missing'], 'parent_missing_sampled': ev['parent']['missing'],
                         'child_sample_rows': ev['child']['rows'], 'parent_sample_rows': ev['parent']['rows'],
                         'child_sample_complete': True, 'parent_sample_complete': True, 'lexical_family_match': False,
                         'cardinality_hint': ev['suggested_cardinality'] if ev['suggested_cardinality'] != 'many_to_one' else None,
                         'reasons': ['Added by %s: %s' % (actor, note[:200]),
                                     '%s of %s distinct child values (%s%%) found in the parent, on all rows' % (
                                         '{:,}'.format(ev['matched_distinct']), '{:,}'.format(ev['child']['distinct']), ev['coverage_pct']),
                                     'Parent key unique and non-null on all rows' if ev['parent_unique'] else 'Parent key repeats values or has empty values'],
                         'limitations': ['Added by a person; Binggio did not propose it', 'Business meaning is the approver\'s judgement'],
                         'warnings': ev['warnings']}}
    saved = graph.add_manual_edge(root, edge, actor, note)
    return {'edge': saved, 'check': ev, 'already_proposed': bool(saved.get('already_proposed'))}
