# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Dependency-aware governance: re-evaluate only relationships touching a reviewed column.
Never auto-approve on a name match alone; explicit technical policy opt-in required.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
from binggio.registry import store, autonomy


def affected_relationships(project: Path, connection_id: str, table: str, column: str):
    edges, truncated = autonomy._all_edges(project)
    affected = [e for e in edges if
        (e['child_connection_id'],e['child_table'],e['child_column']) == (connection_id,table,column) or
        (e['parent_connection_id'],e['parent_table'],e['parent_column']) == (connection_id,table,column)]
    return affected, truncated, edges


def evaluate(project: Path, connection_id: str, table: str, column: str, *,apply: bool = False):
    project=Path(project)
    affected, truncated, edges=affected_relationships(project,connection_id,table,column)
    state=autonomy.status(project)
    if apply and (not state['enabled'] or truncated):
        apply=False
    existing={r['subject_key']:r for r in store._rows(project,'relationship')}
    roles={r['subject_key']:r for r in store._rows(project,'column_role')}
    competing=Counter((e['child_connection_id'],e['child_table'],e['child_column']) for e in edges
                      if e.get('freshness')=='current' and e.get('score',0)>=0.90)
    outcomes=[]
    for edge in affected:
        existing_decision=existing.get(edge['id'])
        reasons=[]
        if existing_decision:
            reasons.append('Existing governance decision preserved; no automated overwrite')
        for prefix in ('child','parent'):
            cid,tbl,col=(edge[f'{prefix}_connection_id'],edge[f'{prefix}_table'],edge[f'{prefix}_column'])
            decision=roles.get(cid+'::'+tbl+'::'+col)
            if not decision or decision['effective_status']!='approved':
                reasons.append(prefix.title()+' column role is not currently approved')
            elif decision['approved'].get('role') not in ('IDENTIFIER','PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE'):
                reasons.append(prefix.title()+' approved role is not an identifier role')
        try:
            cp=store._profile(project,edge['child_connection_id'],edge['child_table'])
            pp=store._profile(project,edge['parent_connection_id'],edge['parent_table'])
            reasons.extend(autonomy._edge_check(edge,cp,pp,competing[(edge['child_connection_id'],edge['child_table'],edge['child_column'])]>1))
        except Exception:
            reasons.append('Evidence profiles unavailable; review required')
        if truncated:reasons.append('Graph evidence scan truncated')
        if not state['enabled']:reasons.append('Strict auto-certification policy disabled')
        approved=False
        if apply and not reasons:
            item={'kind':'relationship','id':'relationship:'+edge['id'],'edge_id':edge['id'],
                  'evidence':{'score':edge['score'],'freshness':edge['freshness']}}
            try:
                autonomy._approve(project,item)
                approved=True
            except (store.ReviewConflict,store.ReviewValidation) as exc:
                reasons.append('Certification blocked: '+str(exc))
        outcomes.append({'edge_id':edge['id'],'child':edge['child_table']+'.'+edge['child_column'],
                         'parent':edge['parent_table']+'.'+edge['parent_column'],
                         'status':'auto_approved' if approved else ('already_decided' if existing_decision else 'review_required'),
                         'reasons':list(dict.fromkeys(reasons))})
    return {'affected_count':len(outcomes),'policy_enabled':state['enabled'],
            'truncated':truncated,'results':outcomes,'auto_approved':sum(x['status']=='auto_approved' for x in outcomes)}
