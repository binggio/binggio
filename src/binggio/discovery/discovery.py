# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Bounded, explainable cross-table candidate discovery; no oracle or LLM.

Only reads registered Phase4 CSV/SQLite sources via Phase5's read-only sampler.
Never writes to source files, never stores raw sample values in graph snapshots.
A score is a deterministic heuristic, NOT a probability or an approval.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from binggio.catalog import catalog
from binggio.catalog.connectors import ConnectionErrorSafe, resolve_source
from binggio.profiling import store as profiles
from binggio.profiling.profiler import SAMPLE_LIMIT, _sample_csv, _sample_sqlite, _is_missing
from . import VERSION

MAX_TABLES = 2000
MAX_EDGES = 20000
DISCOVERY_ROW_LIMIT = 200_000
MAX_IDENTIFIER_COLUMNS_PER_TABLE = 64
SOFT_CHILD_ROLES = ('CLASSIFICATION', 'TEXT', 'IDENTIFIER')
MIN_DISTINCT_CHILD = 3
MIN_OVERLAP = 0.45
MIN_SCORE = 0.60

# Vocabulary is domain-generic, not tied to synthetic source table names.
FAMILIES = {
 'customer': ('customer','cust','client','party'),
 'account': ('account','acct'),
 'transaction': ('transaction','txn','payment','transfer'),
 'complaint': ('complaint','grievance','dispute'),
 'product': ('product','prod'),
 'case': ('case','investigation'),
 'alert': ('alert',),
 'branch': ('branch',),
 'merchant': ('merchant',),
 'manager': ('manager','rm'),
 'ticket': ('ticket',),
 'survey': ('survey',),
 'household': ('household',),
 'period': ('period',),
 'rule': ('rule',),
 'batch': ('batch',),
 'assessment': ('assessment',),
 'center': ('center','centre'),
}
GENERIC = {'id','key','code','ref','reference','identifier','uuid','no','number','record'}

def _tokens(name: str) -> list[str]:
    s = re.sub(r'([a-z])([A-Z])',r'\1_\2',str(name))
    return [x for x in re.split(r'[^a-z0-9]+',s.lower()) if x]

def _family(name: str) -> str | None:
    words=_tokens(name)
    for family,aliases in FAMILIES.items():
        if any(w in aliases for w in words):return family
    return None

def _keyish(name: str, role: str) -> bool:
    words=_tokens(name)
    return role in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER') or bool(set(words)&GENERIC)

def _value(v: Any) -> str | None:
    if _is_missing(v):return None
    s=str(v).strip().casefold()
    if not s:return None
    # Conservative case/whitespace normalization only. No fuzzy value joins.
    return s

def _read_sample(project: Path, conn: dict, table: str, limit: int) -> tuple[list[str],list[list[Any]],dict]:
    source=resolve_source(project,conn['source_path'],conn['kind'])
    if conn['kind']=='csv_folder':
        p=(source/(table+'.csv')).resolve()
        if not p.is_relative_to(source) or not p.is_file():
            raise ConnectionErrorSafe('Registered CSV path no longer exists')
        return _sample_csv(p,limit)
    return _sample_sqlite(source,table,limit)

def _column_observations(headers: list[str], rows: list[list[Any]], p: dict) -> list[dict]:
    meta={x['name']:x for x in p['columns']}
    result=[]
    for i,name in enumerate(headers):
        col=meta.get(name)
        if not col:continue
        soft=not _keyish(name,col['semantic_role_candidate'])
        if soft and (col['semantic_role_candidate'] not in SOFT_CHILD_ROLES or col.get('observed_type') not in ('text','integer')):continue
        vals=[_value(row[i]) for row in rows]
        nonnull=[x for x in vals if x is not None]
        distinct=set(nonnull)
        if soft and len(distinct)<MIN_DISTINCT_CHILD:continue
        if len(result)>=MAX_IDENTIFIER_COLUMNS_PER_TABLE:break  # very wide tables: the first 64 identifier-like columns
        result.append({'name':name,'role':col['semantic_role_candidate'],'soft':soft,
          'family':_family(name),'distinct':distinct,'non_null':len(nonnull),
          'missing':len(vals)-len(nonnull),'unique':len(distinct)/len(nonnull) if nonnull else 0.0,
          'sample_rows':len(vals),'sample_complete':p['sample_complete']})
    return result

