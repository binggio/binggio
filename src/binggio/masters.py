# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Master tables: one row per employee, book, desk ... (and a calendar), proposed by Binggio and
approved by a person.

Enterprise extracts often have no column that is unique on every row: the same book appears once
per reporting month and once per version. A master is the distinct list of an entity's
identifiers, so it is unique by construction, and every table that mentions the entity joins to it
many-to-one.

    recommend(root)             -> proposals (entity masters, a calendar, snapshot tables), nothing saved
    decide(root, ...)           -> approve (with the person's edits), reject or withdraw a master
    build(root)                 -> the approved masters as CSV files in the "Binggio masters" source
    link_members(root, ...)     -> links from each approved member column to its master

Rules kept here:
* Nothing is built or linked without a person's approval, recorded with their name and reason.
* A master never changes the source data. It is rebuilt from the prepared copies (data fixes
  applied) on every refresh; a changed master makes approvals built on it stale, like any source.
* Membership is checked on every row. Where the values of a column are not all in the system of
  record, the master includes them and marks them (in_system_of_record = no): gaps are shown, not
  hidden.
* Integer identifiers that fill most of their range overlap with any other counter by chance, so
  for them a value match is evidence only together with the name or the domain.
"""
from __future__ import annotations
from contextlib import closing
import csv
import json
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from binggio import prepared
from binggio.catalog import catalog
from binggio.profiling import shapes

MASTER_SOURCE = 'Binggio masters'
MAX_DISTINCT = 2_000_000
PEOPLE = ('employee', 'worker', 'person', 'staff', 'trader', 'bookrunner', 'runner', 'controller', 'manager', 'owner', 'approver',
          'delegate', 'contact', 'escalation', 'certifiedby', 'actionby', 'user', 'analyst', 'head', 'reviewer', 'sponsor', 'member')
_LOCK = threading.RLock()


class MasterError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _db(root):
    p = Path(root).resolve() / 'data' / 'phase7_governance' / 'masters.sqlite3'
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS masters (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                  status TEXT NOT NULL, spec_json TEXT NOT NULL, actor TEXT, note TEXT, decided_at TEXT, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS snapshots (connection_id TEXT NOT NULL, table_name TEXT NOT NULL, period_column TEXT NOT NULL,
                  status TEXT NOT NULL, actor TEXT, note TEXT, decided_at TEXT, PRIMARY KEY(connection_id, table_name))''')
    db.execute('''CREATE TABLE IF NOT EXISTS history (at TEXT NOT NULL, master_id TEXT, action TEXT NOT NULL, actor TEXT, note TEXT, detail_json TEXT)''')
    return db


def _slug(name):
    s = re.sub(r'[^A-Za-z0-9]+', '_', name.strip()).strip('_').upper()
    return s[:40] or 'MASTER'


def _norm(v):
    if prepared._is_missing(v): return None
    s = str(v).strip().casefold()
    return s or None


def _digits(v):
    d = re.sub(r'\D', '', v).lstrip('0')
    return d or None


# ====================================================================== the person's decisions

def list_masters(root, status=None):
    with _LOCK, _db(root) as db:
        rows = db.execute('SELECT * FROM masters ORDER BY created_at').fetchall()
    out = []
    for r in rows:
        if status and r['status'] != status: continue
        out.append({'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'status': r['status'], 'spec': json.loads(r['spec_json']),
                    'actor': r['actor'], 'note': r['note'], 'decided_at': r['decided_at']})
    return out


def approved(root):
    return [m for m in list_masters(root, 'approved')]


def snapshot_tables(root, status='approved'):
    with _LOCK, _db(root) as db:
        rows = db.execute('SELECT * FROM snapshots WHERE status=?', (status,)).fetchall()
    return [dict(r) for r in rows]


def _clean_spec(root, kind, spec):
    """Check what the person approves: names, columns that exist, sensible settings."""
    spec = dict(spec or {})
    name = str(spec.get('name') or '').strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9 _-]{0,39}', name): raise MasterError('Give the master a short name (letters, digits, spaces)')
    spec['name'] = name
    if kind == 'time':
        members = [m for m in spec.get('members') or [] if m.get('include', True)]
        for m in members: _check_column(root, m)
        start = int(spec.get('fiscal_year_start_month') or 1)
        if not 1 <= start <= 12: raise MasterError('The fiscal year starts in a month 1 to 12')
        spec['fiscal_year_start_month'] = start
        spec['members'] = members
        return spec
    sor = spec.get('system_of_record')
    if sor: _check_column(root, sor)
    members = [m for m in spec.get('members') or [] if m.get('include', True)]
    if not members and not sor: raise MasterError('A master needs a system of record or at least one member column')
    for m in members:
        _check_column(root, m)
        m['label'] = str(m.get('label') or m['column'])[:40]
    spec['members'] = members
    spec['include'] = spec.get('include') if spec.get('include') in ('union', 'system_of_record') else 'union'
    spec['version_rule'] = spec.get('version_rule') if spec.get('version_rule') in ('latest', 'first') else 'latest'
    spec['attributes'] = [a for a in spec.get('attributes') or [] if isinstance(a, str)][:20]
    if sor:
        header = _header(root, sor['connection'], sor['table'])
        bad = [a for a in spec['attributes'] if a not in header]
        if bad: raise MasterError('Not a column of %s: %s' % (sor['table'], ', '.join(bad)))
    return spec


def _header(root, cid, table):
    return prepared.header_with_fixes(root, _conn(root, cid), table)


def _conn(root, cid):
    c = next((x for x in catalog.list_connections(root) if x['id'] == cid), None)
    if c is None: raise MasterError('Unknown source')
    return c


def _check_column(root, m):
    for k in ('connection', 'table', 'column'):
        if not m.get(k): raise MasterError('Each column needs its source, table and column')
    if m['column'] not in _header(root, m['connection'], m['table']):
        raise MasterError('%s is not a column of %s' % (m['column'], m['table']))
    for k in ('version_column', 'period_column', 'domain_column'):
        if m.get(k) and m[k] not in _header(root, m['connection'], m['table']):
            raise MasterError('%s is not a column of %s' % (m[k], m['table']))


def decide(root, action, actor, note, master_id=None, kind='entity', spec=None):
    """approve (new or edited), reject, or withdraw (back to not decided). Returns the master."""
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise MasterError('Set your name first: decisions on masters are recorded with it')
    if not note: raise MasterError('Say why (kept with the decision)')
    with _LOCK, _db(root) as db:
        row = db.execute('SELECT * FROM masters WHERE id=?', (master_id,)).fetchone() if master_id else None
        if action == 'approve':
            kind = row['kind'] if row else kind
            spec = _clean_spec(root, kind, spec if spec is not None else json.loads(row['spec_json']))
            clash = db.execute("SELECT id FROM masters WHERE status='approved' AND upper(name)=upper(?) AND id<>?", (spec['name'], master_id or '')).fetchone()
            if clash: raise MasterError('There is already an approved master called %s' % spec['name'])
            mid = master_id or 'master:' + uuid.uuid4().hex[:12]
            if row:
                db.execute("UPDATE masters SET name=?, status='approved', spec_json=?, actor=?, note=?, decided_at=? WHERE id=?",
                           (spec['name'], json.dumps(spec), actor[:80], note[:600], _now(), mid))
            else:
                db.execute('INSERT INTO masters VALUES (?,?,?,?,?,?,?,?,?)',
                           (mid, spec['name'], kind, 'approved', json.dumps(spec), actor[:80], note[:600], _now(), _now()))
        elif action in ('reject', 'withdraw'):
            if row is None and action == 'reject' and spec:
                mid = 'master:' + uuid.uuid4().hex[:12]
                db.execute('INSERT INTO masters VALUES (?,?,?,?,?,?,?,?,?)', (mid, str(spec.get('name') or 'master')[:40], kind, 'rejected',
                                                                            json.dumps(spec), actor[:80], note[:600], _now(), _now()))
            elif row is None:
                raise MasterError('Unknown master')
            else:
                mid = row['id']
                db.execute('UPDATE masters SET status=?, actor=?, note=?, decided_at=? WHERE id=?',
                           ('rejected' if action == 'reject' else 'withdrawn', actor[:80], note[:600], _now(), mid))
        else:
            raise MasterError('Unknown action')
        db.execute('INSERT INTO history VALUES (?,?,?,?,?,?)', (_now(), mid, action, actor[:80], note[:600], json.dumps(spec or {})[:20000]))
    return next(m for m in list_masters(root) if m['id'] == mid)


def decide_snapshot(root, connection, table, period_column, action, actor, note):
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise MasterError('Set your name first')
    if not note: raise MasterError('Say why (kept with the decision)')
    if action == 'approve' and period_column not in _header(root, connection, table):
        raise MasterError('%s is not a column of %s' % (period_column, table))
    status = {'approve': 'approved', 'reject': 'rejected', 'withdraw': 'withdrawn'}.get(action)
    if not status: raise MasterError('Unknown action')
    with _LOCK, _db(root) as db:
        db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)', (connection, table, period_column, status, actor[:80], note[:600], _now()))
        db.execute('INSERT INTO history VALUES (?,?,?,?,?,?)', (_now(), None, 'snapshot_' + action, actor[:80], note[:600],
                                                                json.dumps({'table': table, 'period': period_column})))
    return {'connection': connection, 'table': table, 'period_column': period_column, 'status': status}


def history(root, limit=200):
    with _LOCK, _db(root) as db:
        return [dict(r) for r in db.execute('SELECT * FROM history ORDER BY at DESC LIMIT ?', (limit,))]


# ====================================================================== proposals

def _profiles(root):
    from binggio.profiling import store as profiles
    out = []
    mc = master_connection(root, create=False)
    for t in profiles.list_profiles(root):
        if mc and t['connection_id'] == mc['id']: continue
        p = profiles.get_table(root, t['connection_id'], t['table'])
        if p: out.append(p)
    return out


def _entity_name(col, table_profile, domain_value):
    """A readable name for what an identifier identifies."""
    if domain_value:
        tail = re.split(r'[:./#]', domain_value.strip().casefold())[-1]
        tail = re.sub(r'[^a-z0-9]', '', tail)
        if tail and not tail.isdigit(): return tail
    s = shapes.subject(col['name'])
    if s: return s
    words = [w for w in re.split(r'[^a-z0-9]+', table_profile['table'].casefold()) if w and not w.isdigit()
             and w not in ('ytd', 'mtd', 'qtd', 'brds', 'erds', 'irds', 'tbl', 'table', 'data', 'core', 'master', 'dim', 'fact')]
    return words[-1] if words else table_profile['table']


def _is_person(word):
    w = (word or '').casefold()
    return any(p in w for p in PEOPLE)


def _candidates(root, profs, progress=None):
    """Identifier columns worth considering, with their whole-table distinct values (tables in parallel)."""
    from binggio import parallel
    out = []
    for _, res, err in parallel.run(profs, lambda p: _table_candidates(root, p), progress=progress, label=lambda p: p['table']):
        if err: raise err
        out.extend(res)
    return out


def _table_candidates(root, p):
    out = []
    if True:
        conn = _conn(root, p['connection_id'])
        refs = {g['id_column']: g for g in p.get('groups', []) if g['kind'] == 'reference'}
        period = next((c['name'] for c in p['columns'] if shapes.is_period(c['name'])), None)
        for c in p['columns']:
            role = c['semantic_role_candidate']
            ref = refs.get(c['name'])
            idish = role in ('PRIMARY_KEY_CANDIDATE', 'FOREIGN_KEY_CANDIDATE', 'IDENTIFIER') or ref is not None or \
                (shapes.is_code(c['name']) and (c['distinct_count'] >= 30 or any(g.startswith('code_name:') for g in c.get('groups', []))))
            if not idish or c.get('no_information') or c['distinct_count'] < 3 or shapes.is_version(c['name']) or shapes.is_domain(c['name']):
                continue
            if shapes.is_email(c['name']): continue
            vals = prepared.distinct_values(root, conn, p['table'], c['name'], MAX_DISTINCT)
            if vals is None: continue
            norm = {v for v in (_norm(x) for x in vals) if v}
            if len(norm) < 3: continue
            domain = None
            if ref and ref.get('domain_column'):
                dv = prepared.distinct_values(root, conn, p['table'], ref['domain_column'], 3) or []
                dv = [str(x).strip() for x in dv if not prepared._is_missing(x)]
                domain = dv[0] if len(dv) == 1 else None
            ints = [int(v) for v in norm if v.isdigit() and len(v) <= 15]
            dense = bool(ints) and len(ints) == len(norm) and len(ints) / (max(ints) - min(ints) + 1) > 0.3
            own = bool(c.get('own_identifier')) or role == 'PRIMARY_KEY_CANDIDATE'
            out.append({'connection': p['connection_id'], 'table': p['table'], 'column': c['name'], 'values': norm,
                        'digits': {d for d in (_digits(v) for v in norm) if d}, 'domain': domain, 'dense': dense, 'own': own,
                        'subject': shapes.subject(c['name']), 'entity': _entity_name(c, p, domain),
                        'version_column': (ref or {}).get('version_column'), 'domain_column': (ref or {}).get('domain_column'),
                        'period_column': period if own else None, 'repeats': c['distinct_count'] < c['non_null_count'],
                        'rows': c['non_null_count'], 'distinct': len(norm), 'profile': p, 'width': len(p['columns'])})
    return out


def _match(child, parent):
    """Evidence that `child` refers to what `parent` identifies; None when it does not."""
    if child['domain'] and parent['domain'] and child['domain'].casefold() != parent['domain'].casefold():
        return None
    common = len(child['values'] & parent['values'])
    cov = common / len(child['values'])
    fix = None
    if cov < 0.8 and child['digits'] and parent['digits']:
        dcov = len(child['digits'] & parent['digits']) / len(child['digits'])
        if dcov >= 0.9:
            cov, fix = dcov, 'digits'
    if cov < 0.8: return None
    reasons = ['%s%% of the %s different values are in %s.%s' % (_pct(cov), '{:,}'.format(len(child['values'])), parent['table'], parent['column'])]
    same_domain = bool(child['domain'] and parent['domain'])
    if same_domain: reasons.append('Same domain (%s)' % parent['domain'])
    pe, ce = parent['entity'], child['subject'] or child['entity']
    named = bool(ce and pe and (ce == pe or ce.endswith(pe) or pe.endswith(ce) or (_is_person(ce) and _is_person(pe))))
    if named: reasons.append('The names agree (%s, %s)' % (ce, pe))
    specific = not parent['dense']
    if not (same_domain or named or (specific and cov >= 0.9)):
        return None             # a dense integer range overlaps any other counter: values alone prove nothing
    if not specific and not (same_domain or named): return None
    if parent['dense'] and not same_domain:
        reasons.append('Identifiers are consecutive numbers, so the name was needed as well as the values')
    if fix: reasons.append('Written differently (for example %s here, %s there): needs the data fix "Keep digits only" on %s'
                           % (next(iter(child['values'])), next(iter(parent['values'])), child['column']))
    score = cov + (0.3 if same_domain else 0) + (0.2 if named else 0) + (0.1 if specific else 0)
    return {'coverage': round(cov, 4), 'reasons': reasons, 'score': score, 'needs_fix': fix}


def _pct(x):
    return ('%.1f' % (100 * x)).rstrip('0').rstrip('.')


ANALYSIS_VERSION = 1


def _state_key(root, profs):
    import hashlib
    raw = json.dumps([ANALYSIS_VERSION] + sorted([p['connection_id'], p['table'], p['fingerprint'], p.get('profile_version')] for p in profs),
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def cached_analysis(root):
    """The saved analysis when it still matches every table's data and profile; otherwise None."""
    profs = _profiles(root)
    key = _state_key(root, profs)
    with _LOCK, _db(root) as db:
        db.execute('CREATE TABLE IF NOT EXISTS analysis (k TEXT PRIMARY KEY, v TEXT NOT NULL, built_at TEXT NOT NULL, seconds REAL)')
        r = db.execute('SELECT v FROM analysis WHERE k=?', (key,)).fetchone()
    return json.loads(r[0]) if r else None


def analysis(root, progress=None):
    """Which identifiers belong together, worked out from every row of every table. Slow on large
    data (it reads each identifier column's distinct values), so it is saved and reused until a
    table's data, its data fixes or its profile change. Decisions do not invalidate it: they are
    applied when proposals are listed."""
    import time
    hit = cached_analysis(root)
    if hit is not None: return hit
    started = time.time()
    profs = _profiles(root)
    key = _state_key(root, profs)
    cands = _candidates(root, profs, progress)
    out = {'masters': _propose(cands, set())}
    with _LOCK, _db(root) as db:
        db.execute('CREATE TABLE IF NOT EXISTS analysis (k TEXT PRIMARY KEY, v TEXT NOT NULL, built_at TEXT NOT NULL, seconds REAL)')
        db.execute('DELETE FROM analysis')
        db.execute('INSERT INTO analysis VALUES (?,?,?,?)', (key, json.dumps(out), _now(), round(time.time() - started, 1)))
    return out


def recommend(root, progress=None):
    """Proposed masters, calendar and snapshot tables. Approved and rejected masters are returned as
    they are; proposals never repeat their columns."""
    profs = _profiles(root)
    decided = list_masters(root)
    taken = set()
    for m in decided:
        if m['status'] in ('approved', 'rejected'):
            s = m['spec']
            for x in ([s['system_of_record']] if s.get('system_of_record') else []) + s.get('members', []):
                taken.add((x['connection'], x['table'], x['column']))
    proposals = []
    for p in analysis(root, progress)['masters']:
        spec = p['spec']; sor = spec.get('system_of_record')
        if sor and (sor['connection'], sor['table'], sor['column']) in taken: continue
        members = [m for m in spec['members'] if (m['connection'], m['table'], m['column']) not in taken]
        if not members: continue
        p = json.loads(json.dumps(p)); p['spec']['members'] = members
        p['summary']['member_tables'] = len({m['table'] for m in members})
        proposals.append(p)
    cal = _calendar_proposal(root, profs, decided)
    snaps = _snapshot_proposals(root, profs)
    return {'masters': proposals, 'calendar': cal, 'snapshots': snaps,
            'decided': [m for m in decided if m['status'] in ('approved', 'rejected')]}


def _propose(cands, taken):
    # anchors: the identifier of each table's own rows; the one with most values leads its entity
    # equal counts: the table with fewer rows per identifier and more columns describing it leads (book master: BK_CORE, not BK_ASSO)
    anchors = sorted([c for c in cands if c['own']], key=lambda c: (-c['distinct'], round(c['rows'] / c['distinct'], 1), -c['width'], c['table']))
    groups = []                       # [{'sor': cand, 'members': [(cand, evidence)]}]
    for a in anchors:
        home = None
        for g in groups:
            ev = _match(a, g['sor'])
            if ev and ev['coverage'] >= 0.95 and not ev['needs_fix']:
                home = (g, ev); break
        if home: home[0]['members'].append((a, home[1]))
        else: groups.append({'sor': a, 'members': []})
    placed = {id(a) for a in anchors}
    for c in cands:
        if id(c) in placed: continue
        best = None
        for g in groups:
            if g['sor']['table'] == c['table'] and g['sor']['column'] == c['column']: continue
            ev = _match(c, g['sor'])
            if ev and (best is None or ev['score'] > best[1]['score']): best = (g, ev)
        if best:
            best[0]['members'].append((c, best[1])); placed.add(id(c))
    # entities no table owns (a legal entity mentioned by several tables): same domain or same name, overlapping values
    loose = [c for c in cands if id(c) not in placed and not c['own']]
    for c in loose:
        if id(c) in placed: continue
        mates = []
        for d in loose:
            if d is c or id(d) in placed or d['table'] == c['table']: continue
            same_dom = c['domain'] and d['domain'] and c['domain'].casefold() == d['domain'].casefold()
            same_name = c['subject'] and (d['subject'] == c['subject'] or d['subject'].endswith(c['subject']) or c['subject'].endswith(d['subject']))
            if not (same_dom or same_name): continue
            inter = len(c['values'] & d['values'])
            if inter / min(len(c['values']), len(d['values'])) >= 0.5 and (not c['dense'] or same_dom):
                mates.append((d, {'coverage': round(inter / len(d['values']), 4), 'score': 1,
                                  'reasons': ['Shares %s%% of its values with %s.%s' % (_pct(inter / len(d['values'])), c['table'], c['column'])]
                                  + (['Same domain (%s)' % c['domain']] if same_dom else ['The names agree (%s)' % c['subject']]),
                                  'needs_fix': None}))
        if mates:
            groups.append({'sor': None, 'seed': c, 'members': [(c, {'coverage': 1.0, 'score': 1, 'reasons': ['Mentioned in %s' % c['table']], 'needs_fix': None})] + mates})
            placed.add(id(c)); placed.update(id(d) for d, _ in mates)
    proposals = []
    for g in groups:
        if g['sor'] is not None and not g['members']: continue          # nothing else refers to it: a master adds nothing
        sor = g['sor']
        lead = sor or g['seed']
        if sor and (sor['connection'], sor['table'], sor['column']) in taken: continue
        name = lead['entity'].replace('_', ' ').title()[:40] or 'Entity'
        members = []
        for c, ev in g['members']:
            if (c['connection'], c['table'], c['column']) in taken: continue
            members.append({'connection': c['connection'], 'table': c['table'], 'column': c['column'],
                            'label': (c['subject'] or 'same %s' % name.lower()).replace('_', ' ')[:40], 'coverage': ev['coverage'],
                            'reasons': ev['reasons'], 'needs_fix': ev['needs_fix'], 'include': not ev['needs_fix'],
                            'version_column': c['version_column'], 'domain_column': c['domain_column'], 'repeats': c['repeats']})
        if not members: continue
        used = [c for c, ev in g['members'] if not ev['needs_fix'] and (c['connection'], c['table'], c['column']) not in taken]
        everything = set().union(*[c['values'] for c in used]) | (sor['values'] if sor else set())
        missing = len(everything - sor['values']) if sor else None
        spec = {'name': name, 'members': members, 'include': 'union', 'version_rule': 'latest', 'attributes': []}
        if sor:
            spec['system_of_record'] = {'connection': sor['connection'], 'table': sor['table'], 'column': sor['column'],
                                        'version_column': sor['version_column'], 'period_column': sor['period_column'],
                                        'domain_column': sor['domain_column']}
            spec['attributes'] = _default_attributes(sor['profile'], sor['column'])
        proposals.append({'id': None, 'kind': 'entity', 'status': 'proposed', 'name': name, 'spec': spec,
                          'summary': {'distinct': len(everything), 'from_system_of_record': len(sor['values']) if sor else 0,
                                      'not_in_system_of_record': missing, 'member_tables': len({m['table'] for m in members}),
                                      'system_of_record_repeats': bool(sor and sor['repeats'])},
                          'why': ('%s identifies the rows of %s%s. %d column(s) in %d table(s) refer to the same %s.'
                                  % (sor['column'], sor['table'], ' (repeating once per %s)' % ' and '.join(x for x in (sor['period_column'], sor['version_column']) if x)
                                     if sor['repeats'] and (sor['period_column'] or sor['version_column']) else '', len(members),
                                     len({m['table'] for m in members}), name.lower()) if sor else
                                  'No table owns %s, but %d tables mention it; the master lists every value they use.' % (name.lower(), len({m['table'] for m in members})))})
    proposals.sort(key=lambda p: (-len(p['spec']['members']), p['name']))
    return proposals


def _default_attributes(p, key):
    """Descriptive columns of the system of record worth carrying into the master (names, types, status)."""
    out = []
    for c in p['columns']:
        n = c['name']
        if n == key or c.get('no_information') or shapes.is_version(n) or shapes.is_domain(n) or shapes.is_period(n): continue
        if c['semantic_role_candidate'] in ('TEXT', 'CLASSIFICATION', 'STATUS') and not shapes.id_ending(n):
            out.append(n)
        if len(out) >= 8: break
    return out


def _calendar_proposal(root, profs, decided):
    if any(m['kind'] == 'time' and m['status'] in ('approved', 'rejected') for m in decided): return None
    members = []
    for p in profs:
        for c in p['columns']:
            if c['semantic_role_candidate'] != 'DATE' or c.get('no_information'): continue
            fmts = c.get('date_formats') or {}
            fmt = max(fmts, key=fmts.get) if fmts else None
            plain = bool(fmt) and fmt == '%Y-%m-%d'
            ready = plain
            members.append({'connection': p['connection_id'], 'table': p['table'], 'column': c['name'], 'label': c['name'],
                            'include': ready, 'ready': ready, 'format': fmt,
                            'needs_fix': None if ready else ('add_date' if fmt else 'unknown'),
                            'reasons': ['Dates written YYYY-MM-DD: links to the calendar as they are'] if ready else
                            (['%s; add the data fix "Add a date column" (keeps the original column) to link it' % (
                                'Date and time' if fmt and '%H' in fmt else 'Dates written as %s' % _label(fmt))] if fmt else
                             ['No date format recognised in the values'])})
    if len({m['table'] for m in members}) < 2: return None
    return {'id': None, 'kind': 'time', 'status': 'proposed', 'name': 'Calendar',
            'spec': {'name': 'Calendar', 'members': members, 'fiscal_year_start_month': 1},
            'why': 'Dates in %d tables can share one calendar, so "by month", "this quarter" and "year to date" mean the same in every table.'
                   % len({m['table'] for m in members})}


def _label(fmt):
    from binggio.profiling.profiler import _fmt_label
    return _fmt_label(fmt or '')


def _snapshot_proposals(root, profs):
    done = {(s['connection_id'], s['table_name']): s for st in ('approved', 'rejected') for s in snapshot_tables(root, st)}
    out = []
    for p in profs:
        own = next((c for c in p['columns'] if c.get('own_identifier') and c.get('unique_with')), None)
        if not own: continue
        period = next((x for x in own['unique_with'] if shapes.is_period(x)), None)
        if not period: continue
        d = done.get((p['connection_id'], p['table']))
        out.append({'connection': p['connection_id'], 'table': p['table'], 'period_column': period, 'identifier': own['name'],
                    'status': d['status'] if d else 'proposed', 'actor': (d or {}).get('actor'), 'note': (d or {}).get('note'),
                    'why': '%s repeats once per %s: each %s is a full copy of the table. Adding up a measure across %ss counts '
                           'the same records again, so answers use one %s (the latest, unless a question names one).'
                           % (own['name'], period, period, period, period)})
    return out


# ====================================================================== building

def master_connection(root, create=True):
    c = next((x for x in catalog.list_connections(root) if x['name'] == MASTER_SOURCE), None)
    if c or not create: return c
    folder = Path(root).resolve() / 'data' / 'sources' / 'binggio_masters'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'README.txt').write_text('Built by Binggio from approved masters. Do not edit: rebuilt on every refresh.\n', encoding='utf-8')
    return catalog.add_connection(root, name=MASTER_SOURCE, kind='csv_folder', source_path='sources/binggio_masters')


