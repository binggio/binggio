# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio in the browser: a local web app over the same workspace the CLI uses.

    binggio ui                 open the workspace in this folder (or a start screen)
    binggio ui --port 8800 --no-browser

Runs on this computer only (127.0.0.1), standard library only, no internet needed. Every request
to the API carries a random token that only the page opened by `binggio ui` knows, so other web
sites cannot drive it. Database passwords and model keys typed into the page are kept in memory
for the request or the session and never written to disk.
"""
from __future__ import annotations
import json
import sys
import logging
import os
import secrets
import string
import threading
import time
import traceback
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from binggio import __version__
from binggio.workspace import Workspace, WorkspaceError

log = logging.getLogger('binggio.ui')
HERE = Path(__file__).resolve().parent
RECENT = Path.home() / '.binggio' / 'recent.json'
DEFAULT_NOTE = 'Reviewed in the Binggio app'


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message); self.status = status


def _expected_errors():
    from binggio.registry.store import ReviewConflict, ReviewValidation
    from binggio.connectors import ConnectorError
    from binggio.catalog.connectors import ConnectionErrorSafe
    return (WorkspaceError, ReviewConflict, ReviewValidation, ConnectorError, ConnectionErrorSafe, ValueError, KeyError, FileNotFoundError)


# ====================================================================== state

class App:
    def __init__(self, workspace=None):
        self.token = secrets.token_urlsafe(24)
        self.ws = workspace
        self.jobs = {}
        self.job_lock = threading.Lock()      # one data job (add / refresh / profile) at a time
        self.lock = threading.RLock()

    # --------------------------------------------------------------- workspace
    def need(self):
        if self.ws is None: raise ApiError('Open or create a workspace first', 409)
        return self.ws

    def set_workspace(self, ws):
        with self.lock:
            self.ws = ws
        try:
            items = [p for p in _read_recent() if p != str(ws.root)]
            RECENT.parent.mkdir(parents=True, exist_ok=True)
            RECENT.write_text(json.dumps([str(ws.root)] + items[:9]), encoding='utf-8')
        except OSError:
            pass

    # --------------------------------------------------------------- jobs
    def start_job(self, kind, steps, fn):
        if not self.job_lock.acquire(blocking=False):
            raise ApiError('Another data job is still running; wait for it to finish', 409)
        job = {'id': uuid.uuid4().hex[:12], 'kind': kind, 'status': 'running', 'started': time.time(),
               'steps': [{'name': s, 'status': 'waiting'} for s in steps], 'result': None, 'error': None}
        self.jobs[job['id']] = job

        def step(i, status='running', detail=None):
            for j, s in enumerate(job['steps']):
                if j < i and s['status'] in ('waiting', 'running'): s['status'] = 'done'
            job['steps'][i]['status'] = status
            if detail: job['steps'][i]['detail'] = detail

        def run():
            try:
                job['result'] = fn(step)
                for s in job['steps']:
                    if s['status'] in ('waiting', 'running'): s['status'] = 'done'
                job['status'] = 'done'
            except Exception as e:  # reported to the page
                job['status'] = 'failed'; job['error'] = str(e) or type(e).__name__
                for s in job['steps']:
                    if s['status'] == 'running': s['status'] = 'failed'
                if not isinstance(e, _expected_errors()): log.error('job failed\n%s', traceback.format_exc())
            finally:
                job['seconds'] = round(time.time() - job['started'], 1)
                self.job_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return job


def _read_recent():
    try:
        return [p for p in json.loads(RECENT.read_text(encoding='utf-8')) if (Path(p) / 'binggio.json').is_file()]
    except (OSError, ValueError):
        return []


# ====================================================================== helpers

def _model_state():
    from binggio.engine import core
    src = next((k for k in ('BINGGIO_LLM_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY', 'OPENAI_API_KEY') if os.environ.get(k)), None)
    return {'configured': bool(core._llm_enabled()), 'from': 'this session' if os.environ.get('_BINGGIO_UI_KEY') else (src and 'environment variable ' + src),
            'model': os.environ.get('BINGGIO_LLM_MODEL', 'gpt-4.1-mini')}


def _source_kind(ws, conn):
    from binggio.connectors import MANIFEST
    try:
        m = json.loads((ws.root / 'data' / conn['source_path'] / MANIFEST).read_text(encoding='utf-8'))
        return m['source']['kind'], m['source']
    except (OSError, ValueError, KeyError):
        return ('csv' if conn['kind'] == 'csv_folder' else conn['kind']), None


def _decision(d):
    if not d: return None
    return {'revision': d['revision'], 'status': d['status'], 'actor': d['actor'], 'at': d['updated_at'],
            'approved': d.get('approved'), 'id': d['id']}


def _all_roles(root, **kw):
    from binggio.registry import store
    return store.list_roles(root, limit=10 ** 7, **kw)['items']


def _plain(items):
    """Evidence lines without internal build names (\"Phase 6 ...\")."""
    import re
    out = []
    for t in items or []:
        t = re.sub(r'\bPhase ?\d+[a-z]? current relationship\b', 'Current candidate relationship', str(t))
        t = re.sub(r';? ?Phase ?\d+[a-z]? required', '; needs relationship discovery', t)
        t = re.sub(r'\bPhase ?\d+[a-z]?\s*', '', t)
        out.append(t.strip('; '))
    if any(t.startswith('Current candidate relationship') for t in out):
        out = [t.replace('Relationship target not yet checked; needs relationship discovery', '').strip('; ') for t in out]
    return [t for t in out if t]


def _short(field):
    return str(field).split('::')[-1]


def _demo_samples(ws):
    try:
        items = json.loads((HERE / 'demo_questions.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    from binggio.context.context import approved_context
    try:
        ctx = approved_context(ws.root)
    except Exception:
        return []
    tables = set(ctx['tables'])
    need = lambda s: s['plan']['entity'] in tables
    return [q for q in items if all(need(s) for s in q['steps'])]


# ====================================================================== API

def api(app: App, method, path, q, body):
    from binggio.registry import store
    if path == '/api/state' and method == 'GET':
        ws = app.ws
        import platform
        return {'version': __version__, 'python': platform.python_version(), 'platform': '%s %s' % (platform.system(), platform.release()),
                'workspace': str(ws.root) if ws else None,
                'name': ws.root.name if ws else None, 'model': _model_state(), 'recent': _read_recent(),
                'home': str(Path.home()), 'samples': bool(ws and _demo_samples(ws)), 'demo': bool(ws and ws.is_demo), 'excel': _has('openpyxl'),
                'drivers': {'sqlserver': _has('pymssql'), 'oracle': _has('oracledb'), 'postgres': _has('psycopg')}}

    if path == '/api/workspace' and method == 'POST':
        action, p = body.get('action'), (body.get('path') or '').strip()
        if not p: raise ApiError('Choose a folder')
        p = os.path.expanduser(p)
        ws = {'open': Workspace.open, 'create': Workspace.create, 'demo': Workspace.demo}[action](p)
        app.set_workspace(ws)
        return {'workspace': str(ws.root)}

    if path == '/api/fs' and method == 'GET':
        return _browse(q.get('path') or str(Path.home()), q.get('want', 'dir'), q.get('ext', ''))

    if path == '/api/settings/model' and method == 'POST':
        key = (body.get('key') or '').strip()
        if body.get('clear'):
            if os.environ.pop('_BINGGIO_UI_KEY', None): os.environ.pop('BINGGIO_LLM_API_KEY', None)
        elif key:
            os.environ['BINGGIO_LLM_API_KEY'] = key; os.environ['_BINGGIO_UI_KEY'] = '1'
        if body.get('model'): os.environ['BINGGIO_LLM_MODEL'] = body['model'].strip()
        return _model_state()

    if path.startswith('/api/jobs/') and method == 'GET':
        job = app.jobs.get(path.rsplit('/', 1)[1])
        if not job: raise ApiError('Unknown job', 404)
        return job

    ws = app.need(); root = ws.root

    if path == '/api/overview':
        st = ws.status()
        roles = _all_roles(root)
        rels = store.list_relationships(root, limit=10 ** 7)['items']
        count = lambda items, s: sum(x['effective_status'] == s for x in items)
        from binggio.registry import concepts, terms
        return {'status': st, 'roles': {s: count(roles, s) for s in ('pending', 'approved', 'rejected', 'stale', 'revoked')},
                'relationships': {s: count(rels, s) for s in ('pending', 'approved', 'rejected', 'stale', 'revoked')},
                'concepts': len([c for c in concepts.list_concepts(root) if c['effective_status'] == 'approved']),
                'terms': len([t for t in terms.list_terms(root) if t['kind'] == 'term' and t['effective_status'] == 'approved']),
                'sources': _sources(ws), 'recent': store.events(root, limit=8)['items'], 'issues': _open_issues(root)}

    if path == '/api/sources' and method == 'GET':
        return {'items': _sources(ws)}
    if path in ('/api/sources', '/api/sources/refresh') and method == 'POST':
        ws._no_data_changes()          # the sample bank's data is fixed
    if path == '/api/sources' and method == 'POST':
        return _add_source(app, ws, body)
    if path == '/api/sources/refresh' and method == 'POST':
        cid, pw = body['connection'], body.get('password') or None
        def run(step):
            step(0); r = ws.refresh(cid, password=pw)
            step(1); ws.profile()
            step(2); _prepare(ws, step, 2)
            step(3); ws.discover()
            return r
        return app.start_job('refresh', ['Reading the source again', 'Profiling (your data fixes are applied again)', 'Preparing tables for fast reading', 'Finding relationships'], run)
    if path == '/api/sources/test' and method == 'POST':
        return _test_database(body)
    if path == '/api/pipeline' and method == 'POST':
        def run(step):
            step(0); p = ws.profile(force=bool(body.get('force')))
            step(1); _prepare(ws, step, 1)
            step(2); d = ws.discover()
            return {'profile': {k: p[k] for k in ('total', 'profiled_or_cached') if k in p}, 'failures': p.get('failures'), 'discover': d}
        return app.start_job('pipeline', ['Profiling', 'Preparing tables for fast reading', 'Finding relationships'], run)

    if path == '/api/tables':
        from binggio.profiling import store as profiles
        return {'items': profiles.list_profiles(root, connection_id=q.get('connection') or None)}
    if path == '/api/table':
        return _table_detail(root, q['connection'], q['table'])

    if path == '/api/impact':
        from binggio import impact
        return impact.report(root, q.get('connection') or None)
    if path == '/api/onboard' and method == 'GET':
        from binggio import onboard
        props = onboard.proposals(root, q.get('connection') or None)
        for cols in props['roles'].values():
            for c in cols: c['evidence'] = _plain(c.get('evidence'))
        for l in props['links']: l['reasons'] = _plain(l.get('reasons'))
        return props
    if path == '/api/onboard' and method == 'POST':
        return _onboard_apply(root, body)

    if path == '/api/review/roles' and method == 'GET':
        items = _all_roles(root, connection_id=q.get('connection') or None, table=q.get('table') or None,
                           status=q.get('status') or 'all')
        text = (q.get('q') or '').lower()
        if text: items = [x for x in items if text in (x['table'] + ' ' + x['column']).lower()]
        total = len(items); off = int(q.get('offset') or 0); lim = int(q.get('limit') or 200)
        return {'total': total, 'items': [{'connection': x['connection_id'], 'table': x['table'], 'column': x['column'],
                                           'suggested': x['candidate_role'], 'evidence': _plain(x['role_evidence']),
                                           'status': x['effective_status'], 'decision': _decision(x['decision'])}
                                          for x in items[off:off + lim]], 'roles': sorted(store.ROLES)}
    if path == '/api/review/roles' and method == 'POST':
        return _decide_roles(root, body)
    if path == '/api/review/relationships' and method == 'GET':
        items = store.list_relationships(root, status=q.get('status') or 'all', limit=10 ** 7)['items']
        text = (q.get('q') or '').lower()
        if text: items = [x for x in items if text in ' '.join((x['child_table'], x['child_column'], x['parent_table'], x['parent_column'])).lower()]
        from binggio.discovery import store as graph
        conns = {e['id']: (e['child_connection_id'], e['parent_connection_id']) for e in graph.all_edges(root)}
        out = []
        for x in items[:int(q.get('limit') or 500)]:
            r = _rel_public(x); r['child_connection'], r['parent_connection'] = conns.get(x['edge_id'], (r['child_connection'], r['parent_connection']))
            out.append(r)
        return {'total': len(items), 'items': out}
    if path == '/api/review/relationships' and method == 'POST':
        return _decide_relationships(root, body)
    if path == '/api/review/concepts' and method == 'GET':
        from binggio.registry import concepts
        return {'items': [_artifact_public(x) for x in concepts.list_concepts(root)]}
    if path == '/api/review/terms' and method == 'GET':
        from binggio.registry import terms
        return {'items': [_artifact_public(x) for x in terms.list_terms(root)]}
    if path in ('/api/review/concepts', '/api/review/terms') and method == 'POST':
        return _decide_artifact(root, path.rsplit('/', 1)[1], body)
    if path == '/api/terms/create' and method == 'POST':
        from binggio import onboard
        words = [w for w in (body.get('words') or '').split(',') if w.strip()]
        if not words: raise ApiError('Give at least one word or phrase for this value')
        r = onboard.create_term(root, body['connection'], body['table'], body['column'], body['value'], words,
                                _by(body), body.get('note') or DEFAULT_NOTE, domain=body.get('domain') or 'Unassigned')
        return _artifact_public({**r, 'effective_status': r['status']})
    if path == '/api/review/relationship' and method == 'GET':
        return _rel_detail(root, q.get('edge') or '')
    if path == '/api/review/relationship' and method == 'POST':
        return _decide_relationship_one(root, body)
    if path == '/api/review/role' and method == 'GET':
        return _role_detail(root, q['connection'], q['table'], q['column'])
    if path == '/api/review/definitions' and method == 'GET':
        return _definitions(root)
    if path == '/api/review/definitions' and method == 'POST':
        return _decide_definition(root, body)
    if path == '/api/autonomy' and method == 'GET':
        from binggio.registry import autonomy
        return {'status': autonomy.status(root), 'history': autonomy.history(root, 10)['items']}
    if path == '/api/autonomy/preview' and method == 'GET':
        from binggio.registry import autonomy
        r = autonomy.preview(root)
        from binggio.catalog import catalog
        names = {c['id']: c['name'] for c in catalog.list_connections(root)}
        for x in r['eligible_examples'] + r['exceptions']:
            x['reasons'] = _plain(x.get('reasons'))
            cid, sep, rest = str(x.get('source', '')).partition(' / ')
            if sep: x['source'] = '%s: %s' % (names.get(cid, cid), rest)
        return r
    if path == '/api/autonomy' and method == 'POST':
        return _autonomy_action(root, body)
    if path == '/api/issues' and method == 'GET':
        from binggio import issues
        return {'items': issues.list_issues(root, q.get('status') or 'open'), 'counts': issues.counts(root)}
    if path == '/api/issues' and method == 'POST':
        from binggio import issues
        try: return issues.decide(root, body.get('id'), body.get('action'), _by(body), body.get('note'))
        except ValueError as e: raise ApiError(str(e))
    if path == '/api/paths' and method == 'GET':
        return _paths(root, q.get('from') or '', q.get('to') or '')
    if path == '/api/history':
        return store.events(root, artifact_id=q.get('artifact') or None, limit=int(q.get('limit') or 200))

    if path == '/api/fixes' and method == 'GET':
        return _fixes_info(root, q['connection'], q['table'])
    if path == '/api/fixes/preview' and method == 'POST':
        from binggio import prepared
        try: return prepared.preview_fix(root, _conn(root, body['connection']), body['table'], body.get('kind'), body.get('column'), body.get('params') or {})
        except prepared.FixError as e: raise ApiError(str(e))
    if path == '/api/fixes' and method == 'POST':
        ws._no_data_changes()
        return _fix_action(app, ws, body)
    if path == '/api/keys' and method == 'GET':
        from binggio import prepared
        cols = [c for c in (q.get('columns') or '').split(',') if c]
        try: return prepared.key_check(root, _conn(root, q['connection']), q['table'], cols)
        except prepared.FixError as e: raise ApiError(str(e))
    if path == '/api/keys/make' and method == 'POST':
        return _make_key(root, body)
    if path == '/api/discovery/report' and method == 'GET':
        return _discovery_report(root)
    if path == '/api/links/check' and method == 'POST':
        from binggio import links
        try: return links.check(root, _side(body['child']), _side(body['parent']))
        except links.LinkError as e: raise ApiError(str(e))
    if path == '/api/links' and method == 'POST':
        return _link_action(root, body)

    if path == '/api/story':
        from binggio import story
        cid = q.get('connection') or (sorted(ws.connections(), key=lambda c: c['created_at'])[-1]['id'] if ws.connections() else None)
        if not cid: raise ApiError('Add a source first')
        return _story_cached(root, cid, q.get('table') or None)
    if path == '/api/graph':
        return _graph(root)

    if path == '/api/ask/new' and method == 'POST':
        return {'conversation': ws.new_conversation()}
    if path == '/api/ask' and method == 'POST':
        question = (body.get('question') or '').strip()
        if not question: raise ApiError('Type a question')
        conv = body.get('conversation') or ws.new_conversation()
        try:
            ans = ws.ask(question, plan=body.get('plan'), conversation=conv)
        except Exception as e:
            from binggio.engine.sessions import SessionMissing
            if isinstance(e, SessionMissing):
                conv = ws.new_conversation(); ans = ws.ask(question, plan=body.get('plan'), conversation=conv)
            else: raise
        return {'conversation': conv, 'answer': _answer_public(root, ans)}
    if path == '/api/samples':
        return {'items': _demo_samples(ws)}

    raise ApiError('Not found: %s %s' % (method, path), 404)


_STORY_CACHE = {}


def _story_cached(root, cid, table):
    """The data story reads up to 100,000 rows per table; reuse it until a decision is made or a
    source table changes (its fingerprint), so revisiting the page is instant."""
    from binggio import story
    from binggio.registry import store
    from binggio.profiling import store as profiles
    last = (store.events(root, limit=1)['items'] or [{}])[0].get('id')
    prints = tuple(sorted((p['table'], str(p.get('profiled_at')), bool(p.get('stale'))) for p in profiles.list_profiles(root, connection_id=cid)))
    key = (str(root), cid, table, last, prints)
    if key not in _STORY_CACHE:
        if len(_STORY_CACHE) > 50: _STORY_CACHE.clear()
        _STORY_CACHE[key] = story.build(root, cid, table=table)
    return _STORY_CACHE[key]


def _open_issues(root):
    try:
        from binggio import issues
        return issues.counts(root)['open']
    except Exception:
        return 0


def _has(mod):
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def _by(body):
    by = (body.get('by') or '').strip()
    if not by: raise ApiError('Enter your name (top right) so decisions are recorded against it')
    return by


def _browse(path, want, ext):
    p = Path(os.path.expanduser(path))
    if p.is_file(): p = p.parent
    while not p.is_dir() and p.parent != p:   # start from the nearest folder that exists
        p = p.parent
    if not p.is_dir(): p = Path.cwd()
    exts = tuple(e.strip().lower() for e in ext.split(',') if e.strip())
    entries = []
    try:
        for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if c.name.startswith('.'): continue
            try:
                if c.is_dir():
                    entries.append({'name': c.name, 'type': 'dir', 'path': str(c), 'workspace': (c / 'binggio.json').is_file(),
                                    'csv': want == 'csvdir' and any(True for _ in c.glob('*.csv'))})
                elif want != 'dir' and (not exts or c.suffix.lower() in exts):
                    entries.append({'name': c.name, 'type': 'file', 'path': str(c), 'size': c.stat().st_size})
            except OSError:
                continue
    except PermissionError:
        raise ApiError('No permission to open %s' % p)
    roots = ['%s:\\' % d for d in string.ascii_uppercase if os.path.exists('%s:\\' % d)] if os.name == 'nt' else ['/']
    return {'path': str(p), 'parent': str(p.parent) if p.parent != p else None, 'entries': entries[:2000],
            'csv_here': sum(1 for _ in p.glob('*.csv')), 'workspace_here': (p / 'binggio.json').is_file(),
            'roots': roots, 'home': str(Path.home())}


def _sources(ws):
    from binggio.profiling import store as profiles
    prof = {}
    for t in profiles.list_profiles(ws.root):
        prof.setdefault(t['connection_id'], []).append(t['status'])
    out = []
    for c in ws.connections():
        kind, src = _source_kind(ws, c)
        st = prof.get(c['id'], [])
        out.append({'id': c['id'], 'name': c['name'], 'kind': kind, 'tables': c.get('table_count') or 0,
                    'created_at': c['created_at'], 'profiled': st.count('profiled'), 'stale': st.count('stale'),
                    'not_profiled': st.count('not_profiled'), 'refreshable': src is not None,
                    'origin': {k: v for k, v in (src or {}).items() if k in ('kind', 'file', 'host', 'database', 'schema', 'user', 'port')}})
    return out


def _add_source(app, ws, b):
    typ, name = b.get('type'), (b.get('name') or '').strip()
    if not name: raise ApiError('Give the source a name')
    raw = b.get('path') or ''
    path = raw if isinstance(raw, list) else os.path.expanduser(raw.strip())
    tables = b.get('tables') or None
    if isinstance(tables, str): tables = [t.strip() for t in tables.split(',') if t.strip()] or None
    row_limit = int(b['row_limit']) if b.get('row_limit') else None

    def add():
        if typ == 'csv': return ws.add_csv_folder(name, path)
        if typ == 'excel':
            sheets = b.get('sheets') or None
            if isinstance(sheets, str): sheets = [s.strip() for s in sheets.split(',') if s.strip()] or None
            return ws.add_excel(name, path, sheets=sheets, row_limit=row_limit)
        if typ == 'sqlite': return ws.add_database(name, 'sqlite', database=path, tables=tables, row_limit=row_limit)
        if typ == 'database':
            return ws.add_database(name, b['kind'], host=b.get('host') or None, database=b.get('database') or None,
                                   user=(b.get('user') or None) if not b.get('windows_auth') else None,
                                   password=b.get('password') or None, port=int(b['port']) if b.get('port') else None,
                                   schema=b.get('schema') or None, tables=tables, row_limit=row_limit)
        raise ApiError('Unknown source type')
    if typ in ('csv', 'excel', 'sqlite') and not path: raise ApiError('Choose the file or folder')

    def run(step):
        step(0); c = add()
        step(1); p = ws.profile()
        step(2); _prepare(ws, step, 2)
        step(3); d = ws.discover()
        return {'connection': c['id'], 'tables': c.get('table_count'), 'notes': c.get('notes', []),
                'profile_failures': p.get('failures'), 'candidate_relationships': d.get('candidate_edges'),
                'skipped_tables': d.get('skipped_tables', [])}
    return app.start_job('add', ['Copying the data in', 'Profiling every column', 'Preparing tables for fast reading', 'Finding relationships'], run)


def _prepare(ws, step, i):
    from binggio import prepared
    return prepared.prepare_all(ws.root, progress=lambda k, n, t: step(i, 'running', '%d of %d: %s' % (k + 1, n, t)))


def _test_database(b):
    from binggio.connectors import database as db
    kind = b.get('kind') or 'sqlserver'
    d = db.dialect(kind)
    params = {'host': b.get('host') or None, 'database': (os.path.expanduser(b.get('database') or '') or None), 'port': int(b['port']) if b.get('port') else None,
              'user': (b.get('user') or None) if not b.get('windows_auth') else None, 'password': b.get('password') or None}
    if kind == 'sqlite': params = {'database': os.path.expanduser(b.get('path') or b.get('database') or '')}
    conn = d.connect(**params)
    try:
        found = db.list_tables(conn, d, b.get('schema') or None)
    finally:
        try: conn.close()
        except Exception: pass
    return {'tables': [{'schema': s, 'table': t} for s, t in found]}


def _table_detail(root, cid, table):
    from binggio.profiling import store as profiles
    from binggio.catalog import catalog
    from binggio.discovery import store as graph
    prof = profiles.get_table(root, cid, table)
    roles = {r['column']: r for r in _all_roles(root, connection_id=cid, table=table)}
    try: preview = catalog.table_detail(root, cid, table, preview=0)
    except Exception: preview = {}
    try:
        from binggio import prepared
        conn = _conn(root, cid)
        h, rows, meta = prepared.read(root, conn, table, 10)
        preview = {**preview, 'columns': [{'name': x} for x in h],
                   'preview': [['' if v is None else str(v) for v in r] for r in rows]}
        if prepared.is_ready(root, conn, table): preview['row_count'] = prepared.row_count(root, conn, table)
        fixes = prepared.fixes_for(root, cid, table)
    except Exception:
        fixes = []
    cols = []
    for c in (prof or {}).get('columns', []):
        r = roles.get(c['name'], {})
        d = r.get('decision')
        cols.append({'name': c['name'], 'type': c.get('observed_type'), 'distinct': c.get('distinct_count'),
                     'missing_pct': c.get('missing_pct'), 'rows': c.get('observed_rows'), 'findings': c.get('findings') or [],
                     'numeric': c.get('numeric_stats'), 'suggested': r.get('candidate_role'), 'evidence': _plain(r.get('role_evidence')),
                     'status': r.get('effective_status'), 'approved_role': ((d or {}).get('approved') or {}).get('role'),
                     'declared_key': c.get('declared_primary_key')})
    links = [_edge_public(e) for e in graph.all_edges(root)
             if (e['child_connection_id'], e['child_table']) == (cid, table) or (e['parent_connection_id'], e['parent_table']) == (cid, table)]
    return {'connection': cid, 'table': table, 'profiled': bool(prof), 'stale': (prof or {}).get('stale'),
            'sample_rows': (prof or {}).get('sample_rows'), 'sample_complete': (prof or {}).get('sample_complete'),
            'findings': (prof or {}).get('table_findings'), 'columns': cols,
            'preview': _preview(preview), 'fixes': [{'id': f['id'], 'label': f['label'], 'kind': f['kind'], 'column': f['column']} for f in fixes],
            'links': links}


def _preview(meta):
    cols = [c['name'] for c in meta.get('columns', [])]
    rows = meta.get('preview') or []
    return {'columns': cols, 'rows': [[r.get(c) for c in cols] if isinstance(r, dict) else list(r) for r in rows],
            'row_count': meta.get('row_count')}


def _edge_public(e):
    return {'edge_id': e['id'], 'child': {'connection': e['child_connection_id'], 'table': e['child_table'], 'column': e['child_column']},
            'parent': {'connection': e['parent_connection_id'], 'table': e['parent_table'], 'column': e['parent_column']},
            'score': e['score'], 'freshness': e.get('freshness')}


def _rel_public(x):
    ev = x.get('evidence') or {}
    cand = ((x.get('decision') or {}).get('proposed') or {}).get('candidate') or {}
    return {'edge_id': x['edge_id'], 'child_table': x['child_table'], 'child_column': x['child_column'],
            'parent_table': x['parent_table'], 'parent_column': x['parent_column'], 'score': x['score'],
            'status': x['effective_status'], 'freshness': x['freshness'], 'decision': _decision(x['decision']),
            'reasons': _plain(ev.get('reasons')), 'hint': ev.get('cardinality_hint'),
            'coverage': ev.get('distinct_coverage_pct'),
            'evidence': {k: v for k, v in ev.items() if k not in ('reasons',) and not isinstance(v, (dict, list))},
            'child_connection': cand.get('child_connection_id'), 'parent_connection': cand.get('parent_connection_id')}


def _artifact_public(x):
    p = x.get('approved') or x.get('proposed') or {}
    return {'id': x['id'], 'kind': x['kind'], 'name': x.get('subject_key') or p.get('name'), 'status': x['effective_status'],
            'stored_status': x['status'], 'revision': x['revision'], 'actor': x['actor'], 'at': x['updated_at'], 'definition': p}


def _decide_roles(root, b):
    from binggio.registry import store
    action, by, note = b['action'], _by(b), (b.get('note') or DEFAULT_NOTE)
    done, errors = 0, []
    for it in b.get('items') or []:
        try:
            cur = next((r for r in _all_roles(root, connection_id=it['connection'], table=it['table']) if r['column'] == it['column']), None)
            if cur is None: raise ApiError('Column not found')
            rev = cur['decision']['revision'] if cur['decision'] else 0
            role = it.get('role') or ((cur['decision'] or {}).get('approved') or {}).get('role') or cur['candidate_role']
            store.decide_column(root, it['connection'], it['table'], it['column'], action=action, actor=by, note=note,
                                expected_revision=rev, role=role)
            done += 1
        except Exception as e:
            if not isinstance(e, _expected_errors() + (ApiError,)): raise
            errors.append('%s.%s: %s' % (it.get('table'), it.get('column'), e))
    return {'done': done, 'errors': errors}


def _decide_relationships(root, b):
    from binggio.registry import store
    action, by, note = b['action'], _by(b), (b.get('note') or DEFAULT_NOTE)
    done, errors = 0, []
    rows = {r['subject_key']: r for r in store._rows(root, 'relationship')}
    for it in b.get('items') or []:
        try:
            row = rows.get(it['edge_id'])
            kw = {}
            if action == 'approve':
                kw['cardinality'] = it.get('cardinality') or 'many_to_one'
            store.decide_relationship(root, it['edge_id'], action=action, actor=by, note=note,
                                      expected_revision=row['revision'] if row else 0, **kw)
            done += 1
        except Exception as e:
            if not isinstance(e, _expected_errors()): raise
            errors.append('%s: %s' % (it.get('label') or it.get('edge_id'), e))
    return {'done': done, 'errors': errors}


def _decide_artifact(root, which, b):
    from binggio.registry import concepts, terms, store
    mod = concepts if which == 'concepts' else terms
    by, note = _by(b), (b.get('note') or DEFAULT_NOTE)
    cur = store.get_artifact(root, b['id'])
    if cur is None: raise ApiError('Not found', 404)
    r = mod.decide(root, b['id'], action=b['action'], actor=by, note=note, expected_revision=cur['revision'])
    return _artifact_public({**r, 'effective_status': store.get_artifact(root, b['id'])['effective_status']})


def _onboard_apply(root, b):
    from binggio import onboard
    from binggio.registry import store
    by, note = _by(b), (b.get('note') or 'Reviewed with the onboarding steps in the Binggio app')
    props = onboard.proposals(root, b.get('connection') or None)
    cid = props['connection_id']
    out = {'roles': 0, 'links': 0, 'concepts': 0, 'terms': 0, 'errors': []}
    errs = _expected_errors()
    for table, chosen in (b.get('roles') or {}).items():
        items = [{'column': c['column'], 'role': c['role']} for c in chosen]
        try: out['roles'] += onboard.approve_roles(root, cid, table, items, by, note)
        except errs as e: out['errors'].append('roles of %s: %s' % (table, e))
    links = {l['edge_id']: l for l in props['links']}
    for it in b.get('links') or []:
        link = links.get(it['edge_id'])
        if not link: out['errors'].append('link %s is no longer a candidate' % it['edge_id']); continue
        try:
            onboard.approve_link(root, {**link, 'cardinality': it.get('cardinality') or link['cardinality']}, by, note); out['links'] += 1
        except errs as e: out['errors'].append('link %s -> %s: %s' % (link['child'], link['parent'], e))
    concepts = {c['table']: c for c in props['concepts']}
    for t in b.get('concepts') or []:
        c = concepts.get(t if isinstance(t, str) else t['table'])
        if not c: continue
        if isinstance(t, dict) and t.get('name'): c = {**c, 'name': t['name']}
        try: onboard.create_concept(root, cid, c, by, note); out['concepts'] += 1
        except errs as e: out['errors'].append('concept %s: %s' % (c['name'], e))
    for t in b.get('terms') or []:
        words = [w for w in str(t.get('words') or '').split(',') if w.strip()]
        if not words: continue
        try: onboard.create_term(root, cid, t['table'], t['column'], t['value'], words, by, note); out['terms'] += 1
        except errs as e: out['errors'].append('words for %s.%s = %s: %s' % (t['table'], t['column'], t['value'], e))
    return out


def _graph(root):
    from binggio.registry import store
    from binggio.profiling import store as profiles
    from binggio.catalog import catalog
    names = {c['id']: c['name'] for c in catalog.list_connections(root)}
    nodes = [{'id': t['connection_id'] + '::' + t['table'], 'table': t['table'], 'connection': t['connection_id'],
              'source': names.get(t['connection_id'], t['connection_id']), 'status': t['status']}
             for t in profiles.list_profiles(root)]
    rels = store.list_relationships(root, limit=10 ** 7)['items']
    edges = []
    for x in rels:
        e = x['evidence'] if isinstance(x.get('evidence'), dict) else {}
        cand = ((x.get('decision') or {}).get('proposed') or {}).get('candidate') or {}
        edges.append({'edge_id': x['edge_id'], 'status': x['effective_status'], 'score': x['score'],
                      'child_table': x['child_table'], 'child_column': x['child_column'],
                      'parent_table': x['parent_table'], 'parent_column': x['parent_column']})
    # connection ids for edges come from the graph itself
    from binggio.discovery import store as graph
    full = {e['id']: e for e in graph.all_edges(root)}
    for e in edges:
        f = full.get(e['edge_id'])
        if f:
            e['source'] = f['child_connection_id'] + '::' + f['child_table']
            e['target'] = f['parent_connection_id'] + '::' + f['parent_table']
    return {'nodes': nodes, 'edges': [e for e in edges if 'source' in e]}


def _answer_public(root, a):
    if a.get('status') == 'refused':
        return a
    cols = a.get('columns') or []
    labels = [_short(c) for c in cols]
    plan = a.get('plan') or {}
    from binggio.catalog import catalog
    names = {c['id']: c['name'] for c in catalog.list_connections(root)}
    srcs = []
    for s in (a.get('provenance') or {}).get('sources') or []:
        cid, _, t = s['table_id'].partition('::')
        srcs.append({'table': t, 'source': names.get(cid, cid)})
    return {'status': 'answered', 'columns': labels, 'fields': cols, 'rows': a.get('rows') or [], 'row_count': a.get('row_count'),
            'truncated': (a.get('row_count') or 0) > len(a.get('rows') or []), 'sources': srcs,
            'business_terms': a.get('business_terms') or [], 'as_of': a.get('as_of'), 'plan': plan,
            'explanation': _explain(plan, _term_names(root, plan)), 'sql': a.get('sql'), 'parameters': a.get('parameters'),
            'normalisation': (a.get('provenance') or {}).get('normalisation')}


def _term_names(root, plan):
    from binggio.registry import store
    out = {}
    for t in plan.get('terms') or []:
        a = store.get_artifact(root, t.get('term'))
        if a: out[t['term']] = (a.get('approved') or a.get('proposed') or {}).get('name') or a.get('subject_key')
    return out


def _explain(plan, term_names=None):
    """The plan in plain words (what was counted, filtered and joined)."""
    term_names = term_names or {}
    if not plan: return []
    out = ['Started from **%s**' % _short(plan.get('entity', ''))]
    if plan.get('cohort') == 'active': out.append('Limited to the rows from the previous answer')
    ops = {'eq': '=', 'ne': '≠', 'gt': '>', 'gte': '≥', 'lt': '<', 'lte': '≤', 'in': 'in', 'not_in': 'not in', 'is_null': 'is empty', 'not_null': 'is not empty',
           'contains': 'contains'}
    for f in plan.get('filters') or []:
        out.append('Kept rows where %s %s %s' % (_short(f.get('field')), ops.get(f.get('op'), f.get('op')),
                                                 '' if f.get('op') in ('is_null', 'not_null') else repr(f.get('value'))))
    for e in plan.get('exists') or []:
        cond = ', '.join('%s %s %r' % (_short(f['field']), ops.get(f['op'], f['op']), f.get('value')) for f in e.get('filters') or [])
        out.append('%s a linked %s%s' % ('Without' if e.get('negate') else 'With', _short(e['table']), (' where ' + cond) if cond else ''))
    for t in plan.get('terms') or []:
        out.append('Applied business term "%s"%s' % (term_names.get(t.get('term'), t.get('term')), ' (negated)' if t.get('negate') else ''))
    if plan.get('latest'): out.append('Used the latest row per entity')
    for g in plan.get('aggregate') or []:
        out.append('Calculated %s of %s' % (g.get('fn'), _short(g.get('field') or 'rows')))
    if plan.get('group_by'): out.append('Grouped by ' + ', '.join(_short(g) for g in plan['group_by']))
    for s in plan.get('sort') or []:
        out.append('Sorted by %s (%s)' % (_short(s['field']), 'highest first' if s.get('direction') == 'desc' else 'lowest first'))
    if plan.get('limit') and plan['limit'] < 200: out.append('Kept the first %d' % plan['limit'])
    return out


# ====================================================================== HTTP

class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return   # a closed browser tab is not an error
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = 'binggio-ui'

    def log_message(self, fmt, *args):  # quiet
        log.debug(fmt, *args)

    def _send(self, status, body, ctype='application/json; charset=utf-8', extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (extra or {}).items(): self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # the browser left the page before the answer arrived; nothing to report

    def _host_ok(self):
        host = (self.headers.get('Host') or '').split(':')[0].strip('[]').lower()
        return host in ('127.0.0.1', 'localhost', '::1') or host == self.server.server_address[0]

    def _handle(self, method):
        if not self._host_ok():
            return self._send(403, {'error': 'Binggio only answers on this computer'})
        url = urlparse(self.path)
        if not url.path.startswith('/api/'):
            if method != 'GET': return self._send(405, {'error': 'Method not allowed'})
            if url.path in ('/', '/index.html'):
                html = (HERE / 'static' / 'index.html').read_text(encoding='utf-8').replace('__BINGGIO_TOKEN__', self.app.token)
                return self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8',
                                  {'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"})
            if url.path == '/favicon.ico':
                return self._send(204, b'', 'image/x-icon')
            return self._send(404, {'error': 'Not found'})
        if not secrets.compare_digest(self.headers.get('X-Binggio-Token', ''), self.app.token):
            return self._send(403, {'error': 'Missing or wrong session token. Open the address printed by binggio ui.'})
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        body = {}
        if method == 'POST':
            n = int(self.headers.get('Content-Length') or 0)
            if n > 5_000_000: return self._send(413, {'error': 'Request too large'})
            try: body = json.loads(self.rfile.read(n) or b'{}')
            except ValueError: return self._send(400, {'error': 'Request body is not JSON'})
        try:
            from binggio.profiling import store as profiles
            with profiles.evaluation_scope():
                result = api(self.app, method, url.path, q, body)
            return self._send(200, result)
        except ApiError as e:
            return self._send(e.status, {'error': str(e)})
        except _expected_errors() as e:
            msg = str(e)
            if isinstance(e, KeyError): msg = 'Missing field: %s' % e
            return self._send(400, {'error': msg})
        except Exception as e:
            log.error('%s %s failed\n%s', method, url.path, traceback.format_exc())
            return self._send(500, {'error': 'Unexpected error: %s: %s' % (type(e).__name__, e)})

    def do_GET(self): self._handle('GET')
    def do_POST(self): self._handle('POST')


def make_server(workspace=None, host='127.0.0.1', port=8765):
    app = App(workspace)
    handler = type('BoundHandler', (Handler,), {'app': app})
    last = None
    for p in ([port] + list(range(port + 1, port + 20)) if port else [0]):
        try:
            srv = _QuietServer((host, p), handler)
            srv.daemon_threads = True
            srv.app = app
            return srv
        except OSError as e:
            last = e
    raise OSError('No free port near %d: %s' % (port, last))


def serve(workspace=None, host='127.0.0.1', port=8765, open_browser=True):
    srv = make_server(workspace, host, port)
    url = 'http://%s:%d/' % ('127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host, srv.server_address[1])
    print('Binggio is running at:\n\n    %s\n\nKeep this window open while you use it. Press Ctrl+C to stop.' % url, flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        srv.server_close()
    return 0


# ---------------------------------------------------------------- review detail, definitions, autonomy, paths

def _col_summary(root, cid, table, column):
    from binggio.profiling import store as profiles
    prof = profiles.get_table(root, cid, table) or {}
    c = next((x for x in prof.get('columns', []) if x['name'] == column), None)
    role = next((r for r in _all_roles(root, connection_id=cid, table=table) if r['column'] == column), None)
    d = (role or {}).get('decision')
    out = {'connection': cid, 'table': table, 'column': column, 'profiled': c is not None,
           'stale': bool(prof.get('stale')), 'sample_rows': prof.get('sample_rows'), 'sample_complete': prof.get('sample_complete'),
           'role_status': (role or {}).get('effective_status') or 'missing',
           'approved_role': ((d or {}).get('approved') or {}).get('role'), 'suggested_role': (role or {}).get('candidate_role'),
           'role_evidence': _plain((role or {}).get('role_evidence')), 'decision': _decision(d)}
    if c:
        out.update({'type': c.get('observed_type'), 'rows': c.get('observed_rows'), 'distinct': c.get('distinct_count'),
                    'distinct_pct': c.get('distinct_pct'), 'missing': c.get('missing_count'), 'missing_pct': c.get('missing_pct'),
                    'numeric': c.get('numeric_stats'), 'declared_key': c.get('declared_primary_key'),
                    'findings': [f.get('message') or f.get('code') for f in c.get('findings') or []],
                    'confidence': c.get('role_confidence_label')})
    return out


def _sample_values(root, cid, table, column, n=8):
    from binggio import prepared
    try:
        h, rows, _ = prepared.read(root, _conn(root, cid), table, 200)
        i = h.index(column)
    except Exception:
        return []
    seen = []
    for r in rows:
        v = r[i]
        if v not in (None, '') and v not in seen:
            seen.append(v)
        if len(seen) >= n: break
    return seen


RELATIONSHIP_RISKS = ['Both columns mean the same business thing, not just codes that happen to overlap.',
                      'The kind of link (many to one, one to one) holds for all the data, not only the sample.',
                      'Dates matter: effective dates or historical versions may need an as-of rule.',
                      'Keys outside the sample may not match, or the parent may repeat a key.',
                      'If you change a join column, the original evidence no longer supports the new mapping.']


def _rel_detail(root, eid):
    from binggio.registry import store
    from binggio.discovery import store as graph
    e = graph.get_edge(root, eid)
    if not e: raise ApiError('Relationship not found', 404)
    rel = next((x for x in store.list_relationships(root, limit=10 ** 7)['items'] if x['edge_id'] == eid), None)
    d = (rel or {}).get('decision')
    ev = e.get('evidence') or {}
    child = _col_summary(root, e['child_connection_id'], e['child_table'], e['child_column'])
    parent = _col_summary(root, e['parent_connection_id'], e['parent_table'], e['parent_column'])
    child['samples'] = _sample_values(root, e['child_connection_id'], e['child_table'], e['child_column'])
    parent['samples'] = _sample_values(root, e['parent_connection_id'], e['parent_table'], e['parent_column'])
    hist = store.events(root, artifact_id='relationship:' + eid, limit=20)['items']
    return {'edge_id': eid, 'child': child, 'parent': parent, 'score': e['score'], 'score_label': e.get('score_label'),
            'freshness': e.get('freshness'), 'status': (rel or {}).get('effective_status') or 'pending',
            'hint': ev.get('cardinality_hint'), 'revision': (d or {}).get('revision', 0),
            'decision': {'status': d['status'], 'actor': d['actor'], 'at': d['updated_at'], 'approved': d.get('approved')} if d else None,
            'evidence': {'coverage_pct': ev.get('distinct_coverage_pct'), 'matched': ev.get('overlapping_distinct'),
                         'child_keys': ev.get('child_distinct_sampled'), 'parent_keys': ev.get('parent_distinct_sampled'),
                         'parent_unique_pct': ev.get('parent_uniqueness_pct'), 'child_missing': ev.get('child_missing_sampled'),
                         'parent_missing': ev.get('parent_missing_sampled'), 'same_family': ev.get('lexical_family_match'),
                         'reasons': _plain(ev.get('reasons')), 'limitations': _plain(ev.get('limitations')),
                         'manual': bool(ev.get('manual')), 'added_by': ev.get('added_by'), 'added_at': ev.get('added_at'), 'added_note': ev.get('note'),
                         'warnings': ev.get('warnings') or [], 'whole_table': bool(ev.get('whole_table') or ev.get('parent_checked_all_rows')),
                         'row_coverage_pct': ev.get('row_coverage_pct')},
            'prerequisites_met': child['role_status'] == 'approved' and parent['role_status'] == 'approved',
            'risks': RELATIONSHIP_RISKS, 'history': hist}


def _decide_relationship_one(root, b):
    from binggio.registry import store
    if not (b.get('note') or '').strip(): raise ApiError('Write the review rationale: what evidence did you check?')
    kw = {}
    if b['action'] == 'approve':
        kw = {'cardinality': b.get('cardinality') or 'many_to_one', 'temporal_rule': b.get('temporal_rule') or '',
              'join_note': b.get('join_note') or '', 'child_column_override': b.get('child_column_override') or '',
              'parent_column_override': b.get('parent_column_override') or ''}
    r = store.decide_relationship(root, b['edge_id'], action=b['action'], actor=_by(b), note=b['note'].strip(),
                                  expected_revision=int(b.get('expected_revision') or 0), **kw)
    return {'ok': True, 'revision': r.get('revision'), 'status': r.get('status')}


def _role_detail(root, cid, table, column):
    from binggio.registry import store
    from binggio.discovery import store as graph
    col = _col_summary(root, cid, table, column)
    col['samples'] = _sample_values(root, cid, table, column)
    rels = {x['edge_id']: x['effective_status'] for x in store.list_relationships(root, limit=10 ** 7)['items']}
    links = []
    for e in graph.all_edges(root):
        here = ((e['child_connection_id'], e['child_table'], e['child_column']) == (cid, table, column)
                or (e['parent_connection_id'], e['parent_table'], e['parent_column']) == (cid, table, column))
        if here:
            links.append({'edge_id': e['id'], 'label': '%s.%s → %s.%s' % (e['child_table'], e['child_column'], e['parent_table'], e['parent_column']),
                          'status': rels.get(e['id'], 'pending'), 'score': e['score']})
    defs = []
    for x in store.list_definitions(root):
        p = x.get('approved') or x.get('proposed') or {}
        if any((s.get('connection_id'), s.get('table'), s.get('column')) == (cid, table, column) for s in p.get('sources') or []):
            defs.append({'id': x['id'], 'name': p.get('name'), 'aggregation': p.get('aggregation'), 'status': x['effective_status']})
    hist = store.events(root, artifact_id=col['decision']['id'], limit=20)['items'] if col.get('decision') else []
    return {**col, 'links': links, 'definitions': defs, 'history': hist, 'roles': sorted(store.ROLES),
            'checks': ['The business meaning matches the role, not just the column name.',
                       'Missing values, duplicates and sample coverage are acceptable for how it will be used.',
                       'It is the right kind: a business identifier, a technical key, an amount to add up, a date, or a category.']}


def _definitions(root):
    from binggio.registry import store
    items = []
    for x in store.list_definitions(root):
        p = x.get('approved') or x.get('proposed') or {}
        items.append({'id': x['id'], 'kind': x['kind'], 'name': p.get('name'), 'description': p.get('description'),
                      'domain': p.get('domain'), 'owner': p.get('owner'), 'aggregation': p.get('aggregation'),
                      'sources': p.get('sources') or [], 'status': x['effective_status'], 'stored_status': x['status'],
                      'revision': x['revision'], 'actor': x['actor'], 'at': x['updated_at']})
    import re as _re
    taken = {(s['connection_id'], s['table'], s['column'], d['aggregation']) for d in items for s in d['sources']}
    measures = [{'connection': r['connection_id'], 'table': r['table'], 'column': r['column']}
                for r in _all_roles(root, status='approved')
                if ((r.get('decision') or {}).get('approved') or {}).get('role') == 'MEASURE']
    # Suggestions: approved measures whose names say they add up (amounts, balances, fees). Never approved automatically.
    addable = _re.compile(r'(?:^|_)(amount|amt|balance|revenue|sales|fee|fees|cost|price|value|quantity|qty|total|exposure|principal|interest)(?:_|$)', _re.I)
    not_addable = _re.compile(r'(rate|pct|percent|ratio|score|age|days|year|month|number|_no$|_id$|price)', _re.I)
    sugg = [{'name': 'Total %s (%s)' % (m['column'].replace('_', ' ').lower(), m['table']), 'aggregation': 'SUM',
             'ref': {'connection_id': m['connection'], 'table': m['table'], 'column': m['column']},
             'warning': 'Check what to include and the sign convention (refunds, reversals) before approving.'}
            for m in measures if addable.search(m['column']) and not not_addable.search(m['column'])
            and (m['connection'], m['table'], m['column'], 'SUM') not in taken][:25]
    return {'items': items, 'suggestions': sugg, 'measures': measures, 'aggregations': sorted(store.AGGREGATIONS - {'NONE'})}


def _decide_definition(root, b):
    from binggio.registry import store
    by, note = _by(b), (b.get('note') or '').strip()
    if not note: raise ApiError('Add a note: why is this the right definition?')
    if b.get('action') == 'create' or (b.get('action') == 'revise'):
        d = b.get('definition') or {}
        definition = {'kind': 'metric', 'name': (d.get('name') or '').strip(), 'description': (d.get('description') or '').strip(),
                      'domain': (d.get('domain') or 'General').strip(), 'owner': (d.get('owner') or by).strip(),
                      'aggregation': d.get('aggregation'), 'sources': [{'connection_id': d['connection'], 'table': d['table'], 'column': d['column']}]}
        if b['action'] == 'create':
            r = store.create_definition(root, definition, actor=by, note=note)
            return {'ok': True, 'id': r['id']}
        cur = store.get_artifact(root, b['id'])
        r = store.decide_definition(root, b['id'], action='revise', actor=by, note=note, expected_revision=cur['revision'], definition=definition)
        return {'ok': True, 'id': b['id']}
    cur = store.get_artifact(root, b['id'])
    if cur is None: raise ApiError('Definition not found', 404)
    store.decide_definition(root, b['id'], action=b['action'], actor=by, note=note, expected_revision=cur['revision'])
    return {'ok': True, 'id': b['id']}


def _autonomy_action(root, b):
    from binggio.registry import autonomy
    act = b.get('action')
    if act == 'enable': return {'status': autonomy.set_enabled(root, True, b.get('confirmation'))}
    if act == 'disable': return {'status': autonomy.set_enabled(root, False, None)}
    if act == 'run': return autonomy.run(root)
    if act == 'rollback': return autonomy.rollback(root, b.get('run_id'))
    raise ApiError('Unknown action')


def _paths(root, a, b, max_hops=4, limit=5):
    """Shortest link paths between two tables, through approved and waiting relationships."""
    from binggio.registry import store
    from binggio.discovery import store as graph
    if not a or not b or a == b: raise ApiError('Choose two different tables')
    status = {x['edge_id']: x['effective_status'] for x in store.list_relationships(root, limit=10 ** 7)['items']}
    adj = {}
    for e in graph.all_edges(root):
        st = status.get(e['id'], 'pending')
        if st in ('rejected', 'revoked'): continue
        u, v = e['child_connection_id'] + '::' + e['child_table'], e['parent_connection_id'] + '::' + e['parent_table']
        step = {'edge_id': e['id'], 'status': st, 'label': '%s.%s → %s.%s' % (e['child_table'], e['child_column'], e['parent_table'], e['parent_column'])}
        adj.setdefault(u, []).append((v, step)); adj.setdefault(v, []).append((u, step))
    paths, frontier = [], [(a, [], {a})]
    for _ in range(max_hops):
        nxt = []
        for node, steps, seen in frontier:
            for other, step in adj.get(node, []):
                if other in seen: continue
                if other == b: paths.append(steps + [step])
                else: nxt.append((other, steps + [step], seen | {other}))
        if paths: break
        frontier = nxt[:5000]
    out = [{'hops': len(p), 'all_approved': all(s['status'] == 'approved' for s in p), 'steps': p} for p in paths[:limit]]
    out.sort(key=lambda x: (not x['all_approved'], x['hops']))
    return {'from': a, 'to': b, 'paths': out}


# ====================================================================== 0.1.4: fixes, keys, links, discovery report

def _conn(root, cid):
    from binggio.catalog import catalog
    c = next((x for x in catalog.list_connections(root) if x['id'] == cid), None)
    if c is None: raise ApiError('Unknown source', 404)
    return c


def _side(x):
    if not isinstance(x, dict) or not all(x.get(k) for k in ('connection', 'table', 'column')):
        raise ApiError('Choose the table and column on both sides')
    return (x['connection'], x['table'], x['column'])


def _fixes_info(root, cid, table):
    from binggio import prepared
    conn = _conn(root, cid)
    history = prepared.list_fixes(root, cid, table, include_removed=True)
    try: header = prepared.header_with_fixes(root, conn, table)
    except Exception as e: raise ApiError(str(e))
    info = None
    if prepared.is_ready(root, conn, table):
        try:
            i = prepared.info(root, conn, table)
            info = {'rows': i['rows'], 'built_at': i['built_at'], 'notes': {n['fix']: n for n in i.get('fix_notes') or []}}
        except Exception:
            info = None
    return {'connection': cid, 'table': table, 'columns': header, 'active': [f for f in history if f['status'] == 'active'],
            'removed': [f for f in history if f['status'] != 'active'], 'kinds': prepared.KINDS, 'date_formats': list(prepared.DATE_INPUTS),
            'prepared': info}


def _fix_action(app, ws, b):
    from binggio import prepared
    from binggio.profiling import store as profiles
    root = ws.root
    conn = _conn(root, b['connection']); table = b['table']
    try:
        if b.get('action') == 'remove':
            f = prepared.remove_fix(root, b['id'], _by(b), b.get('note'))
        else:
            f = prepared.add_fix(root, conn, table, b.get('kind'), b.get('column'), b.get('params') or {}, _by(b), b.get('note'))
    except prepared.FixError as e:
        raise ApiError(str(e))

    def run(step):
        step(0); prepared.ensure(root, conn, table)
        step(1); profiles.run_table(root, conn['id'], table, force=True)
        step(2)
        try: d = ws.discover(); found = d.get('candidate_edges')
        except Exception as e: found = None; step(2, 'failed', str(e)[:160])
        return {'fix': f, 'candidate_relationships': found}
    job = app.start_job('fix', ['Applying the data fixes to every row', 'Profiling the table again', 'Finding relationships again'], run)
    return {'fix': f, 'job': job}


def _make_key(root, b):
    """Approve a column as the table's own key, only when it is unique and never empty on every row."""
    from binggio import prepared
    from binggio.registry import store
    cid, table, column = b['connection'], b['table'], b['column']
    note = (b.get('note') or '').strip()
    if not note: raise ApiError('Say why this column identifies the rows of %s' % table)
    chk = prepared.key_check(root, _conn(root, cid), table, [column])
    if not chk['unique']:
        why = []
        if chk['missing_rows']: why.append('%s rows have no value' % '{:,}'.format(chk['missing_rows']))
        if chk['duplicate_rows']: why.append('%s rows repeat a value (%s%%)' % ('{:,}'.format(chk['duplicate_rows']), chk['duplicate_pct']))
        raise ApiError('%s cannot be the key of %s: %s, so each value would not point at exactly one row and joined totals would be wrong. '
                       'Add the data fix "Keep one row for each key value" (if the repeats are true duplicates), or combine it with another '
                       'column into a new key, then try again.' % (column, table, ' and '.join(why)))
    cur = next((r for r in _all_roles(root, connection_id=cid, table=table) if r['column'] == column), None)
    if cur is None: raise ApiError('Profile the table first (Settings > Profile and discover again)')
    rev = cur['decision']['revision'] if cur['decision'] else 0
    r = store.decide_column(root, cid, table, column, action='approve', actor=_by(b),
                            note='%s (checked on all %s rows: unique, never empty)' % (note, '{:,}'.format(chk['rows'])),
                            expected_revision=rev, role='PRIMARY_KEY_CANDIDATE')
    return {'ok': True, 'check': chk, 'revision': r['revision']}


def _discovery_report(root):
    from binggio.discovery import store as graph
    from binggio.catalog import catalog
    rep = graph.attempts(root)
    names = {c['id']: c['name'] for c in catalog.list_connections(root)}
    if not rep:
        return {'ran': False, 'message': 'Relationship discovery has not run with this version yet. Run Settings > Profile and discover again.'}
    tables = rep.get('tables') or []
    cols = rep.get('columns') or []
    for x in tables + cols: x['source'] = names.get(x.get('connection_id'), x.get('connection_id'))
    summ = {'tables': len(tables), 'checked': sum(t['status'] == 'checked' for t in tables),
            'skipped': sum(t['status'] != 'checked' for t in tables), 'linked_tables': sum(bool(t.get('linked')) for t in tables),
            'unlinked_tables': [t['table'] for t in tables if not t.get('linked')],
            'columns_without_link': len(cols), 'format_hints': sum(1 for c in cols if c.get('format_hint')),
            'near_misses': sum(1 for c in cols if c.get('near_misses'))}
    return {'ran': True, 'generated_at': rep.get('generated_at'), 'rules': rep.get('rules'), 'summary': summ, 'tables': tables, 'columns': cols,
            'manual': sorted(graph.manual_ids(root))}


def _link_action(root, b):
    from binggio import links
    from binggio.registry import store
    from binggio.discovery import store as graph
    act = b.get('action') or 'add'
    if act == 'remove':
        rel = store.get_artifact(root, 'relationship:' + b['edge_id'])
        if rel and rel['status'] == 'approved': raise ApiError('Revoke the approval first, then remove the link')
        try: graph.remove_manual_edge(root, b['edge_id'], _by(b))
        except Exception as e: raise ApiError(str(e))
        return {'ok': True}
    note = (b.get('note') or '').strip()
    try:
        r = links.add(root, _side(b['child']), _side(b['parent']), _by(b), note)
    except links.LinkError as e:
        raise ApiError(str(e))
    out = {'edge_id': r['edge']['id'], 'already_proposed': r['already_proposed'], 'check': r['check'], 'approved': False}
    if b.get('approve'):
        card = b.get('cardinality') or r['check'].get('suggested_cardinality') or 'many_to_one'
        if r['check']['coverage_pct'] < 45 and not b.get('confirm_weak'):
            out['approve_error'] = 'Only %s%% of the child values are found in the parent. It was added for review but not approved.' % r['check']['coverage_pct']
            return out
        try:
            rev = (store.get_artifact(root, 'relationship:' + out['edge_id']) or {}).get('revision', 0)
            store.decide_relationship(root, out['edge_id'], action='approve', actor=_by(b), note=note, expected_revision=rev, cardinality=card,
                                      join_note=b.get('join_note') or '')
            out['approved'] = True
        except Exception as e:
            if not isinstance(e, _expected_errors()): raise
            out['approve_error'] = str(e)
    return out
