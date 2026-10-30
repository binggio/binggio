# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Atomic, read-only-to-source context graph snapshot store.

No approvals in Phase 6. Candidate edges are overwritten on each successful
full discovery, and prior snapshot remains intact if discovery fails.
"""
from __future__ import annotations
import json
import sqlite3
import threading
from pathlib import Path
from binggio.catalog import catalog
from binggio.catalog.connectors import ConnectionErrorSafe
from binggio.profiling import store as profiles
from .discovery import discover
from . import VERSION

_LOCK=threading.RLock()

def _db(project: Path):
    path=Path(project).resolve()/'data'/'phase6_graph'/'graph.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('''CREATE TABLE IF NOT EXISTS snapshot (
        id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL,
        generated_at TEXT NOT NULL,summary_json TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,table_name TEXT NOT NULL,
        payload_json TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS edges (
        id TEXT PRIMARY KEY,child_connection_id TEXT NOT NULL,child_table TEXT NOT NULL,
        parent_connection_id TEXT NOT NULL,parent_table TEXT NOT NULL,
        score REAL NOT NULL,payload_json TEXT NOT NULL)''')
    db.commit()
    return db

_EXTENSION_TERMINAL={'master','history','log','directory','lines','table','data','details','list','register','records'}


def _decided_edge_ids(project):
    try:
        from binggio.registry import store as registry
        with registry._LOCK, registry._db(project) as db:
            return {r[0] for r in db.execute("SELECT subject_key FROM artifacts WHERE kind='relationship'")}
    except Exception:
        return set()


def _previous_edges(project):
    try:
        with _LOCK,_db(project) as db:
            return {r['id']:json.loads(r['payload_json']) for r in db.execute('SELECT id,payload_json FROM edges')}
    except Exception:
        return {}


def _words(s):
    import re
    s=re.sub(r'([a-z])([A-Z])',r'\1_\2',str(s))
    return [w for w in re.split(r'[^a-z0-9]+',s.lower()) if w]


def _sing(w):
    if w.endswith('ies') and len(w)>4:return w[:-3]+'y'
    if w.endswith('s') and not w.endswith(('ss','us')) and len(w)>3:return w[:-1]
    return w


def _home_rank(table,column,incoming):
    """Lower is more likely the entity's home table (customer_master over customer_contacts)."""
    key={_sing(w) for w in _words(column)}
    extra=[w for w in (_sing(x) for x in _words(table)) if w not in key and w not in _EXTENSION_TERMINAL]
    return (len(extra),-incoming,table)


def resolve_one_to_one(edges,protected):
    """Two tables keyed 1:1 on the same identifier produce a candidate in each direction, and the
    extension table (contacts, profile, preferences) then competes with the entity's home table as
    the parent of every other table. Keep one direction (extension -> home), and drop links to the
    extension that duplicate a link to the home. Decided edges (approved, rejected, revoked) are
    never dropped or changed."""
    by_pair={(e['child_table'],e['child_column'],e['parent_table'],e['parent_column']):e for e in edges}
    incoming={}
    for e in edges:incoming[e['parent_table']]=incoming.get(e['parent_table'],0)+1
    drop=set();ext_of={}
    for e in edges:
        rev=by_pair.get((e['parent_table'],e['parent_column'],e['child_table'],e['child_column']))
        if not rev or e['id'] in drop or rev['id'] in drop:continue
        child_rank=_home_rank(e['child_table'],e['child_column'],incoming.get(e['child_table'],0))
        parent_rank=_home_rank(e['parent_table'],e['parent_column'],incoming.get(e['parent_table'],0))
        keep,lose=(e,rev) if parent_rank<=child_rank else (rev,e)
        if lose['id'] not in protected:drop.add(lose['id'])
        ext_of[(keep['child_table'],keep['child_column'])]=(keep['parent_table'],keep['parent_column'])
        if keep['id'] not in protected:
            keep.setdefault('evidence',{})['cardinality_hint']='one_to_one'
            keep['evidence'].setdefault('reasons',[]).append('One-to-one with %s: this table extends it' % keep['parent_table'])
    # A link from a table's own key is one-to-one even when the reverse was not proposed
    # (dealer_directory.code -> dealers.dealer_id: the directory copies or extends dealers).
    for e in edges:
        ev=e.get('evidence') or {}
        if ev.get('child_is_own_key') and e['id'] not in drop and (e['child_table'],e['child_column']) not in ext_of:
            ext_of[(e['child_table'],e['child_column'])]=(e['parent_table'],e['parent_column'])
            if e['id'] not in protected and ev.get('cardinality_hint')!='one_to_one':
                ev['cardinality_hint']='one_to_one'
                ev.setdefault('reasons',[]).append('One-to-one with %s: this table extends it' % e['parent_table'])
    for e in edges:
        home=ext_of.get((e['parent_table'],e['parent_column']))
        if not home or e['id'] in protected or e['id'] in drop:continue
        twin=by_pair.get((e['child_table'],e['child_column'],home[0],home[1]))
        if twin and twin['score']>=e['score']-0.02 and twin['id'] not in drop:drop.add(e['id'])
    return [e for e in edges if e['id'] not in drop]


