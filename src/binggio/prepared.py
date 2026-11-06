# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Prepared tables and data fixes.

Data fixes are a data steward's corrections, kept as steps and applied every time a table is read:
trim spaces, clean numbers ("$1,234" -> 1234), fill missing values, pad or strip key prefixes,
read dates in a stated format, combine columns into a key, keep one row per key. The source files
are never changed. A refreshed or re-added table gets the same steps again, by table and column
name. Every step is recorded with who added it, when and why; removing one is recorded too.

A prepared table is a local SQLite copy of one source table with its fixes applied, built once for
each version of the table's content and of its fixes, under <workspace>/data/prepared/. Profiling,
discovery, key checks, the data story and answers read it instead of re-parsing a CSV each time,
which is what keeps large workspaces (millions of rows, a hundred tables) fast. It is a cache: it
can be deleted at any time and is rebuilt when needed.

A table's fixes are part of its fingerprint, so adding or removing a fix makes the table's
approvals stale until a person re-confirms them: the data they were approved on has changed.
"""
from __future__ import annotations
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from binggio.catalog.connectors import (ConnectionErrorSafe, MAX_COLUMNS, MAX_CSV_BYTES, _quote_identifier,
                                        _readonly, _unwrapped_row, list_sqlite_tables, resolve_source)

PREPARED_VERSION = 1
BATCH = 20_000
MISSING = {'', 'null', 'none', 'n/a', 'na', 'nan', '?'}
KINDS = {
    'trim': 'Remove spaces at the start and end',
    'case': 'Change to upper or lower case',
    'number': 'Read as a number (remove currency signs, separators and units)',
    'replace': 'Replace exact values',
    'missing': 'Fill missing values',
    'pad': 'Pad on the left to a fixed width',
    'strip_prefix': 'Remove text at the start',
    'add_prefix': 'Add text at the start',
    'digits': 'Keep digits only',
    'date': 'Read dates in a stated format',
    'combine': 'Combine columns into a new key column',
    'dedupe': 'Keep one row for each key value',
}
TABLE_KINDS = ('combine', 'dedupe')
DATE_INPUTS = ('%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%m-%d-%Y', '%Y/%m/%d', '%d.%m.%Y', '%Y%m%d', '%d-%b-%Y', '%d %b %Y',
               '%d/%m/%Y %H:%M:%S', '%m/%d/%Y %H:%M:%S', '%d/%m/%y', '%m/%d/%y')
_LOCK = threading.RLock()
_BUILD_LOCKS: dict = {}


class FixError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _is_missing(v):
    return v is None or (isinstance(v, str) and v.strip().casefold() in MISSING)


# ====================================================================== fix store

def _fixdb(root):
    path = Path(root).resolve() / 'data' / 'phase7_governance' / 'fixes.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS fixes (
        id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, table_name TEXT NOT NULL, seq INTEGER NOT NULL,
        kind TEXT NOT NULL, column_name TEXT NOT NULL, params_json TEXT NOT NULL, status TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL,
        removed_by TEXT, removed_at TEXT, removed_note TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS fixes_table ON fixes(connection_id, table_name, status, seq)')
    return db


def _fix_public(r):
    d = dict(r)
    d['params'] = json.loads(d.pop('params_json'))
    d['column'] = d.pop('column_name'); d['table'] = d.pop('table_name')
    d['label'] = describe(d)
    return d


def describe(f):
    k, c, p = f['kind'], f.get('column'), f.get('params') or {}
    if k == 'trim': return 'Trim spaces in %s' % c
    if k == 'case': return '%s case for %s' % (p.get('to', 'upper').capitalize(), c)
    if k == 'number': return 'Read %s as a number' % c
    if k == 'replace': return 'In %s, replace %s with %s' % (c, ', '.join('"%s"' % x for x in p.get('values', [])), '"%s"' % p.get('to', '') if p.get('to') else 'empty')
    if k == 'missing':
        w = p.get('with')
        return 'Fill missing %s with %s' % (c, '"%s"' % p.get('value') if w == 'value' else {'zero': '0', 'mean': 'the average', 'median': 'the median',
                                                                                                  'most_common': 'the most common value'}.get(w, w))
    if k == 'pad': return 'Pad %s to %s characters with "%s"' % (c, p.get('width'), p.get('char', '0'))
    if k == 'strip_prefix': return 'Remove "%s" at the start of %s' % (p.get('text'), c)
    if k == 'add_prefix': return 'Add "%s" at the start of %s' % (p.get('text'), c)
    if k == 'digits': return 'Keep digits only in %s%s' % (c, ', without leading zeros' if p.get('drop_leading_zeros') else '')
    if k == 'date': return 'Read %s as dates in the form %s' % (c, p.get('format'))
    if k == 'combine': return 'New column %s = %s' % (c, (' + "%s" + ' % p.get('sep', '|')).join(p.get('columns', [])))
    if k == 'dedupe': return 'Keep the first row for each %s' % ' + '.join(p.get('columns', []))
    return k


_SIG_CACHE = {}


def _all_active(root):
    """{(cid, table): [fix, ...]} for active fixes, cached until the fix file changes."""
    path = Path(root).resolve() / 'data' / 'phase7_governance' / 'fixes.sqlite3'
    try:
        st = path.stat(); stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    key = str(path)
    hit = _SIG_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    with _LOCK, _fixdb(root) as db:
        rows = db.execute("SELECT * FROM fixes WHERE status='active' ORDER BY connection_id, table_name, seq").fetchall()
    out = {}
    for r in rows:
        f = _fix_public(r)
        out.setdefault((f['connection_id'], f['table']), []).append(f)
    _SIG_CACHE[key] = (stamp, out)
    return out


def fixes_for(root, cid, table):
    return list(_all_active(root).get((cid, table), []))


def signature(root, cid, table):
    """'' when the table has no fixes; otherwise a hash of its active steps, in order."""
    steps = fixes_for(root, cid, table)
    if not steps: return ''
    return hashlib.sha256(json.dumps([[f['kind'], f['column'], f['params']] for f in steps], sort_keys=True).encode()).hexdigest()


def list_fixes(root, cid=None, table=None, include_removed=False):
    q, args = 'SELECT * FROM fixes WHERE 1=1', []
    if cid: q += ' AND connection_id=?'; args.append(cid)
    if table: q += ' AND table_name=?'; args.append(table)
    if not include_removed: q += " AND status='active'"
    with _LOCK, _fixdb(root) as db:
        return [_fix_public(r) for r in db.execute(q + ' ORDER BY connection_id, table_name, seq', args).fetchall()]


def _clean_params(kind, column, params, header):
    p = dict(params or {})
    cols = set(header)
    if kind not in KINDS: raise FixError('Unknown fix: %s' % kind)
    if kind == 'combine':
        name = (column or '').strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', name):
            raise FixError('Name the new column with letters, digits and _ only (for example loan_key)')
        if name.casefold() in {c.casefold() for c in cols}: raise FixError('There is already a column called %s' % name)
        parts = [c for c in p.get('columns') or [] if c]
        if len(parts) < 2: raise FixError('Choose at least two columns to combine')
        bad = [c for c in parts if c not in cols]
        if bad: raise FixError('Not a column of this table: %s' % ', '.join(bad))
        sep = str(p.get('sep', '|'))
        if len(sep) > 3: raise FixError('Use a short separator, such as |')
        return name, {'columns': parts, 'sep': sep}
    if kind == 'dedupe':
        parts = [c for c in p.get('columns') or [] if c]
        if not parts: raise FixError('Choose the key column(s)')
        bad = [c for c in parts if c not in cols]
        if bad: raise FixError('Not a column of this table: %s' % ', '.join(bad))
        return '*', {'columns': parts, 'keep': 'first'}
    if column not in cols: raise FixError('Not a column of this table: %s' % column)
    if kind == 'case':
        if p.get('to') not in ('upper', 'lower'): raise FixError('Choose upper or lower case')
        return column, {'to': p['to']}
    if kind == 'replace':
        vals = p.get('values')
        if isinstance(vals, str): vals = [v.strip() for v in vals.split(',')]
        vals = [str(v) for v in vals or [] if str(v) != '']
        if not vals: raise FixError('Give the value(s) to replace, separated by commas')
        return column, {'values': vals[:50], 'to': str(p.get('to') or '')}
    if kind == 'missing':
        w = p.get('with')
        if w not in ('value', 'zero', 'mean', 'median', 'most_common'): raise FixError('Choose what to fill missing values with')
        if w == 'value' and not str(p.get('value') or '').strip(): raise FixError('Give the value to use')
        return column, {'with': w, 'value': str(p.get('value') or '') if w == 'value' else ''}
    if kind == 'pad':
        try: width = int(p.get('width'))
        except (TypeError, ValueError): raise FixError('Give the width as a number')
        if not 1 <= width <= 64: raise FixError('Width must be 1 to 64')
        ch = str(p.get('char') or '0')
        if len(ch) != 1: raise FixError('Pad with one character, such as 0')
        return column, {'width': width, 'char': ch}
    if kind == 'strip_prefix':
        t = str(p.get('text') or '')
        if not t: raise FixError('Give the text to remove')
        return column, {'text': t[:40], 'ignore_case': bool(p.get('ignore_case', True))}
    if kind == 'add_prefix':
        t = str(p.get('text') or '')
        if not t: raise FixError('Give the text to add')
        return column, {'text': t[:40]}
    if kind == 'digits':
        return column, {'drop_leading_zeros': bool(p.get('drop_leading_zeros'))}
    if kind == 'date':
        if p.get('format') not in DATE_INPUTS: raise FixError('Choose the date form the column uses')
        return column, {'format': p['format']}
    return column, {}


def add_fix(root, conn, table, kind, column, params, actor, note):
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise FixError('Set your name first: data fixes are recorded with it')
    if not note: raise FixError('Say why this fix is right (it is kept with the fix)')
    header = header_with_fixes(root, conn, table)
    column, params = _clean_params(kind, column, params, header)
    fid = 'fix:' + uuid.uuid4().hex[:16]
    with _LOCK, _fixdb(root) as db:
        seq = (db.execute('SELECT MAX(seq) FROM fixes WHERE connection_id=? AND table_name=?', (conn['id'], table)).fetchone()[0] or 0) + 1
        db.execute('INSERT INTO fixes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (fid, conn['id'], table, seq, kind, column, json.dumps(params, sort_keys=True), 'active', actor[:80], _now(), note[:600], None, None, None))
        return _fix_public(db.execute('SELECT * FROM fixes WHERE id=?', (fid,)).fetchone())


def remove_fix(root, fix_id, actor, note):
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor: raise FixError('Set your name first')
    if not note: raise FixError('Say why the fix is removed')
    with _LOCK, _fixdb(root) as db:
        row = db.execute('SELECT * FROM fixes WHERE id=?', (fix_id,)).fetchone()
        if row is None or row['status'] != 'active': raise FixError('This fix is not active')
        later = db.execute("SELECT * FROM fixes WHERE connection_id=? AND table_name=? AND status='active' AND seq>? AND kind IN ('combine','dedupe')",
                           (row['connection_id'], row['table_name'], row['seq'])).fetchall()
        for l in later:
            p = json.loads(l['params_json'])
            if row['kind'] == 'combine' and row['column_name'] in (p.get('columns') or []):
                raise FixError('The step "%s" uses this column; remove it first' % describe(_fix_public(l)))
        db.execute("UPDATE fixes SET status='removed', removed_by=?, removed_at=?, removed_note=? WHERE id=?", (actor[:80], _now(), note[:600], fix_id))
        return _fix_public(db.execute('SELECT * FROM fixes WHERE id=?', (fix_id,)).fetchone())


# ====================================================================== value functions

_NUM_KEEP = re.compile(r'[^0-9.\-]')


def clean_number(v):
    """'$1,234.50' -> '1234.5', '(500)' -> '-500', '12 kg' -> '12', '45%' -> '45'. None when no number."""
    if _is_missing(v): return None
    s = str(v).strip()
    neg = s.startswith('(') and s.endswith(')')
    s = _NUM_KEEP.sub('', s.replace(',', ''))
    if s.count('-') > 1 or (s.count('-') == 1 and not s.startswith('-')) or s.count('.') > 1 or not re.search(r'\d', s):
        return None
    try: x = float(s)
    except ValueError: return None
    if not math.isfinite(x): return None
    if neg: x = -abs(x)
    return str(int(x)) if x == int(x) and abs(x) < 1e15 and '.' not in s else repr(x).rstrip('0').rstrip('.') if '.' in repr(x) else repr(x)


def _num(v):
    c = clean_number(v)
    return float(c) if c is not None else None


def apply_value(kind, params, v):
    if kind == 'trim': return v.strip() if isinstance(v, str) else v
    if kind == 'case':
        if not isinstance(v, str): return v
        return v.upper() if params['to'] == 'upper' else v.lower()
    if kind == 'number': return clean_number(v)
    if kind == 'replace':
        if v is not None and str(v).strip() in params['values']: return params['to'] if params['to'] != '' else None
        return v
    if kind == 'pad':
        if _is_missing(v): return v
        return str(v).strip().rjust(params['width'], params['char'])
    if kind == 'strip_prefix':
        if not isinstance(v, str): return v
        s, t = v.strip(), params['text']
        if (s.casefold().startswith(t.casefold()) if params.get('ignore_case', True) else s.startswith(t)): return s[len(t):]
        return v
    if kind == 'add_prefix':
        if _is_missing(v): return v
        s = str(v).strip()
        return s if s.startswith(params['text']) else params['text'] + s
    if kind == 'digits':
        if _is_missing(v): return v
        d = re.sub(r'\D', '', str(v))
        if params.get('drop_leading_zeros'): d = d.lstrip('0') or ('0' if d else '')
        return d or None
    if kind == 'date':
        if _is_missing(v): return v
        s = str(v).strip()
        try: d = datetime.strptime(s, params['format'])
        except ValueError: return v
        return d.strftime('%Y-%m-%d %H:%M:%S' if '%H' in params['format'] else '%Y-%m-%d')
    return v


# ====================================================================== reading the source

def _csv_iter(path):
    """(header, rows, meta) streaming every row of a CSV exactly as profiling reads it."""
    if path.stat().st_size > MAX_CSV_BYTES:
        raise ConnectionErrorSafe('CSV exceeds the 2 GB per-table limit')
    f = path.open('r', encoding='utf-8-sig', errors='replace', newline='')
    first = f.read(12000)
    if not first.strip():
        f.close(); raise ConnectionErrorSafe('Empty CSV; no columns to profile')
    try: delim = csv.Sniffer().sniff(first, delimiters=',;\t|').delimiter
    except csv.Error: delim = ';' if first.splitlines()[0].count(';') > first.splitlines()[0].count(',') else ','
    f.seek(0)
    reader = csv.reader(f, delimiter=delim)
    try: header = [x.strip().lstrip('﻿') for x in _unwrapped_row(next(reader), delim)]
    except (StopIteration, csv.Error) as exc:
        f.close(); raise ConnectionErrorSafe('Invalid CSV header') from exc
    if len(header) > MAX_COLUMNS or not header:
        f.close(); raise ConnectionErrorSafe('Unsupported CSV column count')
    if len({h.casefold() for h in header}) != len(header) or any(not h for h in header):
        f.close(); raise ConnectionErrorSafe('Duplicate/blank CSV headers require cleanup before profiling')
    meta = {'malformed_rows': 0, 'encoding_replacement_cells': 0, 'delimiter': 'TAB' if delim == '\t' else delim}
    n = len(header)

    def rows():
        try:
            for raw in reader:
                if not raw or not any(str(x).strip() for x in raw): continue
                row = _unwrapped_row(raw, delim)
                if len(row) != n:
                    meta['malformed_rows'] += 1
                    row = [row[i] if i < len(row) else None for i in range(n)]
                if '�' in ''.join(x for x in row if x):
                    meta['encoding_replacement_cells'] += sum('�' in str(x) for x in row)
                yield row
        except csv.Error as exc:
            raise ConnectionErrorSafe('Malformed CSV quoting; reading stopped safely') from exc
        finally:
            f.close()
    return header, rows(), meta


def _source(root, conn, table):
    src = resolve_source(root, conn['source_path'], conn['kind'])
    if conn['kind'] == 'csv_folder':
        p = (src / (table + '.csv')).resolve()
        if not p.is_relative_to(src) or not p.is_file():
            raise ConnectionErrorSafe('Registered CSV path no longer exists')
        return src, p
    if table not in set(list_sqlite_tables(src)):
        raise ConnectionErrorSafe('Table not found in read-only SQLite source')
    return src, src


def _source_sha(root, conn, table):
    from binggio.profiling.profiler import _fingerprint
    src = resolve_source(root, conn['source_path'], conn['kind'])
    return _fingerprint(src, conn['kind'], table)['sha256']


# ====================================================================== prepared tables

def _dir(root):
    d = Path(root).resolve() / 'data' / 'prepared'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stem(conn, table):
    return hashlib.sha1((conn['id'] + '::' + table).encode('utf-8')).hexdigest()[:16]


def _target(root, conn, table, sha, sig):
    tag = hashlib.sha1(('%s|%s|%d' % (sha, sig, PREPARED_VERSION)).encode()).hexdigest()[:12]
    return _dir(root) / ('%s-%s.sqlite3' % (_stem(conn, table), tag))


def _connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30, check_same_thread=False)
    db.execute('PRAGMA query_only=ON')
    return db


def is_ready(root, conn, table):
    try:
        return _target(root, conn, table, _source_sha(root, conn, table), signature(root, conn['id'], table)).is_file()
    except (OSError, ConnectionErrorSafe):
        return False


def ensure(root, conn, table, progress=None):
    """Path of the prepared copy of this table, building it first if the source or its fixes changed."""
    sha = _source_sha(root, conn, table)
    sig = signature(root, conn['id'], table)
    target = _target(root, conn, table, sha, sig)
    if target.is_file(): return target
    with _LOCK:
        lock = _BUILD_LOCKS.setdefault(str(target), threading.Lock())
    with lock:
        if target.is_file(): return target
        _build(root, conn, table, target, sha, sig, progress)
    for old in _dir(root).glob(_stem(conn, table) + '-*.sqlite3'):   # older versions of this table
        if old != target:
            try: old.unlink()
            except OSError: pass             # still open by a running question; removed next time
    return target


def _build(root, conn, table, target, sha, sig, progress):
    tmp = target.with_name(target.stem + '.%s.tmp' % uuid.uuid4().hex[:6])
    db = sqlite3.connect(tmp)
    try:
        db.execute('PRAGMA journal_mode=OFF'); db.execute('PRAGMA synchronous=OFF'); db.execute('PRAGMA temp_store=FILE')
        src, path = _source(root, conn, table)
        started = time.time()
        if conn['kind'] == 'csv_folder':
            header, rows, meta = _csv_iter(path)
            db.execute('CREATE TABLE t (%s)' % ', '.join(_quote_identifier(h) for h in header))
            ins = 'INSERT INTO t VALUES (%s)' % ','.join('?' * len(header))
            batch, n = [], 0
            for r in rows:
                batch.append(r)
                if len(batch) >= BATCH:
                    db.executemany(ins, batch); n += len(batch); batch = []
                    if progress: progress(n)
            if batch: db.executemany(ins, batch); n += len(batch)
        else:
            with _readonly(src) as s:
                cols = s.execute('PRAGMA table_info(%s)' % _quote_identifier(table)).fetchall()
            header = [c[1] for c in cols]
            meta = {'malformed_rows': 0, 'encoding_replacement_cells': 0, 'declared_types': {c[1]: c[2] for c in cols},
                    'declared_primary_keys': [c[1] for c in cols if c[5]]}
            db.execute('CREATE TABLE t (%s)' % ', '.join(_quote_identifier(h) for h in header))
            with _readonly(src) as s:
                cur = s.execute('SELECT * FROM %s' % _quote_identifier(table))
                ins = 'INSERT INTO t VALUES (%s)' % ','.join('?' * len(header))
                while True:
                    chunk = cur.fetchmany(BATCH)
                    if not chunk: break
                    db.executemany(ins, chunk)
        db.commit()
        steps = fixes_for(root, conn['id'], table) if sig else []
        notes = _apply_fixes(db, header, steps) if steps else []
        header = [r[1] for r in db.execute('PRAGMA table_info(t)')]
        rows_n = db.execute('SELECT COUNT(*) FROM t').fetchone()[0]
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        info = {'version': PREPARED_VERSION, 'source_sha256': sha, 'fixes_signature': sig, 'header': header, 'rows': rows_n,
                'meta': meta, 'fix_notes': notes, 'built_at': _now(), 'seconds': round(time.time() - started, 1)}
        db.executemany('INSERT INTO meta VALUES (?,?)', [(k, json.dumps(v)) for k, v in info.items()])
        db.commit(); db.close()
        os.replace(tmp, target)
    except BaseException:
        try: db.close()
        except Exception: pass
        try: tmp.unlink()
        except OSError: pass
        raise


def _register(db):
    db.create_function('bn_fix', 3, lambda k, p, v: apply_value(k, json.loads(p), v), deterministic=True)
    db.create_function('bn_num', 1, _num, deterministic=True)
    db.create_function('bn_missing', 1, lambda v: 1 if _is_missing(v) else 0, deterministic=True)

    def combine(sep, *vals):
        if any(_is_missing(v) for v in vals): return None
        return sep.join(str(v).strip() for v in vals)
    db.create_function('bn_combine', -1, combine, deterministic=True)


def _apply_fixes(db, header, steps):
    """Each step runs over the whole table in order; returns a note per step with what changed."""
    _register(db)
    q = _quote_identifier
    notes = []
    for f in steps:
        k, c, p = f['kind'], f['column'], f['params']
        before = db.total_changes
        if k == 'combine':
            db.execute('ALTER TABLE t ADD COLUMN %s' % q(c))
            db.execute('UPDATE t SET %s = bn_combine(?, %s)' % (q(c), ', '.join(q(x) for x in p['columns'])), (p['sep'],))
        elif k == 'dedupe':
            keys = ', '.join(q(x) for x in p['columns'])
            db.execute('DELETE FROM t WHERE rowid NOT IN (SELECT MIN(rowid) FROM t GROUP BY %s)' % keys)
        elif k == 'missing':
            w = p['with']
            if w == 'value': fill = p['value']
            elif w == 'zero': fill = '0'
            elif w == 'mean':
                m = db.execute('SELECT AVG(bn_num(%s)) FROM t' % q(c)).fetchone()[0]
                fill = None if m is None else ('%.6f' % m).rstrip('0').rstrip('.')
            elif w == 'median':
                vals = [r[0] for r in db.execute('SELECT bn_num(%s) AS x FROM t WHERE x IS NOT NULL' % q(c))]
                fill = None if not vals else ('%.6f' % statistics.median(vals)).rstrip('0').rstrip('.')
            else:
                r = db.execute('SELECT %s, COUNT(*) FROM t WHERE bn_missing(%s)=0 GROUP BY %s ORDER BY 2 DESC, 1 LIMIT 1' % (q(c), q(c), q(c))).fetchone()
                fill = r[0] if r else None
            if fill is not None:
                db.execute('UPDATE t SET %s = ? WHERE bn_missing(%s)=1' % (q(c), q(c)), (fill,))
            notes.append({'fix': f['id'], 'filled_with': fill})
        else:
            db.execute('UPDATE t SET %s = bn_fix(?, ?, %s) WHERE %s IS NOT bn_fix(?, ?, %s)' % (q(c), q(c), q(c), q(c)),
                       (k, json.dumps(p), k, json.dumps(p)))
        if k != 'missing': notes.append({'fix': f['id'], 'rows_changed': db.total_changes - before})
        else: notes[-1]['rows_changed'] = db.total_changes - before
    db.commit()
    return notes


def info(root, conn, table):
    path = ensure(root, conn, table)
    with _connect(path) as db:
        return {k: json.loads(v) for k, v in db.execute('SELECT k, v FROM meta')}


def header_with_fixes(root, conn, table):
    """Column names after the table's fixes (combine steps add columns)."""
    src, path = _source(root, conn, table)
    if conn['kind'] == 'csv_folder':
        from binggio.profiling.profiler import _sample_csv
        header = _sample_csv(path, 1)[0]
    else:
        with _readonly(src) as s:
            header = [c[1] for c in s.execute('PRAGMA table_info(%s)' % _quote_identifier(table)).fetchall()]
    for f in fixes_for(root, conn['id'], table):
        if f['kind'] == 'combine': header.append(f['column'])
    return header


