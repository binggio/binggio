# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Semantic issues: questions Binggio refused, kept for a person to resolve.

A refusal usually means a meaning nobody has approved yet ("what counts as churned?"). Each
refused question becomes an open issue with the reason; a steward resolves it (for example by
approving a business term) or dismisses it, always with a note. The same question asked again
while its issue is open adds to that issue instead of opening another.
"""
from __future__ import annotations
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()
STATUSES = ('open', 'resolved', 'dismissed')
# Refusals that say nothing about meaning (no model key, network down) are not issues.
NOT_SEMANTIC = re.compile(r'unavailable|api key|model key|network|timed out|timeout|rate limit|more than [0-9,]+ rows|re-profile', re.I)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _db(root):
    path = Path(root).resolve() / 'data' / 'phase7_governance' / 'issues.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS issues (
        id TEXT PRIMARY KEY, question TEXT NOT NULL, question_key TEXT NOT NULL, reason TEXT NOT NULL,
        code TEXT, stage TEXT, status TEXT NOT NULL, times INTEGER NOT NULL, first_at TEXT NOT NULL,
        last_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT, note TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS issues_key ON issues(question_key, status)')
    return db


def _key(question):
    return ' '.join(re.findall(r'[a-z0-9]+', question.lower()))


def record(root, question, refusal):
    """Keep a refused question. Returns the issue, or None when the refusal is not about meaning."""
    question = (question or '').strip()
    reason = str(refusal.get('message') or refusal.get('reason') or '').strip()
    if not question or not reason or NOT_SEMANTIC.search(reason):
        return None
    key, now = _key(question), _now()
    with _LOCK, _db(root) as db:
        row = db.execute("SELECT * FROM issues WHERE question_key=? AND status='open'", (key,)).fetchone()
        if row:
            db.execute('UPDATE issues SET times=times+1, last_at=?, reason=? WHERE id=?', (now, reason[:600], row['id']))
            iid = row['id']
        else:
            iid = 'issue:' + uuid.uuid4().hex[:16]
            db.execute('INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (iid, question[:500], key, reason[:600], refusal.get('code'), refusal.get('stage'), 'open', 1,
                        now, now, None, None, None))
        return dict(db.execute('SELECT * FROM issues WHERE id=?', (iid,)).fetchone())


def list_issues(root, status='open'):
    with _LOCK, _db(root) as db:
        if status == 'all':
            rows = db.execute('SELECT * FROM issues ORDER BY last_at DESC').fetchall()
        else:
            rows = db.execute('SELECT * FROM issues WHERE status=? ORDER BY last_at DESC', (status,)).fetchall()
    return [{k: r[k] for k in r.keys() if k != 'question_key'} for r in rows]


def counts(root):
    with _LOCK, _db(root) as db:
        return {s: db.execute('SELECT COUNT(*) FROM issues WHERE status=?', (s,)).fetchone()[0] for s in STATUSES}


def decide(root, issue_id, action, actor, note):
    """resolve | dismiss | reopen, with the person's name and a note."""
    target = {'resolve': 'resolved', 'dismiss': 'dismissed', 'reopen': 'open'}.get(action)
    if not target:
        raise ValueError('Choose resolve, dismiss or reopen')
    actor, note = (actor or '').strip(), (note or '').strip()
    if not actor:
        raise ValueError('Set your name first: decisions are recorded with it')
    if not note:
        raise ValueError('Add a note saying how this was resolved')
    with _LOCK, _db(root) as db:
        row = db.execute('SELECT * FROM issues WHERE id=?', (issue_id,)).fetchone()
        if row is None:
            raise ValueError('Issue not found')
        if action == 'reopen' and row['status'] == 'open':
            raise ValueError('This issue is already open')
        if action != 'reopen' and row['status'] != 'open':
            raise ValueError('Only open issues can be resolved or dismissed')
        db.execute('UPDATE issues SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?',
                   (target, actor[:80], _now(), note[:600], issue_id))
        return dict(db.execute('SELECT * FROM issues WHERE id=?', (issue_id,)).fetchone())