def _folder(root, conn):
    return Path(root).resolve() / 'data' / conn['source_path']


def table_name(m):
    return 'MASTER_' + _slug(m['spec']['name'] if 'spec' in m else m['name'])


def key_column(m):
    return re.sub(r'[^a-z0-9]+', '_', (m['spec']['name']).casefold()).strip('_') + '_id'


def build(root, progress=None):
    """Write every approved master; remove files of masters no longer approved. Returns what was built."""
    ms = approved(root)
    existing = master_connection(root, create=False)
    if not ms:
        if existing is not None:          # the last master was withdrawn: its source goes too
            for f in _folder(root, existing).glob('MASTER_*.csv'):
                try: f.unlink()
                except OSError: pass
            catalog.remove_connection(root, existing['id'])
        return {'built': []}
    conn = master_connection(root)
    folder = _folder(root, conn)
    built, keep = [], set()
    for i, m in enumerate(ms):
        if progress: progress(i, len(ms), m['name'])
        name = table_name(m)
        keep.add(name + '.csv')
        rows = _build_time(root, m) if m['kind'] == 'time' else _build_entity(root, m)
        tmp = folder / (name + '.csv.tmp')
        with open(tmp, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f); w.writerows(rows)
        target = folder / (name + '.csv')
        if target.exists() and target.read_bytes() == tmp.read_bytes(): tmp.unlink()      # unchanged: approvals stay current
        else: tmp.replace(target)
        built.append({'master': m['name'], 'table': name, 'rows': len(rows) - 1})
    for f in folder.glob('MASTER_*.csv'):
        if f.name not in keep:
            try: f.unlink()
            except OSError: pass
    catalog.refresh_connection(root, conn['id'])
    return {'built': built, 'connection': conn['id']}


