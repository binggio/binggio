# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Excel workbooks: messy layouts become clean tables; the same data answers the same way as CSV."""
import datetime as dt
import pytest
import binggio
from binggio.connectors import ConnectorError, cell_text
from conftest import full_plan

openpyxl = pytest.importorskip('openpyxl')


def make_book(path):
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = 'Customers'
    ws.append(['Customer export, generated 2026-10-01'])        # title row above the header
    ws.append([])
    ws.append(['customer_id', 'Customer Name', 'segment', None])  # blank header cell beyond data is trimmed
    for i in range(1, 31):
        ws.append(['C%03d' % i, 'Customer %d' % i, ['RETAIL', 'PREMIUM'][i % 2]])
    ws2 = wb.create_sheet('Loans')
    ws2.append(['loan_id', 'customer_id', 'balance', 'opened', 'balance'])  # duplicate header name
    for i in range(1, 41):
        ws2.append(['L%03d' % i, 'C%03d' % (i % 30 + 1), 1000.0 * i, dt.datetime(2025, 1, i % 28 + 1), i])
    ws2.append([]); ws2.append(['Summary', 'Total'])        # a second block below: reported, not imported
    ws2.append(['all', 820000])
    wb.create_sheet('Empty')
    wb.save(path)


def test_messy_workbook_becomes_tables(tmp_path):
    make_book(tmp_path / 'bank.xlsx')
    ws = binggio.Workspace.create(tmp_path / 'ws')
    c = ws.add_excel('Bank workbook', tmp_path / 'bank.xlsx')
    assert c['table_count'] == 2
    notes = ' | '.join(c['notes'])
    assert 'header found on row 3' in notes and 'second table' in notes
    m = c['manifest']['tables']
    assert m['Customers']['rows'] == 30 and m['Loans']['rows'] == 40
    assert ws.profile()['failures'] == []
    ws.discover()
    from binggio.registry import store
    edges = store.list_relationships(ws.root, limit=200)['items']
    assert any(e['child_table'] == 'Loans' and e['parent_table'] == 'Customers' for e in edges)
    roles = {(r['table'], r['column']): r['candidate_role'] for r in store.list_roles(ws.root, limit=200)['items']}
    assert roles[('Loans', 'opened')] == 'DATE' and ('Loans', 'balance_2') in roles
    text = (ws.root / 'data' / 'sources' / 'bank_workbook' / 'Loans.csv').read_text()
    assert '2025-01-02' in text and ',1000,' in text  # dates as ISO dates, whole numbers without .0


def test_old_xls_refused_with_advice(tmp_path):
    (tmp_path / 'old.xls').write_bytes(b'\xd0\xcf\x11\xe0')
    ws = binggio.Workspace.create(tmp_path / 'ws')
    with pytest.raises(ConnectorError, match='save as .xlsx'):
        ws.add_excel('Old', tmp_path / 'old.xls')


def test_cell_text_is_uniform():
    import decimal
    assert cell_text(dt.datetime(2026, 1, 5)) == '2026-01-05'
    assert cell_text(dt.datetime(2026, 1, 5, 10, 30)) == '2026-01-05 10:30:00'
    assert cell_text(decimal.Decimal('12.500')) == '12.5' and cell_text(decimal.Decimal('100')) == '100'
    assert cell_text(7.0) == '7' and cell_text(None) == '' and cell_text(True) == 'true'
