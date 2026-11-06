# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Data fixes Binggio recommends, from what profiling found on every table.

    recommend(root, connection=None, table=None) -> [recommendation]
    approve(root, rec_ids, actor, note)          -> the fixes added (a person's decision)
    dismiss(root, rec_ids, actor, note)          -> set aside; not recommended again

A recommendation is only a suggestion with its evidence. Nothing changes until a person approves it,
and then it is an ordinary data fix: recorded with their name and reason, applied to the prepared
copy (never the source), and removable. Recommendations stay away from changes that could damage a
key: numbers are never "read as numbers" in identifier columns, and case is changed only where
values differ by case alone.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from binggio import prepared
from binggio.catalog import catalog
from binggio.profiling import shapes

KEY_ROLES = ('PRIMARY_KEY_CANDIDATE', 'FOREIGN_KEY_CANDIDATE', 'IDENTIFIER')
ORDER = {'digits': 0, 'trim': 1, 'case': 2, 'number': 3, 'replace': 4, 'add_date': 5}


class AdviceError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _db(root):
    db = prepared._fixdb(root)
    db.execute('''CREATE TABLE IF NOT EXISTS dismissed (rec_id TEXT PRIMARY KEY, connection_id TEXT, table_name TEXT, column_name TEXT,
                  kind TEXT, actor TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL)''')
    return db


def _rid(cid, table, kind, column, params):
    raw = json.dumps([cid, table, kind, column, params], sort_keys=True)
    return 'rec:' + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _finding(c, code):
    return next((f for f in c.get('findings') or [] if f.get('code') == code), None)


def _keyish(c):
    return c['semantic_role_candidate'] in KEY_ROLES or bool(shapes.id_ending(c['name'])) or shapes.is_code(c['name'])


def _day_month_order(root, p, column):
    """'dm' when some value has a first part above 12, 'md' when some second part is, None when no value
    tells, 'both' when the column mixes them. Read on every row of the prepared copy."""
    try:
        conn = next(c for c in catalog.list_connections(root) if c['id'] == p['connection_id'])
        path = prepared.ensure(root, conn, p['table'])
        c = "replace(replace(trim(%s), '-', '/'), '.', '/')" % prepared._quote_identifier(column)
        with prepared.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            a, b = db.execute("SELECT MAX(CAST(substr(%s, 1, instr(%s, '/') - 1) AS INTEGER) > 12), "
                              "MAX(CAST(substr(%s, instr(%s, '/') + 1) AS INTEGER) > 12) FROM t WHERE instr(%s, '/') > 0" % (c, c, c, c, c)).fetchone()
    except Exception:
        return None
    if a and b: return 'both'
    return 'dm' if a else 'md' if b else None


def _for_table(root, p, masters_hints, calendar=False):
    cid, table = p['connection_id'], p['table']
    active = {(f['kind'], f['column']) for f in prepared.fixes_for(root, cid, table)}
    header = {c['name'] for c in p['columns']}
    out = []

    def add(kind, column, params, why, count, impact):
        if (kind, column) in active: return
        if kind == 'add_date' and column + '_date' in header: return
        out.append({'id': _rid(cid, table, kind, column, params), 'connection': cid, 'table': table, 'column': column, 'kind': kind,
                    'params': params, 'label': prepared.describe({'kind': kind, 'column': column,
                                                                  'params': {**params, 'name': column + '_date'} if kind == 'add_date' else params}),
                    'why': why, 'rows': count, 'impact': impact})

    for c in p['columns']:
        if c.get('no_information'): continue
        n = c['name']
        ws, coll = _finding(c, 'whitespace'), _finding(c, 'normalized_collisions')
        sample = '' if c.get('counts_scope') == 'all_rows' and not ws else ' (in the rows read)'
        if ws:
            add('trim', n, {}, '%s value(s) start or end with spaces%s, so "C001 " and "C001" count as different values%s.'
                % ('{:,}'.format(ws['count']), sample, ' and do not join' if _keyish(c) else ''), ws['count'], 'keys' if _keyish(c) else 'tidy')
        if coll and not ws:
            add('case', n, {'to': 'upper'}, '%s value(s) differ from others only by upper/lower case ("Open" and "OPEN"); one case makes them one value.'
                % '{:,}'.format(coll['count']), coll['count'], 'keys' if _keyish(c) else 'categories')
        fn = _finding(c, 'formatted_numbers')
        if fn and not _keyish(c) and not shapes.is_email(n):
            add('number', n, {}, '%s value(s) are numbers written with currency signs, separators, units or brackets, so they cannot be added up. '
                'Reading them as numbers makes this column a measure (check the preview: values with no number become empty).'
                % '{:,}'.format(fn['count']), fn['count'], 'measures')
        ph = _finding(c, 'placeholder_values')
        if ph and ph.get('values'):
            add('replace', n, {'values': ph['values'], 'to': ''}, '%s value(s) are placeholders meaning "no value" (%s); as text they count as a real value.'
                % ('{:,}'.format(ph['count']), ', '.join(ph['values'])), ph['count'], 'missing')
        dt = _finding(c, 'date_as_text')
        fmts = c.get('date_formats') or {}
        fmt = max(fmts, key=fmts.get) if fmts else ''
        if dt and c['semantic_role_candidate'] == 'DATE':
            params, extra = {}, ''
            if fmt.startswith(('%d/%m', '%d-%m', '%d.%m')):
                order = _day_month_order(root, p, n)
                sep = fmt[2]
                chosen = {'dm': '%d' + sep + '%m' + sep + '%Y', 'md': '%m' + sep + '%d' + sep + '%Y'}.get(order)
                if chosen and '%H' in fmt: chosen += ' %H:%M:%S'
                if chosen in prepared.DATE_INPUTS:
                    params = {'format': chosen}
                    extra = ' Day and month order checked on every row: %s first.' % ('day' if order == 'dm' else 'month')
                else:
                    extra = (' Day and month order cannot be told from the values (no day above 12): choose the form when you preview it.'
                             if order is None else ' Some values put the day first and others the month: check the source before fixing.')
            add('add_date', n, params, 'Dates are written as %s, so they cannot be compared, filtered by month or linked to a calendar. A new column %s_date '
                'holds them as YYYY-MM-DD; %s is kept as it is.%s' % (_fmt(fmt), n, n, extra), dt['count'], 'dates')
        elif calendar and c['semantic_role_candidate'] == 'DATE' and '%H' in fmt and not shapes.is_period(n):
            add('add_date', n, {}, 'Date and time values (%s) cannot link to the approved calendar, which has one row per day. A new column %s_date holds '
                'the day as YYYY-MM-DD; %s keeps the time.' % (_fmt(fmt), n, n), c.get('non_null_count'), 'dates')
    for h in masters_hints.get((cid, table), []):
        add('digits', h['column'], {'drop_leading_zeros': True}, h['why'], None, 'keys')
    return out


def _fmt(fmt):
    from binggio.profiling.profiler import _fmt_label
    return _fmt_label((fmt or '').split(' (')[0]) or 'text'


def _masters_hints(root):
    """Columns the masters analysis found written differently from their entity (01234567 vs 1234567)."""
    out = {}
    try:
        from binggio import masters
        a = masters.cached_analysis(root)
    except Exception:
        a = None
    for p in (a or {}).get('masters', []):
        sor = p['spec'].get('system_of_record') or {}
        for m in p['spec']['members']:
            if m.get('needs_fix') == 'digits':
                out.setdefault((m['connection'], m['table']), []).append({'column': m['column'], 'why':
                    'Its values match %s.%s only when the digits are compared (for example 01234567 here, 1234567 there). Keeping digits only, '
                    'without leading zeros, lets it link to the %s master.' % (sor.get('table', '?'), sor.get('column', '?'), p['name'])})
    return out


def recommend(root, connection=None, table=None):
    from binggio.profiling import store as profiles
    try:
        from binggio import masters
        mc = masters.master_connection(root, create=False)
    except Exception:
        mc = None
    with _db(root) as db:
        gone = {r[0] for r in db.execute('SELECT rec_id FROM dismissed')}
    hints = _masters_hints(root)
    try: calendar = any(m['kind'] == 'time' for m in masters.approved(root))
    except Exception: calendar = False
    out = []
    for t in profiles.list_profiles(root, connection_id=connection):
        if table and t['table'] != table: continue
        if mc and t['connection_id'] == mc['id']: continue          # master tables are built by Binggio, not fixed
        if t['status'] != 'profiled': continue
        p = profiles.get_table(root, t['connection_id'], t['table'])
        if p: out.extend(r for r in _for_table(root, p, hints, calendar) if r['id'] not in gone)
    out.sort(key=lambda r: (r['table'], ORDER.get(r['kind'], 9), r['column']))
    return out


def _pick(root, rec_ids):
    by = {r['id']: r for r in recommend(root)}
    missing = [i for i in rec_ids if i not in by]
    if missing: raise AdviceError('%d recommendation(s) no longer apply (the data or its fixes changed); reload the list' % len(missing))
    return [by[i] for i in rec_ids]


def approve(root, rec_ids, actor, note, params=None):
    """Add the recommended fixes as data fixes. `params` may override a recommendation's settings
    ({rec_id: {...}}), for example the date form the person chose in the preview."""
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise AdviceError('Set your name first: data fixes are recorded with it')
    if not note: raise AdviceError('Say why these fixes are right (kept with each fix)')
    recs = _pick(root, rec_ids)
    conns = {c['id']: c for c in catalog.list_connections(root)}
    added, tables = [], []
    for r in recs:
        prm = dict(r['params']); prm.update((params or {}).get(r['id']) or {})
        f = prepared.add_fix(root, conns[r['connection']], r['table'], r['kind'], r['column'], prm, actor,
                             '%s (recommended by Binggio: %s)' % (note, r['why'][:300]))
        added.append(f)
        if (r['connection'], r['table']) not in tables: tables.append((r['connection'], r['table']))
    return {'added': added, 'tables': tables}


def dismiss(root, rec_ids, actor, note):
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise AdviceError('Set your name first')
    if not note: raise AdviceError('Say why the fix is not needed')
    recs = _pick(root, rec_ids)
    with _db(root) as db:
        for r in recs:
            db.execute('INSERT OR REPLACE INTO dismissed VALUES (?,?,?,?,?,?,?,?)',
                       (r['id'], r['connection'], r['table'], r['column'], r['kind'], actor[:80], note[:600], _now()))
    return {'dismissed': len(recs)}
