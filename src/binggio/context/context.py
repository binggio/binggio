# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Build queryable context exclusively from CURRENT Phase7 human approvals.

The Phase6 graph and Phase5 roles are evidence, never executable authority.
No ground-truth oracle, LLM-generated joins, or unapproved columns enter context.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from binggio.catalog import catalog
from binggio.profiling import store as profiles
from binggio.registry import store as governance
from .standard_analytics import generate as technical_metrics

class SemanticBlocked(ValueError):
    pass


def table_id(cid: str, table: str) -> str:
    return f'{cid}::{table}'


def field_id(cid: str, table: str, column: str) -> str:
    return f'{cid}::{table}::{column}'


def _resolve_concepts(items,tables,fields):
    """v18.1: approved business concepts expressed in approved table/field IDs.

    A concept never adds a table or column to the universe: its primary table must
    already be approved, and attributes or secondary tables that are not approved are
    left out and reported in concept_issues."""
    concepts={};issues=[]
    for item in items:
        val=item.get('approved') or {}
        p=val.get('primary') or {}
        primary=table_id(p.get('connection_id',''),p.get('table',''))
        if primary not in tables:
            issues.append({'concept':val.get('name'),'id':item['id'],'reason':'primary table '+str(p.get('table'))+' has no approved columns'});continue
        secondary=[]
        for s in val.get('secondary') or []:
            tid=table_id(s['connection_id'],s['table'])
            if tid in tables:secondary.append({'table_id':tid,'use':s['use']})
            else:issues.append({'concept':val['name'],'id':item['id'],'reason':'secondary table '+s['table']+' has no approved columns'})
        attributes=[]
        for a in val.get('attributes') or []:
            fid=field_id(a['connection_id'],a['table'],a['column'])
            if fid in fields:attributes.append({'name':a['name'],'aliases':list(a.get('aliases') or []),'field':fid})
            else:issues.append({'concept':val['name'],'id':item['id'],'reason':'attribute '+a['name']+' uses unapproved column '+a['table']+'.'+a['column']})
        key=field_id(p['connection_id'],p['table'],p.get('key_column',''))
        concepts[item['id']]={'id':item['id'],'name':val['name'],'aliases':list(val.get('aliases') or []),
            'domain':val.get('domain',''),'primary_table':primary,'key_field':key if key in fields else None,
            'secondary':secondary,'attributes':attributes,'revision':item['revision']}
    return concepts,issues


def _resolve_terms(items,tables,fields):
    """v18.3: approved governed terms in approved table/field IDs, and the approved as-of date.

    A term is usable only when every column it uses is approved, and, if it is
    date-relative or uses the latest row, when an approved as-of date exists.
    Unusable terms are reported in term_issues and stay unusable (questions using
    them are refused, never approximated)."""
    as_of=None;terms={};issues=[]
    for item in items:
        if item['kind']=='as_of':
            val=item['approved'];as_of={'id':item['id'],'date':val['date'],'description':val.get('description',''),'revision':item['revision']}
    for item in items:
        if item['kind']!='term':continue
        val=item['approved'];t=val['table'];tid=table_id(t['connection_id'],t['table'])
        def fid(col):return field_id(t['connection_id'],t['table'],col)
        cols=[c['column'] for c in val['conditions']]+[c['value_column'] for c in val['conditions'] if c.get('value_column')]
        if val.get('latest'):cols+=[val['latest']['partition_column'],val['latest']['date_column']]
        missing=[c for c in dict.fromkeys(cols) if fid(c) not in fields]
        if tid not in tables or missing:
            issues.append({'term':val['name'],'id':item['id'],'reason':'uses unapproved column(s) '+', '.join(t['table']+'.'+c for c in missing or ['(table)'])});continue
        if val.get('needs_as_of') and as_of is None:
            issues.append({'term':val['name'],'id':item['id'],'reason':'needs an approved as-of date'});continue
        conditions=[]
        for c in val['conditions']:
            x={'field':fid(c['column']),'op':c['op']}
            if 'value' in c:x['value']=c['value']
            if c.get('value_column'):x['value_field']=fid(c['value_column'])
            if 'as_of_offset_days' in c:x['as_of_offset_days']=c['as_of_offset_days']
            conditions.append(x)
        terms[item['id']]={'id':item['id'],'name':val['name'],'aliases':list(val.get('aliases') or []),
            'description':val['description'],'domain':val.get('domain',''),'table_id':tid,'conditions':conditions,
            'latest':({'partition':fid(val['latest']['partition_column']),'date':fid(val['latest']['date_column'])} if val.get('latest') else None),
            'needs_as_of':bool(val.get('needs_as_of')),'status':'approved','revision':item['revision']}
    return terms,as_of,issues


def approved_context(project: Path) -> dict:
    from binggio.profiling import store as _profiles
    with _profiles.evaluation_scope():
        return _approved_context(Path(project).resolve())


