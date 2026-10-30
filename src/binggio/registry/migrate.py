# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Bring an older workspace's evidence up to date without losing approvals.

Three changes are migrated: (a) fingerprints moved from file size and modification time to the
file's content; (b) column-role approvals no longer include Binggio's own role suggestion in
their evidence; (c) relationship approvals bind to which columns link and the content of both
tables, no longer to Binggio's score and reasons. (b) and (c): better heuristics never make a
person's approval stale; changed data still does.
This migration:
  1. checks every approved item with the OLD rule, against the stored profiles; only items
     that are current under the old rule are carried over (stale ones stay stale);
  2. re-profiles every table with the content fingerprint;
  3. re-signs exactly those items with the new evidence and records an audit event.
Nothing is approved that was not approved and current before. Run it with no other writer.
"""
from __future__ import annotations
import json
from pathlib import Path

from binggio.profiling import profiler, store as profiles
from binggio.discovery import store as graph
from binggio.registry import store


def _legacy_fingerprint(source: Path, kind: str, table: str) -> dict:
    if kind == 'csv_folder':
        source = source / (table + '.csv')
    st = source.stat()
    return {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns, 'path_kind': kind,
            'scope': 'table' if kind == 'csv_folder' else 'database'}


def _signature(project, row):
    kind = row['kind']
    if kind == 'relationship':
        return store._relationship(project, row['proposed']['edge_id'])[1]
    if kind == 'column_role':
        p = row['proposed']
        return store._column(project, p['connection_id'], p['table'], p['column'])[1]
    return store._definition_signature(project, row['approved'])


def _meta(project, value=None):
    with store._LOCK, store._db(project) as db:
        db.execute('CREATE TABLE IF NOT EXISTS binggio_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        if value is not None:
            db.execute("INSERT INTO binggio_meta VALUES('signature_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
            db.commit(); return value
        row = db.execute("SELECT value FROM binggio_meta WHERE key='signature_version'").fetchone()
    return int(row[0]) if row else None


def _legacy_fingerprints(project) -> bool:
    for x in profiles.list_profiles(project):
        p = profiles.get_table(project, x['connection_id'], x['table'], include_stale=True)
        if p and 'sha256' not in (p.get('fingerprint') or {}):
            return True
    return False


def needs_migration(project) -> bool:
    if not store._rows(project) and not _legacy_fingerprints(project):
        return False
    return _legacy_fingerprints(project) or (_meta(project) or 1) < store.SIGNATURE_VERSION


def mark_current(project):
    """A new workspace starts on the current evidence rules."""
    _meta(project, store.SIGNATURE_VERSION)


def migrate(project, actor='binggio migrate') -> dict:
    project = Path(project).resolve()
    new_fp = profiler._fingerprint
    legacy_fp = _legacy_fingerprints(project)
    if legacy_fp: profiler._fingerprint = _legacy_fingerprint
    version = _meta(project) or 1
    store._LEGACY_COLUMN_SIGNATURE[0] = version < 2
    store._FORCE_REL_RULE[0] = version
    try:
        rows = store._rows(project)
        carried = [r['id'] for r in rows if r['effective_status'] == 'approved']
        stale_before = sum(r['effective_status'] == 'stale' for r in rows)
    finally:
        profiler._fingerprint = new_fp
        store._LEGACY_COLUMN_SIGNATURE[0] = False
        store._FORCE_REL_RULE[0] = None
    _meta(project, store.SIGNATURE_VERSION)   # from here on, the current rules
    profiles.run_all(project, force=True)
    graph.run_all(project)
    by_id = {r['id']: r for r in store._rows(project)}
    resigned, failed = 0, []
    with store._LOCK, store._db(project) as db:
        db.execute('BEGIN IMMEDIATE')
        for aid in carried:
            raw = store._get(db, aid)
            try:
                sig = _signature(project, raw)
            except Exception as e:  # the evidence itself is gone (table or edge missing)
                failed.append({'id': aid, 'reason': type(e).__name__ + ': ' + str(e)[:200]}); continue
            if sig == raw['evidence_signature']: continue   # unchanged under the new rules: no event
            db.execute('UPDATE artifacts SET evidence_signature=? WHERE id=?', (sig, aid))
            db.execute('INSERT INTO events(artifact_id,revision,action,actor,note,event_json,at) VALUES(?,?,?,?,?,?,?)',
                       (aid, raw['revision'], 'migrate_fingerprint', actor,
                        'Evidence re-bound to current rules (content fingerprints; approvals independent of Binggio\'s own suggestions and scores); decision unchanged',
                        json.dumps({'algorithm': profiler.FINGERPRINT_ALGORITHM}), store._now()))
            resigned += 1
        db.commit()
    _meta(project, store.SIGNATURE_VERSION)
    after = store._rows(project)
    return {'approved_before': len(carried), 'stale_before': stale_before, 'resigned': resigned, 'failed': failed,
            'approved_after': sum(r['effective_status'] == 'approved' for r in after),
            'stale_after': sum(r['effective_status'] == 'stale' for r in after)}