def read(root, conn, table, limit):
    """(header, rows, meta) for the first `limit` rows, as profiling and discovery expect them.
    Tables without fixes are read straight from the source when the read is small."""
    if not signature(root, conn['id'], table) and limit <= 5000 and not is_ready(root, conn, table):
        from binggio.profiling.profiler import _sample_csv, _sample_sqlite
        src, path = _source(root, conn, table)
        return _sample_csv(path, limit) if conn['kind'] == 'csv_folder' else _sample_sqlite(src, table, limit)
    path = ensure(root, conn, table)
    with _connect(path) as db:
        m = {k: json.loads(v) for k, v in db.execute('SELECT k, v FROM meta')}
        rows = [list(r) for r in db.execute('SELECT * FROM t ORDER BY rowid LIMIT ?', (limit + 1,))]
    meta = dict(m['meta'])
    if 'declared_primary_keys' in meta: meta['declared_primary_keys'] = set(meta['declared_primary_keys'])
    meta['sample_complete'] = len(rows) <= limit
    meta['total_rows'] = m['rows']
    return m['header'], rows[:limit], meta


def column_values(root, conn, table, columns, limit):
    """{column: [values of the first `limit` rows]} read from the prepared copy."""
    path = ensure(root, conn, table)
    q = _quote_identifier
    with _connect(path) as db:
        rows = db.execute('SELECT %s FROM t ORDER BY rowid LIMIT ?' % ', '.join(q(c) for c in columns), (limit,)).fetchall()
    return {c: [r[i] for r in rows] for i, c in enumerate(columns)}