def run_all(project: Path) -> dict:
    project=Path(project).resolve()
    # All sampling/validation before any DB mutation.
    graph=discover(project)
    protected=_decided_edge_ids(project)
    graph['edges']=resolve_one_to_one(graph['edges'],protected)
    present={e['id'] for e in graph['edges']}
    tables={(n['connection_id'],n['table']) for n in graph['nodes']}
    for eid,old in _previous_edges(project).items():
        # A decided edge that today's heuristics no longer propose keeps its recorded evidence;
        # if the data changed, the profiles' fingerprints make its approval stale anyway.
        if eid in protected and eid not in present and (old['child_connection_id'],old['child_table']) in tables \
                and (old['parent_connection_id'],old['parent_table']) in tables:
            graph['edges'].append(old)
    graph['edges'].sort(key=lambda x:(-x['score'],x['child_table'],x['child_column'],x['parent_table']))
    summ={'version':graph['version'],'generated_at':graph['generated_at'],
          'node_count':len(graph['nodes']),'candidate_edges':len(graph['edges']),
          'skipped_tables':graph.get('skipped_tables',[]),
          'method':graph['method'],'disclaimer':graph['disclaimer'],'limits':graph['limits']}
    with _LOCK,_db(project) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM edges')
        db.execute('DELETE FROM nodes')
        db.executemany('INSERT INTO nodes VALUES (?,?,?,?)',[(x['id'],x['connection_id'],x['table'],json.dumps(x,sort_keys=True)) for x in graph['nodes']])
        db.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?,?)',[(x['id'],x['child_connection_id'],x['child_table'],x['parent_connection_id'],x['parent_table'],x['score'],json.dumps(x,sort_keys=True)) for x in graph['edges']])
        db.execute('''INSERT INTO snapshot(id,version,generated_at,summary_json) VALUES(1,?,?,?)
            ON CONFLICT(id) DO UPDATE SET version=excluded.version,generated_at=excluded.generated_at,summary_json=excluded.summary_json''',
            (VERSION,graph['generated_at'],json.dumps(summ)))
        db.commit()
    return summary(project)

def _status_map(project: Path):
    return {(x['connection_id'],x['table']):x['status'] for x in profiles.list_profiles(project)}

def _snap(project: Path):
    with _LOCK,_db(project) as db:
        r=db.execute('SELECT * FROM snapshot WHERE id=1').fetchone()
        return json.loads(r['summary_json']) if r else None

def summary(project: Path) -> dict:
    snap=_snap(project)
    if snap is None:
        return {'status':'not_discovered','node_count':0,'candidate_edges':0,'stale_nodes':0,'stale_edges':0,
                'version':VERSION,'disclaimer':'No relationship graph has been discovered yet.'}
    statuses=_status_map(project)
    with _LOCK,_db(project) as db:
        nodes=[json.loads(x['payload_json']) for x in db.execute('SELECT payload_json FROM nodes')]
        edges=[json.loads(x['payload_json']) for x in db.execute('SELECT payload_json FROM edges')]
    stale_nodes={x['id'] for x in nodes if statuses.get((x['connection_id'],x['table']))!='profiled'}
    stale_edges=sum((x['child_connection_id']+'::'+x['child_table'] in stale_nodes or
                     x['parent_connection_id']+'::'+x['parent_table'] in stale_nodes) for x in edges)
    return {**snap,'status':'stale' if stale_nodes else 'current','stale_nodes':len(stale_nodes),
            'stale_edges':stale_edges,'approved_edges':0,'review_status':'not_available_until_phase7'}

def list_nodes(project: Path, *, connection_id: str|None=None,limit: int=150) -> dict:
    limit=max(1,min(int(limit),150))
    with _LOCK,_db(project) as db:
        if connection_id:rows=db.execute('SELECT payload_json FROM nodes WHERE connection_id=? ORDER BY table_name LIMIT ?', (connection_id,limit)).fetchall()
        else:rows=db.execute('SELECT payload_json FROM nodes ORDER BY connection_id,table_name LIMIT ?', (limit,)).fetchall()
    status=_status_map(project)
    return {'items':[{**json.loads(x['payload_json']), 'freshness':status.get((json.loads(x['payload_json'])['connection_id'],json.loads(x['payload_json'])['table']),'removed')}
                     for x in rows], 'count':len(rows)}

