# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Business concepts with source authority (Binggio v18.1).

A concept says which approved table is the PRIMARY source for a business entity (the
golden table) and which tables are SECONDARY (copies, history, enrichment or excluded),
with the business words people use for it (aliases) and for its attributes.

Concepts are registry artifacts like column roles and relationships: drafted, approved,
rejected, revised, revoked or reopened by a named steward, versioned and audited. The
approval is bound to the current profiles of every referenced table (same signature as
business definitions), so a data change makes the concept stale until re-confirmed.
Nothing here is ever auto-approved, and nothing here executes queries.
"""
from __future__ import annotations
import re
import uuid
from pathlib import Path
from binggio.registry import store

KIND = 'concept'
SECONDARY_USES = {'copy', 'history', 'enrichment', 'excluded'}
MAX_ALIASES, MAX_SECONDARY, MAX_ATTRIBUTES = 20, 10, 25


def normalize_term(text):
    """Lower case, words only, single spaces: 'Risk-Score ' -> 'risk score'."""
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', str(text).lower()).split())


def _aliases(values, what):
    if values is None: return []
    if not isinstance(values, list) or len(values) > MAX_ALIASES:
        raise store.ReviewValidation(what + ' must be a list of at most %d words or phrases' % MAX_ALIASES)
    out = []
    for v in values:
        t = normalize_term(store._require_text(v, what, 60))
        if not t: raise store.ReviewValidation(what + ' must contain letters or digits')
        if t not in out: out.append(t)
    return out


def _ref(project, ref, what, column_key='column'):
    if not isinstance(ref, dict): raise store.ReviewValidation(what + ' must be an object')
    cid = store._require_text(ref.get('connection_id'), what + ' connection', 100)
    table = store._require_text(ref.get('table'), what + ' table', 120)
    column = store._require_text(ref.get(column_key), what + ' ' + column_key.replace('_', ' '), 120)
    store._column(project, cid, table, column)  # must exist in the current profile
    return cid, table, column


def validate(project, concept):
    """Return the normalised concept, or raise ReviewValidation / ReviewConflict."""
    if not isinstance(concept, dict): raise store.ReviewValidation('Concept must be an object')
    name = store._require_text(concept.get('name'), 'Concept name', 100)
    out = {'kind': KIND, 'name': name,
           'description': store._require_text(concept.get('description'), 'Concept description', 600),
           'domain': store._require_text(concept.get('domain'), 'Business domain', 80),
           'owner': store._require_text(concept.get('owner'), 'Concept owner', 80),
           'aliases': _aliases(concept.get('aliases'), 'Alias')}
    cid, table, key = _ref(project, concept.get('primary'), 'Primary source', 'key_column')
    out['primary'] = {'connection_id': cid, 'table': table, 'key_column': key}
    tables = {(cid, table)}
    secondary = concept.get('secondary') or []
    if not isinstance(secondary, list) or len(secondary) > MAX_SECONDARY:
        raise store.ReviewValidation('Secondary sources must be a list of at most %d tables' % MAX_SECONDARY)
    out['secondary'] = []
    for s in secondary:
        scid, stable, skey = _ref(project, s, 'Secondary source', 'key_column')
        use = s.get('use')
        if use not in SECONDARY_USES: raise store.ReviewValidation('Secondary use must be one of ' + ', '.join(sorted(SECONDARY_USES)))
        if (scid, stable) in tables: raise store.ReviewValidation('A table can appear only once in a concept: ' + stable)
        tables.add((scid, stable))
        out['secondary'].append({'connection_id': scid, 'table': stable, 'key_column': skey, 'use': use,
                                 'note': store._require_text(s.get('note', ''), 'Secondary note', 300, 0)})
    attributes = concept.get('attributes') or []
    if not isinstance(attributes, list) or len(attributes) > MAX_ATTRIBUTES:
        raise store.ReviewValidation('Attributes must be a list of at most %d entries' % MAX_ATTRIBUTES)
    out['attributes'], seen = [], set()
    for a in attributes:
        acid, atable, acol = _ref(project, a, 'Attribute')
        aname = normalize_term(store._require_text(a.get('name'), 'Attribute name', 60))
        if aname in seen: raise store.ReviewValidation('Duplicate attribute name: ' + aname)
        seen.add(aname)
        out['attributes'].append({'name': aname, 'aliases': _aliases(a.get('aliases'), 'Attribute alias'),
                                  'connection_id': acid, 'table': atable, 'column': acol})
    refs = [(out['primary']['connection_id'], out['primary']['table'], out['primary']['key_column'])]
    refs += [(s['connection_id'], s['table'], s['key_column']) for s in out['secondary']]
    refs += [(a['connection_id'], a['table'], a['column']) for a in out['attributes']]
    # 'sources' lets the registry bind the approval to the current profiles of every
    # referenced table (store._definition_signature), exactly like a business definition.
    out['sources'] = [{'connection_id': c, 'table': t, 'column': col} for c, t, col in dict.fromkeys(refs)]
    out['scope'] = 'local_demo_semantic_metadata'
    return out


def _approved_others(db, project, aid):
    rows = db.execute('SELECT * FROM artifacts WHERE kind=? AND status=? AND id<>?', (KIND, 'approved', aid)).fetchall()
    others = []
    for r in rows:
        row = store._get_row(r)
        if store._status(project, row) == 'approved': others.append(row['approved'])
    return others


def _check_conflicts(proposed, others):
    """Governance rules: one primary owner per table; unique concept names."""
    pt = (proposed['primary']['connection_id'], proposed['primary']['table'])
    for o in others:
        if o['name'].casefold() == proposed['name'].casefold():
            raise store.ReviewConflict('Another approved concept is already named "%s"; revise or revoke it first' % o['name'])
        if (o['primary']['connection_id'], o['primary']['table']) == pt:
            raise store.ReviewConflict('%s is already the primary source of concept "%s"; a table can be primary for one concept only'
                                       % (pt[1], o['name']))


def create(project, concept, *, actor, note):
    actor = store._require_text(actor, 'Author', 80); note = store._require_text(note, 'Creation rationale', 600)
    item = validate(project, concept)
    aid = 'concept:' + uuid.uuid4().hex
    def op(db):
        return store._public(store._save(db, artifact_id=aid, kind=KIND, subject=item['name'], action='draft', actor=actor,
                                         note=note, expected_revision=0, proposed=item, approved=None, signature=None))
    return store._transact(project, op)


def decide(project, aid, *, action, actor, note, expected_revision, concept=None):
    if action not in ('approve', 'reject', 'revise', 'revoke', 'reopen'): raise store.ReviewValidation('Invalid concept action')
    actor = store._require_text(actor, 'Reviewer', 80); note = store._require_text(note, 'Review rationale', 600)
    if not isinstance(expected_revision, int) or expected_revision < 1: raise store.ReviewValidation('Invalid revision')
    def op(db):
        current = store._get(db, aid)
        if current is None or current['kind'] != KIND: raise store.ReviewConflict('Concept not found')
        if action == 'revoke' and current['status'] != 'approved': raise store.ReviewConflict('Only approved concepts can be revoked')
        if action == 'reopen' and current['status'] not in ('rejected', 'revoked'):
            raise store.ReviewConflict('Only rejected/revoked concepts can be reopened')
        if action == 'revise' and concept is None: raise store.ReviewValidation('Supply a revised concept')
        proposed = validate(project, concept) if concept is not None else current['proposed']
        if action == 'approve': _check_conflicts(proposed, _approved_others(db, project, aid))
        signature = store._definition_signature(project, proposed) if action == 'approve' else None
        return store._public(store._save(db, artifact_id=aid, kind=KIND, subject=proposed['name'], action=action, actor=actor,
                                         note=note, expected_revision=expected_revision, proposed=proposed,
                                         approved=proposed if action == 'approve' else None, signature=signature))
    return store._transact(project, op)


def list_concepts(project):
    return [x for x in store._rows(project, KIND)]