def iter_rows(root, conn, table, batch=BATCH):
    """(header, generator of row lists) over the whole prepared table, in source order."""
    path = ensure(root, conn, table)
    db = _connect(path)
    m = {k: json.loads(v) for k, v in db.execute('SELECT k, v FROM meta')}

    def gen():
        try:
            cur = db.execute('SELECT * FROM t ORDER BY rowid')
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk: break
                for r in chunk: yield list(r)
        finally:
            db.close()
    return m['header'], gen(), m['rows']


def row_count(root, conn, table):
    return info(root, conn, table)['rows']


def key_check(root, conn, table, columns):
    """Whole-table check of a key (one column or several together): rows, missing, duplicates."""
    header = header_with_fixes(root, conn, table)
    bad = [c for c in columns if c not in header]
    if bad or not columns: raise FixError('Not a column of this table: %s' % ', '.join(bad or ['(none)']))
    path = ensure(root, conn, table)
    q = _quote_identifier
    cols = ', '.join(q(c) for c in columns)
    with _connect(path) as db:
        _register(db)
        n = db.execute('SELECT COUNT(*) FROM t').fetchone()[0]
        miss_expr = ' OR '.join('bn_missing(%s)=1' % q(c) for c in columns)
        missing = db.execute('SELECT COUNT(*) FROM t WHERE %s' % miss_expr).fetchone()[0]
        distinct = db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM t WHERE NOT (%s) GROUP BY %s)' % (miss_expr, cols)).fetchone()[0]
        dups = db.execute('SELECT %s, COUNT(*) AS n FROM t WHERE NOT (%s) GROUP BY %s HAVING n > 1 ORDER BY n DESC LIMIT 5'
                          % (cols, miss_expr, cols)).fetchall()
    present = n - missing
    extra = present - distinct
    return {'columns': columns, 'rows': n, 'missing_rows': missing, 'distinct': distinct, 'duplicate_rows': extra,
            'duplicate_pct': round(100 * extra / present, 3) if present else 0.0,
            'unique': n > 0 and missing == 0 and extra == 0,
            'examples': [{'value': ' + '.join('' if v is None else str(v) for v in r[:-1]), 'rows': r[-1]} for r in dups],
            'whole_table': True}