def _subject_family(table: str) -> str | None:
    """Conservative entity subject, not every entity mentioned in a table name.

    A customer_contacts.CUSTOMER_ID column may be unique in a sample, but the
    table is about contacts, not the authoritative customer entity.
    """
    words=_tokens(table)
    if not words:return None
    terminal={'master','history','log','directory','lines','table','data'}
    if words[-1] in terminal and len(words)>1:words=words[:-1]
    last=words[-1]
    if last.endswith('ies'):last=last[:-3]+'y'
    elif last.endswith('s') and not last.endswith('ss'):last=last[:-1]
    return _family(last)

def _singular(token: str) -> str:
    # Simple morphological normalization for unknown business entities.
    if token.endswith('ies') and len(token)>4:return token[:-3]+'y'
    if token.endswith('s') and not token.endswith('ss') and len(token)>3:return token[:-1]
    return token

def _identifier_subject(column: str) -> str | None:
    """Extract a non-generic key prefix without business-domain vocabulary."""
    words=[_singular(w) for w in _tokens(column)]
    while words and words[-1] in GENERIC:words.pop()
    # Standalone ID is too ambiguous to infer ownership.
    return '_'.join(words) if words else None

def _table_subject_tokens(table: str) -> set[str]:
    # Table suffixes can describe an account, log, event or a plural table.
    return {_singular(w) for w in _tokens(table)}

def _key_owned_by_table(table: str, column: str) -> bool:
    subject=_identifier_subject(column)
    if not subject:return False
    return all(w in _table_subject_tokens(table) for w in subject.split('_'))

def _own_key(cols: list[dict]) -> str | None:
    return next((c['name'] for c in cols if c['role']=='PRIMARY_KEY_CANDIDATE' and not c.get('soft')),None)

def _references_elsewhere(table: str, col: dict) -> bool:
    """A table's own key that names something else (vehicle_telematics.vin, dealer_directory.code
    with text codes) may also point at that thing's home table: the table extends or copies it.
    Plain counters (id = 1, 2, 3 ...) are excluded: they overlap with every other counter."""
    subject=_identifier_subject(col['name'])
    if subject is not None:return not _key_owned_by_table(table,col['name'])
    return any(not str(v).isdigit() for v in col['distinct'])

def _parent_candidates(table: str, cols: list[dict]) -> list[dict]:
    family=_subject_family(table)
    # Unknown entity names can still have evidence-backed parent keys.
    # Do not require an enumerated ontology entry such as "customer".
    # Parent key must represent its own table's entity and be unique/non-null
    # in the bounded sample. An unverified sample key is never 'approved'.
    result=[]
    own_key=_own_key(cols)
    for c in cols:
        if c.get('soft'):continue
        known=(family is not None and c['family']==family)
        # The table's own key is a parent key even when its name does not repeat the table's name
        # (vin in vehicles, part_number in parts).
        inferred=(_key_owned_by_table(table,c['name']) and (own_key is None or own_key==c['name'])) or c['name']==own_key
        if not (known or inferred) or c['non_null']<MIN_DISTINCT_CHILD:continue
        if c['missing'] or c['unique']<0.999 or len(c['distinct'])<MIN_DISTINCT_CHILD:continue
        result.append(c)
    return result

