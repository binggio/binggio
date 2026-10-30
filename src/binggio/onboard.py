# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Guided onboarding of a new source: impact -> column roles -> links -> concepts -> terms -> ask.

Every approval is made by a named person. `proposals()` only suggests; `apply()` records the
decisions the person accepted (interactively, or all at once with --accept-suggestions, which is
itself the person's decision and is recorded as such in the audit trail).

Defaults chosen to avoid wrong approvals:
  * duplicate tables (a copy of an existing table) are not approved: a second copy would only
    create two ways to answer the same question;
  * weak links (score below 0.85, or fewer than 95% of the child's values found in the parent)
    are left pending for a person to look at;
  * one-to-one links are approved as one_to_one, everything else as many_to_one (child -> parent);
  * coded values (Y/N, status codes) get business terms only with words a person typed.
"""
from __future__ import annotations
from pathlib import Path

from binggio import impact
from binggio.discovery import store as graph
from binggio.registry import store, concepts as concept_store, terms as term_store

STRONG = 0.85
FULL_COVERAGE = 95.0   # % of the child's values found in the parent; below this a link is never pre-ticked


def proposals(root, connection_id=None):
    root = Path(root).resolve()
    rep = impact.report(root, connection_id)
    cid = rep['connection_id']
    kinds = {t['table']: t for t in rep['tables']}
    decided = {r['subject_key'] for r in store._rows(root, 'relationship')}
    roles = {}
    for r in store.list_roles(root, connection_id=cid, limit=10 ** 6)['items']:
        if r['effective_status'] in ('pending', 'stale') and kinds.get(r['table'], {}).get('kind') != 'duplicate':
            roles.setdefault(r['table'], []).append({'column': r['column'], 'role': r['candidate_role'],
                                                     'evidence': r['role_evidence']})
    links = []
    for e in graph.all_edges(root):
        if cid not in (e['child_connection_id'], e['parent_connection_id']) or e['id'] in decided: continue
        if kinds.get(e['child_table'], {}).get('kind') == 'duplicate' and e['child_connection_id'] == cid: continue
        links.append({'edge_id': e['id'], 'child': e['child_table'] + '.' + e['child_column'],
                      'parent': e['parent_table'] + '.' + e['parent_column'], 'score': e['score'],
                      'cardinality': 'one_to_one' if e['evidence'].get('cardinality_hint') == 'one_to_one' else 'many_to_one',
                      'coverage': e['evidence'].get('distinct_coverage_pct'),
                      'suggested': e['score'] >= STRONG and (e['evidence'].get('distinct_coverage_pct') or 0) >= FULL_COVERAGE,
                      'reasons': e['evidence'].get('reasons', [])})
    new_concepts = [t for t in rep['tables'] if t['kind'] in ('entity', 'reference') and t['own_key']]
    return {'impact': rep, 'connection_id': cid, 'roles': roles, 'links': links,
            'concepts': [{'table': t['table'], 'name': t['table'].replace('_', ' '), 'key': t['own_key']} for t in new_concepts],
            'coded': [{'table': t['table'], **c} for t in rep['tables'] if t['kind'] != 'duplicate' for c in t['coded_columns']]}


def approve_roles(root, cid, table, items, by, note):
    done = 0
    current = {r['column']: r for r in store.list_roles(root, connection_id=cid, table=table, limit=10 ** 6)['items']}
    for it in items:
        cur = current[it['column']]
        rev = cur['decision']['revision'] if cur['decision'] else 0
        store.decide_column(root, cid, table, it['column'], action='approve', actor=by, note=note,
                            expected_revision=rev, role=it['role'])
        done += 1
    return done


def approve_link(root, link, by, note):
    store.decide_relationship(root, link['edge_id'], action='approve', actor=by, note=note, expected_revision=0,
                              cardinality=link['cardinality'])


def create_concept(root, cid, c, by, note, domain='Unassigned'):
    approved = [r for r in store.list_roles(root, connection_id=cid, table=c['table'], limit=10 ** 6)['items']
                if r['effective_status'] == 'approved' and r['column'] != c['key']]
    attrs = [{'name': r['column'].replace('_', ' ').lower(), 'connection_id': cid, 'table': c['table'], 'column': r['column']}
             for r in approved][:concept_store.MAX_ATTRIBUTES]
    concept = {'name': c['name'], 'description': 'Rows of %s (onboarded)' % c['table'], 'domain': domain, 'owner': by,
               'aliases': [], 'primary': {'connection_id': cid, 'table': c['table'], 'key_column': c['key']},
               'secondary': [], 'attributes': attrs}
    created = concept_store.create(root, concept, actor=by, note=note)
    return concept_store.decide(root, created['id'], action='approve', actor=by, note=note,
                                expected_revision=created['revision'])


def create_term(root, cid, table, column, value, words, by, note, domain='Unassigned'):
    words = [w.strip() for w in words if w.strip()]
    term = {'name': words[0][:100].capitalize(), 'description': '%s.%s = %s' % (table, column, value), 'domain': domain,
            'owner': by, 'aliases': words[1:], 'table': {'connection_id': cid, 'table': table},
            'conditions': [{'column': column, 'op': 'eq', 'value': value}]}
    created = term_store.create(root, term, actor=by, note=note)
    return term_store.decide(root, created['id'], action='approve', actor=by, note=note, expected_revision=created['revision'])


def apply(root, props, by, note='Accepted during onboarding', concepts=True):
    """Accept every suggestion in `props` (roles, strong links, new concepts). Returns counts."""
    root = Path(root).resolve(); cid = props['connection_id']
    out = {'roles': 0, 'links': 0, 'concepts': 0, 'left_pending': []}
    for table, items in props['roles'].items():
        out['roles'] += approve_roles(root, cid, table, items, by, note)
    for link in props['links']:
        if not link['suggested']:
            out['left_pending'].append('%s -> %s (score %.2f, %s%% of values found)' % (link['child'], link['parent'], link['score'], link.get('coverage'))); continue
        approve_link(root, link, by, note); out['links'] += 1
    if concepts:
        for c in props['concepts']:
            try:
                create_concept(root, cid, c, by, note); out['concepts'] += 1
            except (store.ReviewValidation, store.ReviewConflict) as e:
                out['left_pending'].append('concept %s: %s' % (c['name'], e))
    return out