def preview_fix(root, conn, table, kind, column, params, n=5000):
    """What a fix would change, on the first `n` rows, before it is added."""
    header = header_with_fixes(root, conn, table)
    column, params = _clean_params(kind, column, params, header)
    h, rows, _ = read(root, conn, table, n)
    out = {'kind': kind, 'column': column, 'params': params, 'label': describe({'kind': kind, 'column': column, 'params': params}),
           'rows_checked': len(rows), 'examples': []}
    if kind == 'combine':
        idx = [h.index(c) for c in params['columns']]
        vals = [None if any(_is_missing(r[i]) for i in idx) else params['sep'].join(str(r[i]).strip() for i in idx) for r in rows]
        present = [v for v in vals if v is not None]
        out.update({'examples': [{'after': v} for v in present[:8]], 'missing': len(vals) - len(present),
                    'duplicates_in_checked_rows': len(present) - len(set(present))})
        return out
    if kind == 'dedupe':
        idx = [h.index(c) for c in params['columns']]
        keys = [tuple(r[i] for i in idx) for r in rows]
        out['rows_removed_in_checked_rows'] = len(keys) - len(set(keys))
        return out
    i = h.index(column)
    changed = empty = 0
    fill = None
    if kind == 'missing':
        w = params['with']
        if w in ('mean', 'median', 'most_common'):
            vals = [r[i] for r in rows]
            nums = [x for x in (_num(v) for v in vals) if x is not None]
            if w == 'mean' and nums: fill = ('%.6f' % (sum(nums) / len(nums))).rstrip('0').rstrip('.')
            elif w == 'median' and nums: fill = ('%.6f' % statistics.median(nums)).rstrip('0').rstrip('.')
            elif w == 'most_common':
                from collections import Counter
                cnt = Counter(v for v in vals if not _is_missing(v))
                fill = cnt.most_common(1)[0][0] if cnt else None
            out['note'] = 'Worked out on the first %s rows here; the whole table is used when the fix is applied.' % '{:,}'.format(len(rows))
        else:
            fill = params['value'] if w == 'value' else '0'
        out['fill'] = fill
    for r in rows:
        v = r[i]
        a = (fill if _is_missing(v) else v) if kind == 'missing' else apply_value(kind, params, v)
        if a != v:
            changed += 1
            if a is None and not _is_missing(v): empty += 1
            if len(out['examples']) < 10 and (v, a) not in [(e['before'], e['after']) for e in out['examples']]:
                out['examples'].append({'before': v, 'after': a})
    out.update({'changed': changed, 'became_empty': empty})
    if kind == 'number' and empty:
        out['warning'] = '%d value(s) could not be read as numbers and would become empty. Check them first.' % empty
    return out


