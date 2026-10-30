# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""One bounded governed plan -> parameterized SQLite SQL -> local read-only source result.

No arbitrary user SQL, no oracle, no silent partial scans. An ephemeral SQLite
workspace is used solely to join registered CSV/SQLite source snapshots; source
connections are read-only. The Phase3.1 legacy route is left intact.
"""
from __future__ import annotations
import sqlite3
import hashlib
import time
import re
from pathlib import Path
from binggio.catalog import catalog
from binggio.catalog.connectors import resolve_source, ConnectionErrorSafe
from binggio.profiling.profiler import _sample_csv, _sample_sqlite, SAMPLE_LIMIT, _is_missing
from binggio.profiling import store as profiles
from .context import SemanticBlocked

# Profiling and discovery look at a bounded sample; answers read the WHOLE table.
EXECUTION_ROW_LIMIT=1_000_000
MAX_SOURCE_ROWS=EXECUTION_ROW_LIMIT
MAX_JOIN_ROWS=250000
MAX_QUERY_SECONDS=5.0

# Safe, bound-memory aggregates on fully materialized local data (up to EXECUTION_ROW_LIMIT rows per source).
# SQLite native AVG, SUM, MIN, MAX, COUNT remain untouched.
class _Statistics:
    def __init__(self, percentile=None, variation=None):
        self.values=[]
        self.percentile=percentile
        self.variation=variation
    def step(self, value):
        if value is not None:
            self.values.append(float(value))
            if len(self.values)>MAX_SOURCE_ROWS:
                raise ValueError('Aggregate input exceeds bounded sample limit')
    def finalize(self):
        if not self.values:return None
        v=sorted(self.values)
        if self.variation:
            mean=sum(v)/len(v)
            variance=sum((x-mean)**2 for x in v)/len(v)
            return variance**0.5 if self.variation=='STDDEV_POP' else variance
        pos=(len(v)-1)*self.percentile
        lo=int(pos);hi=min(lo+1,len(v)-1)
        return v[lo]+(v[hi]-v[lo])*(pos-lo)

def _register_statistics(db):
    for pct in (10,25,50,75,90,95,99):
        db.create_aggregate('BINGGIO_P'+str(pct),1,
             lambda pct=pct:_Statistics(percentile=pct/100))
    db.create_aggregate('BINGGIO_MEDIAN',1,lambda:_Statistics(percentile=0.5))
    db.create_aggregate('BINGGIO_STDDEV_POP',1,lambda:_Statistics(variation='STDDEV_POP'))
    db.create_aggregate('BINGGIO_VAR_POP',1,lambda:_Statistics(variation='VAR_POP'))

def _aggregate_sql(metric,expression):
    name=metric['aggregation']
    if name=='COUNT_DISTINCT':return 'COUNT(DISTINCT '+expression+')'
    supported={'SUM','AVG','MIN','MAX','COUNT','MEDIAN','STDDEV_POP','VAR_POP',
               'P10','P25','P50','P75','P90','P95','P99'}
    if name not in supported:raise SemanticBlocked('Unsupported governed aggregation')
    fn=('BINGGIO_'+name) if name not in {'SUM','AVG','MIN','MAX','COUNT'} else name
    return fn+'('+expression+')'



def physical(tid):return 'binggio_'+hashlib.sha256(tid.encode('utf-8')).hexdigest()[:20]

def quote(value):return '"'+str(value).replace('"','""')+'"'


def _table_names(plan,ctx):
    names={plan['base_table']}
    for eid in plan['joins']:
        edge=ctx['joins'][eid]
        names.update((edge['left_table'],edge['right_table']))
    for ex in plan['exists']:
        edge=ctx['joins'][ex['edge_id']]
        names.update((edge['left_table'],edge['right_table']))
    return sorted(names)


def compile_plan(plan,ctx):
    aliases={plan['base_table']:'t0'}
    pending=list(plan['joins']);joined=[]
    while pending:
        progress=False
        for eid in pending[:]:
            e=ctx['joins'][eid]
            if e['left_table'] in aliases and e['right_table'] not in aliases:
                old=e['left_table'];new=e['right_table'];oldcol=e['left'];newcol=e['right']
            elif e['right_table'] in aliases and e['left_table'] not in aliases:
                old=e['right_table'];new=e['left_table'];oldcol=e['right'];newcol=e['left']
            else:continue
            aliases[new]='t'+str(len(aliases))
            joined.append(f'JOIN {quote(physical(new))} {aliases[new]} ON '
                          f'{aliases[old]}.{quote(ctx["fields"][oldcol]["name"])} = '
                          f'{aliases[new]}.{quote(ctx["fields"][newcol]["name"])}')
            pending.remove(eid);progress=True
        if not progress:raise SemanticBlocked('Join plan contains a cycle or disconnected relationship')
    def expr(fid):
        field=ctx['fields'][fid]
        return aliases[field['table_id']]+'.'+quote(field['name'])
    select=[];labels=[]
    if plan['mode']=='rows':
        for fid in plan['select']:
            name=ctx['fields'][fid]['name']
            label=name if name not in labels else ctx['fields'][fid]['table_id'].split('::')[1]+'.'+name
            labels.append(label)
            select.append(expr(fid)+' AS '+quote(label))
    elif plan['mode']=='count':
        for fid in plan['group_by']:
            label=ctx['fields'][fid]['name'];labels.append(label)
            select.append(expr(fid)+' AS '+quote(label))
        labels.append('row_count');select.append('COUNT(*) AS "row_count"')
    else:
        for fid in plan['group_by']:
            label=ctx['fields'][fid]['name'];labels.append(label)
            select.append(expr(fid)+' AS '+quote(label))
        metric=ctx['metrics'][plan['metric']]
        label=metric['name'];labels.append(label)
        agg=metric['aggregation']
        fn=_aggregate_sql(metric,expr(metric['field']))
        select.append(fn+' AS '+quote(label))
    params=[];where=[]
    cohort=plan.get('_session_cohort')
    if cohort is not None:
        fid=cohort['field']
        values=cohort['values']
        if (not isinstance(values,list) or len(values)>200 or
                fid not in ctx['fields'] or ctx['fields'][fid]['table_id']!=plan['base_table']):
            raise SemanticBlocked('Invalid internal session cohort binding')
        if values:
            where.append(expr(fid)+' IN ('+','.join('?' for _ in values)+')')
            params.extend(values)
        else:
            where.append('1=0')
    for f in plan['filters']:
        where.append(expr(f['field'])+' '+f['op']+' ?');params.append(f['value'])
    for i,ex in enumerate(plan['exists']):
        edge=ctx['joins'][ex['edge_id']]
        base=plan['base_table']
        if edge['left_table']==base:
            basecol=edge['left'];othercol=edge['right'];other=edge['right_table']
        else:
            basecol=edge['right'];othercol=edge['left'];other=edge['left_table']
        alias='ex'+str(i)
        sub=f'SELECT 1 FROM {quote(physical(other))} {alias} WHERE {alias}.{quote(ctx["fields"][othercol]["name"])} = t0.{quote(ctx["fields"][basecol]["name"])}'
        for c in ex['conditions']:
            sub+=f' AND {alias}.{quote(ctx["fields"][c["field"]]["name"])} {c["op"]} ?'
            params.append(c['value'])
        where.append(('NOT ' if ex['negated'] else '')+'EXISTS ('+sub+')')
    sql='SELECT '+', '.join(select)+' FROM '+quote(physical(plan['base_table']))+' t0'
    if joined:sql+=' '+' '.join(joined)
    if where:sql+=' WHERE '+' AND '.join(where)
    if plan['group_by']:sql+=' GROUP BY '+', '.join(expr(fid) for fid in plan['group_by'])
    if plan['having']:
        metric=ctx['metrics'][plan['metric']]
        agg=metric['aggregation']
        aggregate=_aggregate_sql(metric,expr(metric['field']))
        sql+=' HAVING '+' AND '.join(aggregate+' '+h['op']+' ?' for h in plan['having'])
        params.extend(h['value'] for h in plan['having'])
    latest=plan.get('_session_latest')
    if latest is not None:
        if plan['mode']!='rows' or plan['joins'] or plan['group_by'] or plan['exists']:
            raise SemanticBlocked('Latest-per-customer requires a single-table row query')
        key,date,tie=(latest[x] for x in ('key','date','tie'))
        if any(fid not in plan['select'] or ctx['fields'][fid]['table_id']!=plan['base_table'] for fid in (key,date,tie)):
            raise SemanticBlocked('Latest-per-customer requires approved selected key, date and tie-break identifier')
        # Query is built from approved identifiers only, never user SQL.
        key_sql=expr(key);date_sql=expr(date);tie_sql=expr(tie)
        if len(set(labels))!=len(labels):raise SemanticBlocked('Ambiguous latest-result labels')
        # Unknown dates are excluded from latest rather than ranked as a valid complaint.
        sql+=' AND '+date_sql+' IS NOT NULL' if where else ' WHERE '+date_sql+' IS NOT NULL'
        inner=sql.replace('SELECT '+', '.join(select),
            'SELECT '+', '.join(select)+', ROW_NUMBER() OVER (PARTITION BY '+key_sql+
            ' ORDER BY '+date_sql+' DESC, '+tie_sql+' DESC) AS "__binggio_rank"',1)
        sql='SELECT '+', '.join(quote(x) for x in labels)+' FROM ('+inner+') WHERE "__binggio_rank" = 1'
        delayed=plan.get('_phase2_latest_filter')
        if delayed is not None:
            fid=delayed.get('field');op=delayed.get('op');value=delayed.get('value')
            if (fid not in plan['select'] or ctx['fields'][fid]['table_id']!=plan['base_table']
                    or ctx['fields'][fid]['role']!='MEASURE' or op not in ('>','<','>=','<=')
                    or type(value) not in (int,float)):
                raise SemanticBlocked('Invalid latest-measure predicate')
            sql+=' AND '+quote(ctx['fields'][fid]['name'])+' '+op+' ?'
            params.append(value)
        sql+=' ORDER BY '+quote(ctx['fields'][date]['name'])+' DESC, '+quote(ctx['fields'][key]['name'])+' ASC'
        sql+=' LIMIT ?';params.append(plan['limit'])
        return {'sql':sql,'params':params,'columns':labels,'source_tables':_table_names(plan,ctx)}
    if plan['order_by']:
        key=plan['order_by']['field']
        if key=='metric':order=quote(ctx['metrics'][plan['metric']]['name'])
        elif key=='count':order='"row_count"'
        else:order=expr(key)
        sql+=' ORDER BY '+order+' '+plan['order_by']['direction']
        if plan.get('_ranked_customer_cohort'):
            # Resolve equal metric values deterministically, without an arbitrary
            # cohort changing across repeated identical requests.
            sql+=', '+', '.join(expr(fid)+' ASC' for fid in plan['group_by'])
    elif plan['mode']=='rows':sql+=' ORDER BY '+', '.join(expr(x) for x in plan['select'][:2])
    elif plan['group_by']:sql+=' ORDER BY '+', '.join(expr(x) for x in plan['group_by'])
    sql+=' LIMIT ?';params.append(plan['limit'])
    return {'sql':sql,'params':params,'columns':labels,'source_tables':_table_names(plan,ctx)}


def _read_source(project,table_id,ctx):
    t=ctx['tables'][table_id]
    cid=t['connection_id'];table=t['table']
    conns={c['id']:c for c in catalog.list_connections(project)}
    if cid not in conns:raise SemanticBlocked('Source connection was removed')
    conn=conns[cid]
    p=profiles.get_table(project,cid,table,include_stale=True)
    if not p or p['stale']:raise SemanticBlocked('Source profile changed; re-profile and reapprove')
    src=resolve_source(project,conn['source_path'],conn['kind'])
    if conn['kind']=='csv_folder':
        path=(src/(table+'.csv')).resolve()
        if not path.is_relative_to(src) or not path.is_file():raise SemanticBlocked('CSV source path no longer exists')
        headers,rows,meta=_sample_csv(path,EXECUTION_ROW_LIMIT)
    else:headers,rows,meta=_sample_sqlite(src,table,EXECUTION_ROW_LIMIT)
    if not meta['sample_complete']:
        raise SemanticBlocked('Table %s has more than %d rows; this version answers from tables up to that size and refuses partial answers' % (table,EXECUTION_ROW_LIMIT))
    if profiles.current_fingerprint(project,conn,table)!=p['fingerprint']:
        raise SemanticBlocked('Source changed while answering; re-profile and re-confirm approvals')
    return headers,rows


def _cell(value,numeric):
    if _is_missing(value):return None
    if not numeric:return str(value)
    if isinstance(value,(int,float)):return value
    try:return float(str(value).replace(',','').strip())
    except (ValueError,TypeError) as exc:raise SemanticBlocked('Invalid non-null numeric source value; refusing silent omission') from exc


def execute(project:Path,plan:dict,ctx:dict):
    cohort=plan.get('_session_cohort')
    latest=plan.get('_session_latest')
    start=time.monotonic()
    compiled=compile_plan(plan,ctx)
    # SQLite is an ephemeral query workspace, not a second user-facing SQL planner.
    db=sqlite3.connect(':memory:')
    try:
        _register_statistics(db)
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA temp_store=MEMORY')
        loaded=[]
        for tid in compiled['source_tables']:
            headers,rows=_read_source(project,tid,ctx)
            if len(headers)>200:raise SemanticBlocked('Source exceeds column limit')
            latest=plan.get('_session_latest')
            if latest and tid==plan['base_table']:
                date_name=ctx['fields'][latest['date']]['name']
                key_name=ctx['fields'][latest['key']]['name']
                tie_name=ctx['fields'][latest['tie']]['name']
                if not all(x in headers for x in (date_name,key_name,tie_name)):
                    raise SemanticBlocked('Approved latest-complaint columns missing from current source')
                d_i,k_i,t_i=(headers.index(x) for x in (date_name,key_name,tie_name))
                seen=set()
                for row in rows:
                    date=row[d_i]
                    if date is not None and str(date).strip():
                        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?',str(date)):
                            raise SemanticBlocked('Latest-date ordering requires consistent ISO date/datetime values')
                        triple=(str(row[k_i]),str(date),str(row[t_i]))
                        if triple in seen:raise SemanticBlocked('Non-unique date and complaint identifier tie; cannot determine latest safely')
                        seen.add(triple)
            numeric={ctx['fields'][fid]['name'] for fid in ctx['tables'][tid]['columns']
                     if ctx['fields'][fid]['role']=='MEASURE' or
                     any(m['field']==fid and m['aggregation'] in ('SUM','AVG','MIN','MAX')
                         for m in ctx['metrics'].values())}
            # All source columns are loaded so approved joins can use their physical keys.
            columns=', '.join(quote(h)+(' REAL' if h in numeric else ' TEXT') for h in headers)
            db.execute(f'CREATE TABLE {quote(physical(tid))} ({columns})')
            db.executemany(f'INSERT INTO {quote(physical(tid))} VALUES ('+','.join('?' for _ in headers)+')',
                           [tuple(_cell(v,h in numeric) for h,v in zip(headers,row)) for row in rows])
            loaded.append({'table_id':tid,'rows':len(rows),'complete':True})
        # Bound the number of SQLite VM operations and wall-clock time.
        steps=[0]
        def progress():
            steps[0]+=1
            return 1 if steps[0]>30000 or time.monotonic()-start>MAX_QUERY_SECONDS else 0
        db.set_progress_handler(progress,1000)
        try:
            cursor=db.execute(compiled['sql'],compiled['params'])
            result=cursor.fetchmany(plan['limit']+1)
        except sqlite3.OperationalError as exc:
            raise SemanticBlocked('Query execution was stopped or failed validation: '+str(exc)[:140]) from exc
        if len(result)>plan['limit']:raise SemanticBlocked('Result cap violated')
        # Recheck fingerprints after scan and before returning any result.
        for tid in compiled['source_tables']:
            t=ctx['tables'][tid]
            p=profiles.get_table(project,t['connection_id'],t['table'],include_stale=True)
            if not p or p['stale']:raise SemanticBlocked('Source changed during query; no result returned')
        return {'status':'answered','columns':compiled['columns'],
            'rows':[list(row) for row in result], 'row_count':len(result),
            # A validated, deterministic top-N grouping is a complete cohort
            # of the selected top N, even when the Nth result fills the limit.
            'cohort_complete':(bool(plan.get('_ranked_customer_cohort') or plan.get('_ranked_entity_cohort')) or
                               (len(result)<plan['limit'] if plan['mode']!='count' else False)),
            'sql':compiled['sql'],'parameters':compiled['params'],
            'semantic_plan':plan,'provenance':{'sources':loaded,'registry_signature':ctx['revision_signature'],
                'approved_relationships':plan['joins']+[e['edge_id'] for e in plan['exists']],
                'approved_metric':plan['metric'] if not ctx['metrics'].get(plan['metric'],{}).get('technical_only') else None,
                'technical_metric':plan['metric'] if ctx['metrics'].get(plan['metric'],{}).get('technical_only') else None,
                'technical_not_business_approved':bool(ctx['metrics'].get(plan['metric'],{}).get('technical_only')),
                'result_is_bounded':True,
                'session_cohort_applied':cohort is not None,'session_latest_applied':latest is not None,
                'limitations':['Answers read whole tables up to 1,000,000 rows','Current local self-attested approvals only',
                               'No temporal semantics, multi-hop aggregation, or production IAM']}}
    finally:db.close()