def _sor_rows(root, sor, attributes, version_rule):
    """One row per identifier of the system of record: the latest period, then the latest version."""
    conn = _conn(root, sor['connection'])
    path = prepared.ensure(root, conn, sor['table'])
    q = lambda c: '"' + c.replace('"', '""') + '"'
    order = []
    if sor.get('period_column'): order.append('%s DESC' % q(sor['period_column']))
    if sor.get('version_column'):
        order.append('CAST(%s AS REAL) %s' % (q(sor['version_column']), 'ASC' if version_rule == 'first' else 'DESC'))
    order.append('rowid DESC')
    extra = [x for x in (sor.get('version_column'), sor.get('period_column')) if x]
    cols = [sor['column']] + extra + attributes
    sql = ('SELECT %s FROM (SELECT %s, ROW_NUMBER() OVER (PARTITION BY lower(trim(%s)) ORDER BY %s) AS rn FROM t '
           'WHERE %s IS NOT NULL AND trim(%s) <> \'\') WHERE rn = 1' % (', '.join(q(c) for c in cols), ', '.join(q(c) for c in cols),
                                                                     q(sor['column']), ', '.join(order), q(sor['column']), q(sor['column'])))
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        out = {}
        for r in db.execute(sql):
            k = _norm(r[0])
            if k: out[k] = r
    return out, extra


