# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Excel workbooks (.xlsx, .xlsm): each sheet becomes a table.

Real workbooks are rarely clean, so the reader:
  * finds the header row even when it is not row 1 (titles or notes above the table);
  * names blank or merged header cells column_N and de-duplicates repeated names;
  * keeps dates as dates (cells formatted as dates come through as ISO dates);
  * reads cached formula results, never formulas;
  * stops a sheet's table at the first fully empty row, and reports (does not guess) a second
    table further down the same sheet;
  * skips empty sheets and reports hidden ones.
The old binary format (.xls) is not supported: save the file as .xlsx first.
Needs the optional dependency: pip install binggio[excel]
"""
from __future__ import annotations
from pathlib import Path

from binggio.connectors import ConnectorError, DEFAULT_ROW_LIMIT, table_name

HEADER_SCAN_ROWS = 30
GAP_ROWS_TO_STOP = 25


def _filled(row):
    return [v for v in row if v is not None and str(v).strip() != '']


def _looks_like_header(row, nxt):
    cells = _filled(row)
    if len(cells) < 2: return False
    texty = sum(isinstance(v, str) for v in cells) / len(cells)
    return texty >= 0.8 and (nxt is None or len(_filled(nxt)) >= max(1, len(cells) // 2))


def read_workbook(path, row_limit=DEFAULT_ROW_LIMIT, sheets=None):
    """{table_name: (headers, rows, info)} plus a list of notes for the person."""
    try:
        import openpyxl
    except ImportError as e:
        raise ConnectorError('Excel support needs openpyxl: pip install binggio[excel]') from e
    path = Path(path)
    if path.suffix.lower() == '.xls':
        raise ConnectorError('The old .xls format is not supported. Open it in Excel and save as .xlsx')
    if path.suffix.lower() not in ('.xlsx', '.xlsm'):
        raise ConnectorError('Not an Excel workbook: %s' % path.name)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tables, notes, used = {}, [], set()
    try:
        for ws in wb.worksheets:
            if sheets and ws.title not in sheets: continue
            if getattr(ws, 'sheet_state', 'visible') != 'visible':
                notes.append('Sheet "%s" is hidden; imported anyway' % ws.title)
            it = ws.iter_rows(values_only=True)
            head = []
            for _ in range(HEADER_SCAN_ROWS + 1):
                try: head.append(list(next(it)))
                except StopIteration: break
            hdr_at = next((i for i in range(len(head)) if _looks_like_header(head[i], head[i + 1] if i + 1 < len(head) else None)), None)
            if hdr_at is None:
                if any(_filled(r) for r in head):
                    notes.append('Sheet "%s": no header row found in the first %d rows; skipped' % (ws.title, HEADER_SCAN_ROWS))
                continue
            if hdr_at:
                notes.append('Sheet "%s": header found on row %d; rows above it were skipped' % (ws.title, hdr_at + 1))
            headers = list(head[hdr_at])
            while headers and (headers[-1] is None or str(headers[-1]).strip() == ''): headers.pop()
            width = len(headers)
            if any(h is None or str(h).strip() == '' for h in headers):
                notes.append('Sheet "%s": blank or merged header cells were named column_N' % ws.title)
            body = iter(head[hdr_at + 1:])

            def rows(body=body, it=it, width=width, title=ws.title):
                gap = 0
                for source in (body, it):
                    for r in source:
                        r = list(r)[:width] + [None] * max(0, width - len(r))
                        if not _filled(r):
                            gap += 1
                            if gap >= GAP_ROWS_TO_STOP: return
                            continue
                        if gap:
                            nxt_cells = _filled(r)
                            if len(nxt_cells) >= 2 and all(isinstance(v, str) for v in nxt_cells):
                                notes.append('Sheet "%s": a second table seems to start after an empty row; only the first table was imported' % title)
                                return
                            gap = 0
                        yield r
            name = table_name(ws.title)
            base, n = name, 2
            while name.lower() in used: name = '%s_%d' % (base, n); n += 1
            used.add(name.lower())
            materialized = list(_take(rows(), row_limit + 1))
            if not materialized:
                notes.append('Sheet "%s": header only, no rows; skipped' % ws.title); used.discard(name.lower()); continue
            tables[name] = (headers, materialized, {'sheet': ws.title, 'header_row': hdr_at + 1, 'row_limit': row_limit})
    finally:
        wb.close()
    if not tables:
        raise ConnectorError('No tables found in %s' % path.name)
    return tables, notes


def _take(gen, n):
    for i, x in enumerate(gen):
        if i >= n: return
        yield x
