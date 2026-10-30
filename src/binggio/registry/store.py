# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Versioned HITL decisions and approved semantic metadata (the approved registry).

Review evidence is bound to a fingerprint of current graph/profile observations.
Approvals are never inferred from heuristic scores. A stale or missing source
makes previously approved metadata ineligible for downstream use.

No arbitrary SQL or executable expression is accepted. The approved registry is
read-only metadata for future Phase 8 planning; Phase 3.1 is not modified.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from binggio.catalog import catalog
from binggio.catalog.connectors import ConnectionErrorSafe
from binggio.profiling import store as profiles
from binggio.discovery import store as graph

_LOCK=threading.RLock()
ROLES={'PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER','MEASURE','DATE','CLASSIFICATION','STATUS','TEXT','UNKNOWN'}
CARDINALITIES={'one_to_one','one_to_many','many_to_one','many_to_many','unknown'}
AGGREGATIONS={'SUM','AVG','MIN','MAX','COUNT','COUNT_DISTINCT','NONE'}
MAX_NOTE=1000

class ReviewConflict(Exception):pass
class ReviewValidation(Exception):pass

def _now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def _json(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def _sha(value):return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()
def _require_text(value,name,maximum=MAX_NOTE,minimum=1):
    if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum:
        raise ReviewValidation(f'{name} must contain {minimum}–{maximum} characters')
    return value.strip()

def _db(project: Path):
    path=Path(project).resolve()/'data'/'phase7_governance'/'registry.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=15)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('''CREATE TABLE IF NOT EXISTS artifacts (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
      status TEXT NOT NULL, revision INTEGER NOT NULL,
      proposed_json TEXT NOT NULL, approved_json TEXT,
      evidence_signature TEXT, updated_at TEXT NOT NULL, actor TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL,
      revision INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
      note TEXT NOT NULL, event_json TEXT NOT NULL, at TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS ix_events_artifact ON events(artifact_id,id)')
    db.execute('CREATE INDEX IF NOT EXISTS ix_artifacts_kind ON artifacts(kind,status)')
    db.commit()
    return db

def _get(db,artifact_id):
    r=db.execute('SELECT * FROM artifacts WHERE id=?',(artifact_id,)).fetchone()
    if r is None:return None
    return {**dict(r),'proposed':json.loads(r['proposed_json']),
        'approved':json.loads(r['approved_json']) if r['approved_json'] else None}

def _public(row):
    if row is None:return None
    return {k:row[k] for k in ('id','kind','subject_key','status','revision','proposed','approved','updated_at','actor')}

def _profile(project,cid,table):
    try:p=profiles.get_table(project,cid,table,include_stale=True)
    except (ConnectionErrorSafe,OSError,sqlite3.Error):p=None
    if not p or p['stale']:raise ReviewConflict('Source profile is missing or stale; re-profile before reviewing')
    return p

def _relationship(project,edge_id):
    edge=graph.get_edge(project,edge_id)
    if edge is None:raise ReviewConflict('Candidate relationship no longer exists in current graph')
    if edge['freshness']!='current':raise ReviewConflict('Candidate relationship is stale; re-profile and rediscover')
    cp=_profile(project,edge['child_connection_id'],edge['child_table'])
    pp=_profile(project,edge['parent_connection_id'],edge['parent_table'])
    if _relationship_rule(project)<3:
        # older workspaces, until migrated: the whole candidate record, heuristics included
        bound={k:v for k,v in edge.items() if k!='freshness'}
    else:
        # which columns link, and the content of both tables; not Binggio's score or reasons
        bound={k:edge[k] for k in REL_IDENTITY}
    signature=_sha({'edge':bound,'child_fingerprint':cp['fingerprint'],'parent_fingerprint':pp['fingerprint']})
    return edge,signature

REL_IDENTITY=('id','child_connection_id','child_table','child_column','parent_connection_id','parent_table','parent_column')

def _relationship_rule(project):
    """Signature rule version recorded for this workspace (read once per evaluation)."""
    if _FORCE_REL_RULE[0] is not None:return _FORCE_REL_RULE[0]
    def read():
        try:
            with _LOCK,_db(project) as db:
                db.execute('CREATE TABLE IF NOT EXISTS binggio_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                r=db.execute("SELECT value FROM binggio_meta WHERE key='signature_version'").fetchone()
            return int(r[0]) if r else 1
        except (sqlite3.Error,ValueError):return 1
    return profiles._memo(('signature_rule',str(Path(project).resolve())),read)

_FORCE_REL_RULE=[None]

# Heuristic suggestions are not evidence: improving the role suggestions must not make
# a person's approval stale. Approvals bind to the observed column and the data's content.
HEURISTIC_FIELDS=('semantic_role_candidate','role_confidence_label','role_evidence')
SIGNATURE_VERSION=3
_LEGACY_COLUMN_SIGNATURE=[False]

def _column(project,cid,table,column):
    p=_profile(project,cid,table)
    col=next((c for c in p['columns'] if c['name']==column),None)
    if col is None:raise ReviewConflict('Column no longer exists in the current profile')
    evidence=col if _LEGACY_COLUMN_SIGNATURE[0] else {k:v for k,v in col.items() if k not in HEURISTIC_FIELDS}
    signature=_sha({'connection_id':cid,'table':table,'column':evidence,'fingerprint':p['fingerprint']})
    return col,signature

def _definition_signature(project,definition):
    # Source references are allowlisted against the registered Phase4 catalog.
    sources=[]
    for ref in definition['sources']:
        p=_profile(project,ref['connection_id'],ref['table'])
        cols={c['name'] for c in p['columns']}
        if ref['column'] not in cols:raise ReviewConflict('Referenced column missing: '+ref['column'])
        sources.append({'connection_id':ref['connection_id'],'table':ref['table'],
            'column':ref['column'],'fingerprint':p['fingerprint']})
    return _sha({'definition':definition,'sources':sources})

def _status(project,row):
    if row['status']!='approved':return row['status']
    try:
        if row['kind']=='relationship':
            _,current=_relationship(project,row['proposed']['edge_id'])
        elif row['kind']=='column_role':
            p=row['proposed'];_,current=_column(project,p['connection_id'],p['table'],p['column'])
        else:current=_definition_signature(project,row['approved'])
    except (ReviewConflict,ConnectionErrorSafe,OSError,sqlite3.Error):return 'stale'
    return 'approved' if current==row['evidence_signature'] else 'stale'

def _save(db,*,artifact_id,kind,subject,action,actor,note,expected_revision,proposed,approved,signature):
    current=_get(db,artifact_id)
    previous=current['revision'] if current else 0
    if expected_revision!=previous:
        raise ReviewConflict(f'Revision conflict: expected {expected_revision}, current {previous}. Refresh the review item.')
    status={'approve':'approved','reject':'rejected','reopen':'pending','draft':'draft','revise':'draft','revoke':'revoked'}[action]
    rev=previous+1;now=_now()
    db.execute('''INSERT INTO artifacts(id,kind,subject_key,status,revision,proposed_json,approved_json,evidence_signature,updated_at,actor)
       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,
       revision=excluded.revision,proposed_json=excluded.proposed_json,approved_json=excluded.approved_json,
       evidence_signature=excluded.evidence_signature,updated_at=excluded.updated_at,actor=excluded.actor''',
       (artifact_id,kind,subject,status,rev,_json(proposed),_json(approved) if approved else None,
        signature if status=='approved' else None,now,actor))
    db.execute('INSERT INTO events(artifact_id,revision,action,actor,note,event_json,at) VALUES(?,?,?,?,?,?,?)',
        (artifact_id,rev,action,actor,note,_json({'proposed':proposed,'approved':approved,'signature':signature if status=='approved' else None}),now))
    return _get(db,artifact_id)

def _transact(project,callback):
    with _LOCK,_db(project) as db:
        db.execute('BEGIN IMMEDIATE')
        result=callback(db)
        db.commit()
        return result

def decide_relationship(project:Path,edge_id:str,*,action:str,actor:str,note:str,
                        expected_revision:int,cardinality:str='unknown',temporal_rule:str='',join_note:str='',
                        child_column_override:str='',parent_column_override:str=''):
    if action not in ('approve','reject','reopen','revoke'):raise ReviewValidation('Invalid relationship action')
    actor=_require_text(actor,'Reviewer',80);note=_require_text(note,'Review rationale',600)
    if cardinality not in CARDINALITIES:raise ReviewValidation('Invalid cardinality')
    if action=='approve' and cardinality=='unknown':raise ReviewValidation('Select a cardinality before approval')
    temporal_rule=_require_text(temporal_rule,'Temporal rule',400,0)
    join_note=_require_text(join_note,'Join semantics',400,0)
    if not isinstance(expected_revision,int) or expected_revision<0:raise ReviewValidation('Invalid expected revision')
    # Revocation must remain possible even if the underlying source became stale.
    edge,signature=(_relationship(project,edge_id) if action!='revoke' else (None,None))
    aid='relationship:'+edge_id
    if action=='approve':
        child_column_override=_require_text(child_column_override,'Child column override',120,0)
        parent_column_override=_require_text(parent_column_override,'Parent column override',120,0)
        if child_column_override and child_column_override!=edge['child_column']:
            _column(project,edge['child_connection_id'],edge['child_table'],child_column_override)
        if parent_column_override and parent_column_override!=edge['parent_column']:
            _column(project,edge['parent_connection_id'],edge['parent_table'],parent_column_override)
    actual_child=child_column_override or (edge['child_column'] if edge else '')
    actual_parent=parent_column_override or (edge['parent_column'] if edge else '')
    overridden=bool(edge and (actual_child!=edge['child_column'] or actual_parent!=edge['parent_column']))
    approved={'edge_id':edge_id,'child_connection_id':edge['child_connection_id'],
        'child_table':edge['child_table'],'child_column':actual_child,
        'parent_connection_id':edge['parent_connection_id'],'parent_table':edge['parent_table'],
        'parent_column':actual_parent,'cardinality':cardinality,
        'mapping_overridden':overridden,'mapping_evidence':'human_override_not_validated_by_discovery' if overridden else 'phase6_candidate_evidence',
        'temporal_rule':temporal_rule,'join_note':join_note,
        'approval_scope':'local_demo_metadata_only'} if action=='approve' else None
    def op(db):
        old=_get(db,aid)
        if action=='revoke' and (not old or old['status']!='approved'):
            raise ReviewConflict('Only an approved relationship can be revoked')
        if action=='reopen' and (not old or old['status'] not in ('rejected','revoked')):
            raise ReviewConflict('Only a rejected/revoked relationship can be reopened')
        return _public(_save(db,artifact_id=aid,kind='relationship',subject=edge_id,
            action=action,actor=actor,note=note,expected_revision=expected_revision,
            proposed=old['proposed'] if action=='revoke' else {'edge_id':edge_id,'candidate':edge},
            approved=approved,signature=signature))
    return _transact(project,op)

def decide_column(project:Path,cid:str,table:str,column:str,*,action:str,actor:str,
                  note:str,expected_revision:int,role:str,logical_type:str='source'):
    if action not in ('approve','reject','reopen','revoke'):raise ReviewValidation('Invalid column action')
    actor=_require_text(actor,'Reviewer',80);note=_require_text(note,'Review rationale',600)
    if role not in ROLES:raise ReviewValidation('Invalid semantic role')
    if logical_type not in ('source','text','integer','decimal','date','timestamp','boolean'):
        raise ReviewValidation('Invalid logical data type')
    if not isinstance(expected_revision,int) or expected_revision<0:raise ReviewValidation('Invalid expected revision')
    col,signature=(_column(project,cid,table,column) if action!='revoke' else (None,None))
    aid='column_role:'+_sha([cid,table,column])[:24]
    approved={'connection_id':cid,'table':table,'column':column,'role':role,
              'logical_type_override':logical_type,
              'observed_source_type':col.get('observed_type') if col else None,
              'approval_scope':'local_demo_metadata_only'} if action=='approve' else None
    def op(db):
        old=_get(db,aid)
        if action=='revoke' and (not old or old['status']!='approved'):
            raise ReviewConflict('Only an approved column role can be revoked')
        if action=='reopen' and (not old or old['status'] not in ('rejected','revoked')):
            raise ReviewConflict('Only a rejected/revoked role can be reopened')
        return _public(_save(db,artifact_id=aid,kind='column_role',subject=cid+'::'+table+'::'+column,
            action=action,actor=actor,note=note,expected_revision=expected_revision,
            proposed=old['proposed'] if action=='revoke' else {'connection_id':cid,'table':table,'column':column,
                      'candidate_role':col['semantic_role_candidate'],'role_evidence':col['role_evidence']},
            approved=approved,signature=signature))
    return _transact(project,op)

def _validate_definition(project,definition):
    if not isinstance(definition,dict):raise ReviewValidation('Definition must be an object')
    kind=definition.get('kind')
    if kind not in ('metric','business_rule'):raise ReviewValidation('Definition kind must be metric or business_rule')
    name=_require_text(definition.get('name'),'Business name',100)
    description=_require_text(definition.get('description'),'Definition description',600)
    domain=_require_text(definition.get('domain'),'Business domain',80)
    owner=_require_text(definition.get('owner'),'Definition owner',80)
    refs=definition.get('sources')
    if not isinstance(refs,list) or not 1<=len(refs)<=8:raise ReviewValidation('Include 1–8 registered source column references')
    sources=[];seen=set()
    for ref in refs:
        if not isinstance(ref,dict):raise ReviewValidation('Source must be an object')
        cid=_require_text(ref.get('connection_id'),'Connection',100)
        table=_require_text(ref.get('table'),'Table',120)
        column=_require_text(ref.get('column'),'Column',120)
        key=(cid,table,column)
        if key in seen:raise ReviewValidation('Duplicate source reference')
        seen.add(key);_column(project,cid,table,column)
        sources.append({'connection_id':cid,'table':table,'column':column})
    aggregation=definition.get('aggregation','NONE')
    if aggregation not in AGGREGATIONS:raise ReviewValidation('Unsupported aggregation')
    if kind=='business_rule' and aggregation!='NONE':raise ReviewValidation('Business rules cannot declare a metric aggregation')
    if kind=='metric' and aggregation=='NONE':raise ReviewValidation('Metric must declare an aggregation')
    return {'kind':kind,'name':name,'description':description,'domain':domain,
            'owner':owner,'sources':sources,'aggregation':aggregation,
            'executable':False,'scope':'local_demo_semantic_metadata'}

def create_definition(project,definition,*,actor,note):
    actor=_require_text(actor,'Author',80);note=_require_text(note,'Creation rationale',600)
    item=_validate_definition(project,definition)
    aid='definition:'+uuid.uuid4().hex
    def op(db):return _public(_save(db,artifact_id=aid,kind=item['kind'],subject=item['name'],
        action='draft',actor=actor,note=note,expected_revision=0,
        proposed=item,approved=None,signature=None))
    return _transact(project,op)

def decide_definition(project,aid,*,action,actor,note,expected_revision,definition=None):
    if action not in ('approve','reject','revise','revoke','reopen'):raise ReviewValidation('Invalid definition action')
    actor=_require_text(actor,'Reviewer',80);note=_require_text(note,'Review rationale',600)
    if not isinstance(expected_revision,int) or expected_revision<1:raise ReviewValidation('Invalid revision')
    def op(db):
        current=_get(db,aid)
        if current is None or current['kind'] not in ('metric','business_rule'):
            raise ReviewConflict('Business definition not found')
        if action=='revoke' and current['status']!='approved':raise ReviewConflict('Only approved definitions can be revoked')
        if action=='reopen' and current['status'] not in ('rejected','revoked'):
            raise ReviewConflict('Only rejected/revoked definitions can be reopened')
        if action=='revise' and definition is None:raise ReviewValidation('Supply a revised definition')
        proposed=_validate_definition(project,definition) if definition is not None else current['proposed']
        if proposed['kind']!=current['kind']:raise ReviewValidation('Cannot change definition kind')
        if action=='approve':
            # Prevent conflicting simultaneously approved meanings under the same
            # business domain and metric/rule name. This is a governance rule,
            # not a semantic equivalence algorithm.
            others=db.execute('SELECT * FROM artifacts WHERE kind=? AND status=? AND id<>?',
                              (current['kind'],'approved',aid)).fetchall()
            for other_row in others:
                other=_get_row(other_row)
                other_def=other['approved']
                if (other_def and other_def['domain'].casefold()==proposed['domain'].casefold()
                        and other_def['name'].casefold()==proposed['name'].casefold()
                        and _status(project,other)=='approved'):
                    raise ReviewConflict('Another current approved definition has this domain and name; revise or revoke it first')
        signature=_definition_signature(project,proposed) if action=='approve' else None
        approved=proposed if action=='approve' else None
        return _public(_save(db,artifact_id=aid,kind=current['kind'],subject=proposed['name'],
            action=action,actor=actor,note=note,expected_revision=expected_revision,
            proposed=proposed,approved=approved,signature=signature))
    return _transact(project,op)

def _rows(project,kind=None):
    with profiles.evaluation_scope():return _rows_in_scope(project,kind)

def _rows_in_scope(project,kind):
    with _LOCK,_db(project) as db:
        if kind:rows=db.execute('SELECT * FROM artifacts WHERE kind=? ORDER BY updated_at DESC,id',(kind,)).fetchall()
        else:rows=db.execute('SELECT * FROM artifacts ORDER BY updated_at DESC,id').fetchall()
    return [{**_public(_get_row(r)),'effective_status':_status(project,_get_row(r))} for r in rows]

def _get_row(r):
    return {**dict(r),'proposed':json.loads(r['proposed_json']),
            'approved':json.loads(r['approved_json']) if r['approved_json'] else None}

def get_artifact(project,aid):
    with _LOCK,_db(project) as db:row=_get(db,aid)
    return {**_public(row),'effective_status':_status(project,row)} if row else None

def list_definitions(project):
    return [x for x in _rows(project) if x['kind'] in ('metric','business_rule')]

def approved_registry(project):
    items=[x for x in _rows(project) if x['effective_status']=='approved']
    return {'items':items,'count':len(items),'execution_enabled':False,
        'disclaimer':'Locally reviewed metadata only; not integrated with Phase 3.1 SQL execution or production authorization.'}

def events(project,artifact_id=None,limit=100):
    limit=max(1,min(int(limit),500))
    with _LOCK,_db(project) as db:
        if artifact_id:rows=db.execute('SELECT * FROM events WHERE artifact_id=? ORDER BY id DESC LIMIT ?', (artifact_id,limit)).fetchall()
        else:rows=db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return {'items':[{**dict(r),'event':json.loads(r['event_json'])} for r in rows], 'count':len(rows)}

def list_relationships(project,*,status='all',limit=100,offset=0):
    with profiles.evaluation_scope():return _list_relationships(project,status,limit,offset)

def _list_relationships(project,status,limit,offset):
    all_edges=graph.all_edges(project)
    decisions={x['subject_key']:x for x in _rows(project,'relationship')}
    items=[]
    for e in all_edges:
        d=decisions.get(e['id']);effective=d['effective_status'] if d else 'pending'
        if status!='all' and effective!=status:continue
        items.append({'edge_id':e['id'],'child_table':e['child_table'],'child_column':e['child_column'],
            'parent_table':e['parent_table'],'parent_column':e['parent_column'],
            'score':e['score'],'freshness':e['freshness'],'effective_status':effective,
            'decision':d,'evidence':e['evidence']})
    return {'items':items[offset:offset+limit],'total':len(items),'limit':limit,'offset':offset}

def _reconciled_candidate(col:dict, edges:list[dict], connection_id:str,table:str) -> tuple[str,list[str]]:
    """Non-authoritative role proposal combining profiles with current graph evidence.

    A parent must be unique/non-null; child role requires actual current graph
    evidence.  A shared primary/foreign key is possible, so surface conflicts
    rather than changing an approved human decision.
    """
    role=col['semantic_role_candidate']
    evidence=list(col.get('role_evidence') or [])
    name=col['name']
    related=[e for e in edges if e.get('freshness')=='current']
    parent=[e for e in related if e['parent_connection_id']==connection_id and
            e['parent_table']==table and e['parent_column']==name]
    child=[e for e in related if e['child_connection_id']==connection_id and
           e['child_table']==table and e['child_column']==name]
    unique=col.get('distinct_count',0)==col.get('non_null_count',-1) and col.get('non_null_count',0)>=2
    nonnull=col.get('missing_count',1)==0
    if parent and unique and nonnull:
        evidence.append('Phase 6 current relationship: unique, non-null parent identifier')
        if child:
            evidence.append('Also a child in another relationship: primary and foreign key roles may coexist; review required')
        if role!='PRIMARY_KEY_CANDIDATE':
            role='PRIMARY_KEY_CANDIDATE'
    elif child:
        evidence.append('Phase 6 current relationship: observed child identifier')
        if role not in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE'):
            role='FOREIGN_KEY_CANDIDATE'
    return role,evidence

def list_roles(project,*,connection_id=None,table=None,status='all',limit=100,offset=0):
    with profiles.evaluation_scope():return _list_roles(project,connection_id,table,status,limit,offset)

def _list_roles(project,connection_id,table,status,limit,offset):
    decisions={x['subject_key']:x for x in _rows(project,'column_role')}
    # Read the current graph once, not separately for each column.
    edges=graph.all_edges(project)
    items=[]
    for t in profiles.list_profiles(project,connection_id=connection_id):
        if table and t['table']!=table:continue
        if t['status']=='not_profiled':continue
        try:p=profiles.get_table(project,t['connection_id'],t['table'],include_stale=True)
        except (ConnectionErrorSafe,OSError):continue
        if not p:continue
        for c in p['columns']:
            key=t['connection_id']+'::'+t['table']+'::'+c['name']
            d=decisions.get(key)
            effective=d['effective_status'] if d else ('stale' if p['stale'] else 'pending')
            if status!='all' and effective!=status:continue
            suggested,role_evidence=_reconciled_candidate(c,edges,t['connection_id'],t['table'])
            items.append({'connection_id':t['connection_id'],'table':t['table'],'column':c['name'],
                'candidate_role':suggested,'role_evidence':role_evidence,
                'effective_status':effective,'decision':d})
    return {'items':items[offset:offset+limit],'total':len(items),'limit':limit,'offset':offset}

def summary(project):
    with profiles.evaluation_scope():return _summary(project)

def _summary(project):
    rel=list_relationships(project,limit=1)
    rel_all=list_relationships(project,limit=10**7)['items']
    d=_rows(project)
    counts={s:sum(x['effective_status']==s for x in d) for s in ('approved','rejected','stale','draft','revoked','pending')}
    rel_counts={s:sum(x['effective_status']==s for x in rel_all) for s in ('approved','rejected','stale','pending','revoked')}
    return {'candidate_relationships':rel['total'],'reviewed_relationships':sum(x['decision'] is not None for x in rel_all),
        'relationship_status':rel_counts,'registry_status':counts,'approved_current':counts['approved'],
        'reviewed_artifacts':len(d),'local_demo_only':True,
        'disclaimer':'Reviewer names are self-entered, not authenticated identities. No production IAM or automatic SQL promotion.'}
