# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""binggio command line.

  binggio demo  <folder>                 ready-to-ask synthetic bank
  binggio init  <folder>                 empty workspace
  binggio add-csv    <name> <folder|file.csv>  copy a folder of CSV files (or one file) in as a source
  binggio add-sqlite <name> <file>       copy a SQLite database in as a source
  binggio add-excel  <name> <file.xlsx>  each sheet becomes a table (pip install binggio[excel])
  binggio add-db <name> sqlserver|oracle|postgres --host H --database D --user U [--schema S] [--table T ...]
                                         copy tables in, read-only; the password is asked for, never stored
  binggio refresh <connection>           re-read an Excel or database source in place
  binggio profile | discover | status
  binggio review roles|relationships [--status pending] [--table NAME]
  binggio approve-role <connection> <table> <column> <ROLE> --by NAME --note TEXT
  binggio reject-role  <connection> <table> <column> --by NAME --note TEXT
  binggio approve-rel  <edge-id> --cardinality many_to_one --by NAME --note TEXT
  binggio reject-rel   <edge-id> --by NAME --note TEXT
  binggio ask ["question"]               one question, or an interactive conversation
  binggio impact [<connection>]          how a new source fits the existing data (default: newest)
  binggio onboard [<connection>] --by NAME [--accept-suggestions]
                                         guided review of a new source: roles, links, concepts, terms
  binggio migrate                        move an older workspace to the current evidence rules
  binggio ui [--port 8765] [--no-browser] the whole app in your browser (this computer only)
  binggio mcp                            serve governed answers to AI agents (Model Context Protocol, stdio)

Every command works on the workspace in the current folder (or a parent); use -w to choose.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from binggio import __version__
from binggio.workspace import Workspace, WorkspaceError


def _table(rows, cols, limit=None):
    rows = list(rows)[:limit] if limit else list(rows)
    if not rows:
        print('(none)'); return
    widths = [min(48, max(len(c), *(len(str(r.get(c, ''))) for r in rows))) for c in cols]
    print('  '.join(c.ljust(w) for c, w in zip(cols, widths)))
    print('  '.join('-' * w for w in widths))
    for r in rows:
        print('  '.join(str(r.get(c, ''))[:w].ljust(w) for c, w in zip(cols, widths)))