def _build_entity(root, m):
    s = m['spec']
    key = key_column(m)
    sor = s.get('system_of_record')
    attrs = s.get('attributes') or []
    sor_rows, extra = (_sor_rows(root, sor, attrs, s.get('version_rule', 'latest')) if sor else ({}, []))
    seen = {}
    for mem in s['members']:
        conn = _conn(root, mem['connection'])
        vals = prepared.distinct_values(root, conn, mem['table'], mem['column'], MAX_DISTINCT) or []
        for v in vals:
            k = _norm(v)
            if k is None: continue
            e = seen.setdefault(k, [str(v).strip(), 0]); e[1] += 1
    header = [key, 'in_system_of_record', 'referenced_by_columns'] + [('latest_' + x) for x in extra] + attrs
    if not sor: header = [key, 'referenced_by_columns']
    keys = list(sor_rows) if s.get('include') == 'system_of_record' else list(dict.fromkeys(list(sor_rows) + list(seen)))
    keys.sort(key=lambda k: (len(k), k))
    rows = [header]
    for k in keys:
        if sor:
            r = sor_rows.get(k)
            shown = str(r[0]).strip() if r else seen[k][0]
            rows.append([shown, 'yes' if r else 'no', seen.get(k, [None, 0])[1]] + ([r[1 + i] for i in range(len(extra))] if r else [None] * len(extra))
                        + ([r[1 + len(extra) + i] for i in range(len(attrs))] if r else [None] * len(attrs)))
        else:
            rows.append([seen[k][0], seen[k][1]])
    return rows


