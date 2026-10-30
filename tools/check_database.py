# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Check Binggio against a real SQL Server, Oracle or PostgreSQL database.

  python tools/check_database.py sqlserver --host localhost --database master --windows-auth --list
      read-only: connect, list tables, read 5 rows from the first table

  python tools/check_database.py sqlserver --host localhost --database scratch --windows-auth --cross-source
      creates three small tables in a schema you name (default binggio_check), runs the same
      onboarding and answers as the CSV/Excel/SQLite test, compares, then drops the tables.
      Needs rights to create tables; use a scratch database, never production.

The password (if any) is asked for and never stored.
"""
import argparse
import datetime as dt
import getpass
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
import binggio  # noqa: E402
from binggio.connectors import database as db  # noqa: E402

spec = importlib.util.spec_from_file_location('xs', ROOT / 'tests' / 'test_cross_source.py')
xs = importlib.util.module_from_spec(spec); spec.loader.exec_module(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['sqlserver', 'oracle', 'postgres'])
    ap.add_argument('--host', required=True); ap.add_argument('--database', required=True)
    ap.add_argument('--user'); ap.add_argument('--port', type=int); ap.add_argument('--schema')
    ap.add_argument('--windows-auth', action='store_true')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--list', action='store_true'); g.add_argument('--cross-source', action='store_true')
    a = ap.parse_args()
    pw = None if (a.windows_auth or not a.user) else getpass.getpass('Password for %s (not shown, not stored): ' % a.user)
    d = db.dialect(a.kind)
    conn = d.connect(host=a.host, database=a.database, user=a.user, password=pw, port=a.port)
    if a.list:
        tabs = db.list_tables(conn, d, a.schema)
        print('Connected. %d tables%s.' % (len(tabs), ' in ' + a.schema if a.schema else ''))
        for s, t in tabs[:20]: print('  %s.%s' % (s, t))
        if tabs:
            cur = conn.cursor(); cur.execute(d.select_sql(tabs[0][0], tabs[0][1], 5))
            print('First rows of %s: %d read, columns %s' % (tabs[0][1], len(cur.fetchall()), [c[0] for c in cur.description]))
        return
    schema = a.schema or ('BINGGIO_CHECK' if a.kind == 'oracle' else 'binggio_check')
    cur = conn.cursor()
    created = []
    try:
        if a.kind == 'sqlserver':
            cur.execute("IF SCHEMA_ID('%s') IS NULL EXEC('CREATE SCHEMA %s')" % (schema, schema))
        elif a.kind == 'postgres':
            cur.execute('CREATE SCHEMA IF NOT EXISTS %s' % schema)
        for t, (h, rows) in xs.TABLES.items():
            types = {'balance': 'DECIMAL(12,2)', 'opened_on': 'DATE'}
            cols = ', '.join('%s %s' % (c, types.get(c, 'VARCHAR(40)')) for c in h)
            full = d.qualified(schema, t)
            cur.execute('CREATE TABLE %s (%s)' % (full, cols)); created.append(full)
            ph = ', '.join([':%d' % (i + 1) for i in range(len(h))] if a.kind == 'oracle' else ['%s'] * len(h))
            cur.executemany('INSERT INTO %s VALUES (%s)' % (full, ph), [list(r) for r in rows])
        conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            results = {}
            ws = binggio.Workspace.create(Path(tmp) / 'db')
            c = ws.add_database('Check', a.kind, schema=schema, connection=conn)
            xs.onboard(ws, c['id']); results['database'] = xs.answers(ws, c['id'])
            ws2 = binggio.Workspace.create(Path(tmp) / 'csv')
            c2 = ws2.add_csv_folder('Check', xs.as_csv(Path(tmp) / 'csvsrc'))
            xs.onboard(ws2, c2['id']); results['csv'] = xs.answers(ws2, c2['id'])
        same = results['database'] == results['csv']
        print('Same answers from %s and CSV: %s' % (a.kind, 'YES' if same else 'NO'))
        if not same:
            for k in results['csv']:
                if results['csv'][k] != results['database'][k]:
                    print('  differs:', k, '\n    csv:', results['csv'][k], '\n    db: ', results['database'][k])
        sys.exit(0 if same else 1)
    finally:
        for full in reversed(created):
            try: cur.execute('DROP TABLE %s' % full)
            except Exception: pass
        try: conn.commit()
        except Exception: pass
        conn.close()


if __name__ == '__main__':
    main()
