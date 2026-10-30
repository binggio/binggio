# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Governed business terms and the approved as-of date (Binggio v18.3).

A term gives a business word ("overdue", "default") one exact, steward-approved meaning:
a set of conditions on the columns of one approved table, optionally on the latest row
per entity, and optionally relative to the approved as-of date. It is never free SQL.

The as-of date is the "today" every date-relative term uses. It is a registry setting,
fixed and stated in answers, so results are reproducible; it is bound to the data of the
tables it was derived from, so a data refresh makes it stale until a steward confirms
the new date (normally the extract date of the refreshed data).

Both are registry artifacts like concepts: drafted, approved, rejected, revised, revoked
or reopened by a named steward, versioned, audited, bound to current profiles, and never
auto-approved.
"""
from __future__ import annotations
import datetime
import uuid
from binggio.registry import store
from binggio.registry.concepts import normalize_term, _aliases

TERM, AS_OF = 'term', 'as_of'
AS_OF_ID = 'setting:as_of'
OPS = {'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'is_null', 'not_null'}
MAX_CONDITIONS = 8


def _col(project, cid, table, column, what):
    store._column(project, cid, table, column)
    return column


def _scalar(v):
    return v is None or (type(v) in (str, int, float, bool) and (not isinstance(v, str) or len(v) <= 120))


def validate_term(project, term):
    if not isinstance(term, dict): raise store.ReviewValidation('Term must be an object')
    out = {'kind': TERM,
           'name': store._require_text(term.get('name'), 'Term name', 100),
           'description': store._require_text(term.get('description'), 'Meaning in plain words', 600),
           'domain': store._require_text(term.get('domain'), 'Business domain', 80),
           'owner': store._require_text(term.get('owner'), 'Term owner', 80),
           'aliases': _aliases(term.get('aliases'), 'Alias')}
    t = term.get('table')
    if not isinstance(t, dict): raise store.ReviewValidation('Term needs the table it applies to')
    cid = store._require_text(t.get('connection_id'), 'Connection', 100)
    table = store._require_text(t.get('table'), 'Table', 120)
    out['table'] = {'connection_id': cid, 'table': table}
    conds = term.get('conditions')
    if not isinstance(conds, list) or not 1 <= len(conds) <= MAX_CONDITIONS:
        raise store.ReviewValidation('A term needs 1-%d conditions' % MAX_CONDITIONS)
    used = []
    out['conditions'] = []
    for c in conds:
        if not isinstance(c, dict): raise store.ReviewValidation('Condition must be an object')
        column = _col(project, cid, table, store._require_text(c.get('column'), 'Condition column', 120), 'Condition')
        op = c.get('op')
        if op not in OPS: raise store.ReviewValidation('Condition operator must be one of ' + ', '.join(sorted(OPS)))
        kinds = [k for k in ('value', 'value_column', 'as_of_offset_days') if k in c]
        if op in ('is_null', 'not_null'):
            if kinds: raise store.ReviewValidation('is_null / not_null take no value')
            out['conditions'].append({'column': column, 'op': op}); used.append(column); continue
        if len(kinds) != 1: raise store.ReviewValidation('Give exactly one of value, value_column or as_of_offset_days')
        cond = {'column': column, 'op': op}
        if kinds[0] == 'value':
            v = c['value']
            if op in ('in', 'not_in'):
                if not isinstance(v, list) or not 1 <= len(v) <= 20 or not all(_scalar(x) for x in v):
                    raise store.ReviewValidation('in / not_in need a list of 1-20 values')
            elif not _scalar(v): raise store.ReviewValidation('Condition value must be a short literal')
            cond['value'] = v
        elif kinds[0] == 'value_column':
            if op in ('in', 'not_in'): raise store.ReviewValidation('Column comparisons use eq, ne, gt, gte, lt or lte')
            cond['value_column'] = _col(project, cid, table, store._require_text(c['value_column'], 'Compared column', 120), 'Compared')
            used.append(cond['value_column'])
        else:
            n = c['as_of_offset_days']
            if type(n) is not int or not -3660 <= n <= 3660 or op in ('in', 'not_in'):
                raise store.ReviewValidation('as_of_offset_days must be a whole number of days (-3660..3660) with a comparison operator')
            cond['as_of_offset_days'] = n
        out['conditions'].append(cond); used.append(column)
    latest = term.get('latest')
    if latest is not None:
        if not isinstance(latest, dict): raise store.ReviewValidation('latest must be an object')
        out['latest'] = {'partition_column': _col(project, cid, table, store._require_text(latest.get('partition_column'), 'Partition column', 120), 'Latest'),
                         'date_column': _col(project, cid, table, store._require_text(latest.get('date_column'), 'Date column', 120), 'Latest')}
        if out['latest']['partition_column'] == out['latest']['date_column']:
            raise store.ReviewValidation('Latest needs different partition and date columns')
        used += list(out['latest'].values())
    else:
        out['latest'] = None
    out['needs_as_of'] = bool(out['latest']) or any('as_of_offset_days' in c for c in out['conditions'])
    out['sources'] = [{'connection_id': cid, 'table': table, 'column': c} for c in dict.fromkeys(used)]
    out['scope'] = 'local_demo_semantic_metadata'
    return out


def validate_as_of(project, setting):
    if not isinstance(setting, dict): raise store.ReviewValidation('As-of setting must be an object')
    raw = store._require_text(setting.get('date'), 'As-of date', 10)
    try: day = datetime.date.fromisoformat(raw)
    except ValueError: raise store.ReviewValidation('As-of date must be YYYY-MM-DD')
    if day > datetime.date.today(): raise store.ReviewValidation('As-of date cannot be in the future')
    refs = setting.get('sources')
    if not isinstance(refs, list) or not 1 <= len(refs) <= 20: raise store.ReviewValidation('List 1-20 date columns the as-of date was derived from')
    sources = []
    for r in refs:
        if not isinstance(r, dict): raise store.ReviewValidation('Source must be an object')
        cid = store._require_text(r.get('connection_id'), 'Connection', 100); table = store._require_text(r.get('table'), 'Table', 120)
        column = _col(project, cid, table, store._require_text(r.get('column'), 'Column', 120), 'Source')
        sources.append({'connection_id': cid, 'table': table, 'column': column})
    return {'kind': AS_OF, 'name': 'As-of date', 'date': day.isoformat(),
            'description': store._require_text(setting.get('description'), 'Reason', 600), 'sources': sources,
            'scope': 'local_demo_semantic_metadata'}


def _approved_rows(db, project, kind, exclude):
    rows = db.execute('SELECT * FROM artifacts WHERE kind=? AND status=? AND id<>?', (kind, 'approved', exclude)).fetchall()
    return [store._get_row(r) for r in rows if store._status(project, store._get_row(r)) == 'approved']


def create(project, item, *, actor, note):
    actor = store._require_text(actor, 'Author', 80); note = store._require_text(note, 'Creation rationale', 600)
    kind = (item or {}).get('kind', TERM)
    if kind == AS_OF:
        proposed, aid = validate_as_of(project, item), AS_OF_ID
    else:
        proposed, aid = validate_term(project, item), 'term:' + uuid.uuid4().hex
    def op(db):
        if aid == AS_OF_ID and store._get(db, aid) is not None:
            raise store.ReviewConflict('The as-of setting exists; revise it instead')
        return store._public(store._save(db, artifact_id=aid, kind=proposed['kind'], subject=proposed['name'], action='draft', actor=actor,
                                         note=note, expected_revision=0, proposed=proposed, approved=None, signature=None))
    return store._transact(project, op)


def decide(project, aid, *, action, actor, note, expected_revision, item=None):
    if action not in ('approve', 'reject', 'revise', 'revoke', 'reopen'): raise store.ReviewValidation('Invalid action')
    actor = store._require_text(actor, 'Reviewer', 80); note = store._require_text(note, 'Review rationale', 600)
    if not isinstance(expected_revision, int) or expected_revision < 1: raise store.ReviewValidation('Invalid revision')
    def op(db):
        current = store._get(db, aid)
        if current is None or current['kind'] not in (TERM, AS_OF): raise store.ReviewConflict('Term or as-of setting not found')
        if action == 'revoke' and current['status'] != 'approved': raise store.ReviewConflict('Only approved items can be revoked')
        if action == 'reopen' and current['status'] not in ('rejected', 'revoked'): raise store.ReviewConflict('Only rejected/revoked items can be reopened')
        if action == 'revise' and item is None: raise store.ReviewValidation('Supply the revised item')
        if item is not None:
            proposed = validate_as_of(project, item) if current['kind'] == AS_OF else validate_term(project, item)
        else:
            proposed = current['proposed']
        if action == 'approve' and current['kind'] == TERM:
            for other in _approved_rows(db, project, TERM, aid):
                if other['approved']['name'].casefold() == proposed['name'].casefold():
                    raise store.ReviewConflict('Another approved term is already named "%s"' % proposed['name'])
        signature = store._definition_signature(project, proposed) if action == 'approve' else None
        return store._public(store._save(db, artifact_id=aid, kind=current['kind'], subject=proposed['name'], action=action, actor=actor,
                                         note=note, expected_revision=expected_revision, proposed=proposed,
                                         approved=proposed if action == 'approve' else None, signature=signature))
    return store._transact(project, op)


def list_terms(project):
    return store._rows(project, TERM) + store._rows(project, AS_OF)
