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
# Generic words: row_id, line_no and seq are counters, not the name of a business entity.
GENERIC = {'id','key','code','ref','reference','identifier','uuid','no','number','record','row','line','seq','sequence','serial','sr','sno','idx','index','num','nbr'}

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
    # Through the prepared copy: data fixes applied, and no CSV re-parsing for large tables.
    from binggio import prepared
    return prepared.read(project,conn,table,limit)

def _wanted_columns(p: dict, approved_keys=()) -> list[tuple[dict,bool]]:
    """Columns worth comparing, decided from the profile alone (names, roles, types): (column, soft)."""
    out=[]
    for col in p['columns']:
        role=col['semantic_role_candidate']
        approved=col['name'] in approved_keys
        soft=not (approved or _keyish(col['name'],role))
        if soft and (role not in SOFT_CHILD_ROLES or col.get('observed_type') not in ('text','integer')):continue
        out.append(({**col,'approved_key':approved},soft))
    return out


def _observe(col: dict, soft: bool, values: list, p: dict) -> dict | None:
    """Distinct normalised values of one column; each raw value is normalised once."""
    from collections import Counter
    counts=Counter(values)
    distinct={};missing=0;nonnull=0;example=None
    for raw,n in counts.items():          # first-seen order: deterministic
        v=_value(raw)
        if v is None:missing+=n
        else:
            nonnull+=n
            distinct.setdefault(v,None)
            if example is None:example=str(raw).strip()
    if soft and len(distinct)<MIN_DISTINCT_CHILD:return None
    return {'name':col['name'],'role':col['semantic_role_candidate'],'soft':soft,'approved_key':bool(col.get('approved_key')),
      'family':_family(col['name']),'distinct':set(distinct),'order':list(distinct),'example':example,'non_null':nonnull,
      'missing':missing,'unique':len(distinct)/nonnull if nonnull else 0.0,
      'sample_rows':len(values),'sample_complete':p['sample_complete']}


def _column_observations(headers: list[str], rows: list[list[Any]], p: dict) -> list[dict]:
    result=[]
    for col,soft in _wanted_columns(p):
        if col['name'] not in headers:continue
        i=headers.index(col['name'])
        o=_observe(col,soft,[row[i] for row in rows],p)
        if o is None:continue
        if len(result)>=MAX_IDENTIFIER_COLUMNS_PER_TABLE:break  # very wide tables: the first 64 identifier-like columns
        result.append(o)
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

def _pct(x):
    return ('%.1f' % (100*x)).rstrip('0').rstrip('.')


