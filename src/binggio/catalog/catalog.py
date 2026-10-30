# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Persistent Phase4 local catalog. No auto-discovery or approval claims.

Metadata scans are bounded, read-only and stored in a separate SQLite catalog.
This module never imports test-only ground truth.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .connectors import (ConnectionErrorSafe, list_csv_tables, list_sqlite_tables,
                         read_csv_metadata, read_sqlite_metadata, resolve_source)

_LOCK = threading.RLock()
DEFAULTS = []  # the package registers no seed sources
_POC_DEFAULTS = [
    ("csv_clean", "CSV — clean reference", "csv_folder", "phase4_workbench/csv_clean"),
    ("sqlite_imperfect", "SQLite — imperfect reference", "sqlite", "phase4_workbench/sqlite_imperfect/enterprise_demo.db"),
    ("csv_messy", "CSV — messy reference", "csv_folder", "phase4_workbench/csv_messy"),
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db(project: Path) -> sqlite3.Connection:
    target = Path(project).resolve() / "data" / "phase4_catalog" / "catalog.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
        source_path TEXT NOT NULL, created_at TEXT NOT NULL, last_scan TEXT,
        scan_status TEXT NOT NULL DEFAULT 'pending', last_error TEXT,
        UNIQUE(kind, source_path))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS catalog_tables (
        connection_id TEXT NOT NULL, schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL, row_count INTEGER, column_count INTEGER,
        scan_status TEXT NOT NULL, warnings_json TEXT NOT NULL,
        columns_json TEXT NOT NULL, scanned_at TEXT NOT NULL,
        PRIMARY KEY(connection_id, schema_name, table_name),
        FOREIGN KEY(connection_id) REFERENCES connections(id) ON DELETE CASCADE)""")
    conn.commit()
    return conn


def init_catalog(project: Path) -> None:
    with _LOCK, _db(project) as db:
        for cid, name, kind, rel in DEFAULTS:
            try:resolve_source(project, rel, kind)
            except ConnectionErrorSafe:continue
            db.execute("""INSERT OR IGNORE INTO connections
                (id,name,kind,source_path,created_at,scan_status)
                VALUES (?,?,?,?,?,'pending')""", (cid,name,kind,rel,_utc()))
        db.commit()
        for cid, *_ in DEFAULTS:
            row = db.execute("SELECT id, last_scan FROM connections WHERE id=?", (cid,)).fetchone()
            if row and row["last_scan"] is None:
                refresh_connection(project, cid)


def list_connections(project: Path) -> list[dict]:
    with _LOCK, _db(project) as db:
        return [dict(r) for r in db.execute("""SELECT c.*,
            (SELECT COUNT(*) FROM catalog_tables t WHERE t.connection_id=c.id) AS table_count
            FROM connections c ORDER BY c.created_at,c.name""").fetchall()]


def add_connection(project: Path, *, name: str, kind: str, source_path: str) -> dict:
    name = str(name or "").strip()
    if not (1 <= len(name) <= 80):raise ConnectionErrorSafe("Connection name must contain 1–80 characters")
    resolve_source(project, source_path, kind)
    cid = "conn_" + uuid.uuid4().hex[:14]
    with _LOCK, _db(project) as db:
        try:
            db.execute("INSERT INTO connections (id,name,kind,source_path,created_at) VALUES (?,?,?,?,?)",
                       (cid,name,kind,source_path,_utc()))
            db.commit()
        except sqlite3.IntegrityError as exc:
            raise ConnectionErrorSafe("This source is already registered") from exc
    try:
        refresh_connection(project, cid)
    except Exception:
        # Keep connection with failed scan status for diagnostic visibility.
        pass
    return next(c for c in list_connections(project) if c["id"] == cid)


def remove_connection(project: Path, cid: str) -> None:
    if cid in {c[0] for c in DEFAULTS}:
        raise ConnectionErrorSafe("Seed connections cannot be deleted; add another source instead")
    with _LOCK, _db(project) as db:
        cur = db.execute("DELETE FROM connections WHERE id=?", (cid,))
        db.commit()
        if not cur.rowcount:raise ConnectionErrorSafe("Connection not found")


def refresh_connection(project: Path, cid: str) -> dict:
    with _LOCK, _db(project) as db:
        row = db.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if row is None:raise ConnectionErrorSafe("Connection not found")
        try:
            source = resolve_source(project, row["source_path"], row["kind"])
            names = list_csv_tables(source) if row["kind"] == "csv_folder" else list_sqlite_tables(source)
            if not names:raise ConnectionErrorSafe("No supported tables found")
            staged = []
            failures = 0
            for name in names:
                try:
                    if row["kind"] == "csv_folder":
                        p = source / (name + ".csv")
                        if not p.is_file() or not p.resolve().is_relative_to(source):
                            raise ConnectionErrorSafe("Source CSV escaped folder or was removed")
                        meta = read_csv_metadata(p, preview=0)
                    else:meta = read_sqlite_metadata(source, name, preview=0)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    meta = {"columns": [], "row_count": None, "warnings": [str(exc)[:180]],
                            "scan_status": "error"}
                    failures += 1
                staged.append((cid, "files" if row["kind"] == "csv_folder" else "main", name,
                               meta.get("row_count"), len(meta["columns"]),meta["scan_status"],
                               json.dumps(meta["warnings"]), json.dumps(meta["columns"]),_utc()))
            # Swap metadata atomically; failed connection scans don't discard old state.
            db.execute("DELETE FROM catalog_tables WHERE connection_id=?", (cid,))
            db.executemany("""INSERT INTO catalog_tables
                (connection_id,schema_name,table_name,row_count,column_count,scan_status,
                 warnings_json,columns_json,scanned_at) VALUES (?,?,?,?,?,?,?,?,?)""", staged)
            db.execute("UPDATE connections SET last_scan=?,scan_status=?,last_error=NULL WHERE id=?",
                       (_utc(),"partial" if failures else "scanned",cid))
            db.commit()
        except Exception as exc:
            db.execute("UPDATE connections SET scan_status='error',last_error=? WHERE id=?",(str(exc)[:220],cid))
            db.commit()
            raise
    return next(c for c in list_connections(project) if c["id"] == cid)



def list_schemas(project: Path, connection_id: str | None = None) -> list[dict]:
    with _LOCK, _db(project) as db:
        sql = """SELECT connection_id,schema_name,COUNT(*) AS table_count
                 FROM catalog_tables WHERE 1=1"""
        args = []
        if connection_id:
            sql += " AND connection_id=?";args.append(connection_id)
        sql += " GROUP BY connection_id,schema_name ORDER BY connection_id,schema_name"
        return [dict(r) for r in db.execute(sql,args).fetchall()]

def list_tables(project: Path, *, connection_id: str | None = None, search: str = "", limit: int = 250) -> list[dict]:
    limit = min(max(int(limit),1), 5000)
    with _LOCK, _db(project) as db:
        sql = """SELECT t.connection_id,t.schema_name,t.table_name,t.row_count,t.column_count,
                 t.scan_status,t.warnings_json,t.scanned_at,c.name AS connection_name,c.kind
                 FROM catalog_tables t JOIN connections c ON c.id=t.connection_id WHERE 1=1"""
        args: list[Any] = []
        if connection_id:
            sql += " AND t.connection_id=?";args.append(connection_id)
        if search:
            sql += " AND LOWER(t.table_name) LIKE ?";args.append("%"+search.lower().replace("%","\\%").replace("_","\\_")+"%")
            sql += " ESCAPE '\\'"
        sql += " ORDER BY t.connection_id,t.table_name LIMIT ?";args.append(limit)
        result=[]
        for row in db.execute(sql,args).fetchall():
            d=dict(row);d["warnings"]=json.loads(d.pop("warnings_json"));result.append(d)
        return result


def table_detail(project: Path, cid: str, name: str, *, preview: int = 10) -> dict:
    preview = min(max(preview,0),25)
    with _LOCK, _db(project) as db:
        c=db.execute("SELECT * FROM connections WHERE id=?",(cid,)).fetchone()
        if not c:raise ConnectionErrorSafe("Connection not found")
        t=db.execute("SELECT * FROM catalog_tables WHERE connection_id=? AND table_name=?",(cid,name)).fetchone()
        if not t:raise ConnectionErrorSafe("Table is not in the catalog; refresh the connection")
        source=resolve_source(project,c["source_path"],c["kind"])
        if c["kind"] == "csv_folder":
            p=(source/(name+".csv")).resolve()
            if not p.is_relative_to(source):raise ConnectionErrorSafe("CSV source escaped folder")
            meta=read_csv_metadata(p,preview=preview)
        else:meta=read_sqlite_metadata(source,name,preview=preview)
        return {"connection_id":cid,"connection_name":c["name"],"schema":t["schema_name"],
                "table":name,"source_kind":c["kind"],**meta,
                "note":"Basic technical metadata only. Semantic roles and relationships are not yet verified."}


def overview(project: Path) -> dict:
    with _LOCK, _db(project) as db:
        connections=db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
        tables=db.execute("SELECT COUNT(*) FROM catalog_tables").fetchone()[0]
        columns=db.execute("SELECT COALESCE(SUM(column_count),0) FROM catalog_tables").fetchone()[0]
        warnings=db.execute("SELECT warnings_json FROM catalog_tables").fetchall()
        warning_tables=sum(bool(json.loads(r[0])) for r in warnings)
        errors=db.execute("SELECT COUNT(*) FROM catalog_tables WHERE scan_status!='scanned'").fetchone()[0]
        return {"connections":connections,"tables":tables,"columns":columns,
                "tables_with_warnings":warning_tables,"tables_with_scan_errors":errors,
                "phase":"4","data_label":"Synthetic local demonstration",
                "relationship_discovery":"not_implemented","human_review":"not_implemented",
                "session_memory":"planned_phase_8B"}