def _evaluate(child_table: dict, child: dict, parent_table: dict, parent: dict) -> dict | None:
    if child_table['id']==parent_table['id']:return None
    if len(child['distinct'])<MIN_DISTINCT_CHILD:return None
    overlap=len(child['distinct'] & parent['distinct'])
    if overlap<MIN_DISTINCT_CHILD:return None
    # Both distinct-value coverage and row-level coverage are sampled evidence;
    # do not overstate this as full-table referential integrity.
    coverage=overlap/len(child['distinct'])
    if coverage<MIN_OVERLAP:return None
    psub=_identifier_subject(parent['name'])
    lexical=(child['family']==parent['family'] and child['family'] is not None) or (
        _identifier_subject(child['name']) is not None and
        _identifier_subject(child['name'])==psub and
        _key_owned_by_table(parent_table['table'],parent['name'])) or (
        # the parent's own key named inside the child's name: vehicle_vin -> vehicles.vin
        psub is not None and parent['name']==parent_table.get('own_key') and
        set(psub.split('_'))<={_singular(w) for w in _tokens(child['name'])})
    if child.get('soft') and not (_singular(_tokens(parent_table['table'])[-1]) in {_singular(w) for w in _tokens(child['name'])}):
        return None  # a non-identifier column links only to a table its name points at (assigned_agent -> agents)
    if not lexical and coverage<0.90:return None
    if not lexical and child['family'] not in (None,parent['family']):
        # 'case' is an overloaded operational identifier (complaint, risk, ticket).
        # Permit only very strong sampled overlap and explicitly flag ambiguity.
        if not (child['family']=='case' and coverage>=0.95):return None
    completeness=child['sample_complete'] and parent['sample_complete']
    # Bounded, deterministic evidence score, not a statistical probability.
    score=(0.49*coverage+0.23*(1.0 if lexical else 0.45)+
           0.20*parent['unique']+0.08*(1.0 if completeness else 0.5))
    score=round(min(score,0.99),3)
    if score<MIN_SCORE:return None
    cid=child_table['connection_id']; pid=parent_table['connection_id']
    canonical='|'.join([cid,child_table['table'],child['name'],pid,parent_table['table'],parent['name']])
    eid='rel_'+hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]
    reasons=[f'{overlap} of {len(child["distinct"])} distinct child identifiers occur in sampled parent keys',
             'Parent key unique and non-null in observed sample',
             ('Identifier names share a business-entity family' if lexical else 'Strong value overlap despite ambiguous naming; business meaning requires review')]
    if not completeness:reasons.append('At least one source is first-N sampled; coverage may change')
    return {'id':eid,'child_connection_id':cid,'child_table':child_table['table'],'child_column':child['name'],
      'parent_connection_id':pid,'parent_table':parent_table['table'],'parent_column':parent['name'],
      'child_schema':child_table['schema'],'parent_schema':parent_table['schema'],
      'status':'candidate','score':score,'score_label':('strong' if score>=0.85 else 'moderate' if score>=0.72 else 'weak'),
      'evidence':{'child_distinct_sampled':len(child['distinct']),'parent_distinct_sampled':len(parent['distinct']),
        'overlapping_distinct':overlap,'distinct_coverage_pct':round(100*coverage,1),
        'parent_uniqueness_pct':round(100*parent['unique'],1),
        'child_missing_sampled':child['missing'],'parent_missing_sampled':parent['missing'],
        'child_sample_rows':child['sample_rows'],'parent_sample_rows':parent['sample_rows'],
        'child_sample_complete':child['sample_complete'],'parent_sample_complete':parent['sample_complete'],
        'lexical_family_match':lexical,'reasons':reasons,
        'child_is_own_key':child['name']==child_table.get('own_key') and child['unique']>=0.999 and not child['missing'],
        'limitations':['Heuristic evidence score is not a probability','No business or join semantics approved',
                       'Coverage measured on sampled distinct values, not row-level foreign-key validation']}}

