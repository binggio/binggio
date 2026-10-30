# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Bounded local-only conversation metadata. Session IDs are bearer secrets, not identity.

Never persists a raw SQL statement, source snapshot or full query result. A complete
approved customer cohort of <=200 identifiers is retained for 24 hours.
"""
from __future__ import annotations
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

TTL_SECONDS=24*60*60
MAX_TURNS=30
SESSION_RE=re.compile(r'^[A-Za-z0-9_-]{40,80}$')

class SessionMissing(ValueError):pass
class SessionConflict(ValueError):pass

def db_path(project):return Path(project)/'data'/'phase8b_sessions'/'sessions.sqlite3'

def connect(project):
    path=db_path(project);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS turns(
        id TEXT PRIMARY KEY,session_id TEXT NOT NULL,seq INTEGER NOT NULL,
        question TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,plan_json TEXT,cohort_json TEXT,response_json TEXT,
        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        UNIQUE(session_id,seq));
        CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id,seq);''')
    return db

def _purge(db,now):db.execute('DELETE FROM sessions WHERE expires_at<=?',(now,))

def _lookup(db,sid,now):
    if not SESSION_RE.fullmatch(sid):raise SessionMissing('Unknown or expired conversation')
    row=db.execute('SELECT * FROM sessions WHERE id=? AND expires_at>?',(sid,now)).fetchone()
    if row is None:raise SessionMissing('Unknown or expired conversation')
    return row

def create(project):
    now=int(time.time());sid=secrets.token_urlsafe(32)
    with connect(project) as db:
        _purge(db,now)
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(sid,now,now,now+TTL_SECONDS,0))
    return {'session_id':sid,'expires_at':now+TTL_SECONDS,'revision':0,'turns':[],'active_cohort':None}

def _public_turn(row):
    preview=json.loads(row['response_json']) if row['response_json'] else None
    cohort=json.loads(row['cohort_json']) if row['cohort_json'] else None
    return {'turn_id':row['id'],'seq':row['seq'],'question':row['question'],
            'status':row['status'],'reason':row['reason'],'created_at':row['created_at'],
            'result_preview':preview,'cohort':{'entity':cohort.get('entity','customer'),'size':len(cohort['values']),
            'source_field':cohort['source_field'],'complete':True,
            'governance':cohort.get('cohort_kind','phase7_approved')} if cohort else None}

def read(project,sid):
    now=int(time.time())
    with connect(project) as db:
        row=_lookup(db,sid,now)
        turns=db.execute('SELECT * FROM turns WHERE session_id=? ORDER BY seq',(sid,)).fetchall()
    last=next((json.loads(t['cohort_json']) for t in reversed(turns) if t['cohort_json']),None)
    return {'session_id':sid,'expires_at':row['expires_at'],'revision':row['revision'],
            'turns':[_public_turn(t) for t in turns],
            'active_cohort':{'entity':last.get('entity','customer'),'size':len(last['values']),
              'source_field':last['source_field'],'from_turn':last['from_turn'],
              'governance':last.get('cohort_kind','phase7_approved')} if last else None}

def current_cohort(project,sid,reference=None):
    now=int(time.time())
    with connect(project) as db:
        _lookup(db,sid,now)
        if reference:
            r=db.execute('SELECT * FROM turns WHERE session_id=? AND id=?',(sid,reference)).fetchone()
            if r is None:raise SessionMissing('Referenced turn is not in this conversation')
            if not r['cohort_json']:raise SessionConflict('Referenced turn has no complete customer cohort')
            return json.loads(r['cohort_json'])
        r=db.execute('SELECT cohort_json FROM turns WHERE session_id=? AND cohort_json IS NOT NULL ORDER BY seq DESC LIMIT 1',(sid,)).fetchone()
    return json.loads(r['cohort_json']) if r else None

def append(project,sid,expected_revision,question,status,reason='',plan=None,cohort=None,response=None):
    now=int(time.time());tid=secrets.token_urlsafe(18)
    with connect(project) as db:
        _purge(db,now)
        row=_lookup(db,sid,now)
        if row['revision']!=expected_revision:raise SessionConflict('Conversation changed in another tab. Refresh before retrying.')
        seq=row['revision']+1
        if seq>MAX_TURNS:raise SessionConflict('This thread reached 30 turns. Start a new conversation.')
        preview=None
        if response:
            preview={'columns':response.get('columns',[]),'rows':response.get('rows',[])[:10],
                     'row_count':response.get('row_count',0),'cohort_complete':response.get('cohort_complete',False),
                     'cohort_warning':response.get('cohort_warning')}
        db.execute('''INSERT INTO turns(id,session_id,seq,question,status,reason,created_at,plan_json,cohort_json,response_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (tid,sid,seq,question,status,reason,now,json.dumps(plan) if plan else None,
             json.dumps(cohort) if cohort else None,json.dumps(preview) if preview else None))
        db.execute('UPDATE sessions SET revision=?,updated_at=?,expires_at=? WHERE id=?',
                   (seq,now,now+TTL_SECONDS,sid))
    return tid,seq

def clear(project,sid):
    with connect(project) as db:
        _lookup(db,sid,int(time.time()))
        db.execute('DELETE FROM sessions WHERE id=?',(sid,))
    return {'status':'deleted'}

def reset_cohort(project,sid,expected_revision,question):
    # Delete cohort metadata from prior turns, retaining only their result previews.
    # Explicit reset must not let a subsequent reference silently revive old filters.
    now=int(time.time())
    with connect(project) as db:
        row=_lookup(db,sid,now)
        if row['revision']!=expected_revision:raise SessionConflict('Conversation revision changed; refresh')
        db.execute('UPDATE turns SET cohort_json=NULL WHERE session_id=?',(sid,))
    return append(project,sid,expected_revision,question,'reset','Previous cohort context cleared')