def distinct_values(root, conn, table, column, cap):
    """Distinct values of one column over the whole table, or None when there are more than `cap`."""
    path = ensure(root, conn, table)
    with _connect(path) as db:
        rows = db.execute('SELECT DISTINCT %s FROM t WHERE %s IS NOT NULL LIMIT ?' % ((_quote_identifier(column),) * 2), (cap + 1,)).fetchall()
    return None if len(rows) > cap else [r[0] for r in rows]


def prepare_all(root, progress=None):
    """Build the prepared copy of every registered table (used after adding or refreshing data)."""
    from binggio.catalog import catalog
    conns = {c['id']: c for c in catalog.list_connections(root)}
    tables = catalog.list_tables(root, limit=5000)
    done, failures = 0, []
    for i, t in enumerate(tables):
        if progress: progress(i, len(tables), t['table_name'])
        try:
            ensure(root, conns[t['connection_id']], t['table_name']); done += 1
        except (ConnectionErrorSafe, OSError, sqlite3.Error, ValueError) as e:
            failures.append({'table': t['table_name'], 'error': str(e)[:160]})
    return {'prepared': done, 'failures': failures}


def disk_usage(root):
    d = Path(root).resolve() / 'data' / 'prepared'
    return sum(p.stat().st_size for p in d.glob('*.sqlite3')) if d.is_dir() else 0