def _iso(v):
    return prepared.iso_date(v)


def _build_time(root, m):
    s = m['spec']
    lo = hi = None
    for mem in s['members']:
        conn = _conn(root, mem['connection'])
        path = prepared.ensure(root, conn, mem['table'])
        col = '"' + mem['column'].replace('"', '""') + '"'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            for (v,) in db.execute('SELECT DISTINCT substr(trim(%s), 1, 20) FROM t WHERE %s IS NOT NULL' % (col, col)):
                d = _iso(v)
                if not d or not ('1900' <= d[:4] <= '2100'): continue
                lo = d if lo is None or d < lo else lo
                hi = d if hi is None or d > hi else hi
    start_m = int(s.get('fiscal_year_start_month') or 1)
    rows = [['date', 'year', 'quarter', 'month', 'month_name', 'day', 'weekday', 'is_month_end', 'fiscal_year', 'fiscal_quarter']]
    if lo is None: return rows
    d, end = date.fromisoformat(lo).replace(month=1, day=1), date.fromisoformat(hi).replace(month=12, day=31)
    while d <= end:
        nxt = d + timedelta(days=1)
        fy = d.year + (1 if start_m > 1 and d.month >= start_m else 0)
        fq = ((d.month - start_m) % 12) // 3 + 1
        rows.append([d.isoformat(), d.year, '%d-Q%d' % (d.year, (d.month - 1) // 3 + 1), d.strftime('%Y-%m'), d.strftime('%B'), d.day,
                     d.strftime('%A'), 'yes' if nxt.month != d.month else 'no', 'FY%d' % fy, 'FY%d-Q%d' % (fy, fq)])
        d = nxt
    return rows


# ====================================================================== links to the masters

def member_links(root, m):
    """(child, parent, label) for every member column of an approved master that is ready to link."""
    conn = master_connection(root, create=False)
    if not conn: return []
    parent_col = 'date' if m['kind'] == 'time' else key_column(m)
    out = []
    s = m['spec']
    members = list(s['members'])
    if s.get('system_of_record'):
        members = [{**s['system_of_record'], 'label': 'system of record'}] + members
    for mem in members:
        out.append(((mem['connection'], mem['table'], mem['column']), (conn['id'], table_name(m), parent_col), mem.get('label') or mem['column']))
    return out


def link_members(root, m, actor, note, approve=False):
    """Add the links of an approved master for review (or approve them in the same step)."""
    from binggio import links
    from binggio.profiling import store as profiles
    conn = master_connection(root)
    profiles.run_table(root, conn['id'], table_name(m))
    added, failed = [], []
    for child, parent, label in member_links(root, m):
        try:
            r = links.add(root, child, parent, actor, '%s (%s of master %s). %s' % (label, 'member', m['name'], note))
            if approve:
                from binggio.registry import store
                eid = r['edge']['id']
                art = store.get_artifact(root, 'relationship:' + eid) or {}
                if art.get('status') != 'approved':
                    store.decide_relationship(root, eid, action='approve', actor=actor,
                                              note=('Member of approved master %s (%s): %s' % (m['name'], label, note))[:600],
                                              expected_revision=art.get('revision', 0), cardinality='many_to_one',
                                              join_note='Many rows of %s point to one row of the master' % child[1])
            added.append({'child': '%s.%s' % child[1:], 'label': label, 'coverage_pct': r['check']['coverage_pct']})
        except Exception as e:          # the person sees which link could not be added, and why
            failed.append({'child': '%s.%s' % child[1:], 'label': label, 'error': str(e)[:200]})
    return {'added': added, 'failed': failed}


def check_member(root, member, against=None, master=None):
    """How well a column the person adds matches a master: share of its distinct values found in the
    system of record (or in the master's other columns), on every row."""
    _check_column(root, member)
    def values(x):
        vals = prepared.distinct_values(root, _conn(root, x['connection']), x['table'], x['column'], MAX_DISTINCT)
        if vals is None: raise MasterError('%s.%s has more than %s different values' % (x['table'], x['column'], '{:,}'.format(MAX_DISTINCT)))
        return {v for v in (_norm(y) for y in vals) if v}
    mine = values(member)
    if not mine: raise MasterError('%s has no values' % member['column'])
    if against:
        _check_column(root, against)
        ref = values(against)
    else:
        spec = (master or {}).get('spec') or master or {}
        cols = ([spec['system_of_record']] if spec.get('system_of_record') else []) + [m for m in spec.get('members') or [] if m.get('include', True)]
        cols = [c for c in cols if (c['table'], c['column']) != (member['table'], member['column'])]
        if not cols: raise MasterError('Nothing to compare with yet')
        ref = set().union(*[values(c) for c in cols])
    inter = mine & ref
    digits_mine = {d for d in (_digits(v) for v in mine) if d}
    digits_ref = {d for d in (_digits(v) for v in ref) if d}
    dcov = len(digits_mine & digits_ref) / len(digits_mine) if digits_mine else 0.0
    cov = len(inter) / len(mine)
    out = {'distinct': len(mine), 'found': len(inter), 'coverage_pct': round(100 * cov, 2),
           'missing_examples': sorted(mine - ref, key=lambda v: (len(v), v))[:5]}
    if cov < 0.8 and dcov >= 0.9:
        out['digits_coverage_pct'] = round(100 * dcov, 2)
        out['hint'] = 'The values match when only digits are compared: add the data fix "Keep digits only" (without leading zeros) to %s first.' % member['column']
    return out