def _approved_context(project: Path) -> dict:
    approved=governance.approved_registry(project)['items']
    catalog_tables={table_id(t['connection_id'],t['table_name']):t for t in catalog.list_tables(project,limit=5000)}
    connections={c['id']:c for c in catalog.list_connections(project)}
    tables={}
    fields={}
    metrics={}
    joins={}
    source_profiles={}

    def ensure_table(cid,table):
        key=table_id(cid,table)
        if key not in catalog_tables or cid not in connections:
            raise SemanticBlocked('Approved source is missing from the registered catalog')
        if key not in source_profiles:
            profile=profiles.get_table(project,cid,table,include_stale=True)
            if not profile or profile['stale']:
                raise SemanticBlocked('Approved source profile is unavailable or stale')
            source_profiles[key]=profile
        if key not in tables:
            tables[key]={'id':key,'connection_id':cid,'table':table,'connection_kind':connections[cid]['kind'],
                         'columns':[], 'sample_rows':source_profiles[key]['sample_rows'],
                         'sample_complete':source_profiles[key]['sample_complete']}
        return key

    def ensure_field(cid,table,column,*,authority,role='UNKNOWN'):
        key=ensure_table(cid,table)
        c=next((x for x in source_profiles[key]['columns'] if x['name']==column),None)
        if not c:raise SemanticBlocked('Approved source column no longer exists')
        fid=field_id(cid,table,column)
        if fid not in fields:
            fields[fid]={'id':fid,'table_id':key,'name':column,'role':role,
                         'observed_type':c.get('observed_type','unknown'),'authorities':[authority]}
            tables[key]['columns'].append(fid)
        else:
            if authority not in fields[fid]['authorities']:fields[fid]['authorities'].append(authority)
            if role!='UNKNOWN':fields[fid]['role']=role
        return fid

    concept_items=[];term_items=[]
    for item in approved:
        val=item.get('approved') or {}
        if item['kind']=='concept':
            concept_items.append(item);continue  # resolved after all approved fields are known (v18.1)
        if item['kind'] in ('term','as_of'):
            term_items.append(item);continue  # v18.3
        if item['kind']=='column_role':
            fid=ensure_field(val['connection_id'],val['table'],val['column'],authority=item['id'],role=val['role'])
            fields[fid]['logical_type_override']=val.get('logical_type_override','source')
            fields[fid]['logical_type_executable']=False
        elif item['kind']=='relationship':
            if val.get('cardinality') in (None,'unknown','many_to_many'):
                continue  # no safe cardinality semantics for this POC
            left=ensure_field(val['child_connection_id'],val['child_table'],val['child_column'],authority=item['id'])
            right=ensure_field(val['parent_connection_id'],val['parent_table'],val['parent_column'],authority=item['id'])
            joins[item['id']]={'id':item['id'],'left':left,'right':right,'left_table':fields[left]['table_id'],
                'right_table':fields[right]['table_id'],'cardinality':val['cardinality'],
                'mapping_overridden':bool(val.get('mapping_overridden')),
                'temporal_rule':val.get('temporal_rule',''), 'revision':item['revision']}
        elif item['kind']=='metric':
            if val.get('aggregation') not in ('SUM','AVG','MIN','MAX','COUNT','COUNT_DISTINCT'):continue
            if len(val.get('sources',[]))!=1:continue  # multi-source definitions are non-executable metadata
            ref=val['sources'][0]
            fid=ensure_field(ref['connection_id'],ref['table'],ref['column'],authority=item['id'])
            metrics[item['id']]={'id':item['id'],'name':val['name'],'domain':val['domain'],
                'aggregation':val['aggregation'],'field':fid,'table_id':fields[fid]['table_id'],
                'revision':item['revision'], 'owner':val.get('owner','')}

    concepts,concept_issues=_resolve_concepts(concept_items,tables,fields)
    governed_terms,as_of,term_issues=_resolve_terms(term_items,tables,fields)
    business_metric_count=len(metrics)
    # Only approved numeric MEASURE roles receive standard, explicitly technical,
    # on-demand analytics. No policy can turn them into certified business KPIs.
    for mid, entry in technical_metrics(fields).items():
        metrics[mid]=entry

    revision_signature=hashlib.sha256(json.dumps([
        (x['id'],x['revision'],x['kind']) for x in approved],sort_keys=True).encode()).hexdigest()
    return {'tables':tables,'fields':fields,'metrics':metrics,'joins':joins,
        'concepts':concepts,'concept_issues':concept_issues,
        'governed_terms':governed_terms,'as_of':as_of,'term_issues':term_issues,
        'revision_signature':revision_signature,
        'summary':{'tables':len(tables),'approved_fields':len(fields),'approved_joins':len(joins),
                   'executable_metrics':len(metrics),'approved_business_metrics':business_metric_count,
                   'standard_technical_metrics':len(metrics)-business_metric_count,'excluded_unapproved':True},
        'limitations':['Only current Phase7 approvals are usable',
                       'Standard statistical operations are technical and not certified business metrics',
                       'Multi-source metric expressions and temporal join rules are not executable',
                       'Local bounded CSV/SQLite execution; no production authorization']}