def _print_answer(a):
    if a.get('status') == 'refused':
        print('Refused (%s): %s' % (a.get('code'), a.get('message')))
        for h in a.get('hints') or []:
            print('  hint:', h.get('detail'))
        return
    cols = [str(c).split('::')[-1] for c in a.get('columns') or []]
    rows = [dict(zip(cols, r)) for r in a.get('rows') or []]
    _table(rows, cols, limit=50)
    n = a.get('row_count', len(rows))
    print('\n%s row%s.' % (n, '' if n == 1 else 's') + (' Showing the first 50.' if n > 50 else ''))
    terms = a.get('business_terms') or []
    if terms:
        print('Business terms applied:', ', '.join(t.get('name', str(t)) if isinstance(t, dict) else str(t) for t in terms),
              ('· as of ' + a['as_of']) if a.get('as_of') else '')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='binggio', description='Binggio(TM), Enterprise Context Intelligence: governed answers from your data, approved meanings only.',
                                 epilog='Free software under the GNU AGPL v3 or later. Binggio is a trademark of Ashish Kumar. Feedback: ashish@binggio.com')
    ap.add_argument('-w', '--workspace', default='.')
    ap.add_argument('--version', action='version', version='binggio ' + __version__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('init', 'demo'):
        s = sub.add_parser(name); s.add_argument('folder')
    s = sub.add_parser('add-csv'); s.add_argument('name'); s.add_argument('folder')
    s = sub.add_parser('add-sqlite'); s.add_argument('name'); s.add_argument('file')
    s = sub.add_parser('add-excel'); s.add_argument('name'); s.add_argument('file'); s.add_argument('--sheet', action='append')
    s.add_argument('--row-limit', type=int)
    s = sub.add_parser('add-db'); s.add_argument('name'); s.add_argument('kind')
    for opt in ('--host', '--database', '--user', '--schema'): s.add_argument(opt)
    s.add_argument('--port', type=int); s.add_argument('--table', action='append'); s.add_argument('--row-limit', type=int)
    s.add_argument('--windows-auth', action='store_true', help='SQL Server: use the signed-in Windows account')
    s = sub.add_parser('refresh'); s.add_argument('connection')
    s = sub.add_parser('profile'); s.add_argument('--force', action='store_true')
    sub.add_parser('discover'); sub.add_parser('status'); sub.add_parser('migrate')
    s = sub.add_parser('onboard'); s.add_argument('connection', nargs='?'); s.add_argument('--by', required=True)
    s.add_argument('--accept-suggestions', action='store_true')
    s = sub.add_parser('impact'); s.add_argument('connection', nargs='?'); s.add_argument('--json', action='store_true')
    s = sub.add_parser('review'); s.add_argument('what', choices=['roles', 'relationships'])
    s.add_argument('--status', default='pending'); s.add_argument('--table'); s.add_argument('--json', action='store_true')
    for name in ('approve-role', 'reject-role'):
        s = sub.add_parser(name); s.add_argument('connection'); s.add_argument('table'); s.add_argument('column')
        if name == 'approve-role': s.add_argument('role')
        s.add_argument('--by', required=True); s.add_argument('--note', required=True)
    for name in ('approve-rel', 'reject-rel'):
        s = sub.add_parser(name); s.add_argument('edge')
        if name == 'approve-rel':
            s.add_argument('--cardinality', required=True, choices=['one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'])
        s.add_argument('--by', required=True); s.add_argument('--note', required=True)
    sub.add_parser('mcp', help='serve governed answers to AI agents over the Model Context Protocol (stdio)')
    s = sub.add_parser('ui', help='open Binggio in your browser')
    s.add_argument('--port', type=int, default=8765); s.add_argument('--host', default='127.0.0.1')
    s.add_argument('--no-browser', action='store_true')
    s = sub.add_parser('ask'); s.add_argument('question', nargs='?')
    s.add_argument('--plan', help='run a saved canonical plan (JSON file) instead of asking the model')
    s.add_argument('--json', action='store_true', help='print the answer as JSON')
    a = ap.parse_args(argv)
    from binggio.connectors import ConnectorError
    from binggio.catalog.connectors import ConnectionErrorSafe
    try:
        return _run(a)
    except (WorkspaceError, ValueError, ConnectorError, ConnectionErrorSafe) as e:
        print('binggio:', e, file=sys.stderr); return 2


def _run(a):
    if a.cmd == 'init':
        ws = Workspace.create(a.folder); print('Created workspace', ws.root); return 0
    if a.cmd == 'demo':
        ws = Workspace.demo(a.folder); print('Demo bank ready in', ws.root); print(json.dumps(ws.status()['answerable'])); return 0
    if a.cmd == 'ui':
        from binggio.ui.server import serve
        try: ws = Workspace.open(a.workspace)
        except WorkspaceError: ws = None
        return serve(ws, host=a.host, port=a.port, open_browser=not a.no_browser)
    if a.cmd == 'mcp':
        from binggio.mcp_server import serve as serve_mcp
        return serve_mcp(Workspace.open(a.workspace))
    ws = Workspace.open(a.workspace)
    from binggio.registry import store
    if a.cmd == 'add-csv':
        c = ws.add_csv_folder(a.name, a.folder); print('Added', c['id'], '·', c.get('table_count'), 'tables'); return 0
    if a.cmd == 'add-sqlite':
        c = ws.add_sqlite(a.name, a.file); print('Added', c['id'], '·', c.get('table_count'), 'tables'); return 0
    if a.cmd == 'add-excel':
        c = ws.add_excel(a.name, a.file, sheets=a.sheet, row_limit=a.row_limit)
        print('Added', c['id'], '·', c.get('table_count'), 'tables')
        for n in c['notes']: print('  note:', n)
        return 0
    if a.cmd == 'add-db':
        import getpass, os
        password = None
        if a.kind.lower() != 'sqlite' and a.user and not a.windows_auth:
            password = os.environ.get('BINGGIO_DB_PASSWORD') or getpass.getpass('Password for %s (not shown, not stored): ' % a.user)
        c = ws.add_database(a.name, a.kind, host=a.host, database=a.database, user=a.user, password=password,
                            port=a.port, schema=a.schema, tables=a.table, row_limit=a.row_limit)
        print('Added', c['id'], '·', c.get('table_count'), 'tables')
        for n in c['notes']: print('  note:', n)
        return 0
    if a.cmd == 'refresh':
        import getpass, os, json as _j
        pw = os.environ.get('BINGGIO_DB_PASSWORD')
        r = ws.refresh(a.connection, password=pw if pw is not None else None)
        print('Changed:', ', '.join(r['changed_tables']) or 'none', '| unchanged:', len(r['unchanged_tables']))
        print('Next: binggio profile, then review anything that became stale.'); return 0
    if a.cmd == 'profile':
        r = ws.profile(force=a.force); print('Profiled %(profiled_or_cached)s of %(total)s tables' % r, '· failures:', len(r['failures'])); return 0
    if a.cmd == 'discover':
        r = ws.discover(); print('Candidate relationships:', r.get('candidate_edges'), '· tables:', r.get('node_count'))
        if r.get('skipped_tables'): print('Left out (could not be profiled):', ', '.join(t.split('::')[-1] for t in r['skipped_tables']))
        return 0
    if a.cmd == 'status':
        print(json.dumps(ws.status(), indent=2)); return 0
    if a.cmd == 'impact':
        from binggio import impact
        r = impact.report(ws.root, a.connection)
        print(json.dumps(r, indent=2) if a.json else impact.format_report(r)); return 0
    if a.cmd == 'onboard':
        return _onboard(ws, a)
    if a.cmd == 'migrate':
        from binggio.registry import migrate
        if not migrate.needs_migration(ws.root):
            print('Already on content fingerprints. Nothing to do.'); return 0
        print(json.dumps(migrate.migrate(ws.root), indent=2)); return 0
    if a.cmd == 'review':
        if a.what == 'roles':
            items, off = [], 0
            while True:
                page = store.list_roles(ws.root, table=a.table, status=a.status, limit=200, offset=off)
                items += page['items']; off += 200
                if off >= page['total']: break
            rows = [{'connection': x['connection_id'], 'table': x['table'], 'column': x['column'],
                     'suggested': x['candidate_role'], 'status': x['effective_status']} for x in items]
            if a.json: print(json.dumps(rows, indent=2))
            else: _table(rows, ['connection', 'table', 'column', 'suggested', 'status'])
        else:
            page = store.list_relationships(ws.root, status=a.status, limit=200)
            rows = [{'edge': x['edge_id'], 'child': x['child_table'] + '.' + x['child_column'],
                     'parent': x['parent_table'] + '.' + x['parent_column'], 'score': x['score'],
                     'status': x['effective_status']} for x in page['items']
                    if not a.table or a.table in (x['child_table'], x['parent_table'])]
            if a.json: print(json.dumps(rows, indent=2))
            else: _table(rows, ['edge', 'child', 'parent', 'score', 'status'])
        return 0
    if a.cmd in ('approve-role', 'reject-role'):
        item = store.list_roles(ws.root, connection_id=a.connection, table=a.table, limit=200)['items']
        cur = next((x for x in item if x['column'] == a.column), None)
        if cur is None: raise WorkspaceError('Column not found in the review list')
        rev = cur['decision']['revision'] if cur['decision'] else 0
        role = a.role if a.cmd == 'approve-role' else (cur['candidate_role'])
        store.decide_column(ws.root, a.connection, a.table, a.column, action=a.cmd.split('-')[0], actor=a.by,
                            note=a.note, expected_revision=rev, role=role)
        print('Recorded.'); return 0
    if a.cmd in ('approve-rel', 'reject-rel'):
        row = next((r for r in store._rows(ws.root, 'relationship') if r['subject_key'] == a.edge), None)
        rev = row['revision'] if row else 0
        kw = {'cardinality': a.cardinality} if a.cmd == 'approve-rel' else {}
        store.decide_relationship(ws.root, a.edge, action=a.cmd.split('-')[0], actor=a.by, note=a.note,
                                  expected_revision=rev, **kw)
        print('Recorded.'); return 0
    if a.cmd == 'ask':
        if a.plan:
            # A saved plan, or a conversation: [{"question": ..., "plan": {...}}, ...]
            steps = json.loads(Path(a.plan).read_text(encoding='utf-8'))
            if isinstance(steps, dict): steps = [{'question': a.question or 'saved plan', 'plan': steps}]
            answers = [ws.ask(st.get('question') or 'saved plan', plan=st['plan']) for st in steps]
            if a.json:
                print(json.dumps(answers if len(answers) > 1 else answers[0], default=str))
            else:
                for ans in answers: _print_answer(ans)
            return 0 if all(x.get('status') != 'refused' for x in answers) else 3
        if a.question:
            ans = ws.ask(a.question)
            if a.json: print(json.dumps(ans, default=str))
            else: _print_answer(ans)
            return 0 if ans.get('status') != 'refused' else 3
        print('Ask questions about your approved data. Follow-ups like "which of these ..." use the last answer.')
        print('Type "new" for a new conversation, "quit" to leave.')
        while True:
            try: q = input('\nbinggio> ').strip()
            except (EOFError, KeyboardInterrupt): print(); return 0
            if q in ('quit', 'exit'): return 0
            if q == 'new': ws.new_conversation(); print('New conversation.'); continue
            if q: _print_answer(ws.ask(q))
    return 1


def _ask_yn(prompt, default='y'):
    try: ans = input(prompt + (' [Y/n] ' if default == 'y' else ' [y/N] ')).strip().lower()
    except EOFError: ans = ''
    return (ans or default).startswith('y')


def _onboard(ws, a):
    from binggio import onboard, impact
    props = onboard.proposals(ws.root, a.connection)
    cid = props['connection_id']
    print(impact.format_report(props['impact'])); print()
    if a.accept_suggestions:
        out = onboard.apply(ws.root, props, a.by, note='Accepted all suggestions with binggio onboard --accept-suggestions')
        print('Approved: %(roles)d column roles, %(links)d links, %(concepts)d concepts.' % out)
        for x in out['left_pending']: print('  left for review:', x)
    else:
        note = 'Reviewed with binggio onboard'
        print('Step 1 of 4: column roles')
        for table, items in props['roles'].items():
            _table([{'column': i['column'], 'suggested role': i['role']} for i in items], ['column', 'suggested role'])
            if _ask_yn('Approve these roles for %s?' % table):
                onboard.approve_roles(ws.root, cid, table, items, a.by, note)
            print()
        print('Step 2 of 4: links')
        for link in props['links']:
            label = '%s -> %s  (score %.2f, %s)' % (link['child'], link['parent'], link['score'], link['cardinality'])
            if _ask_yn('Approve link ' + label + '?', 'y' if link['suggested'] else 'n'):
                try: onboard.approve_link(ws.root, link, a.by, note)
                except Exception as e: print('  not approved:', e)
        print('\nStep 3 of 4: concepts')
        for c in props['concepts']:
            if _ask_yn('Create concept "%s" (key %s.%s)?' % (c['name'], c['table'], c['key'])):
                try: onboard.create_concept(ws.root, cid, c, a.by, note)
                except Exception as e: print('  not created:', e)
        print('\nStep 4 of 4: words for coded values (Enter to skip)')
        for c in props['coded']:
            for v in c['values']:
                try: words = input('  %s.%s = %s means (words, comma-separated): ' % (c['table'], c['column'], v)).strip()
                except EOFError: words = ''
                if words:
                    try: onboard.create_term(ws.root, cid, c['table'], c['column'], v, words.split(','), a.by, note)
                    except Exception as e: print('  not created:', e)
    st = ws.status()['answerable']
    print('\nAnswerable now: %(tables)s tables, %(relationships)s links, %(concepts)s concepts, %(terms)s terms.' % st)
    return 0


if __name__ == '__main__':
    sys.exit(main())
