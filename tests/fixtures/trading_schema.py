# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Random test data shaped like a bank's book, employee and reference-data extracts.

Only the column names and value shapes resemble real extracts; every value is random. The data
has the problems such extracts have, on purpose:

* references stored as three columns (…identifierdomain / …identifierid / …identifierversion),
  with several versions of the same record and one row per record per reporting month
  (YTD snapshots), so no single column of the big tables is unique;
* people stored as blocks of columns (trader*, bookrunner*: identifier, names, email, city, location);
* code/name pairs, flags, near-empty and empty columns, single-value columns;
* hierarchies flattened into level1…levelN and cc_l1…cc_l10 columns;
* timestamps with milliseconds and a time zone, dates with month names, names that are numbers,
  stray spaces, mixed-type identifiers, and one table whose employee ids are zero-padded.

`generate(folder, scale=1.0, seed=7)` writes the 9 CSV files and returns the links that were
planted (child table.column -> parent table.column), so tests can check what Binggio finds.
scale=1 is a small set (a few thousand rows per big table); scale=50 is roughly 1-2% of the real
row counts.
"""
from __future__ import annotations
import csv
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MONTHS = 6
FIRST = ['Asha', 'Rahul', 'Mei', 'John', 'Olga', 'Kofi', 'Lena', 'Ravi', 'Sara', 'Tom', 'Ines', 'Yuki', 'Omar', 'Nina', 'Paul', 'Zara']
LAST = ['Shah', 'Iyer', 'Chen', 'Smith', 'Ivanova', 'Mensah', 'Novak', 'Rao', 'Khan', 'Brown', 'Costa', 'Sato', 'Haddad', 'Berg']
CITIES = [('LDN', 'London'), ('NYC', 'New York'), ('BLR', 'Bangalore'), ('SGP', 'Singapore'), ('FRA', 'Frankfurt'),
          ('HKG', 'Hong Kong'), ('PUN', 'Pune'), ('JAX', 'Jacksonville')]
STATES = ['Maharashtra', 'Karnataka', 'New York', 'Florida', 'England', 'Hesse', 'Central']

D_BOOK, D_EMP, D_DESK, D_PRT, D_UBR, D_LE, D_PC, D_CC, D_SYS, D_NODE, D_INS = (
    'urn:brds:book', 'urn:erds:worker', 'urn:brds:desk', 'urn:brds:portfolio', 'urn:brds:ubr', 'urn:ref:legalentity',
    'urn:ref:profitcentre', 'urn:ref:costcentre', 'urn:ref:system', 'urn:brds:booknode', 'urn:irds:instrument')


def _months():
    out = []
    for m in range(1, MONTHS + 1):
        nxt = date(2026, m + 1, 1) if m < 12 else date(2027, 1, 1)
        out.append((nxt - timedelta(days=1)).isoformat())
    return out


def _ts(r, d):
    t = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(seconds=r.randrange(86400), milliseconds=r.randrange(1000))
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (t.microsecond // 1000)


def _ids(r, n, lo, hi):
    return r.sample(range(lo, hi), n)


def _write(folder, name, header, rows):
    with open(Path(folder) / (name + '.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(['' if v is None else v for v in (row.get(h) for h in header)])


def _maybe(r, p, v):
    return v if r.random() >= p else None


def generate(folder, scale: float = 1.0, seed: int = 7) -> dict:
    r = random.Random(seed)
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    months = _months()
    n_emp, n_book, n_desk, n_prt, n_ubr = int(120 * scale) + 20, int(80 * scale) + 10, 12 + int(scale), int(20 * scale) + 5, int(30 * scale) + 8
    n_le, n_pc, n_cc, n_ins = 6 + int(scale // 5), 15 + int(scale), 10 + int(scale), int(40 * scale) + 10

    emps = _ids(r, n_emp, 1_000_000, 9_999_999)
    person = {e: {'first': r.choice(FIRST), 'last': r.choice(LAST), 'city': r.choice(CITIES)} for e in emps}
    for e, p in person.items(): p['email'] = ('%s.%s.%d@example.com' % (p['first'], p['last'], e % 1000)).lower()
    books = _ids(r, n_book, 100_000, 999_999)
    desks = _ids(r, n_desk, 1_000, 9_999)
    desk_name = {d: 'DESK-%s-%02d' % (r.choice(['RATES', 'CREDIT', 'FX', 'EQ', 'COMMOD']), i) for i, d in enumerate(desks)}
    prts = _ids(r, n_prt, 10_000, 99_999)
    ubrs = _ids(r, n_ubr, 20_000, 89_999)
    les = ['LE%04d' % x for x in _ids(r, n_le, 1, 9999)]
    pcs = [str(x) if i % 3 else 'PC%d' % x for i, x in enumerate(_ids(r, n_pc, 1000, 99999))]   # mixed types
    ccs = ['CC%05d' % x for x in _ids(r, n_cc, 1, 99999)]
    systems = {s: 500 + i for i, s in enumerate(['MUREX', 'CALYPSO', 'SUMMIT', 'ATLAS', 'KONDOR', 'OPICS'])}
    instruments = _ids(r, n_ins, 3_000_000, 3_999_999)
    managers = r.sample(emps, max(3, n_emp // 8))
    cc_parent = {c: r.choice(ccs[:max(2, len(ccs) // 3)]) for c in ccs}

    # ---------------- ERDS: employees, one row per employee per month ----------------
    erds = []
    hire = {e: date(2005, 1, 1) + timedelta(days=r.randrange(7000)) for e in emps}
    emp_cc = {e: r.choice(ccs) for e in emps}
    for mi, m in enumerate(months):
        for e in emps:
            if mi == 0 and r.random() < 0.05: continue          # joined later in the year
            p = person[e]; cc = emp_cc[e]
            row = {'reportingmonth': m, 'employeeid': e, 'emailid': p['email'], 'firstname': p['first'], 'lastname': p['last'],
                   'citycode': p['city'][0], 'location': p['city'][1], 'state': r.choice(STATES), 'managerid': r.choice(managers) if e not in managers[:1] else None,
                   'hierarchylevel': r.randint(1, 9), 'corporatetitle': r.choice(['Analyst', 'Associate', 'VP', 'Director', 'MD']),
                   'employmentstatus': r.choice(['Active'] * 9 + ['Leave']), 'hiredate': hire[e].strftime('%d-%b-%Y'),
                   'costcentrecode': cc, 'costcentrename': 'Cost centre %s' % cc[2:], 'parentcostcentrecode': cc_parent[cc],
                   'costcentrelevel': r.randint(3, 10), 'version': 1 + mi // 2, 'lastmodifieddatetime': _ts(r, date(2026, 1, 1) + timedelta(days=30 * mi)),
                   'contractorflag': r.choice(['Y', 'N', 'N', 'N']), 'regioncomment': None}
            for lv in range(1, 11):
                code = 'L%d-%03d' % (lv, (int(cc[2:]) % (lv * 7)) + 1) if lv <= 7 or r.random() < 0.5 else None
                row['cc_l%dcode' % lv] = code
                row['cc_l%ddesc' % lv] = ('Level %d unit %s' % (lv, code[-3:])) if code else None
            erds.append(row)
    erds_h = (['reportingmonth', 'employeeid', 'emailid', 'firstname', 'lastname', 'citycode', 'location', 'state', 'managerid',
               'hierarchylevel', 'corporatetitle', 'employmentstatus', 'hiredate', 'costcentrecode', 'costcentrename',
               'parentcostcentrecode', 'costcentrelevel'] + [x for lv in range(1, 11) for x in ('cc_l%dcode' % lv, 'cc_l%ddesc' % lv)]
              + ['version', 'lastmodifieddatetime', 'contractorflag', 'regioncomment'])
    _write(folder, 'ERDS-YTD_2026', erds_h, erds)

    # ---------------- ERDS_MTA: matrix assignments (employee ids zero-padded to 8 digits) ----------------
    mta = []
    for m in months:
        for e in emps:
            for k in range(r.choice([1, 1, 2])):
                mta.append({'reportingmonth': m, 'employeeid': '%08d' % e, 'matrixassignmentidentifierid': 7_000_000 + e % 100_000 * 3 + k,
                            'matrixmanageremployeeid': '%08d' % r.choice(managers), 'assignmenttype': r.choice(['Primary', 'Matrix', 'Functional']),
                            'assignmentallocationpercentage': r.choice([100, 50, 25, 75]),
                            'organisationalunithierarchylevelonedescription': 'Global Markets',
                            'organisationalunithierarchyleveltwodescriptionextendedname': r.choice(['Rates and Credit Trading', 'Foreign Exchange', 'Equities Division']),
                            'secondaryfunctionalreportinglinemanagerworkeridentifier': _maybe(r, 0.7, r.choice(emps)),
                            'assignmentstartdate': (date(2024, 1, 1) + timedelta(days=r.randrange(800))).strftime('%d/%m/%Y')})
    _write(folder, 'ERDS_MTA-YTD_2026', ['reportingmonth', 'employeeid', 'matrixassignmentidentifierid', 'matrixmanageremployeeid', 'assignmenttype',
                                         'assignmentallocationpercentage', 'organisationalunithierarchylevelonedescription',
                                         'organisationalunithierarchyleveltwodescriptionextendedname',
                                         'secondaryfunctionalreportinglinemanagerworkeridentifier', 'assignmentstartdate'], mta)

    # ---------------- BRDS_DESK and BRDS_PRT ----------------
    desk_rows = []
    for m in months:
        for d in desks:
            desk_rows.append({'reportingmonth': m, 'identifierdomain': D_DESK, 'identifierid': d, 'identifierversion': 1 + months.index(m) // 3,
                              'deskname': desk_name[d], 'volckertradingdesk': desk_name[d], 'desktype': r.choice(['Trading', 'Hedging', 'Treasury']),
                              'deskheadidentifierdomain': D_EMP, 'deskheadidentifierid': r.choice(managers), 'deskheadidentifierversion': 1,
                              'legalentityidentifierid': r.choice(les), 'deskstatus': 'Active', 'regioncode': r.choice(['EMEA', 'AMER', 'APAC'])})
    _write(folder, 'BRDS_DESK-YTD_2026', list(desk_rows[0].keys()), desk_rows)
    prt_desk = {p: r.choice(desks) for p in prts}
    prt_rows = []
    for m in months:
        for p in prts:
            prt_rows.append({'reportingmonth': m, 'identifierdomain': D_PRT, 'identifierid': p, 'identifierversion': 1 + r.randrange(3),
                             'portfolioname': 'Portfolio %d' % p, 'portfoliostatus': r.choice(['Open', 'Open', 'Closed']),
                             'portfoliorplcode': 'RPL%d' % (p % 997), 'volckertradingdesktype': r.choice(['Permitted', 'Exempt']),
                             'deskidentifierdomain': D_DESK, 'deskidentifierid': prt_desk[p], 'deskidentifierversion': 1,
                             'portfoliomanageridentifierid': r.choice(emps)})
    _write(folder, 'BRDS_PRT-YTD_2026', list(prt_rows[0].keys()), prt_rows)

    # ---------------- BRDS_UBR: reporting hierarchy nodes ----------------
    ubr_rows = []
    lvl = {u: r.randint(1, 6) for u in ubrs}
    for m in months:
        for u in ubrs:
            row = {'reportingmonth': m, 'identifierdomain': D_UBR, 'identifierid': u, 'identifierversion': 1, 'ubrname': 'UBR node %d' % u,
                   'ubrlevel': lvl[u], 'isleaf': 1 if lvl[u] == 6 else 0, 'parentubridentifierid': r.choice(ubrs) if lvl[u] > 1 else None}
            for k in range(1, 7):
                row['ubrlevel%dcode' % k] = ('U%d-%02d' % (k, u % (k * 5) + 1)) if k <= lvl[u] else None
                row['ubrlevel%dname' % k] = ('Level %d %s' % (k, row['ubrlevel%dcode' % k])) if k <= lvl[u] else None
            ubr_rows.append(row)
    _write(folder, 'BRDS_UBR-YTD_2026', list(ubr_rows[0].keys()), ubr_rows)

    # ---------------- IRDS_INSTR: instruments ----------------
    ins_rows = []
    for i in instruments:
        ins_rows.append({'instrumentidentifierdomain': D_INS, 'instrumentidentifierid': i, 'instrumentidentifierversion': 1 + r.randrange(2),
                         'isin': 'XS%010d' % (i * 7 % 10 ** 10), 'instrumentname': r.choice(['Bond', 'Note', 'Swap']) + ' %d' % (i % 10000),
                         'instrumenttype': r.choice(['BOND', 'FRN', 'IRS', 'CDS']), 'currencycode': r.choice(['USD', 'EUR', 'GBP', 'INR']),
                         'issuerlegalentityidentifierid': r.choice(les), 'couponrate': round(r.uniform(0, 9), 3),
                         'notionalamount': r.choice([1, 5, 10, 25]) * 10 ** 6, 'maturitydate': (date(2026, 6, 1) + timedelta(days=r.randrange(9000))).isoformat()})
    _write(folder, 'IRDS_INSTR-YTD_2026', list(ins_rows[0].keys()), ins_rows)

    # ---------------- BRDS_BK_CORE: books, one row per book per month, versions grow ----------------
    def ref(prefix, domain, value, version=1):
        return {prefix + 'identifierdomain': domain if value is not None else None, prefix + 'identifierid': value,
                prefix + 'identifierversion': version if value is not None else None}

    def person_block(prefix, e, p_missing):
        if e is None or r.random() < p_missing:
            return {**ref(prefix, D_EMP, None), **{prefix + k: None for k in ('citycode', 'emailaddress', 'firstname', 'lastname', 'location')}}
        p = person[e]
        last = p['last'] + (' ' if prefix == 'lastrecertifiedby' and r.random() < 0.2 else '')
        return {**ref(prefix, D_EMP, e), prefix + 'citycode': p['city'][0], prefix + 'emailaddress': p['email'],
                prefix + 'firstname': p['first'], prefix + 'lastname': last, prefix + 'location': p['city'][1]}

    book_attr = {}
    for b in books:
        book_attr[b] = {'trader': r.choice(emps), 'runner': r.choice(emps), 'fc': r.choice(emps), 'cert': r.choice(emps), 'esc': r.choice(emps),
                        'prt': r.choice(prts) if r.random() > 0.75 else None, 'desk': r.choice(desks), 'le': r.choice(les),
                        'pc': r.choice(pcs), 'cc': r.choice(ccs), 'ubrfa': r.choice(ubrs), 'ubrma': r.choice(ubrs),
                        'name': str(r.randint(10000, 99999)) if r.random() < 0.15 else 'BK_%s_%d' % (r.choice(['RATES', 'FX', 'CRD']), b % 1000),
                        'created': date(2015, 1, 1) + timedelta(days=r.randrange(4000)), 'acc': r.randint(1, 8)}
    core = []
    for mi, m in enumerate(months):
        for b in books:
            a = book_attr[b]; ver = 1 + mi // 2 + (b % 3 == 0)
            row = {'reportingmonth': m, **ref('', D_BOOK, b, ver), 'traderbookname': a['name'], 'alternatebookname': _maybe(r, 0.6, a['name']),
                   'bookstatus': r.choice(['Active'] * 8 + ['Closed']), 'bookcreateddatetime': _ts(r, a['created']),
                   'lastmodifieddatetime': _ts(r, date(2026, 1, 1) + timedelta(days=30 * mi + r.randrange(28))),
                   'businessownercode': 'BO%02d' % (b % 12), 'businessownername': 'Business owner %02d' % (b % 12),
                   'managedbycode': 'MB%d' % (b % 5), 'managedbyname': 'Managed by %d' % (b % 5),
                   'middleofficegroupemailid': 'mo.group%d@example.com' % (b % 4),
                   'accountingcategorycode': _maybe(r, 0.8, a['acc']), 'accountingcategoryname': None,
                   'sppiresultscode': None, 'sppiresultsname': None,
                   **ref('legalentity', D_LE, _maybe(r, 0.3, a['le'])), **ref('profitcentre', D_PC, _maybe(r, 0.8, a['pc'])),
                   **ref('primarytradercostcentre', D_CC, _maybe(r, 0.8, a['cc'])),
                   **ref('lowestlevelubrfa', D_UBR, _maybe(r, 0.8, a['ubrfa'])), **ref('lowestlevelubrma', D_UBR, _maybe(r, 0.78, a['ubrma'])),
                   **ref('primarymoescalation', D_EMP, None), **ref('portfolio', D_PRT, a['prt']),
                   'portfolioname': ('Portfolio %d' % a['prt']) if a['prt'] else None, 'portfoliostatus': 'Open' if a['prt'] else None,
                   'portfoliorplcode': ('RPL%d' % (a['prt'] % 997)) if a['prt'] else None,
                   'portfoliovolckertradingdesktype': 'Permitted' if a['prt'] else None,
                   'portfolioxdbgimmiskeyrequiredindicator': r.choice(['Y', 'N']) if a['prt'] else None,
                   'portfoliofrtbnotionaldesk': 'N' if a['prt'] and r.random() < 0.8 else None,
                   'portfoliointerestrateriskirtdeskindicator': 'N' if a['prt'] else None,
                   **person_block('bookrunner', a['runner'], 0.76), **person_block('financialcontroller', a['fc'], 0.79),
                   **person_block('trader', a['trader'], 0.836), **person_block('lastrecertifiedby', a['cert'], 0.805),
                   **ref('lastactionby', D_EMP, r.choice(managers[:20])),
                   'includedintraderrecertflag': r.choice(['true', 'false']),
                   'lastrecertificationdatetime': _maybe(r, 0.6, _ts(r, date(2025, 6, 1) + timedelta(days=r.randrange(300)))),
                   'recertificationstatus': r.choice(['Certified', 'Pending', 'Overdue']),
                   'tradecapturesystemname': r.choice(list(systems)), 'valuationsystemname': _maybe(r, 0.88, 'ATLAS'),
                   'valuationsystemnarid': _maybe(r, 0.88, 'NAR%d' % r.randint(1, 6)),
                   'nonvolckertradingdesk': _maybe(r, 0.2, r.choice(['Treasury', 'Hedging', 'Funding'])),
                   'volckerreportablestatustype': r.choice(['Reportable', 'Excluded', 'Pending', None]),
                   'volckertradingdesk': desk_name[a['desk']],
                   'clearingcomment': 'n/a' if r.random() < 0.0004 else None, 'compressioneligibleindicator': 'Y' if r.random() < 0.0004 else None,
                   'hubbingcomment': None, 'frtbirtbooktype': None}
            row.update(person_block('primarymoescalation', a['esc'], 0.96))
            row.update(ref('tradecapturesystem', D_SYS, systems[row['tradecapturesystemname']]))
            row.update(ref('valuationsystem', D_SYS, systems['ATLAS'] if row['valuationsystemname'] else None))
            row.update(ref('settlementsystem', D_SYS, r.choice(list(systems.values()))))
            core.append(row)
    core_h = list(core[0].keys())
    _write(folder, 'BRDS_BK_CORE-YTD_2026', core_h, core)

    # ---------------- BRDS_BK_ASSO: associations between books and people ----------------
    asso = []
    for m in months:
        for b in books:
            for k in range(r.choice([2, 4, 6, 8])):
                other = r.choice(books)
                asso.append({'reportingmonth': m, **ref('', D_BOOK, b, 1 + months.index(m) // 2),
                             **ref('associatedbook', D_BOOK, other if k % 2 else None),
                             **ref('delegatecontactworker', D_EMP, r.choice(emps) if not k % 2 else None),
                             'associationtype': 'BOOK_LINK' if k % 2 else 'DELEGATE', 'effectivefromdate': (date(2024, 1, 1) + timedelta(days=r.randrange(700))).isoformat(),
                             'effectivetodate': None, 'associationcomment': None})
    _write(folder, 'BRDS_BK_ASSO-YTD_2026', list(asso[0].keys()), asso)

    # ---------------- BRDS_BK_HIRE: book hierarchy flattened into level1..level13 ----------------
    nodes = _ids(r, max(10, n_book // 3), 50_000, 59_999)
    root = nodes[0]
    hire_rows = []
    for m in months:
        for b in books:
            depth = r.choice([6, 7, 8, 9, 10, 11, 12, 13])
            path = [root] + [r.choice(nodes) for _ in range(depth - 2)] + [b]
            row = {'reportingmonth': m, **ref('', D_BOOK, b, 1), 'level': depth, 'isleaf': 1}
            for k in range(1, 14):
                v = path[k - 1] if k <= depth else None
                row.update({'level%ddomain' % k: (D_BOOK if v == b else D_NODE) if v else None, 'level%did' % k: v, 'level%dversion' % k: 1 if v else None})
            hire_rows.append(row)
    _write(folder, 'BRDS_BK_HIRE-YTD_2026', list(hire_rows[0].keys()), hire_rows)

    return {'links': [
        ('BRDS_BK_ASSO-YTD_2026.identifierid', 'BRDS_BK_CORE-YTD_2026.identifierid'),
        ('BRDS_BK_ASSO-YTD_2026.associatedbookidentifierid', 'BRDS_BK_CORE-YTD_2026.identifierid'),
        ('BRDS_BK_ASSO-YTD_2026.delegatecontactworkeridentifierid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_CORE-YTD_2026.traderidentifierid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_CORE-YTD_2026.bookrunneridentifierid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_CORE-YTD_2026.financialcontrolleridentifierid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_CORE-YTD_2026.lastrecertifiedbyidentifierid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_CORE-YTD_2026.lastactionbyidentifierid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_CORE-YTD_2026.portfolioidentifierid', 'BRDS_PRT-YTD_2026.identifierid'),
        ('BRDS_BK_CORE-YTD_2026.lowestlevelubrfaidentifierid', 'BRDS_UBR-YTD_2026.identifierid'),
        ('BRDS_BK_CORE-YTD_2026.volckertradingdesk', 'BRDS_DESK-YTD_2026.volckertradingdesk'),
        ('BRDS_PRT-YTD_2026.deskidentifierid', 'BRDS_DESK-YTD_2026.identifierid'),
        ('ERDS-YTD_2026.managerid', 'ERDS-YTD_2026.employeeid'),
        ('ERDS_MTA-YTD_2026.employeeid', 'ERDS-YTD_2026.employeeid'),
        ('BRDS_BK_HIRE-YTD_2026.identifierid', 'BRDS_BK_CORE-YTD_2026.identifierid'),
    ], 'rows': {'ERDS': len(erds), 'ERDS_MTA': len(mta), 'BK_CORE': len(core), 'BK_ASSO': len(asso), 'BK_HIRE': len(hire_rows)}}


if __name__ == '__main__':
    import sys
    out = generate(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)
    print(out['rows'])
