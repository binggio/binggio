# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Phase 4 local CSV and SQLite read-only metadata connectors.

No arbitrary SQL endpoint, no external credentials, no network connections.
Path sandbox is <project>/data only. This is not production connector infrastructure.
"""
from __future__ import annotations
import csv
import io
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

MAX_CSV_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB per CSV table; files are streamed, never loaded whole
MAX_PREVIEW = 25
MAX_COLUMNS = 200
MAX_TABLES = 2000


class ConnectionErrorSafe(ValueError):
    pass


def resolve_source(project: Path, relative_path: str, kind: str) -> Path:
    if kind not in ("csv_folder", "sqlite"):
        raise ConnectionErrorSafe("Supported connection types are CSV folder and SQLite database")
    rel = str(relative_path or "").strip().replace("\\", "/")
    if not rel or os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
        raise ConnectionErrorSafe("Use a relative path under the project's data/ folder")
    root = (Path(project) / "data").resolve()
    candidate = (root / rel).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise ConnectionErrorSafe("Connection path must be inside a subfolder of data/")
    if kind == "csv_folder" and not candidate.is_dir():
        raise ConnectionErrorSafe("CSV folder does not exist")
    if kind == "sqlite" and (not candidate.is_file() or candidate.suffix.lower() not in (".db", ".sqlite", ".sqlite3")):
        raise ConnectionErrorSafe("SQLite database does not exist or has an unsupported extension")
    return candidate


def _unwrapped_row(row: list[str], delim: str) -> list[str]:
    # Some legacy exports wrap an entire comma-separated line in one quoted field.
    if len(row) == 1 and delim in row[0]:
        try:
            parsed = next(csv.reader([row[0]], delimiter=delim))
            if len(parsed) > 1:return parsed
        except (csv.Error, StopIteration):pass
    return row


def read_csv_metadata(path: Path, *, preview: int = MAX_PREVIEW) -> dict[str, Any]:
    warnings: list[str] = []
    if path.stat().st_size > MAX_CSV_BYTES:
        return {"columns": [], "row_count": None, "preview": [],
                "warnings": ["File exceeds the 2 GB per-table limit; not scanned"],
                "scan_status": "limited"}
    try:
        handle = path.open("r", encoding="utf-8-sig", errors="replace", newline="")
        first = handle.read(12000)
    except OSError as exc:
        raise ConnectionErrorSafe("Cannot read CSV file") from exc
    if not first.strip():
        handle.close()
        return {"columns": [], "row_count": 0, "preview": [],
                "warnings": ["Empty CSV"], "scan_status": "limited"}
    handle.seek(0)
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if first.splitlines()[0].count(";") > first.splitlines()[0].count(",") else ","
        warnings.append("Delimiter inferred heuristically")
    try:
        reader = csv.reader(handle, delimiter=delimiter)
        header = _unwrapped_row(next(reader), delimiter)
    except (csv.Error, StopIteration) as exc:
        raise ConnectionErrorSafe("Cannot parse CSV header") from exc
    header = [str(x).strip().lstrip("\ufeff") for x in header]
    if len(header) > MAX_COLUMNS:
        return {"columns": [], "row_count": None, "preview": [],
                "warnings": ["More than 200 columns; scan stopped"], "scan_status": "limited"}
    if len(set(x.casefold() for x in header)) != len(header):
        warnings.append("Duplicate column names detected")
    if any(not h for h in header):warnings.append("Blank column names detected")
    if len(header) < 2:warnings.append("Single-column CSV; verify delimiter and export formatting")
    rows = []
    row_count = 0
    replaced = False
    malformed = 0
    padded_cells = 0
    blanks = [0] * len(header)
    sample_values: list[list[str]] = [[] for _ in header]
    try:
        for raw in reader:
            if not raw or not any(x.strip() for x in raw):continue
            row = _unwrapped_row(raw, delimiter)
            row_count += 1
            if len(row) != len(header):malformed += 1
            padded_cells += sum(str(v) != str(v).strip() for v in row)
            if not replaced and any("\ufffd" in str(v) for v in row):
                replaced = True; warnings.append("Some invalid UTF-8 characters were replaced during scan")
            values = [str(row[i]).strip() if i < len(row) else "" for i in range(len(header))]
            for i, v in enumerate(values):
                if not v or v.upper() in ("NULL", "N/A", "NA"):
                    blanks[i] += 1
                if len(sample_values[i]) < 100:sample_values[i].append(v)
            if len(rows) < preview:rows.append(dict(zip(header, values)))
    except csv.Error:
        warnings.append("CSV contains malformed quoting; partial scan only")
    finally:
        handle.close()
    if malformed:warnings.append(f"{malformed} rows differ from header width")
    if padded_cells:warnings.append(f"{padded_cells} values have leading/trailing whitespace")
    if any(blanks):warnings.append("Missing or null-like values observed; detailed profiling is Phase 5")
    def inferred(values: list[str]) -> str:
        present = [v for v in values if v and v.upper() not in ("NULL", "NA", "N/A")]
        if not present:return "unknown"
        if all(re.fullmatch(r"[+-]?\d+", v) for v in present):return "integer-like"
        if all(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", v) for v in present):return "decimal-like"
        if all(re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", v) for v in present):return "date-like"
        return "text/mixed"
    columns = [{"name": h, "declared_type": None, "observed_type": inferred(sample_values[i]),
                "missing_count": blanks[i], "missing_pct": round(100 * blanks[i] / row_count, 1) if row_count else 0}
               for i, h in enumerate(header)]
    return {"columns": columns, "row_count": row_count, "preview": rows,
            "warnings": warnings, "scan_status": "scanned", "delimiter": "TAB" if delimiter == "\t" else delimiter}


def list_csv_tables(folder: Path) -> list[str]:
    return [p.stem for p in sorted(folder.iterdir(), key=lambda p:p.name.lower())
            if p.is_file() and p.suffix.lower() == ".csv"][:MAX_TABLES]


def _readonly(db: Path) -> sqlite3.Connection:
    # mode=ro ensures metadata/preview calls cannot mutate customer-provided DBs.
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def list_sqlite_tables(db: Path) -> list[str]:
    with _readonly(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT ?",
            (MAX_TABLES,)).fetchall()]


def read_sqlite_metadata(db: Path, table: str, *, preview: int = MAX_PREVIEW) -> dict[str, Any]:
    with _readonly(db) as conn:
        allowed = set(list_sqlite_tables(db))
        if table not in allowed:raise ConnectionErrorSafe("Table not found in this SQLite connection")
        quoted = _quote_identifier(table)
        rawcols = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        cols = [r[1] for r in rawcols]
        if len(cols) > MAX_COLUMNS:
            return {"columns": [], "row_count": None, "preview": [],
                    "warnings": ["More than 200 columns; scan stopped"], "scan_status": "limited"}
        count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        data = conn.execute(f"SELECT * FROM {quoted} LIMIT ?", (min(MAX_PREVIEW, max(0, preview)),)).fetchall()
        columns = [{"name": r[1], "declared_type": r[2] or None, "observed_type": "not profiled",
                    "missing_count": None, "missing_pct": None, "primary_key": bool(r[5])} for r in rawcols]
        return {"columns": columns, "row_count": count,
                "preview": [{cols[i]:str(v) if v is not None else None for i,v in enumerate(row)} for row in data],
                "warnings": [], "scan_status": "scanned"}
