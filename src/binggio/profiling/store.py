# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Persistent Phase5 profile snapshots. Independent of Phase4 catalog and oracle.

Profile metadata are not certified. Staleness is detected from source fingerprints;
SQLite fingerprints are database-wide, deliberately conservative.
"""
from __future__ import annotations
import json
import sqlite3
import contextlib
import contextvars
import copy
import threading
from pathlib import Path
from typing import Any
from binggio.catalog import catalog
from binggio.catalog.connectors import ConnectionErrorSafe
from .profiler import VERSION, SAMPLE_LIMIT, current_fingerprint, profile_table

_LOCK=threading.RLock()

# One evaluation (a status call, a review list, building the approved context) asks for the same
# profiles and freshness checks many times. Inside `evaluation_scope()` each is worked out once;
# outside it nothing is reused, so a change to the data is always seen by the next call.
_SCOPE=contextvars.ContextVar('binggio_evaluation_scope',default=None)


@contextlib.contextmanager
def evaluation_scope():
    if _SCOPE.get() is not None:
        yield; return
    token=_SCOPE.set({})
    try:yield
    finally:_SCOPE.reset(token)


def _memo(key,compute):
    scope=_SCOPE.get()
    if scope is None:return compute()
    if key not in scope:scope[key]=compute()
    return copy.deepcopy(scope[key])


def _db(project: Path) -> sqlite3.Connection:
    path=Path(project).resolve()/'data'/'phase5_profiles'/'profiles.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('''CREATE TABLE IF NOT EXISTS profiles (
        connection_id TEXT NOT NULL, table_name TEXT NOT NULL,
        profile_version TEXT NOT NULL, sample_limit INTEGER NOT NULL,
        fingerprint_json TEXT NOT NULL, report_json TEXT NOT NULL,
        profiled_at TEXT NOT NULL,
        PRIMARY KEY(connection_id,table_name))''')
    db.commit()
    return db


def _catalog_table(project: Path,cid: str,table: str) -> dict:
    c=next((x for x in catalog.list_connections(project) if x['id']==cid),None)
    if c is None:raise ConnectionErrorSafe('Connection not found')
    # Use catalog's registry as the allowlist; no arbitrary table or path access.
    ts=catalog.list_tables(project,connection_id=cid,limit=5000)
    if not any(x['table_name']==table for x in ts):
        raise ConnectionErrorSafe('Table is not registered in the Phase 4 catalog; refresh source first')
    return c


def run_table(project: Path,cid: str,table: str, *, force: bool=False,limit: int=SAMPLE_LIMIT) -> dict:
    connection=_catalog_table(project,cid,table)
    if not 1<=int(limit)<=SAMPLE_LIMIT:raise ConnectionErrorSafe('Sample limit must be 1..2500')
    with _LOCK:
        if not force:
            prior=get_table(project,cid,table,include_stale=True)
            if prior and not prior['stale'] and prior['sample_limit']==limit and prior['profile_version']==VERSION:
                return {**prior,'cache_hit':True}
        report=profile_table(project,connection,table,limit=limit)
        with _db(project) as db:
            db.execute('''INSERT INTO profiles(connection_id,table_name,profile_version,sample_limit,
                fingerprint_json,report_json,profiled_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(connection_id,table_name) DO UPDATE SET
                profile_version=excluded.profile_version,sample_limit=excluded.sample_limit,
                fingerprint_json=excluded.fingerprint_json,report_json=excluded.report_json,
                profiled_at=excluded.profiled_at''',
                (cid,table,VERSION,limit,json.dumps(report['fingerprint'],sort_keys=True),
                 json.dumps(report,ensure_ascii=False),report['profiled_at']))
            db.commit()
        return {**report,'stale':False,'cache_hit':False}


def get_table(project: Path,cid: str,table: str, *, include_stale: bool=True) -> dict | None:
    return _memo(('get_table',str(project),cid,table,include_stale),lambda:_get_table(project,cid,table,include_stale))


def _get_table(project,cid,table,include_stale):
    c=_catalog_table(project,cid,table)
    with _LOCK,_db(project) as db:
        row=db.execute('SELECT * FROM profiles WHERE connection_id=? AND table_name=?',(cid,table)).fetchone()
    if row is None:return None
    report=json.loads(row['report_json'])
    try:stale=current_fingerprint(project,c,table)!=json.loads(row['fingerprint_json'])
    except (OSError,ConnectionErrorSafe):stale=True
    report['stale']=stale
    report['cache_hit']=False
    if stale and not include_stale:return None
    return report


def list_profiles(project: Path, *, connection_id: str | None=None) -> list[dict]:
    return _memo(('list_profiles',str(project),connection_id),lambda:_list_profiles(project,connection_id))


def _list_profiles(project,connection_id):
    tables=catalog.list_tables(project,connection_id=connection_id,limit=5000)
    with _LOCK,_db(project) as db:
        rows=db.execute('SELECT connection_id,table_name,report_json,fingerprint_json FROM profiles').fetchall()
    profiles={(r['connection_id'],r['table_name']):r for r in rows}
    connections={c['id']:c for c in catalog.list_connections(project)}
    result=[]
    for t in tables:
        r=profiles.get((t['connection_id'],t['table_name']))
        status='not_profiled';sample=0;warnings=0;profiled_at=None;complete=None
        if r:
            p=json.loads(r['report_json']);sample=p['sample_rows'];warnings=p['warning_count'];
            profiled_at=p['profiled_at'];complete=p['sample_complete']
            try:stale=current_fingerprint(project,connections[t['connection_id']],t['table_name'])!=json.loads(r['fingerprint_json'])
            except (OSError,ConnectionErrorSafe):stale=True
            status='stale' if stale else 'profiled'
        result.append({'connection_id':t['connection_id'],'connection_name':t['connection_name'],
            'schema':t['schema_name'],'table':t['table_name'],'status':status,
            'sample_rows':sample,'sample_complete':complete,'warning_count':warnings,
            'profiled_at':profiled_at})
    return result


def summary(project: Path) -> dict:
    items=list_profiles(project)
    counts={s:sum(x['status']==s for x in items) for s in ('profiled','stale','not_profiled')}
    warnings=sum(x['warning_count'] for x in items if x['status']=='profiled')
    return {'total_tables':len(items),**counts,'active_warning_signals':warnings,
        'sample_limit':SAMPLE_LIMIT,'profile_version':VERSION,
        'description':'Technical profiling only; semantic roles are unverified candidates and metrics may be sample-limited.'}


def run_all(project: Path, *, force: bool=False,limit: int=SAMPLE_LIMIT) -> dict:
    tables=catalog.list_tables(project,limit=5000)
    if len(tables)>2000:raise ConnectionErrorSafe('Profiling is limited to 2000 tables per workspace')
    passed=0;cached=0;failures=[]
    for t in tables:
        try:
            result=run_table(project,t['connection_id'],t['table_name'],force=force,limit=limit)
            passed+=1
            if result['cache_hit']:cached+=1
        except (ConnectionErrorSafe,OSError,sqlite3.Error,ValueError) as exc:
            failures.append({'connection_id':t['connection_id'],'table':t['table_name'],
                             'error':str(exc)[:180]})
    return {'total':len(tables),'profiled_or_cached':passed,'cache_hits':cached,
        'failures':failures,'summary':summary(project)}
