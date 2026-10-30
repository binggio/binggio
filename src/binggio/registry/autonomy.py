# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio Phase 7.1: bounded, policy-driven *local demo* semantic auto-approvals.

Only mechanically verifiable column roles and join candidates can be promoted.
No LLM approval, business metric approval, production IAM, or background task.
Every auto-approval uses Phase 7's revisioned artifact and audit event ledger.
"""
from __future__ import annotations
import json
import re
import sqlite3
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from binggio.profiling import store as profiles
from binggio.discovery import store as graph
from binggio.registry import store as governance

VERSION='ASG-1.1.0'
POLICY='ASG-TECHNICAL-STRICT-v1'
ACTOR='Binggio Policy Engine'
MIN_ROWS=20
MIN_CHILD_KEYS=10
MAX_COLUMNS=5000
MAX_EDGES=1000
_LOCK=threading.RLock()


def _now():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _db(project):
    path=Path(project).resolve()/'data'/'phase7_governance'/'autonomy.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=15)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, changed_at TEXT NOT NULL, actor TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, at TEXT NOT NULL, policy TEXT NOT NULL,
        summary_json TEXT NOT NULL, artifacts_json TEXT NOT NULL,
        rolled_back_at TEXT)''')
    db.commit()
    return db

def status(project):
    with _LOCK,_db(project) as db:
        row=db.execute('SELECT * FROM config WHERE id=1').fetchone()
        last=db.execute('SELECT id,at,summary_json,rolled_back_at FROM runs ORDER BY at DESC LIMIT 1').fetchone()
    return {'enabled':bool(row['enabled']) if row else False,'policy':POLICY,'version':VERSION,
        'changed_at':row['changed_at'] if row else None,
        'last_run':({'id':last['id'],'at':last['at'],'summary':json.loads(last['summary_json']),
                    'rolled_back_at':last['rolled_back_at']} if last else None),
        'local_demo_only':True,'run_mode':'explicit_on_demand',
        'limitations':['No unattended background scheduling in v1',
                       'First-N profile evidence is accepted only when the sample is complete',
                       'Business definitions are suggestions, never auto-approved',
                       'No production identity, authorization, or calibrated confidence']}

def set_enabled(project,enabled,confirmation):
    if type(enabled) is not bool:raise governance.ReviewValidation('enabled must be boolean')
    if enabled and confirmation!='ENABLE LOCAL AUTO-APPROVAL':
        raise governance.ReviewValidation('Explicit local-demo confirmation is required')
    with _LOCK,_db(project) as db:
        db.execute('INSERT INTO config VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,changed_at=excluded.changed_at,actor=excluded.actor',
                   (int(enabled),_now(),'local_demo_admin'))
    return status(project)

def _warnings(column):
    return [x for x in column.get('findings',[]) if x.get('severity')=='warning']

def _role_check(profile,col,verified_edge_id=None):
    reasons=[]
    if profile.get('stale'):reasons.append('Stale source profile')
    if not profile.get('sample_complete'):reasons.append('First-N sample does not cover the full source')
    if profile.get('sample_rows',0)<MIN_ROWS:reasons.append('Insufficient observed rows for strict policy')
    if any(x.get('severity')=='warning' for x in profile.get('table_findings',[])):
        reasons.append('Table-level quality warnings')
    if _warnings(col):reasons.append('Column quality warnings')
    if col.get('missing_count',0)!=0:reasons.append('Null or missing values observed')
    inferred=col.get('semantic_role_candidate','UNKNOWN')
    verified_identifier=bool(verified_edge_id and inferred in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE'))
    if col.get('role_confidence_label')!='high' and not verified_identifier:
        reasons.append('Not a high-evidence role candidate')
    role='IDENTIFIER' if verified_identifier else inferred
    if role=='IDENTIFIER':
        pass  # Strictly validated Phase 6 join supplies identifier evidence; not a PK certification.
    elif role=='PRIMARY_KEY_CANDIDATE':
        if not col.get('declared_primary_key'):reasons.append('No source-declared primary-key constraint')
        if col.get('distinct_pct')!=100:reasons.append('Key not fully unique in observed rows')
    elif role=='MEASURE':
        if col.get('observed_type') not in ('integer','decimal'):
            reasons.append('Not consistently numeric')
        if not re.search(r'amount|revenue|balance|cost|fee|interest|limit|budget|score|threshold|total|price|value|quantity|count',col['name'],re.I):
            reasons.append('No clear measure naming evidence')
    elif role=='DATE':
        if col.get('observed_type')!='date':reasons.append('Observed values not consistently dates')
    elif role=='STATUS':
        if col.get('distinct_count',100)>20:reasons.append('Too many status values')
    else:reasons.append('Role requires a human or verified relationship')
    return role,reasons

def _edge_check(edge,cp,pp,conflicting):
    ev=edge.get('evidence') or {}
    reasons=[]
    if edge.get('freshness')!='current':reasons.append('Stale relationship graph')
    if cp.get('stale') or pp.get('stale'):reasons.append('Source profile is stale')
    if not cp.get('sample_complete') or not pp.get('sample_complete'):
        reasons.append('Source coverage is only a first-N sample')
    if min(cp.get('sample_rows',0),pp.get('sample_rows',0))<MIN_ROWS:
        reasons.append('Insufficient observed source rows')
    if edge.get('score',0)<0.95:reasons.append('Heuristic join evidence below strict threshold')
    if ev.get('distinct_coverage_pct')!=100.0:reasons.append('Distinct child keys not fully covered')
    if ev.get('parent_uniqueness_pct')!=100.0:reasons.append('Parent key not fully unique')
    if ev.get('child_distinct_sampled',0)<MIN_CHILD_KEYS:reasons.append('Too few distinct child keys')
    if ev.get('child_missing_sampled',1)!=0 or ev.get('parent_missing_sampled',1)!=0:
        reasons.append('Missing join keys observed')
    if ev.get('lexical_family_match') is not True:reasons.append('Business identifier families do not match')
    if conflicting:reasons.append('Multiple candidate parent joins for the same child field')
    if ev.get('cardinality_hint')=='one_to_one':reasons.append('One-to-one link: a person confirms which table extends which')
    child=next((x for x in cp.get('columns',[]) if x['name']==edge['child_column']),None)
    parent=next((x for x in pp.get('columns',[]) if x['name']==edge['parent_column']),None)
    if not child or not parent:reasons.append('Join field absent from current profile')
    else:
        if _warnings(child) or _warnings(parent):reasons.append('Join-key quality warning')
        if parent.get('distinct_pct')!=100:reasons.append('Parent key not unique in profile')
        if child.get('distinct_pct')==100 and not child.get('missing_count'):
            reasons.append('Child key is also unique (one-to-one or reverse direction): needs a person')
        if child.get('semantic_role_candidate') not in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER'):
            reasons.append('Child field not an identifier candidate')
        if parent.get('semantic_role_candidate') not in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER'):
            reasons.append('Parent field not an identifier candidate')
    return reasons

def _all_edges(project):
    edges=graph.all_edges(project)
    return edges,False

def _build(project):
    """Evaluate fresh evidence; never return raw source values."""
    project=Path(project).resolve()
    existing={x['id']:x for x in governance._rows(project)}
    defined_metrics={(ref['connection_id'],ref['table'],ref['column'],x['proposed'].get('aggregation'))
        for x in existing.values() if x['kind']=='metric'
        for ref in x['proposed'].get('sources',[]) if len(x['proposed'].get('sources',[]))==1}
    all_edges,truncated_edges=_all_edges(project)
    candidates=Counter((e['child_connection_id'],e['child_table'],e['child_column'])
                       for e in all_edges if e['freshness']=='current' and e.get('score',0)>=0.90)
    tables={};profiled=[]
    for t in profiles.list_profiles(project):
        if t['status']=='not_profiled':continue
        p=profiles.get_table(project,t['connection_id'],t['table'],include_stale=True)
        if p:
            tables[(t['connection_id'],t['table'])]=p
            profiled.append((t,p))
    # A verified, unambiguous technical join can also establish a generic IDENTIFIER
    # role for its endpoint columns; it does not certify a primary key or business meaning.
    verified_fields={}
    for e in all_edges:
        cp=tables.get((e['child_connection_id'],e['child_table']))
        pp=tables.get((e['parent_connection_id'],e['parent_table']))
        if cp and pp and not _edge_check(e,cp,pp,candidates[(e['child_connection_id'],e['child_table'],e['child_column'])]>1):
            for key in ((e['child_connection_id'],e['child_table'],e['child_column']),
                        (e['parent_connection_id'],e['parent_table'],e['parent_column'])):
                verified_fields.setdefault(key,e['id'])
    items=[];exceptions=[];suggestions=[];counts=Counter();truncated_columns=False
    column_count=0
    for t,p in profiled:
        for c in p['columns']:
            column_count+=1
            if column_count>MAX_COLUMNS:
                truncated_columns=True;break
            key=[t['connection_id'],t['table'],c['name']]
            aid='column_role:'+governance._sha(key)[:24]
            if aid in existing:
                counts['previously_decided']+=1
                continue
            verified_edge=verified_fields.get(tuple(key))
            role,reasons=_role_check(p,c,verified_edge)
            row={'kind':'column_role','id':aid,'source':f'{key[0]} / {key[1]}.{key[2]}',
                 'role':role,'reasons':reasons,'evidence':{'sample_rows':p['sample_rows'],
                 'sample_complete':p['sample_complete'],'confidence_label':c['role_confidence_label'],
                 'missing_pct':c['missing_pct'],'distinct_pct':c['distinct_pct']},
                 '_key':key,'_verified_edge_id':verified_edge}
            if reasons:
                counts['requires_review']+=1
                if len(exceptions)<80:exceptions.append({k:v for k,v in row.items() if not k.startswith('_')})
            else:
                counts['eligible_roles']+=1;items.append(row)
            # Suggested definitions are explicitly non-approved, non-executable.
            if (role=='MEASURE' and c.get('observed_type') in ('integer','decimal')
                and re.search(r'(?:^|_)(?:amount|revenue|fee|cost|quantity|sales)(?:_|$)',c['name'],re.I)
                and (key[0],key[1],key[2],'SUM') not in defined_metrics
                and len(suggestions)<60):
                suggestions.append({'kind':'metric_suggestion','source':row['source'],
                    'name':'Sum of '+t['table']+'.'+c['name'],'aggregation':'SUM','status':'human_review_required',
                    'ref':{'connection_id':key[0],'table':key[1],'column':key[2]},
                    'warning':'Inclusion rules, sign conventions and business meaning are not validated'})
        if truncated_columns:break
    for e in all_edges:
        aid='relationship:'+e['id']
        if aid in existing:
            counts['previously_decided']+=1;continue
        cp=tables.get((e['child_connection_id'],e['child_table']))
        pp=tables.get((e['parent_connection_id'],e['parent_table']))
        if not cp or not pp:reasons=['Missing or out-of-scope source profile']
        else:reasons=_edge_check(e,cp,pp,candidates[(e['child_connection_id'],e['child_table'],e['child_column'])]>1)
        row={'kind':'relationship','id':aid,'edge_id':e['id'],
             'source':f"{e['child_table']}.{e['child_column']} → {e['parent_table']}.{e['parent_column']}",
             'role':'many_to_one','reasons':reasons,
             'evidence':{'score':e['score'],'distinct_coverage_pct':e['evidence'].get('distinct_coverage_pct'),
                         'parent_uniqueness_pct':e['evidence'].get('parent_uniqueness_pct'),
                         'sample_complete':bool(cp and pp and cp['sample_complete'] and pp['sample_complete'])}}
        if reasons:
            counts['requires_review']+=1
            if len(exceptions)<80:exceptions.append(row)
        else:
            counts['eligible_joins']+=1;items.append(row)
    counts['suggested_definitions']=len(suggestions)
    counts['scanned_columns']=min(column_count,MAX_COLUMNS)
    counts['scanned_edges']=len(all_edges)
    return {'items':items,'exceptions':exceptions,'suggestions':suggestions,'counts':dict(counts),
            'truncated':truncated_columns or truncated_edges,'policy':POLICY}

def preview(project):
    plan=_build(project)
    return {'policy':POLICY,'enabled':status(project)['enabled'],'counts':plan['counts'],
            'eligible_examples':[{k:v for k,v in x.items() if not k.startswith('_')} for x in plan['items'][:30]],
            'exceptions':plan['exceptions'][:35],'definition_suggestions':plan['suggestions'][:25],
            'truncated':plan['truncated'],
            'notice':'Dry run only. Approval requires explicit local enablement and Run. Confidence labels are not probabilities. All business metrics remain unapproved.'}

def _approve(project,item):
    """Atomic Phase7 revision-zero approval; never replaces human decisions."""
    if item['kind']=='column_role':
        cid,table,column=item['_key']
        c,signature=governance._column(project,cid,table,column)
        p=governance._profile(project,cid,table)
        verified_edge=item.get('_verified_edge_id')
        if verified_edge:
            edge,_=governance._relationship(project,verified_edge)
            cp=governance._profile(project,edge['child_connection_id'],edge['child_table'])
            pp=governance._profile(project,edge['parent_connection_id'],edge['parent_table'])
            edges,_truncated=_all_edges(project)
            competing=sum(e['child_connection_id']==edge['child_connection_id'] and
                          e['child_table']==edge['child_table'] and
                          e['child_column']==edge['child_column'] and e['freshness']=='current' and
                          e.get('score',0)>=0.90 for e in edges)>1
            if (_truncated or _edge_check(edge,cp,pp,competing) or
                (cid,table,column) not in ((edge['child_connection_id'],edge['child_table'],edge['child_column']),
                                            (edge['parent_connection_id'],edge['parent_table'],edge['parent_column']))):
                raise governance.ReviewConflict('Identifier relationship evidence changed')
        role,reasons=_role_check(p,c,verified_edge)
        if reasons or role!=item['role']:raise governance.ReviewConflict('Role evidence changed before approval')
        proposed={'connection_id':cid,'table':table,'column':column,
                  'candidate_role':c['semantic_role_candidate'],'role_evidence':c['role_evidence']}
        approved={'connection_id':cid,'table':table,'column':column,'role':role,
                  'approval_scope':'local_demo_metadata_only','approval_authority':'system_policy',
                  'policy_id':POLICY,'evidence_summary':item['evidence']}
        subject='::'.join((cid,table,column))
    else:
        edge,signature=governance._relationship(project,item['edge_id'])
        ev=edge['evidence'];cp=governance._profile(project,edge['child_connection_id'],edge['child_table'])
        pp=governance._profile(project,edge['parent_connection_id'],edge['parent_table'])
        # The complete candidate set is evaluated again by run() immediately before each batch;
        # no override is ever permitted.
        if _edge_check(edge,cp,pp,False):raise governance.ReviewConflict('Join evidence changed before approval')
        proposed={'edge_id':edge['id'],'candidate':edge}
        approved={'edge_id':edge['id'],'child_connection_id':edge['child_connection_id'],
                  'child_table':edge['child_table'],'child_column':edge['child_column'],
                  'parent_connection_id':edge['parent_connection_id'],'parent_table':edge['parent_table'],
                  'parent_column':edge['parent_column'],'cardinality':'many_to_one',
                  'mapping_overridden':False,'mapping_evidence':'phase6_candidate_evidence',
                  'temporal_rule':'','join_note':'Auto-validated technical key match; business meaning not certified',
                  'approval_scope':'local_demo_metadata_only','approval_authority':'system_policy',
                  'policy_id':POLICY,'evidence_summary':item['evidence']}
        subject=edge['id']
    def save(db):
        return governance._save(db,artifact_id=item['id'],kind=item['kind'],subject=subject,
                action='approve',actor=ACTOR,
                note='Auto-approved by '+POLICY+'; strict source completeness and evidence checks',
                expected_revision=0,proposed=proposed,approved=approved,signature=signature)
    return governance._transact(project,save)

def run(project):
    with _LOCK:
        if not status(project)['enabled']:
            raise governance.ReviewConflict('Autonomous approval is disabled. Enable it explicitly in local admin first.')
        plan=_build(project)
        if plan['truncated']:
            raise governance.ReviewConflict('Evidence scan exceeded its limits; no approvals applied. Narrow the source scope.')
        applied=[];conflicts=[]
        for item in plan['items']:
            try:
                _approve(project,item)
                applied.append(item['id'])
            except (governance.ReviewConflict,governance.ReviewValidation) as exc:
                conflicts.append({'id':item['id'],'reason':str(exc)[:180]})
        summary={'approved_roles':sum(x.startswith('column_role:') for x in applied),
                 'approved_joins':sum(x.startswith('relationship:') for x in applied),
                 'not_approved':plan['counts'].get('requires_review',0),
                 'suggested_definitions':plan['counts'].get('suggested_definitions',0),
                 'conflicts':len(conflicts)}
        run_id='asg_'+uuid.uuid4().hex[:20]
        with _db(project) as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,NULL)',
                       (run_id,_now(),POLICY,json.dumps(summary,sort_keys=True),json.dumps(applied)))
        return {'run_id':run_id,'summary':summary,'conflicts':conflicts[:20],
                'notice':'Only eligible technical metadata was approved. Metrics and exceptions remain for human review.'}

def history(project,limit=20):
    limit=max(1,min(int(limit),50))
    with _LOCK,_db(project) as db:
        rows=db.execute('SELECT * FROM runs ORDER BY at DESC LIMIT ?',(limit,)).fetchall()
    return {'items':[{'id':r['id'],'at':r['at'],'policy':r['policy'],
            'summary':json.loads(r['summary_json']),'rolled_back_at':r['rolled_back_at']} for r in rows]}

def rollback(project,run_id):
    with _LOCK,_db(project) as db:
        row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise governance.ReviewConflict('Unknown autonomy run')
        if row['rolled_back_at']:raise governance.ReviewConflict('Run was already rolled back')
        ids=json.loads(row['artifacts_json'])
        revoked=[];skipped=[]
        for aid in ids:
            def revoke(reg):
                current=governance._get(reg,aid)
                if (not current or current['status']!='approved' or current['actor']!=ACTOR
                        or current['revision']!=1 or
                        (current['approved'] or {}).get('policy_id')!=POLICY):
                    return False
                governance._save(reg,artifact_id=aid,kind=current['kind'],subject=current['subject_key'],
                    action='revoke',actor=ACTOR,note='Reversed autonomous run '+run_id,
                    expected_revision=current['revision'],proposed=current['proposed'],
                    approved=None,signature=None)
                return True
            if governance._transact(project,revoke):revoked.append(aid)
            else:skipped.append(aid)
        db.execute('UPDATE runs SET rolled_back_at=? WHERE id=?',(_now(),run_id))
    return {'run_id':run_id,'revoked':len(revoked),'skipped':len(skipped),
            'notice':'Human-modified or previously revoked artifacts were not overwritten.'}
