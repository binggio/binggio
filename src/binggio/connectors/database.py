# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Databases (SQL Server, Oracle, PostgreSQL, SQLite): copy chosen tables in, read-only.

Binggio only ever runs SELECT statements against your database, with a row limit, and keeps the
copy in the workspace. The password is used for the connection and never stored; the manifest
records the server, database, schema and tables so the copy can be refreshed later.

Drivers (optional, installed with pip; no vendor client software needed):
  SQL Server  pip install binggio[sqlserver]   (pymssql)
  Oracle      pip install binggio[oracle]      (python-oracledb, thin mode)
  PostgreSQL  pip install binggio[postgres]    (psycopg)
"""
from __future__ import annotations

from binggio.connectors import ConnectorError, DEFAULT_ROW_LIMIT


class Dialect:
    name = 'generic'
    default_port = None

    def quote(self, ident):
        return '"' + str(ident).replace('"', '""') + '"'

    def qualified(self, schema, table):
        return (self.quote(schema) + '.' if schema else '') + self.quote(table)

    def list_tables_sql(self, schema):
        raise NotImplementedError

    def primary_keys_sql(self, schema):
        """Rows of (table, column) for declared primary keys, or None if unsupported."""
        return None

    def select_sql(self, schema, table, limit):
        return 'SELECT * FROM %s LIMIT %d' % (self.qualified(schema, table), int(limit))

    def connect(self, **kw):
        raise NotImplementedError


class SQLServer(Dialect):
    name = 'sqlserver'
    default_port = 1433

    def quote(self, ident):
        return '[' + str(ident).replace(']', ']]') + ']'

    def list_tables_sql(self, schema):
        sql = ("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
        return (sql + ' AND TABLE_SCHEMA = %s ORDER BY TABLE_NAME', (schema,)) if schema else (sql + ' ORDER BY TABLE_SCHEMA, TABLE_NAME', ())

    def primary_keys_sql(self, schema):
        return ("SELECT k.TABLE_NAME, k.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS c "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON c.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
                "AND c.TABLE_SCHEMA = k.TABLE_SCHEMA AND c.TABLE_NAME = k.TABLE_NAME "
                "WHERE c.CONSTRAINT_TYPE = 'PRIMARY KEY' AND c.TABLE_SCHEMA = %s", (schema or 'dbo',))

    def select_sql(self, schema, table, limit):
        return 'SELECT TOP (%d) * FROM %s' % (int(limit), self.qualified(schema or 'dbo', table))

    def connect(self, host, database, user=None, password=None, port=None, **kw):
        try:
            import pymssql
        except ImportError as e:
            raise ConnectorError('SQL Server support needs pymssql: pip install binggio[sqlserver]') from e
        args = {'server': host, 'database': database, 'port': str(port or self.default_port), 'login_timeout': 20,
                'timeout': 600, 'charset': 'UTF-8'}
        if user: args.update(user=user, password=password or '')
        return pymssql.connect(**args)


class Oracle(Dialect):
    name = 'oracle'
    default_port = 1521

    def list_tables_sql(self, schema):
        if schema:
            return 'SELECT owner, table_name FROM all_tables WHERE owner = :1 ORDER BY table_name', (schema.upper(),)
        return 'SELECT USER, table_name FROM user_tables ORDER BY table_name', ()

    def primary_keys_sql(self, schema):
        return ("SELECT cc.table_name, cc.column_name FROM all_constraints c JOIN all_cons_columns cc "
                "ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name "
                "WHERE c.constraint_type = 'P' AND c.owner = :1", (schema.upper() if schema else None,))

    def select_sql(self, schema, table, limit):
        return 'SELECT * FROM %s FETCH FIRST %d ROWS ONLY' % (self.qualified(schema.upper() if schema else None, table), int(limit))

    def connect(self, host, database, user=None, password=None, port=None, **kw):
        try:
            import oracledb
        except ImportError as e:
            raise ConnectorError('Oracle support needs python-oracledb: pip install binggio[oracle]') from e
        dsn = oracledb.makedsn(host, int(port or self.default_port), service_name=database)
        return oracledb.connect(user=user, password=password, dsn=dsn)


class PostgreSQL(Dialect):
    name = 'postgres'
    default_port = 5432

    def list_tables_sql(self, schema):
        return ("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                "AND table_schema = %s ORDER BY table_name", (schema or 'public',))

    def primary_keys_sql(self, schema):
        return ("SELECT k.table_name, k.column_name FROM information_schema.table_constraints c "
                "JOIN information_schema.key_column_usage k ON c.constraint_name = k.constraint_name "
                "AND c.table_schema = k.table_schema AND c.table_name = k.table_name "
                "WHERE c.constraint_type = 'PRIMARY KEY' AND c.table_schema = %s", (schema or 'public',))

    def connect(self, host, database, user=None, password=None, port=None, **kw):
        try:
            import psycopg
        except ImportError as e:
            raise ConnectorError('PostgreSQL support needs psycopg: pip install binggio[postgres]') from e
        return psycopg.connect(host=host, dbname=database, user=user, password=password, port=port or self.default_port,
                               connect_timeout=20, options='-c default_transaction_read_only=on')


class SQLite(Dialect):
    """A local SQLite file opened read-only (also used to test the database path)."""
    name = 'sqlite'

    def list_tables_sql(self, schema):
        return "SELECT 'main', name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name", ()

    def primary_keys_sql(self, schema):
        return None

    def select_sql(self, schema, table, limit):
        return 'SELECT * FROM %s LIMIT %d' % (self.quote(table), int(limit))

    def connect(self, database, **kw):
        import sqlite3
        from pathlib import Path
        return sqlite3.connect('file:%s?mode=ro' % Path(database).resolve().as_posix(), uri=True)

    def sqlite_primary_keys(self, conn, table):
        return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % self.quote(table)) if r[5]]


DIALECTS = {'sqlserver': SQLServer, 'mssql': SQLServer, 'oracle': Oracle, 'postgres': PostgreSQL,
            'postgresql': PostgreSQL, 'sqlite': SQLite}


def dialect(kind):
    try:
        return DIALECTS[kind.lower()]()
    except KeyError:
        raise ConnectorError('Unsupported database type %r; choose one of: %s' % (kind, ', '.join(sorted(set(DIALECTS)))))


def _execute(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params) if params else cur.execute(sql)
    return cur


def list_tables(conn, d, schema=None):
    sql, params = d.list_tables_sql(schema)
    return [(r[0], r[1]) for r in _execute(conn, sql, params).fetchall()]


def read_tables(kind, params, schema=None, tables=None, row_limit=DEFAULT_ROW_LIMIT, connection=None):
    """{table: (headers, rows, info)} for the chosen tables (all base tables of the schema by default)."""
    d = dialect(kind)
    conn = connection or d.connect(**params)
    try:
        found = list_tables(conn, d, schema)
        if not found:
            raise ConnectorError('No tables found%s' % (' in schema %s' % schema if schema else ''))
        wanted = {t.lower() for t in tables} if tables else None
        chosen = [(s, t) for s, t in found if wanted is None or t.lower() in wanted]
        if wanted:
            missing = wanted - {t.lower() for _, t in chosen}
            if missing:
                raise ConnectorError('Not found: %s' % ', '.join(sorted(missing)))
        pks = {}
        pk_sql = d.primary_keys_sql(schema)
        if pk_sql:
            try:
                for t, c in _execute(conn, *pk_sql).fetchall():
                    pks.setdefault(t.lower(), []).append(c)
            except Exception:
                pks = {}  # declared keys are a hint only; lacking rights to read them is fine
        out = {}
        for s, t in chosen:
            cur = _execute(conn, d.select_sql(s, t, row_limit + 1))
            headers = [c[0] for c in cur.description]
            rows = cur.fetchall()
            if isinstance(d, SQLite):
                pk = d.sqlite_primary_keys(conn, t)
            else:
                pk = pks.get(t.lower(), [])
            out[t] = (headers, rows, {'schema': s, 'table': t, 'primary_key': pk, 'row_limit': row_limit})
        return out
    finally:
        if connection is None:
            try: conn.close()
            except Exception: pass
