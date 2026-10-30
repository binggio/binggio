# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Source impact report: how a newly added source fits the data Binggio already knows.

For each table in the source it says what the table most likely is, with the evidence:

  extends    one row per row of an existing table (1:1 on its key): new attributes for that entity
  duplicate  1:1 with an existing table AND most columns carry the same values: a copy
  entity     its own key, and links to existing entities: a new kind of thing (cases, collateral)
  records    no own key, links to existing entities: events or lines about them
  reference  other tables point to it, it points nowhere: a lookup list (agents, branches)
  standalone no links found: check its keys before relying on it

It also lists coded columns (few distinct values such as Y/N or OPEN/CLOSED) that will need
approved business terms before questions can use them ("broken promise" -> kept = N), and
suggests which existing concept each table belongs to. Everything is a suggestion for a
steward; nothing is approved here. Sample values are read for comparison and never stored.
"""
from __future__ import annotations
from pathlib import Path

from binggio.catalog import catalog
from binggio.profiling import store as profiles
from binggio.discovery import store as graph
from binggio.discovery.discovery import _read_sample, _value

CODED_MAX_DISTINCT = 20
DUPLICATE_SHARE = 0.5
VALUE_MATCH = 0.9


def _values(project, conn, table, raw=False):
    headers, rows, _ = _read_sample(project, conn, table, 2500)
    if raw:
        return {h: {str(r[i]).strip() for r in rows if _value(r[i]) is not None} for i, h in enumerate(headers)}
    return {h: {v for v in (_value(r[i]) for r in rows) if v is not None} for i, h in enumerate(headers)}


def _concept_index(project):
    try:
        from binggio.context.context import approved_context
        ctx = approved_context(project)
    except Exception:
        return {}
    out = {}
    for c in (ctx.get('concepts') or {}).values():
        out.setdefault(c.get('primary_table'), []).append(c.get('name'))
    return out


def report(project, connection_id=None):
    project = Path(project).resolve()
    conns = {c['id']: c for c in catalog.list_connections(project)}
    if not conns:
        raise ValueError('No sources in this workspace')
    if connection_id is None:
        connection_id = sorted(conns.values(), key=lambda c: c['created_at'])[-1]['id']
    if connection_id not in conns:
        raise ValueError('Unknown source: %s' % connection_id)
    conn = conns[connection_id]
    edges = graph.all_edges(project)
    concepts = _concept_index(project)
    tid = lambda c, t: c + '::' + t
    cache = {}

    def vals(cid, table):
        k = (cid, table)
        if k not in cache:
            cache[k] = _values(project, conns[cid], table)
        return cache[k]

    tables = []
    for p in profiles.list_profiles(project, connection_id=connection_id):
        prof = profiles.get_table(project, connection_id, p['table'])
        cols = prof['columns']
        own = next((c['name'] for c in cols if c['semantic_role_candidate'] == 'PRIMARY_KEY_CANDIDATE'), None)
        out = [e for e in edges if e['child_connection_id'] == connection_id and e['child_table'] == p['table']]
        inc = [e for e in edges if e['parent_connection_id'] == connection_id and e['parent_table'] == p['table']]
        existing_out = [e for e in out if e['parent_connection_id'] != connection_id]
        one_to_one = [e for e in existing_out if e['child_column'] == own and e['score'] >= 0.85]
        kind, target, detail = 'standalone', None, 'No candidate links found. Check its key columns.'
        if one_to_one:
            e = max(one_to_one, key=lambda x: x['score'])
            target = tid(e['parent_connection_id'], e['parent_table'])
            mine, theirs = vals(connection_id, p['table']), vals(e['parent_connection_id'], e['parent_table'])
            others = [c for c in mine if c != own and mine[c]]
            matched = {}
            for c in others:
                best = max(((len(mine[c] & tv) / len(mine[c]), tc) for tc, tv in theirs.items() if tv), default=(0, None))
                if best[0] >= VALUE_MATCH: matched[c] = best[1]
            if others and len(matched) / len(others) >= DUPLICATE_SHARE:
                kind = 'duplicate'
                detail = 'Same rows as %s: %d of %d columns carry the same values (%s).' % (
                    e['parent_table'], len(matched), len(others), ', '.join('%s = %s' % kv for kv in matched.items()))
            else:
                kind = 'extends'
                new = [c for c in others if c not in matched]
                detail = 'One row per %s row; adds %s.' % (e['parent_table'], ', '.join(new) or 'no new columns')
        elif own and existing_out:
            kind = 'entity'
            detail = 'Own key %s; linked to %s.' % (own, ', '.join(sorted({e['parent_table'] for e in existing_out})))
        elif existing_out:
            kind = 'records'
            detail = 'No own key; records about %s.' % ', '.join(sorted({e['parent_table'] for e in existing_out}))
        elif own and (inc or out):
            kind = 'entity' if out else 'reference'
            linked = sorted({e['parent_table'] for e in out} | {e['child_table'] for e in inc})
            detail = ('Own key %s; linked within this source to %s.' if out else 'Looked up by %s.') % (
                (own, ', '.join(linked)) if out else (', '.join(linked),))
        coded = []
        v = None
        linked_cols = {e['child_column'] for e in out}
        for c in cols:
            if c['name'] in linked_cols: continue
            if c['semantic_role_candidate'] in ('STATUS', 'CLASSIFICATION') and 0 < c['distinct_count'] <= CODED_MAX_DISTINCT:
                v = v or _values(project, conn, p['table'], raw=True)
                coded.append({'column': c['name'], 'values': sorted(v.get(c['name'], set()))[:CODED_MAX_DISTINCT]})
        concept = None
        if kind in ('extends', 'duplicate') and target in concepts:
            concept = {'concept': concepts[target][0], 'as': 'secondary (copy)' if kind == 'duplicate' else 'secondary (enrichment)'}
        elif kind in ('entity', 'reference'):
            concept = {'concept': 'new: ' + p['table'].replace('_', ' '), 'as': 'primary'}
        tables.append({'table': p['table'], 'kind': kind, 'related_to': target, 'detail': detail,
                       'own_key': own, 'links_to_existing': len(existing_out), 'links_within_source': len(out) - len(existing_out),
                       'linked_from': len(inc), 'coded_columns': coded, 'suggested_concept': concept})
    order = {'duplicate': 0, 'extends': 1, 'entity': 2, 'records': 3, 'reference': 4, 'standalone': 5}
    tables.sort(key=lambda t: (order[t['kind']], t['table']))
    return {'source': conn['name'], 'connection_id': connection_id, 'tables': tables,
            'summary': {k: sum(t['kind'] == k for t in tables) for k in order}}


def format_report(r):
    lines = ['Impact of "%s" (%s): %d tables' % (r['source'], r['connection_id'], len(r['tables'])),
             '  ' + ' · '.join('%s %d' % (k, n) for k, n in r['summary'].items() if n), '']
    for t in r['tables']:
        lines.append('%-26s %-10s %s' % (t['table'], t['kind'].upper(), t['detail']))
        if t['suggested_concept']:
            lines.append('%-26s %-10s concept: %s as %s' % ('', '', t['suggested_concept']['concept'], t['suggested_concept']['as']))
        for c in t['coded_columns']:
            lines.append('%-26s %-10s coded: %s = %s' % ('', '', c['column'], ', '.join(c['values'])))
    if any(t['coded_columns'] for t in r['tables']):
        lines += ['', 'Coded values need approved business terms before questions can use words for them',
                  '(for example "broken promise" -> kept = N). Questions that state the code itself work as they are.']
    return '\n'.join(lines)
