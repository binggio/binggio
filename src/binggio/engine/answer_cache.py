# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Answer cache: approved fields of each source table, in normal form, kept on disk for answers.

The first question that needs a table streams its prepared copy (data fixes applied) through the
same checks and normalisation as before (numbers parsed strictly, join keys normalised, dates
standardised) into <workspace>/data/cache/answers.sqlite3, with indexes on its key columns. Later
questions query it directly, so a table of millions of rows is read once, not on every question.

An entry is keyed by the table's fingerprint (its content and its data fixes) and by the approved
fields and their roles. Any change to either builds a new entry; nothing stale is ever used. It is
a cache: deleting the file is safe.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE_VERSION = 2
MAX_ROWS = 50_000_000       # rows per table an answer may read through the cache
_LOCK = threading.Lock()
_ISO = None


def path(project):
    p = Path(project).resolve() / 'data' / 'cache' / 'answers.sqlite3'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _db(project):
    db = sqlite3.connect(path(project), timeout=60)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('''CREATE TABLE IF NOT EXISTS cached (name TEXT PRIMARY KEY, table_id TEXT NOT NULL, rows INTEGER NOT NULL,
                  built_at TEXT NOT NULL, seconds REAL, info_json TEXT NOT NULL)''')
    return db


def _key(u, tid, fingerprint):
    from binggio.engine import core
    spec = []
    for fid in u.by_table[tid]:
        f = u.fields[fid]
        spec.append([f['name'], core._type_for_field(f), fid in getattr(u, 'normalized_keys', ()), bool(core._date_field(f)),
                     f.get('role')])
    raw = json.dumps([CACHE_VERSION, tid, fingerprint, spec], sort_keys=True)
    return 'a_' + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _source(project, u, tid):
    from binggio.catalog import catalog
    from binggio.profiling import store as profiles
    from binggio.engine.core import Blocked
    t = u.tables[tid]
    conn = next((c for c in catalog.list_connections(project) if c['id'] == t['connection_id']), None)
    if conn is None: raise Blocked('Source connection was removed')
    p = profiles.get_table(project, conn['id'], t['table'], include_stale=True)
    if not p or p['stale']: raise Blocked('Source profile changed; re-profile and reapprove')
    return conn, t['table'], p['fingerprint']


def table(project, u, tid):
    """(cache table name, rows, info) for the approved fields of `tid`, building it if needed."""
    from binggio import prepared
    from binggio.engine import core
    from binggio.profiling import store as profiles
    conn, table_name, fp = _source(project, u, tid)
    name = _key(u, tid, fp)
    with _db(project) as db:
        r = db.execute('SELECT rows, info_json FROM cached WHERE name=?', (name,)).fetchone()
    if r: return name, r[0], json.loads(r[1])
    with _LOCK:
        with _db(project) as db:
            r = db.execute('SELECT rows, info_json FROM cached WHERE name=?', (name,)).fetchone()
        if r: return name, r[0], json.loads(r[1])
        started = time.time()
        header, rows, total = prepared.iter_rows(project, conn, table_name)
        core._need(total <= MAX_ROWS, 'Table %s has %s rows; answers read tables of up to %s rows' % (table_name, '{:,}'.format(total), '{:,}'.format(MAX_ROWS)))
        ids = u.by_table[tid]
        types = [core._type_for_field(u.fields[fid]) for fid in ids]
        dates = [i for i, fid in enumerate(ids) if core._date_field(u.fields[fid]) and types[i] != 'REAL']
        bad = {i: 0 for i in dates}
        db = _db(project)
        try:
            db.execute('DROP TABLE IF EXISTS "%s"' % name)
            db.execute('CREATE TABLE "%s" (%s)' % (name, ','.join('c%d %s' % (i, t) for i, t in enumerate(types))))
            ins = 'INSERT INTO "%s" VALUES (%s)' % (name, ','.join('?' * len(ids)))
            batch, n = [], 0
            for out in core.arrange_rows(u, tid, header, rows):
                for i in dates:
                    v = out[i]
                    if v is not None and not core._ISO_DATE.fullmatch(str(v)): bad[i] += 1
                batch.append(out)
                if len(batch) >= 20000:
                    db.executemany(ins, batch); n += len(batch); batch = []
            if batch: db.executemany(ins, batch); n += len(batch)
            for i, fid in enumerate(ids):
                f = u.fields[fid]
                if f.get('role') in ('PRIMARY_KEY_CANDIDATE', 'FOREIGN_KEY_CANDIDATE', 'IDENTIFIER') or fid in getattr(u, 'normalized_keys', ()):
                    db.execute('CREATE INDEX "%s_i%d" ON "%s"(c%d)' % (name, i, name, i))
            info = {'bad_dates': {str(k): v for k, v in bad.items()}, 'table': table_name, 'fingerprint': fp}
            db.execute('INSERT OR REPLACE INTO cached VALUES (?,?,?,?,?,?)',
                       (name, tid, n, datetime.now(timezone.utc).isoformat(timespec='seconds'), round(time.time() - started, 2), json.dumps(info)))
            # older entries for the same table are dropped (its data, fixes or approved fields changed)
            for (old,) in db.execute('SELECT name FROM cached WHERE table_id=? AND name<>?', (tid, name)).fetchall():
                db.execute('DROP TABLE IF EXISTS "%s"' % old); db.execute('DELETE FROM cached WHERE name=?', (old,))
            db.commit()
        except BaseException:
            db.rollback()
            try: db.execute('DROP TABLE IF EXISTS "%s"' % name); db.commit()
            except sqlite3.Error: pass
            raise
        finally:
            db.close()
        if profiles.current_fingerprint(project, conn, table_name) != fp:
            raise core.Blocked('Source changed while answering; re-profile and re-confirm approvals')
        return name, n, info


class DiskRows:
    """Rows of a cached table, read on demand through the answer's own connection."""
    def __init__(self, con, schema, name, rows, info):
        self.con, self.ref, self.n, self.info = con, '%s."%s"' % (schema, name), rows, info

    def __len__(self): return self.n

    def __bool__(self): return self.n > 0

    def __iter__(self):
        cur = self.con.execute('SELECT * FROM %s ORDER BY rowid' % self.ref)
        while True:
            chunk = cur.fetchmany(10000)
            if not chunk: return
            for r in chunk: yield list(r)

    def bad_dates(self, idx):
        return self.info.get('bad_dates', {}).get(str(idx))

    def values(self, idx):
        return [r[0] for r in self.con.execute('SELECT c%d FROM %s WHERE c%d IS NOT NULL' % (idx, self.ref, idx))]

    def minmax(self, idx):
        return self.con.execute('SELECT COUNT(c%d), MIN(c%d), MAX(c%d) FROM %s' % (idx, idx, idx, self.ref)).fetchone()

    def distinct(self, idx, cap):
        rows = self.con.execute('SELECT DISTINCT c%d FROM %s WHERE c%d IS NOT NULL LIMIT ?' % (idx, self.ref, idx), (cap + 1,)).fetchall()
        return [r[0] for r in rows]


def distinct(project, u, tid, idx, cap):
    """Distinct non-empty normalised values of one approved field (for observed status labels)."""
    name, n, info = table(project, u, tid)
    db = sqlite3.connect(path(project).as_uri() + '?mode=ro', uri=True, timeout=60)
    try:
        return [r[0] for r in db.execute('SELECT DISTINCT c%d FROM "%s" WHERE c%d IS NOT NULL LIMIT ?' % (idx, name, idx), (cap + 1,))]
    finally:
        db.close()