def discover(project: Path, *, max_tables: int=MAX_TABLES, limit: int=SAMPLE_LIMIT) -> dict:
    project=Path(project).resolve()
    tables=catalog.list_tables(project,limit=max_tables+1)
    if not tables:raise ConnectionErrorSafe('No registered tables')
    if len(tables)>max_tables:raise ConnectionErrorSafe('Discovery is limited to %d tables per workspace' % max_tables)
    conns={x['id']:x for x in catalog.list_connections(project)}
    table_data=[]
    skipped=[]
    for t in tables:
        p=profiles.get_table(project,t['connection_id'],t['table_name'],include_stale=True)
        if not p:
            # A table that could not be profiled (empty file, duplicate headers) is reported by
            # profiling; it cannot be reviewed, so discovery leaves it out instead of stopping.
            skipped.append(t['connection_id']+'::'+t['table_name']);continue
        if p['stale']:raise ConnectionErrorSafe('Table %s changed since it was profiled. Run profile first.' % t['table_name'])
        if p['sample_limit']!=limit:raise ConnectionErrorSafe('Table %s was profiled with other settings. Run profile --force first.' % t['table_name'])
        # Key overlap is measured on up to DISCOVERY_ROW_LIMIT rows (profiles use a smaller sample),
        # so a child key that only appears beyond row 2,500 of its parent table is still found.
        headers,rows,_=_read_sample(project,conns[t['connection_id']],t['table_name'],DISCOVERY_ROW_LIMIT)
        # Reject a source changing during a discovery run; all-or-nothing publish.
        if profiles.get_table(project,t['connection_id'],t['table_name'])['stale']:
            raise ConnectionErrorSafe('Source changed during discovery; re-profile first')
        cols=_column_observations(headers,rows,p)
        table_data.append({'id':t['connection_id']+'::'+t['table_name'],
            'connection_id':t['connection_id'],'connection_name':t['connection_name'],
            'schema':t['schema_name'],'table':t['table_name'],
            'columns':cols,'parents':_parent_candidates(t['table_name'],cols),'own_key':_own_key(cols),
            'fingerprint':p['fingerprint'],'profiled_at':p['profiled_at'],
            'sample_rows':p['sample_rows'],'sample_complete':p['sample_complete']})
    # Inverted index over parent key values: only pairs that share values are evaluated, so the
    # cost grows with actual overlaps rather than with tables x columns.
    index=defaultdict(set)
    parents={}
    for pt in table_data:
        for pc in pt['parents']:
            k=(pt['id'],pc['name']);parents[k]=(pt,pc)
            for v in pc['distinct']:index[v].add(k)
    edges=[]
    for child_t in table_data:
        for child in child_t['columns']:
            if child['family'] is None and child['role']=='PRIMARY_KEY_CANDIDATE' and not _references_elsewhere(child_t['table'],child):continue
            hits=defaultdict(int)
            for v in child['distinct']:
                for k in index.get(v,()):hits[k]+=1
            for k,n in hits.items():
                if n<MIN_DISTINCT_CHILD or k[0]==child_t['id']:continue
                parent_t,parent=parents[k]
                candidate=_evaluate(child_t,child,parent_t,parent)
                if candidate:edges.append(candidate)
    # Same child identifier can overlap multiple parents. Keep ambiguity visible;
    # never automatically certify one parent over another.
    edges.sort(key=lambda x:(-x['score'],x['child_table'],x['child_column'],x['parent_table']))
    truncated=len(edges)>MAX_EDGES
    edges=edges[:MAX_EDGES]
    nodes=[{k:t[k] for k in ('id','connection_id','connection_name','schema','table','fingerprint','profiled_at','sample_rows','sample_complete')}
           for t in table_data]
    return {'version':VERSION,'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
      'nodes':nodes,'edges':edges,'skipped_tables':skipped,'limits':{'tables':MAX_TABLES,'sample_rows_per_table':limit,'max_edges':MAX_EDGES,'truncated':truncated},
      'method':'read_only_bounded_identifier_overlap_plus_schema_signals',
      'pairing':'inverted value index',
      'disclaimer':'Candidate graph only. No relationships approved; heuristic scores are not probabilities. No source rows retained.'}