class KeyIndex:
    """Whole-table membership test for one key column (values matched ignoring spaces and case),
    through an index in the prepared copy. Discovery uses it for parent tables larger than the rows
    it reads, so a child key is not reported missing just because its parent row is further down."""

    def __init__(self, root, conn, table, column):
        path = ensure(root, conn, table)
        self.expr = 'lower(trim(%s))' % _quote_identifier(column)
        name = 'kx_' + hashlib.sha1(column.encode('utf-8')).hexdigest()[:12]
        with _LOCK:
            lock = _BUILD_LOCKS.setdefault(str(path), threading.Lock())
        with lock:
            w = sqlite3.connect(path, timeout=60)
            try:
                w.execute('CREATE INDEX IF NOT EXISTS %s ON t(%s)' % (name, self.expr)); w.commit()
            finally:
                w.close()
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=60, check_same_thread=False)

    def hits(self, values):
        """How many of these (already normalised, distinct) values occur in the column."""
        values = [v for v in values if v is not None]
        n = 0
        for i in range(0, len(values), 500):          # index lookups in batches; about 1 ms per batch
            chunk = values[i:i + 500]
            q = 'SELECT COUNT(*) FROM (SELECT DISTINCT %s AS v FROM t WHERE %s IN (%s))' % (self.expr, self.expr, ','.join('?' * len(chunk)))
            n += self.db.execute(q, chunk).fetchone()[0]
        return n

    def close(self):
        try: self.db.close()
        except Exception: pass