def list_edges(project: Path, *, table: str|None=None,connection_id: str|None=None,
               min_score: float=0.0,limit: int=100,offset: int=0) -> dict:
    limit=max(1,min(int(limit),200));offset=max(0,min(int(offset),10_000_000))
    if not 0<=min_score<=1:raise ConnectionErrorSafe('min_score must be 0..1')
    where=['score>=?'];args=[min_score]
    if table:
        where.append('(INSTR(LOWER(child_table), ?) > 0 OR INSTR(LOWER(parent_table), ?) > 0)');term=table.lower();args.extend([term,term])
    if connection_id:
        where.append('(child_connection_id=? OR parent_connection_id=?)');args.extend([connection_id,connection_id])
    clause=' AND '.join(where)
    with _LOCK,_db(project) as db:
        total=db.execute('SELECT COUNT(*) FROM edges WHERE '+clause,args).fetchone()[0]
        rows=db.execute('SELECT payload_json FROM edges WHERE '+clause+' ORDER BY score DESC,id LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
    status=_status_map(project)
    items=[]
    for x in rows:
        edge=json.loads(x['payload_json'])
        stale=(status.get((edge['child_connection_id'],edge['child_table']))!='profiled' or
               status.get((edge['parent_connection_id'],edge['parent_table']))!='profiled')
        items.append({**edge,'freshness':'stale' if stale else 'current'})
    return {'items':items,'total':total,'limit':limit,'offset':offset}

def all_edges(project: Path) -> list[dict]:
    """Every candidate edge (paged internally), highest score first."""
    out=[];offset=0
    while True:
        page=list_edges(project,limit=200,offset=offset)
        out.extend(page['items']);offset+=200
        if offset>=page['total']:return out

def get_edge(project: Path,eid: str) -> dict|None:
    with _LOCK,_db(project) as db:
        r=db.execute('SELECT payload_json FROM edges WHERE id=?',(eid,)).fetchone()
    if not r:return None
    edge=json.loads(r['payload_json']);status=_status_map(project)
    stale=(status.get((edge['child_connection_id'],edge['child_table']))!='profiled' or
           status.get((edge['parent_connection_id'],edge['parent_table']))!='profiled')
    return {**edge,'freshness':'stale' if stale else 'current'}

def find_paths(project: Path, *, source: str,target: str,max_hops: int=3,max_paths: int=5) -> dict:
    """Bounded candidate-graph traversal; not approved or executable join paths."""
    if not 1<=int(max_hops)<=3:raise ConnectionErrorSafe('max_hops must be 1..3')
    if not 1<=int(max_paths)<=10:raise ConnectionErrorSafe('max_paths must be 1..10')
    with _LOCK,_db(project) as db:
        node_ids={r[0] for r in db.execute('SELECT id FROM nodes')}
        if source not in node_ids or target not in node_ids:
            raise ConnectionErrorSafe('Both tables must exist in the current graph snapshot')
        rows=[json.loads(x[0]) for x in db.execute('SELECT payload_json FROM edges')]
    status=_status_map(project)
    adjacency={n:[] for n in node_ids}
    for edge in rows:
        a=edge['child_connection_id']+'::'+edge['child_table']
        b=edge['parent_connection_id']+'::'+edge['parent_table']
        if status.get((edge['child_connection_id'],edge['child_table']))!='profiled':continue
        if status.get((edge['parent_connection_id'],edge['parent_table']))!='profiled':continue
        adjacency[a].append((b,edge['id'],edge['score']))
        adjacency[b].append((a,edge['id'],edge['score']))
    for neighbors in adjacency.values():neighbors.sort(key=lambda x:(-x[2],x[0],x[1]))
    if source==target:
        return {'source':source,'target':target,'paths':[{'nodes':[source],'edge_ids':[],'hop_count':0}],
                'disclaimer':'Identity path only; no relationship inferred'}
    frontier=[([source],[])]
    paths=[];expanded=0
    for _ in range(max_hops):
        new=[]
        for nodes,ids in frontier:
            for nxt,eid,_ in adjacency[nodes[-1]]:
                if nxt in nodes:continue
                expanded+=1
                if expanded>10000:raise ConnectionErrorSafe('Path exploration cap reached; narrow the graph')
                pnodes=nodes+[nxt];pids=ids+[eid]
                if nxt==target:
                    paths.append({'nodes':pnodes,'edge_ids':pids,'hop_count':len(pids)})
                    if len(paths)>=max_paths:break
                else:new.append((pnodes,pids))
            if len(paths)>=max_paths:break
        if len(paths)>=max_paths:break
        frontier=new
        if not frontier:break
    return {'source':source,'target':target,'paths':paths,
      'max_hops':max_hops,'max_paths':max_paths,
      'disclaimer':'Exploratory paths through unverified candidate edges only. Not approved joins or safe SQL routes.'}
