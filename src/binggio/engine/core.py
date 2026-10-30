# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio POC unified, registry-driven semantic query engine.

Consumes only approved Phase8A context and Phase8A's existing source reader.
Accepts a constrained, inspectable canonical AST, never executable model SQL.
Read-only in-memory SQLite is used to run AST plans across approved snapshots.
No organization data, existing DB state or approval flags are ever modified.
"""
from __future__ import annotations
import collections
import datetime
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import time
import urllib.request
from pathlib import Path

class Blocked(ValueError): pass
class Unrecognized(ValueError): pass
class InvalidCanonicalPlan(Unrecognized): pass
class UnsupportedClass(Blocked):
    """The question belongs to a class the engine cannot answer yet (ratio,
    relative window, duration, decile, change over time, aggregate filter).
    A capability gap, not a missing business definition: no review item."""

AGGS={'count','count_distinct','sum','avg','min','max','median','percentile'}
OPS={'eq','ne','gt','gte','lt','lte','in','not_in','like','between','is_null','not_null'}
SORTS={'asc','desc'}
ID_ROLES={'PRIMARY_KEY','PRIMARY_KEY_CANDIDATE','IDENTIFIER'}
KEY_ROLES=ID_ROLES|{'FOREIGN_KEY','FOREIGN_KEY_CANDIDATE'}
_PLAIN_NUMBER=re.compile(r'-?\d+(?:\.\d+)?\Z')
# Thousands separators in Western (1,075,759.95) or Indian (10,75,759.95) grouping.
_GROUPED_NUMBER=re.compile(r'-?\d{1,3}(?:,\d{2,3})*,\d{3}(?:\.\d+)?\Z')
_ISO_DATE=re.compile(r'\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]*)?\Z')
_SLASH_ISO_DATE=re.compile(r'(\d{4})/(\d{2})/(\d{2})\Z')


def normalize_key(value):
    return str(value).strip().upper()


def parse_number(value):
    """Strict numeric parse (v17): plain numbers, or thousands separators in a
    valid grouping. Anything else is refused rather than guessed."""
    if isinstance(value,(int,float)) and not isinstance(value,bool):return float(value)
    text=str(value).strip()
    if _PLAIN_NUMBER.fullmatch(text):return float(text)
    if _GROUPED_NUMBER.fullmatch(text):return float(text.replace(',',''))
    raise ValueError('not a number: unrecognised format')


def _date_field(info):
    return str(info.get('role','')).upper()=='DATE' or str(info.get('observed_type','')).lower()=='date'
MODEL_ALLOWLIST={'gpt-4.1-mini','gpt-4.1','gpt-4o-mini','gpt-4o'}
MAX_RESULT=200
QUERY_SECONDS=120  # an answer that takes longer is stopped and reported, never returned partly
MAX_SOURCE_ROWS=1_000_000  # rows per source table read for an answer (profiling uses a smaller sample)
ENGINE_VERSION='v18.3.2'
log=logging.getLogger('binggio.unified')


def _need(condition,message):
    if not condition:raise Blocked(message)


def _name(s):
    return str(s).strip().lower().replace('_',' ')


def _quote(v):return '"'+str(v).replace('"','""')+'"'


def _digest(ctx):
    if not ctx.get('revision_signature'):raise Blocked('Approved registry signature is unavailable')
    return str(ctx['revision_signature'])


class Universe:
    def __init__(self,ctx):
        _need(isinstance(ctx,dict),'Approved context must be a mapping')
        self.revision=_digest(ctx)
        self.ctx=ctx
        self.tables=dict(ctx.get('tables',{}));self.fields=dict(ctx.get('fields',{}));self.joins=dict(ctx.get('joins',{}))
        _need(self.tables and self.fields,'The approved universe has no tables or fields')
        self.by_table=collections.defaultdict(list)
        for fid,info in self.fields.items():
            tid=info.get('table_id')
            _need(tid in self.tables,'Approved field has unknown table: '+str(fid))
            self.by_table[tid].append(fid)
        # v17: identifier and join-key fields are compared after trimming spaces
        # and ignoring case - the same rule Phase 6 used when the relationships
        # were discovered and approved. Without it ' c0004 ' silently fails to
        # join to 'C0004' and rows drop out of answers.
        self.normalized_keys={fid for fid,info in self.fields.items()
                              if str(info.get('role','')).upper() in KEY_ROLES}
        for edge in self.joins.values():
            for side in ('left','right'):
                if edge.get(side) in self.fields:self.normalized_keys.add(edge[side])
        self.adj=collections.defaultdict(list)
        for eid,edge in self.joins.items():
            left=self.fields.get(edge.get('left'));right=self.fields.get(edge.get('right'))
            if not left or not right:continue
            if edge.get('temporal_rule'):continue
            a,b=left['table_id'],right['table_id']
            if a==b:continue
            self.adj[a].append((b,eid));self.adj[b].append((a,eid))

    def concepts(self):
        """v18.1: approved business concepts (primary/secondary sources, aliases, attributes)."""
        return {cid:c for cid,c in (self.ctx.get('concepts') or {}).items()
                if isinstance(c,dict) and c.get('primary_table') in self.tables}

    def terms(self):
        """v18.3: approved governed terms whose columns are approved (and as-of, when needed)."""
        return {tid:t for tid,t in (self.ctx.get('governed_terms') or {}).items()
                if isinstance(t,dict) and t.get('table_id') in self.tables and str(t.get('status','approved')).lower()=='approved'
                and all(c.get('field') in self.fields for c in t.get('conditions') or [])
                and (not t.get('needs_as_of') or self.as_of_date())}

    def as_of_date(self):
        a=self.ctx.get('as_of')
        return a.get('date') if isinstance(a,dict) else None

    def secondary_tables(self):
        primaries={c['primary_table'] for c in self.concepts().values()}
        return {s['table_id'] for c in self.concepts().values() for s in c.get('secondary') or []
                if s.get('table_id') in self.tables and s['table_id'] not in primaries}

    def preserves_rows(self,base,tid):
        """True when joining base to tid can never repeat a base row (v18.1): every step
        of the approved path goes from the child to the parent of a many-to-one (or a
        one-to-one) relationship, whose parent key was verified unique at approval."""
        if base==tid:return True
        try:edges=self.path(base,tid);nodes=self.tables_in_path(base,edges)
        except Blocked:return False
        for i,eid in enumerate(edges):
            j=self.joins[eid];card=str(j.get('cardinality','')).lower()
            child=self.fields[j['left']]['table_id']
            if card=='one_to_one':continue
            if card!='many_to_one' or child!=nodes[i]:return False
        return True

    def path(self,source,target,max_hops=4):
        _need(source in self.tables and target in self.tables,'Unknown source in approved registry')
        if source==target:return []
        queue=collections.deque([(source,(),frozenset([source]))]);solutions=[];best=None
        while queue:
            node,edges,seen=queue.popleft()
            if best is not None and len(edges)>=best:continue
            if len(edges)>=max_hops:continue
            for nxt,eid in self.adj[node]:
                if nxt in seen:continue
                nxt_edges=edges+(eid,)
                if nxt==target:
                    if best is None:best=len(nxt_edges)
                    if len(nxt_edges)==best:solutions.append(nxt_edges)
                elif best is None or len(nxt_edges)<best:
                    queue.append((nxt,nxt_edges,seen|{nxt}))
        if not solutions:raise Blocked('No approved relationship path between '+source+' and '+target)
        if len(solutions)!=1:raise Blocked('Multiple approved relationship paths between '+source+' and '+target+'; select one explicitly')
        return list(solutions[0])

    def tables_in_path(self,start,edges):
        nodes=[start]
        for eid in edges:
            e=self.joins[eid]
            a=self.fields[e['left']]['table_id'];b=self.fields[e['right']]['table_id']
            if nodes[-1]==a:nodes.append(b)
            elif nodes[-1]==b:nodes.append(a)
            else:raise Blocked('Broken relationship chain')
        return nodes

    def _connected(self,a,b):
        try:self.path(a,b);return True
        except Blocked:return False

    def catalog(self,focus=None,max_chars=65000,observed_status_domains=None,concepts=None,terms=None):
        # Entire bounded POC universe, with source identifiers. No row values.
        result=[]
        # v18.1: tables in a fixed order (by ID). The registry lists approvals newest
        # first, so re-approving a table used to move it in the prompt and change
        # the model's choices for unrelated questions.
        selected=set(focus) if focus is not None else set(self.tables)-self.secondary_tables()
        for tid in sorted(self.tables):
            t=self.tables[tid]
            if tid not in selected:continue
            cols=[{'id':fid,'name':self.fields[fid]['name'],'role':self.fields[fid].get('role',''),
                  'type':str(self.fields[fid].get('data_type',self.fields[fid].get('type',''))
                             or self.fields[fid].get('observed_type',''))}
                  for fid in self.by_table[tid]]
            result.append({'id':tid,'name':t.get('table',tid),'description':t.get('description',''),'fields':cols})
        edges=[{'id':eid,'left':j['left'],'right':j['right']}
               for eid,j in sorted(self.joins.items()) if j.get('left') in self.fields and j.get('right') in self.fields
               and self.fields[j['left']]['table_id'] in selected and self.fields[j['right']]['table_id'] in selected]
        # v18.0: approved business definitions from the registry (approved_context
        # puts them under 'metrics'); before v18.0 the model never saw them.
        usable,_excluded=business_definitions(self)
        defs=[{k:d[k] for k in ('name','domain','aggregation','field','table')} for d in usable if d['table'] in selected]
        legacy=self.ctx.get('business_definitions',self.ctx.get('definitions'))
        if legacy:defs={'registry':defs,'other':legacy}
        domains={fid:v for fid,v in sorted((observed_status_domains or {}).items())
                 if fid in self.fields and self.fields[fid]['table_id'] in selected}
        content={'tables':result,'approved_joins':edges,'approved_business_definitions':defs,'observed_status_domains':domains}
        if focus is not None:
            # Focused tables may also be linked through approved tables not shown; the
            # engine applies those joins itself.
            ordered=sorted(selected)
            content['connected_tables']=[[a,b] for i,a in enumerate(ordered) for b in ordered[i+1:] if self._connected(a,b)]
        if concepts:content['business_concepts']=concepts
        if terms:content['approved_terms']=terms
        dump=json.dumps(content,default=str)
        _need(len(dump)<=max_chars,'Approved context exceeds the bounded semantic prompt. Narrow the source selection.')
        return dump

    def key_fields(self,tid):
        return [fid for fid in self.by_table[tid] if self.fields[fid].get('role','').upper() in ID_ROLES]


def _safe_fields(x,u):
    _need(isinstance(x,list) and len(x)<=20,'Projection must have at most 20 approved fields')
    for fid in x:_need(fid in u.fields,'Unapproved field: '+str(fid))
    return x


def _check_filter(f,u):
    _need(type(f)==dict and set(f)=={'field','op','value'},
          'Filter must contain exactly field, op, value; found '+str(sorted(f) if isinstance(f,dict) else type(f).__name__))
    fid,op,val=f['field'],f['op'],f['value']
    _need(fid in u.fields,'Filter uses unapproved field '+str(fid))
    _need(op in OPS,'Unsupported comparison operator '+str(op))
    if op in ('is_null','not_null'):_need(val is None,'Null operators require null value')
    elif op in ('in','not_in'):_need(isinstance(val,list) and 0<len(val)<=200 and all(_scalar(x) for x in val),'Membership requires bounded scalar values')
    elif op=='between':_need(isinstance(val,list) and len(val)==2 and all(_scalar(x) for x in val),'Between requires two scalar bounds')
    else:_need(val is not None and _scalar(val),'Comparison requires a non-null scalar; use is_null/not_null for NULL')
    if op in ('gt','gte','lt','lte','between') and (type(val) in (int,float) or (op=='between' and all(type(x) in (int,float) for x in val))):
        _need(_type_for_field(u.fields[fid])=='REAL','Numeric comparison requires an approved numeric field: '+fid)
    return f


def _scalar(x):return x is None or (type(x) in (bool,int,float,str) and (not isinstance(x,float) or math.isfinite(x)) and (not isinstance(x,str) or len(x)<=300))


def _check_where(node,u,depth=0):
    _need(depth<=5,'Boolean expression exceeds safe nesting depth')
    _need(type(node)==dict,'Boolean expression must be an object')
    if 'field' in node:
        return _check_filter(node,u)
    _need(set(node)=={'mode','items'},'Boolean expression needs mode and items')
    _need(node['mode'] in ('all','any','not'),'Unsupported Boolean operator')
    _need(isinstance(node['items'],list) and 1<=len(node['items'])<=12,'Boolean expression must have 1..12 conditions')
    if node['mode']=='not':_need(len(node['items'])==1,'NOT requires exactly one nested condition')
    for item in node['items']:_check_where(item,u,depth+1)
    return node


def _where_fields(node):
    if node is None:return []
    if 'field' in node:return [node['field']]
    return [fid for item in node['items'] for fid in _where_fields(item)]


def validate(raw,u,prior=None):
    """Validate every model-generated token. Unexpected semantics are rejected."""
    _need(type(raw)==dict,'Canonical plan must be a JSON object')
    allowed={'entity','select','filters','where','exists','aggregate','group_by','sort','limit','cohort','latest','terms'}
    _need(not (set(raw)-allowed),'Unknown canonical plan keys '+str(sorted(set(raw)-allowed)))
    entity=raw.get('entity');_need(entity in u.tables,'Entity must be an approved table ID')
    select=_safe_fields(raw.get('select',[]),u)
    filters=raw.get('filters',[])
    _need(isinstance(filters,list) and len(filters)<=15,'Maximum 15 filters')
    for f in filters:_check_filter(f,u)
    where=raw.get('where')
    if where is not None:_check_where(where,u)
    exists=raw.get('exists',[])
    _need(isinstance(exists,list) and len(exists)<=5,'Maximum five EXISTS scopes')
    for e in exists:
        _need(type(e)==dict and set(e)=={'table','filters','negate'},'EXISTS needs table, filters, negate')
        _need(e['table'] in u.tables and type(e['negate']) is bool,'Invalid EXISTS target or negate flag')
        _need(isinstance(e['filters'],list) and len(e['filters'])<=10,'Too many EXISTS filters')
        for f in e['filters']:
            _check_filter(f,u)
            _need(u.fields[f['field']]['table_id']==e['table'],'EXISTS predicates must belong to EXISTS source')
        u.path(entity,e['table'])
    group=_safe_fields(raw.get('group_by',[]),u)
    ag=raw.get('aggregate',[])
    _need(isinstance(ag,list) and len(ag)<=6,'Maximum six aggregates')
    aliases=set()
    for a in ag:
        expected={'fn','field','alias','percentile'} if a.get('fn')=='percentile' else {'fn','field','alias'}
        _need(type(a)==dict and set(a)==expected,'Aggregate needs fn, field, alias (and percentile for percentile fn)')
        if a.get('fn')=='percentile':
            _need(type(a['percentile']) in (int,float) and math.isfinite(a['percentile']) and 0<=a['percentile']<=100,
                  'Percentile must be a numeric value between 0 and 100')
        _need(a['fn'] in AGGS,'Unknown aggregate function')
        if a['fn'] in ('sum','avg','median','percentile'):
            _need(a['field'] in u.fields and _type_for_field(u.fields[a['field']])=='REAL',
                  'Numeric aggregate requires an approved numeric measure')
        _need(a['field'] is None and a['fn']=='count' or a['field'] in u.fields,'Aggregate field must be approved (COUNT may use null)')
        _need(isinstance(a['alias'],str) and re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,39}',a['alias']),'Unsafe aggregate alias')
        _need(a['alias'].lower() not in aliases,'Repeated aggregate alias')
        aliases.add(a['alias'].lower())
    _need(not (ag and select),'Use GROUP BY rather than raw SELECT with aggregate')
    _need(not (group and not ag),'GROUP BY requires an aggregate')
    sort=raw.get('sort',[])
    _need(isinstance(sort,list) and len(sort)<=3,'Maximum three sort keys')
    for s in sort:
        _need(type(s)==dict and set(s)=={'field','direction'},'Sort needs field, direction')
        _need(s['direction'] in SORTS,'Unsupported sort direction')
        _need(s['field'] in u.fields or s['field'].lower() in aliases,'Sort must be an approved field or output aggregate alias')
        if ag:_need(s['field'] in group or s['field'].lower() in aliases,'Grouped queries may sort only by group fields or aggregates')
    limit=raw.get('limit',MAX_RESULT)
    _need(type(limit)==int and 1<=limit<=MAX_RESULT,'Limit must be 1..200')
    use_cohort=raw.get('cohort',None)
    _need(use_cohort in (None,'active'),'Cohort reference must be active or null')
    if use_cohort:
        _need(prior is not None,'No complete verified cohort for the referenced conversation')
        _need(prior.get('cohort_kind') in ('phase7_approved','approved_entity'),
              'Cannot join an unverified or Classic-only cohort into a governed query')
        _need(prior.get('registry_signature')==u.revision,'Registry changed; rerun the original cohort query')
        _need(prior.get('source_field') in u.fields,'Cohort identity no longer approved')
        _need(isinstance(prior.get('values'),list) and len(prior['values'])<=MAX_RESULT,'Cohort is not complete and bounded')
        u.path(entity,u.fields[prior['source_field']]['table_id'])
    latest=raw.get('latest',None)
    if latest is not None:
        _need(type(latest)==dict and set(latest)=={'table','partition','date','tie'},'LATEST requires table, partition, date, tie')
        tid=latest['table'];_need(tid in u.tables,'LATEST table not approved')
        for k in ('partition','date','tie'):_need(latest[k] in u.fields and u.fields[latest[k]]['table_id']==tid,'LATEST fields must come from one approved source')
        _need(len({latest['date'],latest['tie'],latest['partition']})==3,'LATEST needs distinct keys')
    applied=raw.get('terms') or []
    _need(isinstance(applied,list) and len(applied)<=5,'At most five approved terms per question')
    approved_terms=u.terms() if hasattr(u,'terms') else {}
    for a in applied:
        _need(type(a)==dict and set(a)=={'term','negate'} and type(a['negate']) is bool,'Each term needs term and negate')
        _need(a['term'] in approved_terms,'Not an approved, usable business term: '+str(a['term']))
        if approved_terms[a['term']]['table_id']!=entity:u.path(entity,approved_terms[a['term']]['table_id'])
    _need(len({a['term'] for a in applied})==len(applied),'A term can be applied once')
    used=set(select+group+[f['field'] for f in filters]+_where_fields(where)+[a['field'] for a in ag if a['field']]+[s['field'] for s in sort if s['field'] in u.fields])
    tables={entity}|{u.fields[fid]['table_id'] for fid in used}
    for tid in tables:u.path(entity,tid)
    # No empty implicit SELECT *. Select all fields from base only on explicit no-op rows.
    if not (select or ag):select=u.by_table[entity][:10]
    return {'entity':entity,'select':select,'filters':filters,'where':where,'exists':exists,'aggregate':ag,'group_by':group,
            'sort':sort,'limit':limit,'cohort':use_cohort,'latest':latest,'terms':applied}



# Generic, data-derived categorical vocabulary, NOT a loan or complaint-specific rule.
# No underlying records, identifiers, measures, dates, or free text are returned.
_STATUS_NAME = re.compile(r'(?:^|_)(?:STATUS|STATE|ST)$',re.I)
_SAFE_CODE = re.compile(r'[A-Za-z][A-Za-z0-9_/-]{0,24}\Z')


# v17: approved CLASSIFICATION columns (segment, product, city ...) are label
# fields too. Their exact labels are discovered under the same bounds as STATUS
# and every equality filter on them must use an observed literal.
_LABEL_ROLES={'STATUS','CLASSIFICATION'}
# TEXT is deliberately not excluded: a TEXT-role column whose name ends in
# STATUS/STATE is still recognised by name, as in v15/v16 (backward compatible).
_EXCLUDED_LABEL_ROLES={'PRIMARY_KEY','PRIMARY_KEY_CANDIDATE','IDENTIFIER','FOREIGN_KEY',
                       'FOREIGN_KEY_CANDIDATE','MEASURE','DATE'}
MAX_LABELS=12


def _status_field(u, fid):
    info=u.fields[fid]
    if _type_for_field(info)!='TEXT':return False
    role=str(info.get('role','')).upper()
    if role in _EXCLUDED_LABEL_ROLES:
        return False
    return role in _LABEL_ROLES or bool(_STATUS_NAME.search(str(info.get('name',''))))


def _labels_enabled():
    # Status labels (observed status values shown to the planner) are on by default in the package.
    return os.environ.get('BINGGIO_UNIFIED_V15_STATUS_LABELS','true').lower()=='true'


def observed_status_report(project,u,source_override=None):
    """Bounded, safe categorical labels from authorized snapshots, plus diagnostics.

    Enabled only with the explicit POC opt-in. Never includes frequencies, rows,
    free text, IDs or customer attributes. Unreadable sources are reported, not
    silently skipped (v17). Returns
    {enabled, domains:{field_id:[labels]}, skipped_sources:[...], unbounded_fields:[...]}.
    """
    report={'enabled':_labels_enabled(),'domains':{},'skipped_sources':[],'unbounded_fields':[]}
    if not report['enabled']:
        return report
    by_table=collections.defaultdict(list)
    for tid, ids in u.by_table.items():
        for i,fid in enumerate(ids):
            if _status_field(u,fid):by_table[tid].append((i,fid))
    for tid,fields in by_table.items():
        try:records=source_override(tid) if source_override else _load_source(project,u,tid)
        except Blocked as e:
            # No inferred labels from unreadable snapshots, but make it visible.
            report['skipped_sources'].append({'table_id':tid,'reason':str(e)[:200],
                                              'fields':[fid for _,fid in fields]})
            log.warning('Label discovery skipped %s: %s',tid,str(e)[:200])
            continue
        for index,fid in fields:
            observed=set()
            problem=None
            for row in records:
                if index>=len(row):problem='source row shape mismatch';break
                value=row[index]
                if value is None:continue
                if not isinstance(value,str) or not _SAFE_CODE.fullmatch(value):
                    problem='values are not short codes (free text, numbers or spaces)';break
                observed.add(value)
                if len(observed)>MAX_LABELS:problem='more than '+str(MAX_LABELS)+' distinct values';break
            if problem:report['unbounded_fields'].append({'field_id':fid,'reason':problem})
            elif observed:report['domains'][fid]=sorted(observed)
    return report


def observed_status_domains(project,u,source_override=None):
    """Compatibility wrapper: {approved_field_id: [exact source literals]}."""
    return observed_status_report(project,u,source_override)['domains']


def _status_filter_leaves(plan):
    result=list(plan.get('filters') or [])
    def walk(node):
        if node is None:return
        if 'field' in node:result.append(node)
        else:
            for item in node['items']:walk(item)
    walk(plan.get('where'))
    for part in plan.get('exists') or []:result.extend(part.get('filters') or [])
    return result


def validate_status_literals(plan,u,domains):
    """Reject made-up categorical codes; preserve exact source casing.

    A label such as MISSED can be filtered as an observed literal. It is not a
    claim that late, overdue or delinquent mean MISSED. Those need a definition.
    """
    for condition in _status_filter_leaves(plan):
        fid=condition['field']
        if not _status_field(u,fid):continue
        if condition['op'] in ('is_null','not_null'):continue
        available=domains.get(fid)
        _need(bool(available),'No bounded, validated status vocabulary is available for '+fid+
              '; its values are not a short list of codes, its source could not be read, or label discovery is off. '
              'Approve a business definition for this condition instead')
        _need(condition['op'] in ('eq','ne','in','not_in'),
              'Status predicates require exact approved literals; no substring or ordering inference')
        lookup={x.casefold():x for x in available}
        def normalize(x):
            _need(isinstance(x,str) and x.casefold() in lookup,
                  'Status value is not an observed literal for '+fid+'; available codes: '+', '.join(available))
            return lookup[x.casefold()]
        if condition['op'] in ('in','not_in'):
            condition['value']=[normalize(x) for x in condition['value']]
        else:condition['value']=normalize(condition['value'])
    return plan


def _llm_key():
    # BINGGIO_LLM_API_KEY is the package name; the POC names and OPENAI_API_KEY are accepted.
    for k in ('BINGGIO_LLM_API_KEY','BINGGIO_PHASE10_LLM_API_KEY','OPENAI_API_KEY'):
        if os.environ.get(k):return os.environ[k]
    return ''


def _llm_enabled():
    return bool(_llm_key())



# v16.2: canonical structural bound, independent of business/domain vocabulary.
# This is not a secondary interpreter. It handles only the mechanical `limit`
# contract; all semantic fields continue through strict full-plan validation.
_EXPLICIT_ROW_LIMIT = re.compile(
    r"\b(?:top|bottom|first|limit(?:ed)?(?:\s+to)?)\s+(\d{1,6})\b", re.I
)

def normalize_model_limit(raw, question):
    """Resolve canonical row-limit typing without changing query predicates.

    An explicit top/bottom/first/limit N is authoritative and never truncated.
    Otherwise a missing, null, noninteger or out-of-range model-generated limit
    represents no user-requested row cap: use the executor's existing 200-row
    safety ceiling, which *rejects* excess unbounded results rather than
    saving a silently incomplete cohort.
    """
    if type(raw) is not dict or raw.get('unsupported'):
        return raw
    result=dict(raw)
    found=_EXPLICIT_ROW_LIMIT.search(question)
    if found:
        n=int(found.group(1))
        # Ranking by percentile is NOT a requested row count.
        tail=question[found.end():].lstrip().lower()
        if not (found.group(0).lower().lstrip().startswith(('top ', 'bottom ')) and
                re.match(r'(?:%|percent(?:ile)?s?\b)',tail)):
            if not 1<=n<=MAX_RESULT:
                raise InvalidCanonicalPlan(
                    'Requested row limit '+str(n)+' is outside the supported range 1..'+str(MAX_RESULT))
            result['limit']=n
            return result
    requested=result.get('limit')
    if type(requested) is not int or not 1<=requested<=MAX_RESULT:
        result['limit']=MAX_RESULT
    return result


def canonical_response_schema(u):
    """Strict typed LLM response. Syntax is guaranteed at the model boundary;
    business semantics are *still* checked by validate() and status validation.

    v17: FIXED-SIZE schema. Table and field slots are plain strings, so the
    schema does not grow with the registry and never approaches the API limit
    on enum values (the v16.3 schema embedded every ID several times and hit
    1,125 values). Approved IDs are given to the model in the catalog, and
    validate() remains the authority that rejects any unapproved ID.
    A typed predicate requires field, op, AND value, so malformed filters are
    rejected by the model API rather than turned into governance tickets.
    """
    ident={'type':'string'}
    scalar={'anyOf':[{'type':'string'},{'type':'number'},{'type':'boolean'},{'type':'null'}]}
    predicate={'type':'object','properties':{
        'field':ident,
        'op':{'type':'string','enum':sorted(OPS)},
        'value':{'anyOf':[{'$ref':'#/$defs/scalar'},
                          {'type':'array','items':{'$ref':'#/$defs/scalar'}}]},
    },'required':['field','op','value'],'additionalProperties':False}
    boolean={'anyOf':[{'$ref':'#/$defs/predicate'},{'$ref':'#/$defs/boolean_group'}]}
    group={'type':'object','properties':{
        'mode':{'type':'string','enum':['all','any','not']},
        'items':{'type':'array','items':{'$ref':'#/$defs/boolean'}},
    },'required':['mode','items'],'additionalProperties':False}
    exists={'type':'object','properties':{
        'table':ident,
        'filters':{'type':'array','items':{'$ref':'#/$defs/predicate'}},
        'negate':{'type':'boolean'},
    },'required':['table','filters','negate'],'additionalProperties':False}
    aggregate={'type':'object','properties':{
        'fn':{'type':'string','enum':sorted(AGGS)},
        'field':{'anyOf':[ident,{'type':'null'}]},
        'alias':{'type':'string'},
        'percentile':{'type':['number','null']},
    },'required':['fn','field','alias','percentile'],'additionalProperties':False}
    sort={'type':'object','properties':{
        'field':{'type':'string'},'direction':{'type':'string','enum':['asc','desc']},
    },'required':['field','direction'],'additionalProperties':False}
    latest={'type':'object','properties':{
        'table':ident,'partition':ident,'date':ident,'tie':ident,
    },'required':['table','partition','date','tie'],'additionalProperties':False}
    plan={'type':'object','properties':{
        'entity':ident,
        'select':{'type':'array','items':ident},
        'filters':{'type':'array','items':{'$ref':'#/$defs/predicate'}},
        'where':{'anyOf':[{'$ref':'#/$defs/boolean'},{'type':'null'}]},
        'exists':{'type':'array','items':{'$ref':'#/$defs/exists'}},
        'aggregate':{'type':'array','items':{'$ref':'#/$defs/aggregate'}},
        'group_by':{'type':'array','items':ident},
        'sort':{'type':'array','items':{'$ref':'#/$defs/sort'}},
        'limit':{'type':'integer'},
        'cohort':{'type':['string','null'],'enum':['active',None]},
        'latest':{'anyOf':[{'$ref':'#/$defs/latest'},{'type':'null'}]},
        'terms':{'type':'array','items':{'$ref':'#/$defs/term'}},
    },'required':['entity','select','filters','where','exists','aggregate',
                 'group_by','sort','limit','cohort','latest','terms'],
        'additionalProperties':False}
    return {'type':'object','properties':{
        'status':{'type':'string','enum':['plan','unsupported']},
        'reason':{'type':['string','null']},
        'plan':{'anyOf':[{'$ref':'#/$defs/plan'},{'type':'null'}]},
    },'required':['status','reason','plan'],'additionalProperties':False,
      '$defs':{'scalar':scalar,'predicate':predicate,'boolean_group':group,
               'boolean':boolean,'exists':exists,'aggregate':aggregate,
               'sort':sort,'latest':latest,'plan':plan,
               'term':{'type':'object','properties':{'term':ident,'negate':{'type':'boolean'}},
                       'required':['term','negate'],'additionalProperties':False}}}


def _unwrap_model_result(result):
    """No alternate interpreter: unwrap one strictly typed canonical envelope."""
    if type(result)!=dict:raise InvalidCanonicalPlan('Canonical response is not an object')
    if set(result)=={'status','reason','plan'}:
        if result['status']=='unsupported':
            raise Blocked('Question cannot be fully represented by the approved semantic universe: '
                          +str(result.get('reason') or 'unspecified')[:170])
        if result['status']!='plan' or type(result.get('plan'))!=dict:
            raise InvalidCanonicalPlan('Canonical response declares plan but has no plan object')
        raw=dict(result['plan'])
        # The API schema requires percentile on every aggregate; a non-percentile
        # function must not pass that optional field to the audited AST compiler.
        for agg in raw.get('aggregate',[]):
            if isinstance(agg,dict) and agg.get('fn')!='percentile' and agg.get('percentile') is None:
                agg.pop('percentile')
        return raw
    # Preserve direct canonical-AST usage from existing API-level unit fixtures.
    # The live model API *always* requests the typed envelope above.
    if result.get('unsupported'):
        raise Blocked('Question cannot be fully represented by the approved semantic universe: '
                      +str(result.get('reason','unspecified'))[:170])
    return result



def _api_safe_canonical_schema(u):
    """Kept for compatibility with v16.3 local patches; the v17 schema is already
    fixed-size, so nothing needs to be stripped."""
    return canonical_response_schema(u)


def schema_enum_count(schema):
    """Total enum values in a schema (the model API allows at most 1,000)."""
    if isinstance(schema,dict):
        own=len(schema['enum']) if isinstance(schema.get('enum'),list) else 0
        return own+sum(schema_enum_count(v) for v in schema.values())
    if isinstance(schema,list):return sum(schema_enum_count(v) for v in schema)
    return 0


_SECRET=re.compile(r'(sk-[A-Za-z0-9_-]{6,}|Bearer\s+[A-Za-z0-9._-]+)')


def _redact(text):
    return _SECRET.sub('[REDACTED]',str(text))


def _api_error_summary(e):
    """Short, secret-free description of a model API failure (v17).

    The upstream error message (for example a schema rejection) is logged and
    included in the user-facing reason, so a failure is diagnosable without
    temporary print statements. Keys and bearer tokens are always redacted.
    """
    summary=str(e)[:140]
    if getattr(e,'code',None) is not None and hasattr(e,'read'):
        try:body=e.read(4000).decode('utf-8',errors='replace')
        except Exception:body=''
        message=''
        try:message=str((json.loads(body).get('error') or {}).get('message') or '')
        except (ValueError,AttributeError):message=body[:300]
        log.warning('Model API error HTTP %s: %s',e.code,_redact(body)[:1500])
        if message:summary+=' - '+_redact(message)[:220]
    else:
        log.warning('Model API call failed: %s',_redact(summary))
    return _redact(summary)


# ---------------------------------------------------------------- v17.1 guards
# Refusal-only rules. They can stop a question or reject a plan; they can never
# produce or change an answer. Each exists because the live question bank
# showed the model doing exactly this despite the prompt telling it not to.

_DEFINITION_TERMS=re.compile(r"\b(overdue|past[- ]due|delinquen\w*|arrears?|default(?:s|ed|er|ers)?|non[- ]performing|npas?|"
                             r"churn\w*|dormant|late payments?|paid late|bad debts?|write[- ]offs?)\b",re.I)
_UNSUPPORTED_CLASSES=[
    ('ratio','ratios, shares and percentages of two counts or totals',
     re.compile(r"\b(share|percentage|percent|proportion|ratio|fraction)\b|\d\s*%",re.I)),
    ('relative_time','time windows relative to today (last 30 days, this month)',
     re.compile(r"\b(last|past|recent|coming|next)\s+(\d+\s+)?(days?|weeks?|months?|quarters?|years?)\b|"
                r"\b(today|yesterday|tomorrow|recently|this (week|month|quarter|year)|year[- ]to[- ]date|ytd|month[- ]to[- ]date|mtd)\b",re.I)),
    ('duration','durations between dates (open for more than 15 days)',
     re.compile(r"\b(for|than|over|under|within)\s+(more|less|longer|shorter)?\s*(than\s+)?\d+\s*(days?|weeks?|months?|years?)\b|"
                r"\b\d+\s*(days?|weeks?|months?|years?)\s+(old|ago|since)\b|\bdays? since\b|\bage of\b",re.I)),
    ('band','deciles, quartiles and percentage bands',
     re.compile(r"\b(deciles?|quartiles?|quintiles?|terciles?)\b|\b(top|bottom)\s+\d+\s*(%|percent)",re.I)),
    ('change_over_time','change since a previous value or period',
     re.compile(r"\b(increased|decreased|grew|grown|dropped|rose|risen|fell|fallen|changed|worsened|improved)\b.*\b(since|from|compared|versus|vs\.?|than)\b",re.I)),
    ('aggregate_filter','conditions on a count or total per item (more than one complaint)',
     re.compile(r"\b(more than|fewer than|less than|over)\s+(one|two|three|four|five|\d+)\s+(?!days?\b|weeks?\b|months?\b|years?\b)[a-z]{3,}\b|"
                r"\bat least\s+(two|three|four|five|[2-9]|\d{2,})\s+[a-z]+s\b|\b(multiple|repeated|repeat)\s+[a-z]+s\b",re.I)),
]
_GROUPING_CUE=re.compile(r"\b(by|per|each|every|breakdown|broken down|split|across|grouped?|wise)\b",re.I)
_DATE_LITERAL=re.compile(r"^(\d{4})-\d{2}-\d{2}")


_MEASURE_AGGS={'SUM','AVG','MIN','MAX'}
_COUNT_AGGS={'COUNT','COUNT_DISTINCT'}
_UNAPPROVED_ROLES={'','UNKNOWN'}


def business_definitions(u):
    """v18.0: approved business definitions (one column, one aggregation) that the
    model may use, each checked against the approved column roles.

    Returns (usable, excluded). A definition is excluded, with the reason, when it
    sums/averages a column that is not an approved MEASURE, counts a column with no
    approved role, has no name, or uses an unsupported aggregation. Technical
    standard analytics are not business definitions and are skipped."""
    usable,excluded=[],[]
    items=u.ctx.get('metrics') or {}
    for key,m in (items.items() if isinstance(items,dict) else enumerate(items)):
        if not isinstance(m,dict) or m.get('technical_only'):continue
        name=str(m.get('name') or '').strip()
        agg=str(m.get('aggregation') or '').upper()
        fid=m.get('field');f=u.fields.get(fid) if isinstance(fid,str) else None
        role=str((f or {}).get('role','')).upper()
        if not name:reason='it has no business name'
        elif f is None:reason='its column is not in the approved universe'
        elif agg in _MEASURE_AGGS and role!='MEASURE':
            reason=agg+' needs an approved MEASURE column, but '+str(f.get('name'))+' is '+(role if role not in _UNAPPROVED_ROLES else 'not approved')
        elif agg in _COUNT_AGGS and role in _UNAPPROVED_ROLES:
            reason='its column '+str(f.get('name'))+' has no approved role'
        elif agg not in _MEASURE_AGGS|_COUNT_AGGS:reason='aggregation '+(agg or 'missing')+' is not supported'
        else:reason=None
        entry={'id':str(m.get('id',key)),'name':name,'domain':str(m.get('domain') or ''),'aggregation':agg,
               'field':fid,'table':(f or {}).get('table_id',m.get('table_id'))}
        if reason:entry['reason']=reason;excluded.append(entry)
        else:usable.append(entry)
    return usable,excluded


def _approved_terms(u):
    """Names that lift the undefined-term guard (v18.0).

    Only governed business terms do (registry kind added in v18.3, exposed as
    ctx['governed_terms']). A one-column aggregation cannot express a condition
    such as overdue, so a metric's name no longer lifts the guard: before v18.0 a
    metric called 'overdue' would have removed the refusal while the model still
    had no meaning for it."""
    names=[]
    # v18.3: the name of an approved business concept is defined too ("delinquency"
    # is the approved delinquency snapshot table, not an undefined condition)
    for c in (u.concepts().values() if hasattr(u,'concepts') else []):
        names+=[str(n).lower() for n in [c['name']]+list(c.get('aliases') or [])]
    items=u.terms() if hasattr(u,'terms') else (u.ctx.get('governed_terms') or {})  # usable terms only
    for v in (items.values() if isinstance(items,dict) else items):
        if not isinstance(v,dict) or str(v.get('status','approved')).lower()!='approved':continue
        for n in [v.get('name')]+list(v.get('aliases') or []):
            if n:names.append(str(n).lower())
    return names


_AGG_FN={'SUM':'sum','AVG':'avg','MIN':'min','MAX':'max','COUNT':'count','COUNT_DISTINCT':'count_distinct'}


def _definition_mentions(question,u):
    q=' '+re.sub(r'[^a-z0-9]+',' ',str(question).lower())+' '
    out=[]
    for d in business_definitions(u)[0]:
        phrase=re.sub(r'[^a-z0-9]+',' ',d['name'].lower()).strip()
        if len(phrase)>=4 and ' '+phrase+' ' in q:out.append(d)
    return out


# ---------------------------------------------------------------- v18.1 concept resolution

def _cstem(token):
    t=token.lower()
    if len(t)>4 and t.endswith('ies'):t=t[:-3]+'y'
    elif len(t)>4 and t.endswith(('ches','shes','sses','xes','zes')):t=t[:-2]
    elif len(t)>3 and t.endswith('s') and not t.endswith('ss'):t=t[:-1]
    if len(t)>6 and t.endswith('ing'):t=t[:-3]
    elif len(t)>5 and t.endswith('ed'):t=t[:-2]
    return t


def _cstems(text):
    return [_cstem(w) for w in re.findall(r'[a-z0-9]+',str(text).lower())]


def _same(a,b):
    # 'complain' ~ 'complaint', 'escalat' ~ 'escalation'; short words must match exactly.
    return a==b or (min(len(a),len(b))>=5 and (a.startswith(b) or b.startswith(a)))


def _spans(words,term):
    n=len(term)
    return [(i,i+n) for i in range(len(words)-n+1) if all(_same(words[i+k],term[k]) for k in range(n))]


class Resolution(dict):
    """focus (set of table IDs or None), concepts, attributes, named_tables."""


def _term_index(u):
    """(term stems, kind, concept id, field or None) for every approved name and alias."""
    out=[]
    for cid,c in sorted(u.concepts().items()):
        for t in dict.fromkeys([c['name']]+list(c.get('aliases') or [])):
            st=tuple(_cstems(t))
            if st:out.append((st,'concept',cid,None))
        for a in c.get('attributes') or []:
            if a.get('field') not in u.fields:continue
            for t in dict.fromkeys([a['name']]+list(a.get('aliases') or [])):
                st=tuple(_cstems(t))
                if st:out.append((st,'attribute',cid,a['field']))
    return out


def _concept_label(u,cid):
    c=u.concepts()[cid];return '%s (%s)'%(c['name'],u.tables[c['primary_table']].get('table',c['primary_table']))


def resolve_concepts(question,u,prior=None):
    """Which approved concepts, attributes and tables a question is about (v18.1).

    Deterministic, from steward-approved names and aliases only; no model call.
    Longest phrases win ("kyc review" before "kyc"). A phrase that belongs to several
    concepts must be settled by another word in the question; otherwise the question
    is refused with the choices. An attribute word shared by many concepts ("status")
    only counts for a concept the question already names. Secondary sources are used
    only when the question names their table. Returns focus=None when no concept is
    named: the whole catalog is used, minus secondary sources (as before v18.1)."""
    concepts=u.concepts()
    result=Resolution(focus=None,concepts=[],attributes=[],named_tables=[])
    words=_cstems(question)
    lower=str(question).lower()
    named=sorted(t for t in u.tables if re.search(r'(?<![a-z0-9_])'+re.escape(str(u.tables[t].get('table','')).lower())+r'(?![a-z0-9_])',lower)
                 and '_' in str(u.tables[t].get('table','')))
    if not concepts and not named:return result
    hits=collections.defaultdict(set)  # span -> {(kind, concept, field)}
    for term,kind,cid,fid in _term_index(u):
        for sp in _spans(words,term):hits[sp].add((kind,cid,fid))
    # Concept names and aliases first (longest first), then attribute words on what
    # is left: "accounts with account status OPEN" is about accounts, not loans.
    def concept_pass(skip):
        used=set();picked=[]
        spans=[sp for sp in hits if sp not in skip and any(k=='concept' for k,_,_ in hits[sp])]
        for sp in sorted(spans,key=lambda x:(-(x[1]-x[0]),x[0])):
            if used & set(range(*sp)):continue
            used|=set(range(*sp));picked.append(sp)
        return picked
    picked=concept_pass(set())
    owners_of=lambda sp:{c for k,c,_ in hits[sp] if k=='concept'}
    # ...except a one-word concept inside an attribute phrase of a concept named
    # elsewhere: in "loan accounts by account status", "account status" is the loan's.
    named_c={c for sp in picked if len(owners_of(sp))==1 for c in owners_of(sp)}
    drop={sp for sp in picked if sp[1]-sp[0]==1 and any(
        k=='attribute' and c in named_c-owners_of(sp) and a[0]<=sp[0] and sp[1]<=a[1] and a[1]-a[0]>1
        for a in hits for k,c,_ in hits[a])}
    if drop:picked=concept_pass(drop)
    used=set();matched=set();ambiguous=[];attr_spans=[]
    for sp in picked:
        used|=set(range(*sp));cons=owners_of(sp)
        if len(cons)==1:matched|=cons
        else:ambiguous.append((sp,cons))
    for sp in sorted([sp for sp in hits if any(k=='attribute' for k,_,_ in hits[sp])],key=lambda x:(-(x[1]-x[0]),x[0])):
        if used & set(range(*sp)):continue
        used|=set(range(*sp));attr_spans.append(sp)
    for sp,cons in ambiguous:
        if not cons & matched:
            phrase=' '.join(re.findall(r'[a-z0-9]+',lower)[sp[0]:sp[1]])
            raise Blocked('"%s" can mean %s. Say which one you mean; Binggio will not choose between approved concepts.'
                          %(phrase,' or '.join(sorted(_concept_label(u,c) for c in cons))))
    attributes=[]
    for sp in attr_spans:
        found=sorted({(c,f) for k,c,f in hits[sp] if k=='attribute'})
        mine=[(c,f) for c,f in found if c in matched]
        if mine:attributes.extend(mine)
        elif len({c for c,_ in found})==1 and (not matched or sp[1]-sp[0]>1):
            # a specific attribute phrase ("amount paid") brings its concept; a single
            # word ("score") does so only when the question names no concept
            matched.add(found[0][0]);attributes.extend(found)
        # else: an attribute word of a concept the question does not name ("status",
        # or "score" when the question is about fraud signals): ignored
    focus={concepts[c]['primary_table'] for c in matched}|{u.fields[f]['table_id'] for _,f in attributes}|set(named)
    mapped={c['primary_table'] for c in concepts.values()}|u.secondary_tables()
    for tid,t in u.tables.items():  # tables not yet assigned to any concept: matched by their own name
        tw=_cstems(str(t.get('table','')).replace('_',' '))
        if tid not in mapped and tw and _spans(words,tuple(tw)):focus.add(tid)
    if not focus and prior:
        # "How many of them are there?": the remembered result's own table
        focus={t for t in (prior.get('entity'),u.fields.get(prior.get('source_field'),{}).get('table_id')) if t in u.tables}
    if not focus:return result
    if prior and prior.get('source_field') in u.fields:focus.add(u.fields[prior['source_field']]['table_id'])
    if prior and prior.get('entity') in u.tables:focus.add(prior['entity'])
    result.update(focus=focus,concepts=sorted(matched),attributes=sorted(set(attributes)),named_tables=named)
    return result


def concept_context(u,res):
    """Concept summary for the prompt: only the concepts the question resolved to."""
    out=[]
    for cid in res.get('concepts') or []:
        c=u.concepts()[cid]
        out.append({'concept':c['name'],'primary_table':c['primary_table'],'key_field':c.get('key_field'),
                    'attributes':[{'name':a['name'],'field':a['field']} for a in c.get('attributes') or [] if a.get('field') in u.fields]})
    return out


# ---------------------------------------------------------------- v18.3 governed terms

def resolve_terms(question,u,concept_spans=None):
    """Approved terms named in the question (deterministic; names and aliases only).

    Longest phrase first. A phrase shared by two terms ("repayment plan") is refused
    with the choices. Returns a list of term IDs."""
    terms=u.terms()
    if not terms:return []
    words=_cstems(question);lower=str(question).lower()
    hits=collections.defaultdict(set)
    for tid,t in sorted(terms.items()):
        for name in dict.fromkeys([t['name']]+list(t.get('aliases') or [])):
            st=tuple(_cstems(name))
            if st:
                for sp in _spans(words,st):hits[sp].add(tid)
    used=set();found=[]
    for sp in sorted(hits,key=lambda x:(-(x[1]-x[0]),x[0])):
        if used & set(range(*sp)):continue
        used|=set(range(*sp))
        owners=hits[sp]
        if len(owners)>1:
            phrase=' '.join(re.findall(r'[a-z0-9]+',lower)[sp[0]:sp[1]])
            raise Blocked('"%s" can mean %s. Say which one you mean; Binggio will not choose between approved terms.'
                          %(phrase,' or '.join(sorted(terms[t]['name'] for t in owners))))
        found.extend(owners)
    return sorted(set(found))


def term_context(u,term_ids):
    out=[]
    for tid in term_ids:
        t=u.terms()[tid]
        out.append({'id':tid,'term':t['name'],'meaning':t['description'],'applies_to_table':t['table_id'],
                    'as_of':u.as_of_date() if t['needs_as_of'] else None,
                    'how_to_apply':'add {"term":"%s","negate":false} to plan.terms; the engine applies the exact definition and as-of date; add no filters, statuses or dates for it'%tid})
    return out


def _term_predicates(u,t):
    out=[]
    as_of=datetime.date.fromisoformat(u.as_of_date()) if t['needs_as_of'] else None
    for c in t['conditions']:
        p={'field':c['field'],'op':c['op'],'_term':t['name']}
        if c['op'] in ('is_null','not_null'):p['value']=None
        elif c.get('value_field'):p['value']={'field':c['value_field']}
        elif 'as_of_offset_days' in c:p['value']=(as_of+datetime.timedelta(days=c['as_of_offset_days'])).isoformat()
        else:p['value']=c['value']
        out.append(p)
    return out


def expand_terms(plan,u):
    """The plan with every applied term replaced by its approved conditions (v18.3).

    On the term's own table the conditions become filters (negated: NOT(all of them));
    on a related table they become EXISTS / NOT EXISTS, optionally on the latest row per
    entity on or before the approved as-of date. Model-written filters are never mixed
    with column comparisons or as-of dates: only approved terms produce those."""
    applied=plan.get('terms') or []
    if not applied:return plan
    out=dict(plan);out['filters']=list(plan['filters']);out['exists']=list(plan['exists'])
    for a in applied:
        t=u.terms()[a['term']];preds=_term_predicates(u,t)
        if t['table_id']==plan['entity'] and not t['latest']:
            if a['negate']:
                node={'mode':'not','items':[{'mode':'all','items':preds}]}
                out['where']=node if out.get('where') is None else {'mode':'all','items':[out['where'],node]}
            else:out['filters']+=preds
        else:
            _need(t['table_id']!=plan['entity'],'"%s" uses the latest row per entity of %s; ask about the related entity instead'%(t['name'],t['table_id']))
            e={'table':t['table_id'],'filters':preds,'negate':bool(a['negate'])}
            if t['latest']:
                e['_latest']={'partition':t['latest']['partition'],'date':t['latest']['date'],'as_of':u.as_of_date(),'term':t['name']}
            out['exists'].append(e)
    return out


def question_guard(question,u):
    """Refuse, before any model call, questions that need an undefined business
    term or an unsupported question class (v17.1)."""
    q=str(question)
    m=_DEFINITION_TERMS.search(q)
    if m and not any(m.group(0).lower() in name for name in _approved_terms(u)):
        raise Blocked('"'+m.group(0)+'" has no approved business definition. Binggio will not equate it with a status '
                      'code, a date rule or another column. A data steward can approve a definition for it.')
    for cls,label,pattern in _UNSUPPORTED_CLASSES:
        if pattern.search(q):
            raise UnsupportedClass('This question needs '+label+', which Binggio cannot calculate yet. '
                                   'It was not approximated. Rephrase it without that part, or ask for the underlying list or count.')


def _plan_leaves(plan):
    return _status_filter_leaves(plan)


def _looks_like_reference(value,u):
    if not isinstance(value,str):return False
    v=value.strip()
    return v in u.fields or v in u.tables or bool(re.fullmatch(r"[\w.-]+::[\w.-]+(::[\w.-]+)?",v))


def check_plan_alignment(plan,question,u,prior=None,allowed=None,mentioned=None):
    """Reject validated plans that do not mean what the question says (v17.1).
    Raised as Blocked inside interpret, so the model gets one correction."""
    if mentioned is not None:
        # v18.3: an approved term named in the question is applied through plan.terms
        # (its exact definition), and only terms the question names may be applied.
        used_terms={a['term'] for a in plan.get('terms') or []}
        names=lambda ids:', '.join(u.terms()[t]['name'] for t in sorted(ids) if t in u.terms())
        if set(mentioned)-used_terms:
            raise Blocked('The question uses the approved term(s) '+names(set(mentioned)-used_terms)+'. Apply each through plan.terms '
                          '(term id, negate true for not/no/without); never express a term with your own filters')
        if used_terms-set(mentioned):
            raise Blocked('plan.terms contains '+names(used_terms-set(mentioned))+', which the question does not mention. Remove it')
        # v18.3.2: an EXISTS without conditions on the table an applied term already
        # covers changes its meaning ("not delinquent" became "no repayments at all").
        term_tables={u.terms()[t]['table_id']:u.terms()[t]['name'] for t in used_terms if t in u.terms()}
        negated_tables={u.terms()[a['term']]['table_id'] for a in plan.get('terms') or [] if a['negate'] and a['term'] in u.terms()}
        for e in plan.get('exists') or []:
            # a plain "has rows" EXISTS next to a positive term is redundant and harmless;
            # negated, or next to a negated term, it changes the meaning
            if e['table'] in term_tables and e['table']!=plan['entity'] and not e['filters'] and (e['negate'] or e['table'] in negated_tables):
                raise Blocked('The plan adds an EXISTS on '+e['table']+' without conditions, but the approved term "'+term_tables[e['table']]+
                              '" already defines the condition on that table. Remove that EXISTS; negate the term itself if the question says not')
    if allowed is not None:
        # v18.1: only the tables the question resolved to (or, without concepts, all
        # tables except secondary sources the question did not name).
        used={plan['entity']}|{e['table'] for e in plan.get('exists') or []}|{u.fields[f]['table_id'] for f in _plan_fields(plan) if f in u.fields}
        if plan.get('latest'):used.add(plan['latest']['table'])
        outside=sorted(used-set(allowed)-({u.fields[prior['source_field']]['table_id']} if prior and prior.get('source_field') in u.fields else set()))
        if outside:
            raise Blocked('The plan uses '+', '.join(outside)+', which is not the approved source for what the question asks. '
                          'Use only tables in the catalog given, or return unsupported')
    for f in _plan_leaves(plan):
        values=f['value'] if isinstance(f['value'],list) else [f['value']]
        if any(_looks_like_reference(v,u) for v in values):
            raise Blocked('A filter on '+f['field']+' compares it with the NAME of another field. Never add join conditions: '
                          'relationships between tables are applied automatically from approved joins. Remove that filter')
        for v in values:
            m=_DATE_LITERAL.match(str(v)) if isinstance(v,str) else None
            if m and not re.search(r"\b"+m.group(1)+r"\b",str(question)):
                raise Blocked('The plan uses the date '+str(v)+', but the question does not state that date or year. '
                              'Do not derive dates from today. If the question depends on such a date, return unsupported')
    seen=set()
    for a in plan.get('aggregate') or []:
        sig=(a['fn'],a['field'],a.get('percentile'))
        if sig in seen:
            raise Blocked('Two aggregates compute exactly the same value ('+a['fn']+' of '+str(a['field'])+') under different names. '
                          'They cannot represent different quantities; ratios and comparisons of counts are not supported')
        seen.add(sig)
    if any(a['fn'] in ('sum','avg','count') for a in plan.get('aggregate') or []):
        # Same rule the executor enforces, checked here so the model can correct it (v17.2).
        used=list(plan.get('group_by') or [])+[a['field'] for a in plan['aggregate'] if a['field']]
        used+=[f['field'] for f in plan.get('filters') or []]+_where_fields(plan.get('where'))
        used+=[s['field'] for s in plan.get('sort') or [] if s['field'] in u.fields]
        tables={u.fields[f]['table_id'] for f in used if f in u.fields}
        strict=set()
        if plan.get('cohort') and prior and prior.get('source_field') in u.fields:
            # A follow-up total or count ("how many of them") is about the remembered
            # entity: its table must be the entity, even through a many-to-one join.
            strict.add(u.fields[prior['source_field']]['table_id'])
        if plan.get('latest'):strict.add(plan['latest']['table'])
        if (strict-{plan['entity']}) or any(not u.preserves_rows(plan['entity'],t) for t in tables-{plan['entity']}):
            raise Blocked('A sum, average or count over fields from joined tables could double count rows. Use the table that '
                          'holds the measure or the counted rows as entity, group by its own identifier, and express conditions '
                          'on other tables with EXISTS (or count_distinct for counts)')
    for d in _definition_mentions(question,u):
        # v18.0: a question that names an approved business definition must use it
        # exactly (same aggregation, same column), so it is answered the same way every time.
        fn=_AGG_FN[d['aggregation']]
        if not any(a['fn']==fn and (a['field']==d['field'] or (fn=='count' and a['field'] is None
                   and plan.get('entity')==d['table'])) for a in plan.get('aggregate') or []):
            raise Blocked('The question names the approved business definition "'+d['name']+'", which is '+fn+' of '+str(d['field'])+
                          '. Use exactly that aggregate (entity '+str(d['table'])+'), or return unsupported if the question needs something else')
    if plan.get('group_by') and not _GROUPING_CUE.search(str(question)):
        raise Blocked('The plan groups by '+', '.join(plan['group_by'])+' but the question does not ask for a breakdown. '
                      'Use group_by=[] for a single overall value')
    return plan


def _plan_fields(plan):
    out=list(plan.get('select') or [])+list(plan.get('group_by') or [])+[f['field'] for f in plan.get('filters') or []]
    out+=_where_fields(plan.get('where'))+[a['field'] for a in plan.get('aggregate') or [] if a.get('field')]
    out+=[s['field'] for s in plan.get('sort') or []]
    for e in plan.get('exists') or []:out+=[f['field'] for f in e.get('filters') or []]
    if plan.get('latest'):out+=[x for x in (plan['latest'].get('partition'),plan['latest'].get('date'),plan['latest'].get('tie')) if isinstance(x,str)]
    return out


_LITERAL_ROLES={'STATUS','CLASSIFICATION'}


def _model_predicates(plan):
    out=list(plan.get('filters') or [])
    def walk(node):
        if node is None:return
        if 'field' in node:out.append(node)
        else:
            for item in node['items']:walk(item)
    walk(plan.get('where'))
    for e in plan.get('exists') or []:out.extend(e.get('filters') or [])
    return out


def check_stated_literals(plan,question,u):
    """v18.3.1: a text value compared in the plan (status, category, identifier) must be stated in the question.

    The model may not add a condition the user did not ask for, such as
    payment_status = MISSED for "delinquent" (which the approved term already
    defines), or equate a synonym with a code ("unresolved" -> OPEN). Words are
    compared after the same stemming as concept resolution."""
    words=_cstems(question)
    for f in _model_predicates(plan):
        info=u.fields.get(f['field']) or {}
        if f['op'] not in ('eq','ne','in','not_in'):continue  # any text value: status, category, free text or identifier
        for v in (f['value'] if isinstance(f['value'],list) else [f['value']]):
            if not isinstance(v,str) or not v.strip():continue
            lit=tuple(_cstems(v.replace('_',' ')))
            if lit and not _spans(words,lit):
                raise Blocked('The plan adds the condition '+str(info.get('name',f['field']))+' = '+v+', but the question does not say "'+v+'". '
                              'Do not add conditions the question does not state; approved terms in plan.terms already carry their full definition')


def normalize_model_projection(raw):
    """With an aggregate, drop selected fields that only repeat a group_by field
    or an aggregated field (v17.1). Anything else is left for validate() to reject."""
    if type(raw)!=dict or not raw.get('aggregate') or not isinstance(raw.get('select'),list):return raw
    covered=set(raw.get('group_by') or [])|{a.get('field') for a in raw['aggregate'] if isinstance(a,dict)}
    out=dict(raw);out['select']=[f for f in raw['select'] if f not in covered]
    return out


def interpret(question,u,prior=None,project=None):
    """Use a metadata-only structured interpreter; no identifiers/data sent externally."""
    _need(type(question)==str and 0<len(question.strip())<=1200,'Question must be 1..1200 characters')
    question_guard(question,u)
    res=resolve_concepts(question,u,prior)  # may refuse an ambiguous concept before any model call (v18.1)
    mentioned=resolve_terms(question,u)   # v18.3; may refuse an ambiguous term before any model call
    term_tables={u.terms()[t]['table_id'] for t in mentioned}
    if mentioned and res['focus'] is not None:res['focus']|=term_tables  # no concept named: whole catalog stays (v18.3.1)
    allowed=res['focus'] if res['focus'] is not None else (set(u.tables)-u.secondary_tables())|set(res['named_tables'])|term_tables
    if not _llm_enabled():raise Unrecognized('Asking questions needs a model key: set BINGGIO_LLM_API_KEY (or OPENAI_API_KEY)')
    model=os.environ.get('BINGGIO_LLM_MODEL',os.environ.get('BINGGIO_UNIFIED_V14_MODEL',os.environ.get('BINGGIO_PHASE10_LLM_MODEL','gpt-4.1-mini')))
    _need(model in MODEL_ALLOWLIST,'LLM model not in approved allowlist')
    domains=observed_status_domains(project,u)
    catalog=u.catalog(focus=res['focus'],observed_status_domains=domains,concepts=concept_context(u,res) or None,
                      terms=term_context(u,mentioned) or None)
    instruct=(
      'You transform an analytical question into a complete JSON canonical plan. No SQL. '
      'Return the STRICT typed response envelope {status:plan|unsupported,reason:null|string,plan:object|null}. For supported questions put the complete canonical AST into plan with keys entity, select, filters, where, exists, aggregate, group_by, sort, limit, cohort, latest. '
      'Use exact approved table IDs and field IDs from catalog, never guess. '
      'Filters {field,op,value}, where op is eq/ne/gt/gte/lt/lte/in/not_in/like/between/is_null/not_null. '
      'Complex WHERE may use nested {mode:all|any|not,items:[filters or nested where]} preserving AND/OR/NOT. '
      'Existence checks {table, filters, negate:false|true}; all EXISTS filters refer to the EXISTS table. '
      'Aggregates {fn:count|count_distinct|sum|avg|min|max|median|percentile,field:<field id or null for count>,alias:<safe name>}, and add percentile:0..100 for percentile function. '
      'Sort keys {field:<field id or aggregate alias>,direction:asc|desc}. '
      'LATEST is {table,partition,date,tie} only when expressly requested and date/tie are suitable. '
      'For references to prior result cohort use cohort="active"; otherwise null. '
      'The limit field must ALWAYS be a JSON integer from 1 through 200, even for scalar aggregates and follow-ups. '
      'When the user did not explicitly request a number of result rows, use limit=200, NEVER null, 0, -1, a numeric string, or 1000. '
      'For a requested TOP/BOTTOM/FIRST N use limit=N; do not confuse dates, thresholds, ranking percentiles or time windows with row limits. '
      'Return {status:unsupported,reason:"specific missing definition",plan:null} if ANY requested condition, metric, join, comparison, time frame or grouping cannot be precisely expressed. '
      'Never omit any semantic condition; no assumptions, no natural language text outside JSON. '
      'For customer counts use count_distinct on approved customer ID where possible to prevent 1-to-many duplication. '
      'For TOP N, BOTTOM N, or ordered row listings, use aggregate=[] and group_by=[]; project the entity identifier and approved ranking field, specify sort and limit. '
      'Use group_by ONLY for a genuine grouped aggregate, accompanied by at least one aggregate function. Never use group_by merely to deduplicate, identify a row, or sort. '
      'Do not use unsupported temporal offsets or predicted values. '
      'A referenced cohort has verified prior entity and its source-field, but you do NOT know its member values. '
      'Only use joins listed in approved catalog; ambiguous relationship paths cannot be selected by the model. '
      'For any row-level entity result, project its approved primary key if present, so the result can be retained as a complete conversational cohort. '
      'Observed status domains list exact, source-verified categorical labels for approved status AND category fields (for example segment or product), not derived business definitions. '
      'Any equality filter on a field listed there must use one of its exact labels. '
      'When the user explicitly names a status label such as missed and the related status field has exact observed value MISSED, '
      'an equality EXISTS predicate on that exact label is allowed, without inferring an overdue calculation. '
      'approved_terms (when present) are steward-approved business terms with an exact definition the engine applies itself: put {term:<id>, negate:false} in plan.terms for each term the question uses (negate:true for not, no, without, never), and do not add filters, statuses or dates for it. Every plan has a terms array, usually []. '
      'A year stated in the question (for example "in 2024") means the date field is between YYYY-01-01 and YYYY-12-31: use op between with those two dates. '
      'business_concepts (when present) are steward-approved: for that business entity use its primary_table as entity or EXISTS table, and its attribute fields for the attributes named. '
      'The catalog may be narrowed to the tables this question is about; connected_tables are joined automatically through approved relationships. If something required is not in the catalog, return unsupported. '
      'approved_business_definitions are steward-approved names for one aggregation of one field; when the question uses such a name, use exactly that aggregation and field. They define no conditions or statuses. '
      'Never equate synonyms (late, overdue, delinquent, past due) to a status code unless an approved definition explicitly says so. '
      'Never invent business status definitions or make up a status literal absent from observed_status_domains. '
      'If requested semantics cannot be answered by an exact label or approved definition, return unsupported with the missing definition. '
      'Every predicate requires all three keys: field, op and value, even when value is null for is_null/not_null. '
      'For related-entity existence such as a loan with a repayment status, use exists:[{table:<approved related table ID>,filters:[{field:<status field ID>,op:eq,value:<observed literal>}],negate:false}]. '
      'Do not place nested Boolean groups inside the filters array; use the separate where field. '
      'Each aggregate entry has percentile:null unless fn=percentile, which needs 0..100. '
      'When aggregate is non-empty, select MUST be [] (grouping columns go in group_by only). '
      'For a total per related item (revenue per customer), use the table holding the measure as entity and group by its own identifier column. '
      'Use group_by only when the question asks for a breakdown (by, per, each); an overall value has group_by=[]. '
      'Relationships between tables are applied automatically from approved joins: never write a filter whose value is a field or table ID. '
      'Never compute dates relative to today; use only dates and years stated in the question (approved terms are the exception: the engine applies them with the approved as-of date, so they need no dates in your plan). '
      'Ratios, percentages, durations between dates, deciles and change over time are not supported: return unsupported. ')
    base_messages=[{'role':'system','content':instruct},
        # v18.1: catalog first and question last, so a repeated catalog prefix can be
        # served from the provider's prompt cache.
        {'role':'user','content':json.dumps({'approved_catalog':json.loads(catalog),
                'active_cohort':({'entity':prior.get('entity'), 'source_field':prior.get('source_field')} if prior else None),
                'current_date':datetime.date.today().isoformat(),'question':question},default=str)}]
    # Repair is a second attempt by the SAME canonical interpreter, not a
    # fallback engine. Never remove invalid plan clauses without consulting the
    # user's original question; execution happens only after full validation.
    messages=list(base_messages)
    response_format={'type':'json_schema','json_schema':{'name':'binggio_canonical_plan_v17',
                       'strict':True,'schema':canonical_response_schema(u)}}
    for attempt in range(2):
        payload={'model':model,'temperature':0,'response_format':response_format,'messages':messages}
        data=json.dumps(payload).encode('utf-8')
        req=urllib.request.Request('https://api.openai.com/v1/chat/completions',data=data,headers={
           'Authorization':'Bearer '+_llm_key(),
           'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=35) as resp:
                obj=json.load(resp)
            result=json.loads(obj['choices'][0]['message']['content'])
        except Exception as e:
            raise Unrecognized('Canonical interpretation unavailable: '+_api_error_summary(e)) from e
        try:raw=_unwrap_model_result(result)
        except Blocked as e:
            # v18.3.1: the model may refuse because it thinks it must define the term or
            # compute the as-of date itself. One correction, only when approved terms apply.
            if attempt or not mentioned:raise
            problem=('You returned unsupported ('+str(e)[:200]+'), but this question uses approved terms that the ENGINE applies with '
                     'their approved definition and as-of date. You need no dates or definitions for them: put each in plan.terms and plan '
                     'everything else in the question. Return unsupported only if something other than these terms cannot be represented')
            log.info('Canonical refusal corrected for approved terms (attempt 1): %s',str(e)[:200])
            messages=base_messages+[{'role':'assistant','content':json.dumps(result,default=str)},{'role':'user','content':problem}]
            continue
        try:
            # Normalize only mechanical result-count data. Every semantic
            # predicate, WHERE/EXISTS scope, and cohort goes to strict validator.
            checked=normalize_model_projection(normalize_model_limit(raw,question))
            plan=validate(checked,u,prior)
            check_plan_alignment(plan,question,u,prior,allowed,mentioned)
            check_stated_literals(plan,question,u)
            if prior is not None and plan.get('cohort')!='active':
                raise Blocked('Explicitly referenced prior cohort was omitted')
            return validate_status_literals(plan,u,domains)
        except (Blocked,InvalidCanonicalPlan) as e:
            problem=str(e)
            log.info('Canonical plan rejected (attempt %d): %s | plan=%s',attempt+1,problem[:200],json.dumps(raw,default=str)[:1500])
        if attempt:
            raise InvalidCanonicalPlan('The canonical model could not produce a valid plan after one correction: '+problem[:170])
        messages=base_messages+[
            {'role':'assistant','content':json.dumps(result,default=str)},
            {'role':'user','content':(
                'Your previous JSON failed CANONICAL PLAN VALIDATION: '+problem[:350]+'. '
                'Re-read the ORIGINAL QUESTION and the full approved catalog. '
                'Return a corrected complete JSON plan preserving EVERY requested condition, '
                'scope, cohort reference, ranking, sort and limit. '
                'Ensure limit is an integer 1..200 (default 200 unless user requests a TOP/BOTTOM/FIRST N); '
                'For a row-level TOP N ranking use group_by=[] and aggregate=[]; '
                'group_by is allowed only with an actual aggregate. '
                'Do not invent relationships, source fields, statuses or calculations. '
                'If the question cannot be fully represented, return status=unsupported, reason, plan=null. '
                'All predicates must have field, op and value; use EXISTS for conditions on a related table. '
                'With an aggregate, select must be []. Never use a field ID as a filter value.')}
        ]
    raise InvalidCanonicalPlan('The canonical interpreter did not return a validated plan')


def _type_for_field(f):
    typ=str(f.get('data_type',f.get('type',''))).upper()
    if any(x in typ for x in ('INT','DECIMAL','NUMERIC','DOUBLE','FLOAT','REAL')) or f.get('role','').upper()=='MEASURE':return 'REAL'
    return 'TEXT'


def _is_source_integrity_error(e):
    if isinstance(e,OSError):return True
    names={type(e).__name__}|{c.__name__ for c in type(e).__mro__}
    return bool(names & {'SemanticBlocked','ConnectionErrorSafe'})


def _load_source(project,u,tid):
    from binggio.context import execution
    reader=getattr(execution,'_read_source',None)
    _need(callable(reader),'Phase8A source reader is unavailable')
    _need(list(inspect.signature(reader).parameters)[:3]==['project','table_id','ctx'],
          'Phase8A source reader interface differs from the audited contract')
    try:result=reader(project,tid,u.ctx)
    except Blocked:raise
    except Exception as e:
        # v17: Phase8A raises SemanticBlocked / ConnectionErrorSafe / OSError for
        # stale profiles, removed connections or missing files. These are
        # source-integrity conditions, not crashes: surface them as Blocked so
        # callers report them instead of failing with an incident ID.
        if not _is_source_integrity_error(e):raise
        raise Blocked('Source '+str(tid)+' cannot be read safely: '+str(e)[:160]) from e
    _need(isinstance(result,(list,tuple)) and len(result)==2,'Source reader must return (columns,rows)')
    columns,rows=result
    _need(isinstance(columns,list) and isinstance(rows,list),'Source reader result is malformed')
    _need(len(rows)<=MAX_SOURCE_ROWS,'Source table exceeds the %d-row limit for answers' % MAX_SOURCE_ROWS)
    names=[u.fields[fid]['name'] for fid in u.by_table[tid]]
    _need(len(names)==len(set(names)), 'Approved source has duplicate column names')
    positions={name:[i for i,col in enumerate(columns) if str(col)==name] for name in names}
    _need(all(len(idxs)==1 for idxs in positions.values()),'Approved field is absent/ambiguous in physical source '+str(tid))
    arranged=[]
    for row in rows:
        _need(isinstance(row,(list,tuple)) and len(row)==len(columns),'Malformed source row in '+str(tid))
        out=[]
        for fid,name in zip(u.by_table[tid],names):
            value=row[positions[name][0]]
            if value is None or str(value).strip()=='':out.append(None);continue
            if _type_for_field(u.fields[fid])=='REAL':
                try:v=parse_number(value)
                except (ValueError,TypeError) as e:raise Blocked('Non-numeric value in approved measure '+name) from e
                _need(math.isfinite(v),'Non-finite approved measure '+name)
                out.append(v)
            elif fid in getattr(u,'normalized_keys',()):out.append(normalize_key(value))
            elif _date_field(u.fields[fid]):
                text=str(value).strip();m=_SLASH_ISO_DATE.fullmatch(text)
                out.append(m.group(1)+'-'+m.group(2)+'-'+m.group(3) if m else text)
            else:out.append(str(value))
        arranged.append(out)
    return arranged


def _recheck_provenance(project,u,prior,profile_override=None):
    if not prior:return
    _need(prior.get('registry_signature')==u.revision,'Semantic registry changed since referenced result')
    if profile_override is not None:
        actual=profile_override(prior.get('fingerprints',{}).keys())
        _need(all(actual.get(tid)==digest for tid,digest in prior.get('fingerprints',{}).items()),
              'Source profile changed since the previous answer')
        return
    from binggio.profiling import store as profiles
    for tid,expected in prior.get('fingerprints',{}).items():
        _need(tid in u.tables,'Referenced source no longer approved')
        t=u.tables[tid]
        profile=profiles.get_table(project,t['connection_id'],t['table'],include_stale=True)
        _need(profile is not None and not profile.get('stale') and profile.get('fingerprint')==expected,
              'Source changed or became stale since referenced result; rerun its question')


class Compiler:
    def __init__(self,u,plan,prior):
        plan=expand_terms(plan,u)  # v18.3: approved terms become filters / EXISTS
        self.u=u;self.p=plan;self.prior=prior;self.virtual={}
        self.base=plan['entity'];self.required=[self.base];self.chains={}
        tids={u.fields[x]['table_id'] for x in plan['select']+plan['group_by']+
              [f['field'] for f in plan['filters']]+_where_fields(plan.get('where'))+[a['field'] for a in plan['aggregate'] if a['field']]+[s['field'] for s in plan['sort'] if s['field'] in u.fields]}
        if plan['cohort']:tids.add(u.fields[prior['source_field']]['table_id'])
        if plan['latest']:tids.add(plan['latest']['table'])
        for tid in sorted(tids):
            chain=u.path(self.base,tid);nodes=u.tables_in_path(self.base,chain)
            self.chains[tid]=(chain,nodes)
            for x in nodes:
                if x not in self.required:self.required.append(x)
        self.a={tid:'t'+str(i) for i,tid in enumerate(self.required)}
        self.needed_src=set(self.required)
        for i,e in enumerate(plan['exists']):
            edges=u.path(self.base,e['table'])
            self.needed_src.update(u.tables_in_path(self.base,edges))
            if e.get('_latest'):
                self.virtual[i]=('latest_'+str(i)+'_'+e['table'],e['table'],e['_latest'])
        self.params=[]
        self.column_index={tid:{fid:i for i,fid in enumerate(u.by_table[tid])} for tid in self.needed_src}

    def col(self,fid,aliases=None):
        tid=self.u.fields[fid]['table_id']
        return (aliases or self.a)[tid]+'.c'+str(self.column_index[tid][fid])

    def predicate(self,f,aliases=None):
        c=self.col(f['field'],aliases);op=f['op'];v=f['value']
        if f['field'] in getattr(self.u,'normalized_keys',()) and op in ('eq','ne','in','not_in'):
            v=[normalize_key(x) if isinstance(x,str) else x for x in v] if isinstance(v,list) else (normalize_key(v) if isinstance(v,str) else v)
        if f.get('_term') and isinstance(v,dict):  # v18.3: column comparison from an approved term only
            _need(op in ('eq','ne','gt','gte','lt','lte') and v.get('field') in self.u.fields,'Invalid term comparison')
            return c+' '+{'eq':'=','ne':'!=','gt':'>','gte':'>=','lt':'<','lte':'<='}[op]+' '+self.col(v['field'],aliases)
        if op=='is_null':return c+' IS NULL'
        if op=='not_null':return c+' IS NOT NULL'
        if op in ('in','not_in'):
            self.params.extend(v);return c+(' NOT IN (' if op=='not_in' else ' IN (')+','.join('?' for _ in v)+')'
        if op=='between':self.params.extend(v);return c+' BETWEEN ? AND ?'
        self.params.append(v)
        return c+' '+{'eq':'=','ne':'!=','gt':'>','gte':'>=','lt':'<','lte':'<=','like':'LIKE'}[op]+' ?'

    def boolean(self,node):
        if 'field' in node:return self.predicate(node)
        items=[self.boolean(x) for x in node['items']]
        if node['mode']=='not':return '(NOT '+items[0]+')'
        return '('+(' AND ' if node['mode']=='all' else ' OR ').join(items)+')'

    def _join_path(self,edges,nodes,aliases,prefix='JOIN',sources=None):
        bits=[];sources=sources or {}
        for i,eid in enumerate(edges):
            prev,nxt=nodes[i:i+2];j=self.u.joins[eid]
            lhs,rhs=j['left'],j['right']
            _need(set([self.u.fields[lhs]['table_id'],self.u.fields[rhs]['table_id']])==set([prev,nxt]),'Invalid approved relationship')
            first=lhs if self.u.fields[lhs]['table_id']==prev else rhs
            second=rhs if first==lhs else lhs
            bits.append(prefix+' '+_quote('src_'+sources.get(nxt,nxt))+' AS '+aliases[nxt]+' ON '+self.col(first,aliases)+' = '+self.col(second,aliases))
        return bits

    def _exists(self,e,index):
        tid=e['table'];edges=self.u.path(self.base,tid);nodes=self.u.tables_in_path(self.base,edges)
        _need(edges,'EXISTS on the base entity should be expressed as a normal filter')
        aliases={self.base:self.a[self.base]}
        for i,node in enumerate(nodes[1:]):aliases[node]='x'+str(index)+'_'+str(i)
        first=nodes[1];eid=edges[0];j=self.u.joins[eid]
        f1,f2=j['left'],j['right']
        left=f1 if self.u.fields[f1]['table_id']==self.base else f2
        right=f2 if left==f1 else f1
        # v18.3: a latest-row term reads its table from the as-of snapshot source
        sources={tid:self.virtual[index][0]} if index in self.virtual else {}
        joins=self._join_path(edges[1:],nodes[1:],aliases,sources=sources)
        where=[self.col(left,aliases)+' = '+self.col(right,aliases)]
        where.extend(self.predicate(f,aliases) for f in e['filters'])
        # ON condition is placed exclusively in WHERE, not immediately after FROM.
        clause='SELECT 1 FROM '+_quote('src_'+sources.get(first,first))+' AS '+aliases[first]
        if joins:clause+=' '+' '.join(joins)
        clause+=' WHERE '+' AND '.join(where)
        return ('NOT EXISTS (' if e['negate'] else 'EXISTS (')+clause+')'

    def sql(self):
        u=self.u;p=self.p
        ag=bool(p['aggregate'])
        strict={u.fields[self.prior['source_field']]['table_id']} if p['cohort'] else set()
        if p['latest']:strict.add(p['latest']['table'])
        if ag and ((strict-{self.base}) or any(not u.preserves_rows(self.base,t) for t in self.required if t!=self.base)) \
                and any(a['fn'] in ('sum','avg','count') for a in p['aggregate']):
            # Without proven key preservation, a 1:N join changes SUM/AVG/COUNT.
            # COUNT DISTINCT and MAX/MIN remain semantically stable under fanout.
            raise Blocked('Ambiguous aggregate fanout across joined tables; use COUNT DISTINCT or group by a certified grain')
        if ag:
            projections=[self.col(f)+' AS '+_quote(f) for f in p['group_by']]
            for a in p['aggregate']:
                target=self.col(a['field']) if a['field'] is not None else '*'
                fn=a['fn'].upper()
                if fn=='COUNT_DISTINCT':expr='COUNT(DISTINCT '+target+')'
                elif fn=='PERCENTILE':expr='PERCENTILE('+target+','+str(float(a['percentile']))+')'
                else:expr=fn+'('+target+')'
                projections.append(expr+' AS '+_quote(a['alias']))
        else:projections=[self.col(f)+' AS '+_quote(f) for f in p['select']]
        base='FROM '+_quote('src_'+self.base)+' AS '+self.a[self.base]
        joined={self.base};joins=[]
        for tid,(chain,nodes) in self.chains.items():
            for i,eid in enumerate(chain):
                prev,nxt=nodes[i:i+2]
                if nxt in joined:continue
                joins.extend(self._join_path([eid],[prev,nxt],self.a,'LEFT JOIN'))
                joined.add(nxt)
        # Re-check all requested paths belong to a single rooted join graph.
        for tid in self.required:_need(tid in joined,'Cannot compile disconnected approved source')
        where=[self.predicate(f) for f in p['filters']]
        if p.get('where') is not None:where.append(self.boolean(p['where']))
        for index,e in enumerate(p['exists']):where.append(self._exists(e,index))
        if p['cohort']:
            ids=p['cohort'] and self.prior['values']
            field=self.prior['source_field']
            if ids and field in getattr(u,'normalized_keys',()):ids=list(dict.fromkeys(normalize_key(x) for x in ids))
            if ids:
                self.params.extend(ids)
                where.append(self.col(field)+' IN ('+','.join('?' for _ in ids)+')')
            else:where.append('0=1')
        single_entity_id=(not ag and len(p['select'])==1 and p['select'][0] in u.key_fields(self.base))
        sql='SELECT '+('DISTINCT ' if single_entity_id else '')+', '.join(projections)+' '+base
        if joins:sql+=' '+' '.join(joins)
        if where:sql+=' WHERE '+' AND '.join(where)
        if ag and p['group_by']:sql+=' GROUP BY '+', '.join(self.col(f) for f in p['group_by'])
        if p['sort']:
            parts=[]
            for s in p['sort']:
                ref=_quote(s['field']) if ag else self.col(s['field'])
                parts.append(ref+' '+s['direction'].upper())
            if ag and p['group_by']:
                parts += [self.col(fid)+' ASC' for fid in p['group_by'] if not any(s['field']==fid for s in p['sort'])]
            elif not ag:
                ids=[fid for fid in u.key_fields(self.base) if fid in p['select']]
                parts += [self.col(fid)+' ASC' for fid in ids if not any(s['field']==fid for s in p['sort'])]
            sql+=' ORDER BY '+', '.join(parts)
        sql+=' LIMIT '+str(p['limit']+1)
        return sql


def _fingerprints(project,u,tids):
    from binggio.profiling import store as profiles
    result={}
    for tid in tids:
        t=u.tables[tid]
        p=profiles.get_table(project,t['connection_id'],t['table'],include_stale=True)
        _need(p is not None and not p.get('stale') and p.get('fingerprint'),
              'Missing or stale profile for '+tid+'; cannot certify a reusable cohort')
        result[tid]=p['fingerprint']
    return result


_PK_ROLES={'PRIMARY_KEY','PRIMARY_KEY_CANDIDATE'}


def cohort_key(u,fid):
    """The approved key a result column can be remembered by, or None (v17.2).

    - a primary-key field: itself;
    - the parent (one) side of an approved many-to-one relationship: itself, as
      parent uniqueness was verified when that relationship was approved;
    - a child identifier referencing exactly one such parent key: that parent key
      (risk_assessments.CUSTOMER_ID -> customer_master.CUSTOMER_ID).
    Joins without an explicit many_to_one cardinality never qualify."""
    if str(u.fields[fid].get('role','')).upper() in _PK_ROLES:return fid
    m2o=[j for j in u.joins.values() if j.get('cardinality')=='many_to_one'
         and j.get('left') in u.fields and j.get('right') in u.fields]
    if any(j['right']==fid for j in m2o):return fid
    parents={j['right'] for j in m2o if j['left']==fid}
    return parents.pop() if len(parents)==1 else None


def _cohort(u,plan,headers,rows,profiles):
    columns=plan['group_by'] if plan['aggregate'] else plan['select']
    mapped=[(fid,cohort_key(u,fid)) for fid in columns if fid in headers]
    mapped=[(fid,key) for fid,key in mapped if key]
    # Preference: the entity's own primary key, then any key of the entity,
    # then a single key anywhere in the result (v16 behaviour kept first).
    tiers=[[x for x in mapped if u.fields[x[0]]['table_id']==plan['entity'] and x[0]==x[1]
            and str(u.fields[x[0]].get('role','')).upper() in _PK_ROLES],
           [x for x in mapped if u.fields[x[0]]['table_id']==plan['entity']],
           mapped]
    chosen=None
    for tier in tiers:
        if len({key for _,key in tier})==1:chosen=tier[0];break
        if tier:return None  # several different keys at the preferred level: ambiguous
    if chosen is None:return None
    fid,key=chosen
    idx=headers.index(fid)
    values=[str(row[idx]).strip() for row in rows]
    if any(not v for v in values):return None
    if key in getattr(u,'normalized_keys',()):values=[normalize_key(v) for v in values]
    return {'cohort_kind':'phase7_approved','entity':u.tables[u.fields[key]['table_id']].get('table',u.fields[key]['table_id']),
            'source_field':key,'values':list(dict.fromkeys(values)),'registry_signature':u.revision,
            'fingerprints':profiles,'from_turn':'pending',
            'derived_from':(fid if fid!=key else None)}


class _Median:
    def __init__(self):self.values=[]
    def step(self,value):
        if value is not None:self.values.append(float(value))
    def finalize(self):return statistics.median(self.values) if self.values else None


class _Percentile:
    def __init__(self):self.values=[];self.percentile=None
    def step(self,value,p):
        if self.percentile is None:self.percentile=float(p)
        if value is not None:self.values.append(float(value))
    def finalize(self):
        if not self.values:return None
        values=sorted(self.values);rank=(len(values)-1)*(self.percentile/100)
        lo,hi=math.floor(rank),math.ceil(rank)
        return values[lo]+(values[hi]-values[lo])*(rank-lo)


def _check_date_comparisons(u,plan,loaded):
    """Refuse date comparisons on fields holding non-standard dates (v17).

    Dates are compared as YYYY-MM-DD text; a value such as 03/04/2026 would
    compare wrongly and silently change the answer. Such fields are refused
    with an explanation until the source is corrected or normalised."""
    checked=set()
    targets=[f['field'] for f in _status_filter_leaves(plan) if f['op'] in ('gt','gte','lt','lte','between')]
    if plan.get('latest'):targets.append(plan['latest']['date'])
    for fid in targets:
        info=u.fields.get(fid)
        if fid in checked or info is None or not _date_field(info):continue
        checked.add(fid);tid=info['table_id']
        if tid not in loaded:continue
        idx=u.by_table[tid].index(fid)
        bad=[r[idx] for r in loaded[tid] if r[idx] is not None and not _ISO_DATE.fullmatch(str(r[idx]))]
        _need(not bad,'Dates in '+fid+' are not all in YYYY-MM-DD form ('+str(len(bad))+
              ' values differ), so date comparisons on it would be unreliable. The source needs correcting before this question can be answered')


def _empty_result_context(u,plan,loaded):
    """For an empty answer, the observed range of each compared field (v17).

    Uses only approved fields of sources already loaded for this query; values
    stay local (never sent to the model). Helps the user see that, for example,
    a date condition falls outside the period the data covers.
    """
    out=[]
    for f in _status_filter_leaves(plan):
        op=f['op'];fid=f['field'];info=u.fields.get(fid)
        if info is None or op not in ('gt','gte','lt','lte','between','eq'):continue
        if op=='eq' and _type_for_field(info)!='REAL':continue
        tid=info['table_id']
        if tid not in loaded:continue
        idx=u.by_table[tid].index(fid)
        values=[r[idx] for r in loaded[tid] if r[idx] is not None]
        entry={'field':fid,'op':op,'value':f['value'],'non_empty_values':len(values)}
        if values:
            try:entry.update(observed_min=min(values),observed_max=max(values))
            except TypeError:pass
        out.append(entry)
        if len(out)>=5:break
    return out


_STOPWORDS=set("""the a an of for in on at to and or not no with by from what which who whose whom how many much
is are was were be been being have has had do does did can could should would will these those this that
them they their there here any all each every show list find give get tell me my our us top bottom first last
number count total average sum mean per than more less over under between same previous earlier above below
please only also just its it as""".split())


def _stem(word):
    if word.endswith(('us','ss','is')):return word  # status, class, analysis
    for suffix in ('ings','ing','ies','es','ed','s'):
        if word.endswith(suffix) and len(word)-len(suffix)>=3:
            return word[:-len(suffix)]+('y' if suffix=='ies' else '')
    return word


def _terms(text):
    return {_stem(w) for w in re.findall(r'[a-z][a-z0-9]{2,}',str(text).lower()) if w not in _STOPWORDS}


_KEY_ROLE_CANDIDATES={'PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER','PRIMARY_KEY','FOREIGN_KEY'}


def _column_words(table,column):
    """Words of a column name, without words that merely repeat its table name
    (loan_accounts.loan_product -> {product}); falls back to all words when
    nothing else is left (customer_revenue.REVENUE -> {revenue})."""
    words=_terms(column.replace('_',' '))
    own=words-_terms(table.replace('_',' '))
    return own or words


def answerability_hints(project,u,question,profile_source=None,source_reader=None,limit=3):
    """Explain a refusal in terms of what is missing (v17). Read-only.

    Finds profiled but UNAPPROVED, non-key columns whose name or short code
    values match words in the question, ranked by how many question words they
    explain. Labels shared by several unrelated columns are flagged as possible
    copied data. Nothing here is sent to the model; the result is shown to the
    local user and steward only. Returns [{kind, table_id, column, matched, detail}].
    """
    words=_terms(question)
    if not words:return []
    if profile_source is None:
        from binggio.profiling import store as profiles
        def profile_source():
            for t in profiles.list_profiles(project):
                if t.get('status')=='not_profiled':continue
                p=profiles.get_table(project,t['connection_id'],t['table'],include_stale=True)
                if p:yield t['connection_id'],t['table'],p
    if source_reader is None:
        def source_reader(tid):
            from binggio.context import execution
            return execution._read_source(project,tid,u.ctx)
    approved_fields=set(u.fields)
    entity_words=set()
    for t in u.tables.values():entity_words|=_terms(str(t.get('table','')).replace('_',' '))
    approved_words=set()
    for fid,info in u.fields.items():
        approved_words|=_column_words(str(u.tables.get(info.get('table_id'),{}).get('table','')),str(info.get('name','')))
    candidates=[];label_sets=collections.Counter()
    for cid,table,p in profile_source():
        tid=cid+'::'+table
        cols=[c for c in p.get('columns',[]) if tid+'::'+c['name'] not in approved_fields
              and str(c.get('semantic_role_candidate','')).upper() not in _KEY_ROLE_CANDIDATES]
        if not cols:continue
        table_words=_terms(table.replace('_',' '))
        source=None
        for c in cols:
            name_hit=words & _column_words(table,c['name'])
            label=None;labels=None
            low_card=(c.get('observed_type')=='text' and 0<int(c.get('distinct_count') or 99)<=MAX_LABELS)
            if low_card and tid in u.tables:
                if source is None:
                    try:source=source_reader(tid)
                    except Exception:source=False
                if source:
                    headers,rows=source
                    if c['name'] in headers:
                        i=headers.index(c['name'])
                        found={str(r[i]).strip() for r in rows if r[i] not in (None,'')}
                        if found and all(_SAFE_CODE.fullmatch(v) for v in found):
                            labels=frozenset(found);label_sets[labels]+=1
                            for value in sorted(found):
                                # AUTO_LOAN in loan_accounts must not match "loans":
                                # words repeating the table name are not evidence.
                                hit=words & (_terms(value.replace('_',' '))-table_words-entity_words)
                                if hit:label=(value,sorted(hit)[0]);break
            name_hit-=approved_words  # concept already approved elsewhere: not the missing piece
            if not name_hit and not label:continue
            score=len(name_hit)+(1 if label else 0)+0.5*len(words & table_words)
            candidates.append((score,table,c['name'],tid,sorted(name_hit),label,labels))
    candidates.sort(key=lambda x:(-x[0],x[1],x[2]))
    hints=[]
    for score,table,column,tid,name_hit,label,labels in candidates[:limit]:
        where=table+'.'+column
        if label:
            detail=where+' is not approved yet. It contains the value '+label[0]+', which matches "'+label[1]+'".'
            matched=label[1]
        else:
            detail=where+' is not approved yet, and its name matches "'+name_hit[0]+'".'
            matched=name_hit[0]
        if labels is not None and label_sets[labels]>=3:
            detail+=(' Caution: '+str(label_sets[labels]-1)+' other unapproved columns hold exactly the same values,'
                     ' which may mean copied or placeholder data; check the source before approving.')
        detail+=' A data steward can review this column in Column semantics.'
        hints.append({'kind':'unapproved_column','table_id':tid,'column':column,'matched':matched,'detail':detail})
    return hints


def execute(project,u,plan,prior=None,source_override=None,profile_override=None):
    _recheck_provenance(project,u,prior if plan['cohort'] else None,profile_override)
    compiler=Compiler(u,plan,prior)
    con=sqlite3.connect(':memory:')
    _deadline=time.monotonic()+QUERY_SECONDS
    con.set_progress_handler(lambda:1 if time.monotonic()>_deadline else 0,200000)
    con.create_aggregate('MEDIAN',1,_Median)
    con.create_aggregate('PERCENTILE',2,_Percentile)
    try:
        con.execute('PRAGMA trusted_schema=OFF')
        loaded={}
        for tid in sorted(compiler.needed_src):
            definitions=','.join('c'+str(i)+' '+_type_for_field(u.fields[fid]) for i,fid in enumerate(u.by_table[tid]))
            con.execute('CREATE TABLE '+_quote('src_'+tid)+' ('+definitions+')')
            records=(source_override(tid) if source_override else _load_source(project,u,tid))
            _need(len(records)<=MAX_SOURCE_ROWS,'Source exceeds bounded-read limit')
            if plan['latest'] and plan['latest']['table']==tid:
                # Deterministic as-of snapshot: latest partition with approved date and identifier tie break.
                spec=plan['latest'];pidx=compiler.column_index[tid][spec['partition']]
                didx=compiler.column_index[tid][spec['date']]
                tidx=compiler.column_index[tid][spec['tie']]
                latest_rows={}
                for record in records:
                    partition,date_value,tie=(record[pidx],record[didx],record[tidx])
                    _need(partition is not None and date_value is not None and tie is not None,'Latest requires non-null partition/date/tie')
                    try:parsed=datetime.date.fromisoformat(str(date_value)[:10])
                    except ValueError as err:raise Blocked('LATEST requires ISO-date source values') from err
                    rank=(parsed,str(tie))
                    if partition not in latest_rows or rank>latest_rows[partition][0]:latest_rows[partition]=(rank,record)
                    elif rank==latest_rows[partition][0]:raise Blocked('LATEST has unresolved date/tie duplicate')
                records=[entry[1] for entry in latest_rows.values()]
            loaded[tid]=records
            if records:
                con.executemany('INSERT INTO '+_quote('src_'+tid)+' VALUES('+','.join(['?']*len(u.by_table[tid]))+')',records)
            if len(records)>5000:
                # Large tables: index approved key columns so joins and EXISTS stay fast.
                for i,fid in enumerate(u.by_table[tid]):
                    if u.fields[fid].get('role') in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER'):
                        con.execute('CREATE INDEX '+_quote('ix_'+str(abs(hash((tid,i)))))+' ON '+_quote('src_'+tid)+'(c'+str(i)+')')
        for vname,tid,spec in compiler.virtual.values():
            # v18.3: latest row per entity on or before the approved as-of date
            definitions=','.join('c'+str(i)+' '+_type_for_field(u.fields[fid]) for i,fid in enumerate(u.by_table[tid]))
            con.execute('CREATE TABLE '+_quote('src_'+vname)+' ('+definitions+')')
            pidx=compiler.column_index[tid][spec['partition']];didx=compiler.column_index[tid][spec['date']]
            best={}
            for record in loaded[tid]:
                part,day=record[pidx],record[didx]
                if part is None or day is None:continue
                _need(_ISO_DATE.fullmatch(str(day)) is not None,'Dates in '+spec['date']+' are not all YYYY-MM-DD; the term "'+spec['term']+'" cannot be applied reliably')
                if str(day)>spec['as_of']:continue
                if part not in best or str(day)>best[part][0]:best[part]=(str(day),record,False)
                elif str(day)==best[part][0]:best[part]=(str(day),record,True)
            _need(not any(x[2] for x in best.values()),'Two rows share the latest date for one entity; the term "'+spec['term']+'" cannot pick one')
            rows_l=[x[1] for x in best.values()]
            if rows_l:con.executemany('INSERT INTO '+_quote('src_'+vname)+' VALUES('+','.join(['?']*len(u.by_table[tid]))+')',rows_l)
        _check_date_comparisons(u,compiler.p,loaded)
        query=compiler.sql()
        raw=con.execute(query,compiler.params)
        rows=[list(row) for row in raw.fetchall()]
        total=None
        if len(rows)>plan['limit'] and plan['limit']<MAX_RESULT:
            rows=rows[:plan['limit']]  # An explicit bounded Top-N is a complete ordered cohort.
        elif len(rows)>MAX_RESULT:
            # More rows than can be shown: show the first 200 and say how many there are. Nothing is
            # hidden silently, and the partial list is never kept as a result to ask follow-ups about.
            base=query.rsplit(' LIMIT ',1)[0]
            total=con.execute('SELECT COUNT(*) FROM ('+base+')',compiler.params).fetchone()[0]
            rows=rows[:MAX_RESULT]
        headers=[d[0] for d in raw.description]
        empty_context=_empty_result_context(u,plan,loaded) if not rows else []
        profiles=(profile_override(compiler.needed_src) if profile_override else _fingerprints(project,u,compiler.needed_src))
        cohort=_cohort(u,plan,headers,rows,profiles) if total is None else None
        answer={'status':'answered','columns':headers,'rows':rows,'row_count':len(rows) if total is None else total,
                'cohort_complete':bool(cohort),'sql':query,'parameters':compiler.params,
                'semantic_plan':{'mode':'unified_v14','plan':plan},
                'provenance':{'registry_signature':u.revision,'sources':[{'table_id':x} for x in sorted(compiler.needed_src)],
                              'approved_graph':True,'read_only':True,'sql_executed':True,
                              'normalisation':'Identifier and join keys matched ignoring spaces and case, as in relationship discovery; '
                                              'numbers with thousands separators and YYYY/MM/DD dates read in standard form.'}}
        if total is not None:
            answer['truncated_for_display']=True
            answer['note']=('Showing the first %d of %d rows. Ask for a count, the top N, or add a filter. '
                            'Follow-up questions about "these" need an answer of %d rows or fewer.' % (MAX_RESULT,total,MAX_RESULT))
        if plan.get('terms'):
            # v18.3: which approved definitions were applied, and the as-of date they used
            used=[u.terms()[a['term']] for a in plan['terms']]
            answer['business_terms']=[{'term':t['name'],'negated':a['negate'],'meaning':t['description'],'revision':t['revision']}
                                      for t,a in zip(used,plan['terms'])]
            if any(t['needs_as_of'] for t in used):answer['as_of']=u.as_of_date()
        if not rows:
            answer['answerability']={'status':'no_matching_rows',
                'message':'No rows matched these conditions in the approved data.'+
                          (' The observed range of each filtered field is shown, so you can see whether the condition falls outside the data.' if empty_context else ''),
                'filters':empty_context}
        return answer,cohort
    except sqlite3.DatabaseError as e:
        if 'interrupt' in str(e).lower():
            raise Blocked('The question took longer than %d seconds to answer and was stopped. Narrow it with a filter.' % QUERY_SECONDS) from e
        raise Blocked('Verified relational execution failed: '+str(e)[:160]) from e
    finally:con.close()
