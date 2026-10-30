# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Connectors: bring tables from files and databases into a workspace, read-only.

Every connector works the same way: it reads the chosen tables (within a row limit) and writes
them as one CSV file per table under data/sources/<name>/, with a manifest saying where they
came from and when. Binggio then profiles, links and answers from those copies, so:

  * nothing is ever written to the user's database or file;
  * each table has its own content fingerprint: refreshing one table only affects approvals
    that depend on that table;
  * answers are reproducible from the snapshot until the source is refreshed.

Querying a database live, at the source, is a separate (enterprise) capability.
"""
from __future__ import annotations
import csv
import datetime as _dt
import decimal
import json
import re
from pathlib import Path

DEFAULT_ROW_LIMIT = 200_000
MANIFEST = '_binggio_source.json'


class ConnectorError(Exception):
    pass


def table_name(text: str) -> str:
    s = re.sub(r'[^A-Za-z0-9]+', '_', str(text)).strip('_')
    return (s or 'table')[:64]


def cell_text(v) -> str:
    """One representation per value type, so the same data gives the same CSV from any source."""
    if v is None: return ''
    if isinstance(v, bool): return 'true' if v else 'false'
    if isinstance(v, _dt.datetime):
        if v.hour == v.minute == v.second == v.microsecond == 0 and v.tzinfo is None:
            return v.date().isoformat()          # Oracle DATE and Excel dates without a time
        return v.isoformat(sep=' ', timespec='seconds' if not v.microsecond else 'microseconds')
    if isinstance(v, (_dt.date, _dt.time)): return v.isoformat()
    if isinstance(v, decimal.Decimal):
        v = v.normalize()
        return format(v, 'f') if v == v.to_integral() or abs(v.as_tuple().exponent) < 20 else str(v)
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15: return str(int(v))
        return repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)): return '<binary %d bytes>' % len(bytes(v))
    return str(v)


def unique_headers(headers):
    seen, out = {}, []
    for i, h in enumerate(headers):
        h = str(h).strip() if h is not None and str(h).strip() else 'column_%d' % (i + 1)
        h = re.sub(r'\s+', ' ', h)
        base, n = h, 2
        while h.lower() in seen:
            h = '%s_%d' % (base, n); n += 1
        seen[h.lower()] = True
        out.append(h)
    return out


def write_snapshot(dest: Path, source: dict, tables: dict, replace: bool = False) -> dict:
    """tables: {name: (headers, row iterator, info)} -> CSV files + manifest.

    With replace=True (refresh), each table is written to a temporary file and swapped in, so a
    failed refresh never leaves a half-written table."""
    dest.mkdir(parents=True, exist_ok=replace)
    manifest = {'source': source, 'extracted_at': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds'),
                'tables': {}}
    for name, (headers, rows, info) in tables.items():
        fname = table_name(name)
        n, truncated = 0, False
        limit = info.get('row_limit', DEFAULT_ROW_LIMIT)
        target = dest / (fname + '.csv')
        tmp = target.with_suffix('.csv.tmp')
        with open(tmp, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(unique_headers(headers))
            for row in rows:
                if n >= limit: truncated = True; break
                w.writerow([cell_text(v) for v in row]); n += 1
        tmp.replace(target)
        manifest['tables'][fname] = {**{k: v for k, v in info.items() if k != 'row_limit'},
                                     'rows': n, 'truncated_at_row_limit': truncated, 'row_limit': limit}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2, default=str), encoding='utf-8')
    return manifest