def _parent_candidates(table: str, cols: list[dict], why: list | None=None) -> list[dict]:
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
        inferred=(_key_owned_by_table(table,c['name']) and (own_key is None or own_key==c['name'])) or c['name']==own_key \
            or c.get('approved_key')   # a person approved it as this table's key
        reason=None
        if not (known or inferred):reason='its name does not say it identifies the rows of %s' % table
        elif c['non_null']<MIN_DISTINCT_CHILD or len(c['distinct'])<MIN_DISTINCT_CHILD:reason='fewer than %d different values' % MIN_DISTINCT_CHILD
        elif c['missing']:reason='%s of %s rows read are empty; the key a table is known by cannot be empty' % ('{:,}'.format(c['missing']),'{:,}'.format(c['sample_rows']))
        elif c['unique']<0.999:
            rep=c['non_null']-len(c['distinct'])
            reason='%s of %s values repeat (%s%%); the key a table is known by must be unique' % ('{:,}'.format(rep),'{:,}'.format(c['non_null']),_pct(rep/c['non_null']))
        if reason:
            if why is not None and (known or inferred or c['role'] in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE')):
                why.append({'column':c['name'],'why':reason,'near':bool(known or inferred)})
            continue
        result.append(c)
    return result

def _evaluate(child_table: dict, child: dict, parent_table: dict, parent: dict) -> dict | None:
    return _evaluate_why(child_table,child,parent_table,parent)[0]


def _evaluate_why(child_table: dict, child: dict, parent_table: dict, parent: dict, overlap: int | None=None):
    """(candidate, None) or (None, plain reason the pair was not proposed, coverage)."""
    r=_evaluate_inner(child_table,child,parent_table,parent,overlap)
    return r if isinstance(r,tuple) else (r,None)


def _evaluate_inner(child_table: dict, child: dict, parent_table: dict, parent: dict, overlap: int | None=None):
    if child_table['id']==parent_table['id']:return (None,'same table')
    if len(child['distinct'])<MIN_DISTINCT_CHILD:return (None,'fewer than %d different values' % MIN_DISTINCT_CHILD)
    if overlap is None:overlap=len(child['distinct'] & parent['distinct'])
    if overlap<MIN_DISTINCT_CHILD:return (None,'%d value(s) in common' % overlap)
    # Both distinct-value coverage and row-level coverage are sampled evidence;
    # do not overstate this as full-table referential integrity.
    coverage=overlap/len(child['distinct'])
    if coverage<MIN_OVERLAP:return (None,'only %s%% of its values are found there; a link needs at least %d%%' % (_pct(coverage),round(100*MIN_OVERLAP)))
    psub=_identifier_subject(parent['name'])
    lexical=(child['family']==parent['family'] and child['family'] is not None) or (
        _identifier_subject(child['name']) is not None and
        _identifier_subject(child['name'])==psub and
        _key_owned_by_table(parent_table['table'],parent['name'])) or (
        # the parent's own key named inside the child's name: vehicle_vin -> vehicles.vin
        psub is not None and parent['name']==parent_table.get('own_key') and
        set(psub.split('_'))<={_singular(w) for w in _tokens(child['name'])})
    if child.get('soft') and not (_singular(_tokens(parent_table['table'])[-1]) in {_singular(w) for w in _tokens(child['name'])}
                                  or (child['family'] is not None and child['family']==_subject_family(parent_table['table']))):
        return (None,'not an identifier column, and its name does not point at that table')  # a non-identifier column links only to a table its name points at (assigned_agent -> agents)
    if not lexical and coverage<0.90:return (None,'the names do not match and %s%% of its values are found there; %d%% is needed when names differ' % (_pct(coverage),90))
    if not lexical and child['family'] not in (None,parent['family']):
        # 'case' is an overloaded operational identifier (complaint, risk, ticket).
        # Permit only very strong sampled overlap and explicitly flag ambiguity.
        if not (child['family']=='case' and coverage>=0.95):return (None,'the names point at different things (%s, %s)' % (child['family'],parent['family'] or parent_table['table']))
    completeness=child['sample_complete'] and parent['sample_complete']
    # Bounded, deterministic evidence score, not a statistical probability.
    score=(0.49*coverage+0.23*(1.0 if lexical else 0.45)+
           0.20*parent['unique']+0.08*(1.0 if completeness else 0.5))
    score=round(min(score,0.99),3)
    if score<MIN_SCORE:return (None,'evidence score %.2f is below %.2f' % (score,MIN_SCORE))
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

def _digits(v: str) -> str:
    d=re.sub(r'\D','',v).lstrip('0')
    return d


def _probe(order: list, n: int=256) -> list:
    """Up to n values spread evenly over a column's distinct values (data order)."""
    if len(order)<=n:return order
    step=len(order)/n
    return [order[int(k*step)] for k in range(n)]


def discover(project: Path, *, max_tables: int=MAX_TABLES, limit: int=SAMPLE_LIMIT) -> dict:
    from binggio import prepared
    project=Path(project).resolve()
    tables=catalog.list_tables(project,limit=max_tables+1)
    if not tables:raise ConnectionErrorSafe('No registered tables')
    if len(tables)>max_tables:raise ConnectionErrorSafe('Discovery is limited to %d tables per workspace' % max_tables)
    conns={x['id']:x for x in catalog.list_connections(project)}
    approved_keys=_approved_keys(project)
    table_data=[]
    skipped=[]
    report_tables=[]
    for t in tables:
        p=profiles.get_table(project,t['connection_id'],t['table_name'],include_stale=True)
        entry={'connection_id':t['connection_id'],'table':t['table_name']}
        if not p:
            # A table that could not be profiled (empty file, duplicate headers) is reported by
            # profiling; it cannot be reviewed, so discovery leaves it out instead of stopping.
            skipped.append(t['connection_id']+'::'+t['table_name'])
            report_tables.append({**entry,'status':'skipped','why':'not profiled (profiling could not read it)'});continue
        if p['stale']:raise ConnectionErrorSafe('Table %s changed since it was profiled. Run profile first.' % t['table_name'])
        if p['sample_limit']!=limit:raise ConnectionErrorSafe('Table %s was profiled with other settings. Run profile --force first.' % t['table_name'])
        wanted=_wanted_columns(p,approved_keys.get((t['connection_id'],t['table_name']),()))
        # Key overlap is measured on up to DISCOVERY_ROW_LIMIT rows (profiles use a smaller sample),
        # so a child key that only appears beyond row 2,500 of its parent table is still found.
        conn=conns[t['connection_id']]
        cols=[]
        if wanted:
            values=prepared.column_values(project,conn,t['table_name'],[c['name'] for c,_ in wanted],DISCOVERY_ROW_LIMIT)
            for col,soft in wanted:
                o=_observe(col,soft,values[col['name']],p)
                if o is None:continue
                if len(cols)>=MAX_IDENTIFIER_COLUMNS_PER_TABLE:break  # very wide tables: the first 64 identifier-like columns
                cols.append(o)
        # Reject a source changing during a discovery run; all-or-nothing publish.
        if profiles.get_table(project,t['connection_id'],t['table_name'])['stale']:
            raise ConnectionErrorSafe('Source changed during discovery; re-profile first')
        why=[]
        parents=_parent_candidates(t['table_name'],cols,why)
        total=prepared.row_count(project,conn,t['table_name']) if wanted else 0
        read_n=max([c['sample_rows'] for c in cols] or [0])
        for pc in parents+[c for c in cols if c['name'] in {w['column'] for w in why if w['near']}]:
            pc['fraction']=min(1.0,read_n/total) if total else 1.0
        for pc in parents:
            # A parent table larger than the rows read is checked on all its rows (see KeyIndex).
            if total>read_n:pc['index']=(conn,t['table_name'])
        report_tables.append({**entry,'status':'checked','rows_read':read_n,'rows':total,
                              'columns_compared':[c['name'] for c in cols],'parent_keys':[c['name'] for c in parents],
                              'not_parent_keys':why,
                              'why':None if cols else 'no identifier-like columns (names such as *_id, *_no, *_code, *_ref, or a key the profile found)'})
        table_data.append({'id':t['connection_id']+'::'+t['table_name'],
            'connection_id':t['connection_id'],'connection_name':t['connection_name'],
            'schema':t['schema_name'],'table':t['table_name'],
            'columns':cols,'parents':parents,'own_key':_own_key(cols),'near_parents':[c for c in cols if c['name'] in {w['column'] for w in why if w['near']}],
            'why':{w['column']:w['why'] for w in why},
            'fingerprint':p['fingerprint'],'profiled_at':p['profiled_at'],
            'sample_rows':p['sample_rows'],'sample_complete':p['sample_complete']})
    parents=[(pt,pc) for pt in table_data for pc in pt['parents']]
    near=[(pt,pc) for pt in table_data for pc in pt['near_parents']]
    edges=[];columns_report=[]
    digit_cache={}
    indexes={}
    def index_of(parent):
        k=id(parent)
        if k not in indexes:indexes[k]=prepared.KeyIndex(project,parent['index'][0],parent['index'][1],parent['name'])
        return indexes[k]
    def hits_in(parent,values):
        if parent.get('index'):return index_of(parent).hits(values)
        return sum(1 for v in values if v in parent['distinct'])
    def digits_of(c):
        k=id(c)
        if k not in digit_cache:digit_cache[k]={d for d in (_digits(v) for v in c['distinct']) if d}
        return digit_cache[k]
    for child_t in table_data:
        for child in child_t['columns']:
            if child['family'] is None and child['role']=='PRIMARY_KEY_CANDIDATE' and not _references_elsewhere(child_t['table'],child):continue
            probe=_probe(child['order'])
            found=[];best=None;tried=0
            for parent_t,parent in parents:
                if parent_t['id']==child_t['id']:continue
                hits=hits_in(parent,probe)
                if hits==0:continue
                tried+=1
                # A probe of up to 256 values estimates coverage; pairs far below the 45% needed are
                # not worth an exact comparison (a pair at 45% shows about 115 of 256).
                if len(probe)<len(child['order']) and hits/len(probe)<0.2:
                    reason='only about %s%% of its values are found there; a link needs at least %d%%' % (_pct(hits/len(probe)),round(100*MIN_OVERLAP))
                    cand=None;cov=hits/len(probe)
                else:
                    ov=hits_in(parent,child['order']) if parent.get('index') else len(child['distinct']&parent['distinct'])
                    cand,reason=_evaluate_why(child_t,child,parent_t,parent,ov)
                    cov=ov/len(child['distinct']) if child['distinct'] else 0
                    if cand and parent.get('index'):
                        cand['evidence']['reasons'][0]+=' (all %s rows of %s checked)' % ('{:,}'.format(prepared.row_count(project,parent['index'][0],parent['index'][1])),parent_t['table'])
                        cand['evidence']['parent_checked_all_rows']=True
                if cand:found.append(cand)
                elif best is None or cov>best['coverage']:
                    best={'table':parent_t['table'],'column':parent['name'],'connection_id':parent_t['connection_id'],'coverage':cov,'why':reason}
            edges.extend(found)
            if found or (child['soft'] and not (best and best['coverage']>=MIN_OVERLAP)):continue
            entry={'connection_id':child_t['connection_id'],'table':child_t['table'],'column':child['name'],'parents_compared':tried,
                   'best':({**best,'coverage_pct':round(100*best['coverage'],1)} if best else None)}
            # Near misses: keys that would be parents but repeat or have gaps (the 0.1% duplicates case).
            for parent_t,parent in near:
                if parent_t['id']==child_t['id']:continue
                hits=sum(1 for v in probe if v in parent['distinct'])
                if probe and hits/len(probe)/parent.get('fraction',1.0)>=0.45:
                    entry.setdefault('near_misses',[]).append({'table':parent_t['table'],'column':parent['name'],'connection_id':parent_t['connection_id'],
                        'coverage_pct':round(min(100.0,100*hits/len(probe)/parent.get('fraction',1.0)),1),'estimated':parent.get('fraction',1.0)<1,
                        'why':parent_t['why'].get(parent['name'],'not usable as a parent key')})
            # Same keys written differently (CUST-000123 here, C0000123 there): compare digits only,
            # against parent keys whose names point at the same thing.
            if not (best and best['coverage']>=MIN_OVERLAP) and child['role']!='PRIMARY_KEY_CANDIDATE' and len(child['order'])>=20:
                cd={d for d in (_digits(v) for v in probe) if d}
                csub=_identifier_subject(child['name']);cfam=child['family']
                for parent_t,parent in parents:
                    if parent_t['id']==child_t['id']:continue
                    psub=_identifier_subject(parent['name'])
                    # only keys whose names say they are the same thing (cust_ref and customer_id)
                    related=(cfam is not None and cfam==parent['family']) or (csub is not None and csub==psub)
                    if not related or len(cd)<MIN_DISTINCT_CHILD:continue
                    rate=min(1.0,sum(1 for d in cd if d in digits_of(parent))/len(cd)/parent.get('fraction',1.0))
                    if rate>=0.6:
                        ex_c=child.get('example') or child['order'][0];ex_p=parent.get('example') or parent['order'][0]
                        entry['format_hint']={'table':parent_t['table'],'column':parent['name'],'connection_id':parent_t['connection_id'],
                            'digits_match_pct':round(100*rate,1),'example_here':ex_c,'example_there':ex_p,
                            'advice':'These may be the same keys written differently (%s here, %s in %s.%s): their digits match. If they are, '
                                     'add the data fix "Keep digits only" (without leading zeros) to both columns and run discovery again.' % (ex_c,ex_p,parent_t['table'],parent['name'])}
                        break
            columns_report.append(entry)
    for ix in indexes.values():ix.close()
    # Same child identifier can overlap multiple parents. Keep ambiguity visible;
    # never automatically certify one parent over another.
    edges.sort(key=lambda x:(-x['score'],x['child_table'],x['child_column'],x['parent_table']))
    truncated=len(edges)>MAX_EDGES
    edges=edges[:MAX_EDGES]
    linked={(e['child_connection_id'],e['child_table']) for e in edges}|{(e['parent_connection_id'],e['parent_table']) for e in edges}
    for r in report_tables:
        r['linked']=(r['connection_id'],r['table']) in linked
    nodes=[{k:t[k] for k in ('id','connection_id','connection_name','schema','table','fingerprint','profiled_at','sample_rows','sample_complete')}
           for t in table_data]
    return {'version':VERSION,'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
      'nodes':nodes,'edges':edges,'skipped_tables':skipped,'limits':{'tables':MAX_TABLES,'sample_rows_per_table':limit,'max_edges':MAX_EDGES,'truncated':truncated},
      'attempts':{'tables':report_tables,'columns':columns_report,'rules':{'min_coverage_pct':round(100*MIN_OVERLAP),
                  'min_coverage_names_differ_pct':90,'min_score':MIN_SCORE,'rows_read_per_table':DISCOVERY_ROW_LIMIT}},
      'method':'read_only_bounded_identifier_overlap_plus_schema_signals',
      'pairing':'probe then exact overlap',
      'disclaimer':'Candidate graph only. No relationships approved; heuristic scores are not probabilities. No source rows retained.'}


def _approved_keys(project: Path) -> dict:
    """Columns a person approved as the table's own key: discovery uses them as parent keys."""
    try:
        from binggio.registry import store as registry
        with registry._LOCK,registry._db(project) as db:
            rows=db.execute("SELECT approved_json FROM artifacts WHERE kind='column_role' AND status='approved'").fetchall()
    except Exception:
        return {}
    out={}
    for (raw,) in rows:
        try:a=__import__('json').loads(raw)
        except (TypeError,ValueError):continue
        if a.get('role')=='PRIMARY_KEY_CANDIDATE':out.setdefault((a['connection_id'],a['table']),set()).add(a['column'])
    return out
