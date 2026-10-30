# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""A Binggio workspace: one folder holding sources, profiles, the candidate graph and the
approved registry. Everything Binggio knows about your data lives here, as plain files.

    import binggio
    ws = binggio.Workspace.create("my-bank")
    ws.add_csv_folder("Core banking export", r"C:\\exports\\core")
    ws.profile(); ws.discover()
    ws.status()
    answer = ws.ask("Which customers have a delinquent loan?")
"""
from __future__ import annotations
import json
import os
import re
import shutil
from pathlib import Path

from binggio.catalog import catalog
from binggio.catalog.connectors import ConnectionErrorSafe
from binggio.profiling import store as profiles
from binggio.discovery import store as graph
from binggio.registry import store as registry

MARKER = 'binggio.json'
FORMAT = 1


class WorkspaceError(Exception):
    pass


def _slug(text):
    s = re.sub(r'[^a-z0-9]+', '_', str(text).lower()).strip('_')
    return s[:40] or 'source'


def _clean(x):
    return Path(os.path.expanduser(str(x).strip().strip('"')))


def _csv_files(spec):
    """The .csv files named by a folder, a file, a list of files or 'a.csv; b.csv'."""
    if isinstance(spec, (list, tuple)):
        items = [_clean(x) for x in spec if str(x).strip()]
    elif _clean(spec).exists():            # one real path, even if its name contains ';'
        items = [_clean(spec)]
    else:
        items = [_clean(x) for x in str(spec).split(';') if x.strip()]
    if not items:
        raise WorkspaceError('Choose a folder or one or more .csv files')
    if len(items) == 1 and items[0].is_dir():
        files = sorted(p for p in items[0].glob('*.csv') if p.is_file())
        if not files: raise WorkspaceError('No .csv files in the folder %s' % items[0])
        return files
    missing = [str(x) for x in items if not x.exists()]
    if missing: raise WorkspaceError('Folder or file not found: %s' % ', '.join(missing[:3]))
    if any(x.is_dir() for x in items) and len(items) > 1:
        raise WorkspaceError('Choose one folder, or files; not several folders at once')
    bad = [x.name for x in items if x.suffix.lower() != '.csv']
    if bad: raise WorkspaceError('Not a .csv file: %s' % ', '.join(bad[:5]))
    names = [x.name.lower() for x in items]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup: raise WorkspaceError('Two files are called %s; each file becomes a table, so names must differ' % ', '.join(dup))
    return items


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / MARKER).is_file():
            raise WorkspaceError('Not a Binggio workspace (no %s): %s' % (MARKER, self.root))
        self._session = None

    @property
    def is_demo(self):
        """The sample bank: its data is fixed so the prepared questions and approvals stay correct."""
        try:
            return bool(json.loads((self.root / MARKER).read_text(encoding='utf-8')).get('demo'))
        except (OSError, ValueError):
            return False

    def _no_data_changes(self):
        if self.is_demo:
            raise WorkspaceError('The sample bank is read-only, so its data stays exactly as prepared. To use your own data, '
                                 'create a new workspace ("New workspace" in the app, or: binggio init <folder>).')

    # ------------------------------------------------------------ create / open
    @classmethod
    def create(cls, root):
        root = Path(root).resolve()
        if (root / MARKER).exists():
            raise WorkspaceError('A workspace already exists here: %s' % root)
        (root / 'data' / 'sources').mkdir(parents=True, exist_ok=True)
        (root / MARKER).write_text(json.dumps({'format': FORMAT, 'kind': 'binggio-workspace'}, indent=2), encoding='utf-8')
        ws = cls(root)
        catalog.list_connections(ws.root)  # creates the catalog
        from binggio.registry import migrate
        migrate.mark_current(ws.root)
        return ws

    @classmethod
    def open(cls, start='.'):
        """The workspace containing `start` (searching parent folders)."""
        p = Path(start).resolve()
        for cand in [p, *p.parents]:
            if (cand / MARKER).is_file():
                return cls(cand)
        raise WorkspaceError('No Binggio workspace found here or above. Run: binggio init <folder>')

    @classmethod
    def demo(cls, root, locked=True):
        """A ready-to-ask copy of the synthetic demo bank (approved registry included). Its data is
        locked (no sources can be added or refreshed) unless locked=False, which tests use to
        check onboarding on top of an approved bank."""
        root = Path(root).resolve()
        if root.exists() and any(root.iterdir()):
            raise WorkspaceError('Choose an empty or new folder for the demo: %s' % root)
        src = Path(__file__).resolve().parent / 'demo_bank'
        if not src.is_dir():
            raise WorkspaceError('The demo bank is not bundled with this installation')
        shutil.copytree(src, root, dirs_exist_ok=True)
        (root / MARKER).write_text(json.dumps({'format': FORMAT, 'kind': 'binggio-workspace', 'demo': bool(locked)}, indent=2), encoding='utf-8')
        return cls(root)

    # ------------------------------------------------------------ sources
    def _copy_in(self, name, copy):
        self._no_data_changes()
        dest = self.root / 'data' / 'sources' / _slug(name)
        n, base = 2, dest
        while dest.exists():
            dest = base.with_name('%s_%d' % (base.name, n)); n += 1
        copy(dest)
        return dest.relative_to(self.root / 'data').as_posix()

    def add_csv_folder(self, name, folder):
        """A folder of .csv files, one .csv file, or several files (a list, or paths separated by
        ';'). Each file becomes a table."""
        files = _csv_files(folder)

        def copy(dest):
            dest.mkdir(parents=True)
            for f in files: shutil.copy2(f, dest / f.name)
        rel = self._copy_in(name, copy)
        return catalog.add_connection(self.root, name=name, kind='csv_folder', source_path=rel)

    def add_sqlite(self, name, path):
        path = Path(path)
        if not path.is_file():
            raise WorkspaceError('SQLite file not found: %s' % path)

        def copy(dest):
            dest.mkdir(parents=True)
            shutil.copy2(path, dest / (path.stem + '.sqlite'))
        rel = self._copy_in(name, copy)
        return catalog.add_connection(self.root, name=name, kind='sqlite', source_path=rel + '/' + path.stem + '.sqlite')

    def _add_snapshot(self, name, source, tables, notes=()):
        from binggio.connectors import write_snapshot
        holder = {}

        def copy(dest):
            holder['manifest'] = write_snapshot(dest, source, tables)
        rel = self._copy_in(name, copy)
        conn = catalog.add_connection(self.root, name=name, kind='csv_folder', source_path=rel)
        truncated = [t for t, m in holder['manifest']['tables'].items() if m['truncated_at_row_limit']]
        notes = list(notes) + ['Table %s was cut at the row limit (%d rows); answers about it would be partial, so raise the limit' % (
            t, holder['manifest']['tables'][t]['row_limit']) for t in truncated]
        return {**conn, 'notes': notes, 'manifest': holder['manifest']}

    def add_excel(self, name, path, sheets=None, row_limit=None):
        """Each sheet of an .xlsx workbook becomes a table (needs: pip install binggio[excel])."""
        from binggio.connectors import excel, DEFAULT_ROW_LIMIT
        tables, notes = excel.read_workbook(path, row_limit or DEFAULT_ROW_LIMIT, sheets)
        return self._add_snapshot(name, {'kind': 'excel', 'file': str(Path(path).resolve())}, tables, notes)

    def add_database(self, name, kind, host=None, database=None, user=None, password=None, port=None,
                     schema=None, tables=None, row_limit=None, connection=None):
        """Copy tables from SQL Server, Oracle, PostgreSQL or SQLite, read-only. The password is
        used for this connection only and never stored."""
        from binggio.connectors import database as db, DEFAULT_ROW_LIMIT
        params = {'host': host, 'database': database, 'user': user, 'password': password, 'port': port}
        data = db.read_tables(kind, params, schema=schema, tables=tables, row_limit=row_limit or DEFAULT_ROW_LIMIT,
                              connection=connection)
        source = {'kind': kind, 'host': host, 'port': port, 'database': database if kind != 'sqlite' else str(Path(database).resolve()),
                  'user': user, 'schema': schema, 'tables': sorted(data)}
        return self._add_snapshot(name, source, data)

    def refresh(self, connection_id, password=None, connection=None):
        """Re-read an Excel or database source into the same place. Approvals stay bound to the
        old content: tables whose data changed make their approvals stale until re-confirmed;
        unchanged tables keep theirs."""
        self._no_data_changes()
        from binggio.connectors import write_snapshot, MANIFEST, excel, database as db
        conn = next((c for c in self.connections() if c['id'] == connection_id), None)
        if conn is None: raise WorkspaceError('Unknown source: %s' % connection_id)
        folder = self.root / 'data' / conn['source_path']
        try:
            manifest = json.loads((folder / MANIFEST).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise WorkspaceError('Only Excel and database sources can be refreshed (re-add CSV folders)')
        src = manifest['source']
        limit = max([t.get('row_limit') for t in manifest['tables'].values()] or [None]) or None
        if src['kind'] == 'excel':
            tables, notes = excel.read_workbook(src['file'], limit or 200_000)
        else:
            params = {k: src.get(k) for k in ('host', 'database', 'user', 'port')}
            params['password'] = password
            tables, notes = db.read_tables(src['kind'], params, schema=src.get('schema'), tables=src.get('tables'),
                                           row_limit=limit or 200_000, connection=connection), []
        before = {t: (folder / (t + '.csv')).read_bytes() for t in manifest['tables'] if (folder / (t + '.csv')).exists()}
        new = write_snapshot(folder, src, tables, replace=True)
        changed = [t for t in new['tables'] if before.get(t) != (folder / (t + '.csv')).read_bytes()]
        catalog.refresh_connection(self.root, connection_id)
        return {'changed_tables': changed, 'unchanged_tables': [t for t in new['tables'] if t not in changed], 'notes': notes}

    def connections(self):
        return catalog.list_connections(self.root)

    # ------------------------------------------------------------ pipeline
    def profile(self, force=False):
        return profiles.run_all(self.root, force=force)

    def discover(self):
        """Find candidate relationships. An older workspace is first moved to the current evidence
        rules (binggio migrate), so improved discovery never makes existing approvals stale."""
        from binggio.registry import migrate
        if migrate.needs_migration(self.root):
            migrate.migrate(self.root)
            return graph.summary(self.root)
        return graph.run_all(self.root)

    def status(self):
        with profiles.evaluation_scope():
            return self._status()

    def _status(self):
        conns = self.connections()
        prof = profiles.summary(self.root)
        try:
            g = graph.summary(self.root)
        except Exception:
            g = {}
        reg = registry.summary(self.root)
        from binggio.context.context import approved_context
        try:
            ctx = approved_context(self.root)
            universe = {'tables': len(ctx['tables']), 'relationships': len(ctx['joins']),
                        'concepts': len(ctx.get('concepts') or {}), 'terms': len(ctx.get('governed_terms') or {}),
                        'as_of': (ctx.get('as_of') or {}).get('date')}
        except Exception as e:
            universe = {'error': str(e)}
        return {'workspace': str(self.root), 'sources': len(conns),
                'tables': sum(c.get('table_count') or 0 for c in conns),
                'profiled': prof.get('profiled'), 'stale_profiles': prof.get('stale'),
                'candidate_relationships': g.get('candidate_edges'),
                'registry': reg['registry_status'], 'answerable': universe}

    # ------------------------------------------------------------ asking
    def new_conversation(self):
        from binggio.engine import sessions
        self._session = sessions.create(self.root)['session_id']
        return self._session

    def ask(self, question, plan=None, conversation=None):
        """Answer from approved meanings only, or refuse with the reason. Follow-ups such as
        "which of these ..." use the previous answer in the same conversation.

        `conversation` is a conversation id from new_conversation() (default: this object's own).
        `plan` runs a canonical plan directly (no model call); it is validated exactly like a
        plan the model produced. Useful for tests, scripted checks and reproducing an answer."""
        from binggio.engine import sessions
        from binggio.engine.runtime import execute_question, PipelineFailure
        if conversation is None:
            if self._session is None:
                self.new_conversation()
            sid = self._session
        else:
            sid = conversation
        rev = sessions.read(self.root, sid)['revision']
        try:
            answer, plan, cohort = execute_question(self.root, sid, question, canonical_plan=plan)
        except PipelineFailure as e:
            sessions.append(self.root, sid, rev, question, 'blocked', str(e))
            return {'status': 'refused', 'stage': e.stage, 'code': e.code, 'message': str(e),
                    'hints': getattr(e, 'hints', [])}
        if cohort: cohort['from_turn'] = 'pending'
        tid, _ = sessions.append(self.root, sid, rev, question, 'answered', plan=plan, cohort=cohort, response=answer)
        if cohort:
            cohort['from_turn'] = tid
            with sessions.connect(self.root) as db:
                db.execute('UPDATE turns SET cohort_json=? WHERE id=? AND session_id=?', (json.dumps(cohort), tid, sid))
        return {**answer, 'plan': plan}
