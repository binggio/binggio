# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Random test data for a fictional retail chain: stores, staff, districts, assortments, zones and
products, exported the way large organisations often export data.

Every name and value is invented. The data has, on purpose, the problems such extracts have:

* references stored as three columns (…identifierdomain / …identifierid / …identifierversion),
  with several versions of the same record and one row per record per reporting month (monthly
  snapshots), so no single column of the big tables is unique;
* people stored as blocks of columns (buyer*, storemanager*: identifier, names, email, city, location);
* code/name pairs, flags, near-empty and empty columns, single-value columns;
* hierarchies flattened into level1…levelN and org_l1…org_l10 columns;
* prices written as text ($1,234.99), timestamps with milliseconds and a time zone, dates with month names, names that are numbers,
  stray spaces, mixed-type identifiers, and one table whose staff ids are zero-padded.

`generate(folder, scale=1.0, seed=7)` writes 9 CSV files and returns the links that were planted
(child table.column -> parent table.column), so tests can check what Binggio finds. scale=1 is a
small set (a few thousand rows in the big tables); scale=300 is about 1.5 million rows.
"""
from __future__ import annotations
import csv
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MONTHS = 6
STORES, LINKS, TREE, DISTRICTS, ASSORTMENTS, ZONES, STAFF, ROTA, PRODUCTS = (
    'STORES-2026', 'STORE_LINKS-2026', 'STORE_TREE-2026', 'DISTRICTS-2026', 'ASSORTMENTS-2026', 'ZONES-2026',
    'STAFF-2026', 'STAFF_ROTA-2026', 'PRODUCTS-2026')
FIRST = ['Asha', 'Rahul', 'Mei', 'John', 'Olga', 'Kofi', 'Lena', 'Ravi', 'Sara', 'Tom', 'Ines', 'Yuki', 'Omar', 'Nina', 'Paul', 'Zara']
LAST = ['Shah', 'Iyer', 'Chen', 'Smith', 'Ivanova', 'Mensah', 'Novak', 'Rao', 'Khan', 'Brown', 'Costa', 'Sato', 'Haddad', 'Berg']
CITIES = [('LDN', 'London'), ('NYC', 'New York'), ('BLR', 'Bangalore'), ('SGP', 'Singapore'), ('FRA', 'Frankfurt'),
          ('HKG', 'Hong Kong'), ('PUN', 'Pune'), ('JAX', 'Jacksonville')]
STATES = ['Maharashtra', 'Karnataka', 'New York', 'Florida', 'England', 'Hesse', 'Central']

D_STORE, D_STAFF, D_DIST, D_ASSORT, D_ZONE, D_LE, D_PC, D_CC, D_SYS, D_NODE, D_PROD = (
    'urn:retail:store', 'urn:people:staff', 'urn:retail:district', 'urn:retail:assortment', 'urn:retail:zone', 'urn:ref:legalentity',
    'urn:ref:profitcentre', 'urn:ref:costcentre', 'urn:ref:system', 'urn:retail:storenode', 'urn:catalog:product')


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
    n_staff, n_store, n_dist, n_ass, n_zone = int(120 * scale) + 20, int(80 * scale) + 10, 12 + int(scale), int(20 * scale) + 5, int(30 * scale) + 8
    n_le, n_pc, n_cc, n_prod = 6 + int(scale // 5), 15 + int(scale), 10 + int(scale), int(40 * scale) + 10

    staff = _ids(r, n_staff, 1_000_000, 9_999_999)
    person = {e: {'first': r.choice(FIRST), 'last': r.choice(LAST), 'city': r.choice(CITIES)} for e in staff}
    for e, p in person.items(): p['email'] = ('%s.%s.%d@example.com' % (p['first'], p['last'], e % 1000)).lower()
    stores = _ids(r, n_store, 100_000, 999_999)
    districts = _ids(r, n_dist, 1_000, 9_999)
    dist_name = {d: 'DIST-%s-%02d' % (r.choice(['NORTH', 'SOUTH', 'EAST', 'WEST', 'CENTRAL']), i) for i, d in enumerate(districts)}
    assortments = _ids(r, n_ass, 10_000, 99_999)
    zones = _ids(r, n_zone, 20_000, 89_999)
    les = ['LE%04d' % x for x in _ids(r, n_le, 1, 9999)]
    pcs = [str(x) if i % 3 else 'PC%d' % x for i, x in enumerate(_ids(r, n_pc, 1000, 99999))]   # mixed types
    ccs = ['CC%05d' % x for x in _ids(r, n_cc, 1, 99999)]
    systems = {s: 500 + i for i, s in enumerate(['TILLPOINT', 'SHELFWISE', 'STOCKLY', 'PAYGRID', 'ORBIT', 'LEDGERA'])}
    products = _ids(r, n_prod, 3_000_000, 3_999_999)
    managers = r.sample(staff, max(3, n_staff // 8))
    cc_parent = {c: r.choice(ccs[:max(2, len(ccs) // 3)]) for c in ccs}

    # ---------------- STAFF: one row per person per month ----------------
    staff_rows = []
    hire = {e: date(2005, 1, 1) + timedelta(days=r.randrange(7000)) for e in staff}
    emp_cc = {e: r.choice(ccs) for e in staff}
    for mi, m in enumerate(months):
        for e in staff:
            if mi == 0 and r.random() < 0.05: continue          # joined later in the year
            p = person[e]; cc = emp_cc[e]
            row = {'reportingmonth': m, 'staffid': e, 'emailid': p['email'], 'firstname': p['first'], 'lastname': p['last'],
                   'citycode': p['city'][0], 'location': p['city'][1], 'state': r.choice(STATES), 'managerid': r.choice(managers) if e not in managers[:1] else None,
                   'hierarchylevel': r.randint(1, 9), 'jobgrade': r.choice(['Associate', 'Supervisor', 'Manager', 'Senior Manager', 'Director']),
                   'employmentstatus': r.choice(['Active'] * 9 + ['Leave']), 'hiredate': hire[e].strftime('%d-%b-%Y'),
                   'costcentrecode': cc, 'costcentrename': 'Cost centre %s' % cc[2:], 'parentcostcentrecode': cc_parent[cc],
                   'costcentrelevel': r.randint(3, 10), 'version': 1 + mi // 2, 'lastmodifieddatetime': _ts(r, date(2026, 1, 1) + timedelta(days=30 * mi)),
                   'contractorflag': r.choice(['Y', 'N', 'N', 'N']), 'regioncomment': None}
            for lv in range(1, 11):
                code = 'L%d-%03d' % (lv, (int(cc[2:]) % (lv * 7)) + 1) if lv <= 7 or r.random() < 0.5 else None
                row['org_l%dcode' % lv] = code
                row['org_l%ddesc' % lv] = ('Level %d unit %s' % (lv, code[-3:])) if code else None
            staff_rows.append(row)
    staff_h = (['reportingmonth', 'staffid', 'emailid', 'firstname', 'lastname', 'citycode', 'location', 'state', 'managerid',
                'hierarchylevel', 'jobgrade', 'employmentstatus', 'hiredate', 'costcentrecode', 'costcentrename',
                'parentcostcentrecode', 'costcentrelevel'] + [x for lv in range(1, 11) for x in ('org_l%dcode' % lv, 'org_l%ddesc' % lv)]
               + ['version', 'lastmodifieddatetime', 'contractorflag', 'regioncomment'])
    _write(folder, STAFF, staff_h, staff_rows)

    # ---------------- STAFF_ROTA: shift assignments (staff ids zero-padded to 8 digits) ----------------
    rota = []
    for m in months:
        for e in staff:
            for k in range(r.choice([1, 1, 2])):
                rota.append({'reportingmonth': m, 'staffid': '%08d' % e, 'rotaidentifierid': 7_000_000 + e % 100_000 * 3 + k,
                             'supervisorstaffid': '%08d' % r.choice(managers), 'shifttype': r.choice(['Early', 'Late', 'Night']),
                             'allocationpercentage': r.choice([100, 50, 25, 75]),
                             'organisationunitlevelonedescription': 'Retail Operations',
                             'organisationunitleveltwodescriptionextendedname': r.choice(['Fresh Food and Bakery', 'Home and Garden', 'Clothing and Footwear']),
                             'secondaryreportinglinesupervisorstaffidentifier': _maybe(r, 0.7, r.choice(staff)),
                             'shiftstartdate': (date(2024, 1, 1) + timedelta(days=r.randrange(800))).strftime('%d/%m/%Y')})
    _write(folder, ROTA, ['reportingmonth', 'staffid', 'rotaidentifierid', 'supervisorstaffid', 'shifttype', 'allocationpercentage',
                          'organisationunitlevelonedescription', 'organisationunitleveltwodescriptionextendedname',
                          'secondaryreportinglinesupervisorstaffidentifier', 'shiftstartdate'], rota)

    # ---------------- DISTRICTS and ASSORTMENTS ----------------
    dist_rows = []
    for m in months:
        for d in districts:
            dist_rows.append({'reportingmonth': m, 'identifierdomain': D_DIST, 'identifierid': d, 'identifierversion': 1 + months.index(m) // 3,
                              'districtname': dist_name[d], 'districtlabel': dist_name[d], 'districttype': r.choice(['Urban', 'Suburban', 'Rural']),
                              'districtheadidentifierdomain': D_STAFF, 'districtheadidentifierid': r.choice(managers), 'districtheadidentifierversion': 1,
                              'legalentityidentifierid': r.choice(les), 'districtstatus': 'Active', 'areacode': r.choice(['EMEA', 'AMER', 'APAC'])})
    _write(folder, DISTRICTS, list(dist_rows[0].keys()), dist_rows)
    ass_dist = {p: r.choice(districts) for p in assortments}
    ass_rows = []
    for m in months:
        for p in assortments:
            ass_rows.append({'reportingmonth': m, 'identifierdomain': D_ASSORT, 'identifierid': p, 'identifierversion': 1 + r.randrange(3),
                             'assortmentname': 'Assortment %d' % p, 'assortmentstatus': r.choice(['Open', 'Open', 'Closed']),
                             'planogramcode': 'PLG%d' % (p % 997), 'displaytype': r.choice(['Standard', 'Premium']),
                             'districtidentifierdomain': D_DIST, 'districtidentifierid': ass_dist[p], 'districtidentifierversion': 1,
                             'assortmentmanageridentifierid': r.choice(staff)})
    _write(folder, ASSORTMENTS, list(ass_rows[0].keys()), ass_rows)

    # ---------------- ZONES: reporting hierarchy nodes ----------------
    zone_rows = []
    lvl = {u: r.randint(1, 6) for u in zones}
    for m in months:
        for u in zones:
            row = {'reportingmonth': m, 'identifierdomain': D_ZONE, 'identifierid': u, 'identifierversion': 1, 'zonename': 'Zone %d' % u,
                   'zonelevel': lvl[u], 'isleaf': 1 if lvl[u] == 6 else 0, 'parentzoneidentifierid': r.choice(zones) if lvl[u] > 1 else None}
            for k in range(1, 7):
                row['zonelevel%dcode' % k] = ('Z%d-%02d' % (k, u % (k * 5) + 1)) if k <= lvl[u] else None
                row['zonelevel%dname' % k] = ('Level %d %s' % (k, row['zonelevel%dcode' % k])) if k <= lvl[u] else None
            zone_rows.append(row)
    _write(folder, ZONES, list(zone_rows[0].keys()), zone_rows)

    # ---------------- PRODUCTS ----------------
    prod_rows = []
    for i in products:
        prod_rows.append({'productidentifierdomain': D_PROD, 'productidentifierid': i, 'productidentifierversion': 1 + r.randrange(2),
                          'gtin': '50%011d' % (i * 7 % 10 ** 11), 'productname': r.choice(['Tea', 'Rice', 'Soap', 'Lamp']) + ' %d' % (i % 10000),
                          'producttype': r.choice(['GROCERY', 'HOME', 'BEAUTY', 'FRESH']), 'currencycode': r.choice(['USD', 'EUR', 'GBP', 'INR']),
                          'supplierlegalentityidentifierid': r.choice(les), 'marginrate': round(r.uniform(0, 9), 3),
                          'unitcostamount': r.choice([1, 5, 10, 25]) * 10,
                          'listprice': '$' + format(i % 2500 + 0.99, ',.2f'),    # a price written as text ($1,104.99)
                          'discontinueddate': (date(2026, 6, 1) + timedelta(days=r.randrange(9000))).isoformat()})
    _write(folder, PRODUCTS, list(prod_rows[0].keys()), prod_rows)

    # ---------------- STORES: one row per store per month, versions grow ----------------
    def ref(prefix, domain, value, version=1):
        return {prefix + 'identifierdomain': domain if value is not None else None, prefix + 'identifierid': value,
                prefix + 'identifierversion': version if value is not None else None}

    def person_block(prefix, e, p_missing):
        if e is None or r.random() < p_missing:
            return {**ref(prefix, D_STAFF, None), **{prefix + k: None for k in ('citycode', 'emailaddress', 'firstname', 'lastname', 'location')}}
        p = person[e]
        last = p['last'] + (' ' if prefix == 'lastinspectedby' and r.random() < 0.2 else '')
        return {**ref(prefix, D_STAFF, e), prefix + 'citycode': p['city'][0], prefix + 'emailaddress': p['email'],
                prefix + 'firstname': p['first'], prefix + 'lastname': last, prefix + 'location': p['city'][1]}

    attr = {}
    for b in stores:
        attr[b] = {'buyer': r.choice(staff), 'mgr': r.choice(staff), 'area': r.choice(staff), 'insp': r.choice(staff), 'esc': r.choice(staff),
                   'ass': r.choice(assortments) if r.random() > 0.75 else None, 'dist': r.choice(districts), 'le': r.choice(les),
                   'pc': r.choice(pcs), 'cc': r.choice(ccs), 'z1': r.choice(zones), 'z2': r.choice(zones),
                   'name': str(r.randint(10000, 99999)) if r.random() < 0.15 else 'ST_%s_%d' % (r.choice(['NORTH', 'SOUTH', 'WEST']), b % 1000),
                   'opened': date(2015, 1, 1) + timedelta(days=r.randrange(4000)), 'tax': r.randint(1, 8)}
    core = []
    for mi, m in enumerate(months):
        for b in stores:
            a = attr[b]; ver = 1 + mi // 2 + (b % 3 == 0)
            row = {'reportingmonth': m, **ref('', D_STORE, b, ver), 'storename': a['name'], 'alternatename': _maybe(r, 0.6, a['name']),
                   'storestatus': r.choice(['Active'] * 8 + ['Closed']), 'storeopeneddatetime': _ts(r, a['opened']),
                   'lastmodifieddatetime': _ts(r, date(2026, 1, 1) + timedelta(days=30 * mi + r.randrange(28))),
                   'formatcode': 'FMT%02d' % (b % 12), 'formatname': 'Format %02d' % (b % 12),
                   'operatedbycode': 'OPS%d' % (b % 5), 'operatedbyname': 'Operated by %d' % (b % 5),
                   'supportgroupemailid': 'support.group%d@example.com' % (b % 4),
                   'taxcategorycode': _maybe(r, 0.8, a['tax']), 'taxcategoryname': None,
                   'auditresultcode': None, 'auditresultname': None,
                   **ref('legalentity', D_LE, _maybe(r, 0.3, a['le'])), **ref('profitcentre', D_PC, _maybe(r, 0.8, a['pc'])),
                   **ref('primarycostcentre', D_CC, _maybe(r, 0.8, a['cc'])),
                   **ref('primaryzone', D_ZONE, _maybe(r, 0.8, a['z1'])), **ref('secondaryzone', D_ZONE, _maybe(r, 0.78, a['z2'])),
                   **ref('escalationcontact', D_STAFF, None), **ref('assortment', D_ASSORT, a['ass']),
                   'assortmentname': ('Assortment %d' % a['ass']) if a['ass'] else None, 'assortmentstatus': 'Open' if a['ass'] else None,
                   'assortmentplanogramcode': ('PLG%d' % (a['ass'] % 997)) if a['ass'] else None,
                   'assortmentdisplaytype': 'Standard' if a['ass'] else None,
                   'assortmentchilledindicator': r.choice(['Y', 'N']) if a['ass'] else None,
                   'assortmentseasonalcode': 'N' if a['ass'] and r.random() < 0.8 else None,
                   'assortmentorganicindicator': 'N' if a['ass'] else None,
                   **person_block('storemanager', a['mgr'], 0.76), **person_block('areamanager', a['area'], 0.79),
                   **person_block('buyer', a['buyer'], 0.836), **person_block('lastinspectedby', a['insp'], 0.805),
                   **ref('lastactionby', D_STAFF, r.choice(managers[:20])),
                   'includedinauditflag': r.choice(['true', 'false']),
                   'lastinspectiondatetime': _maybe(r, 0.6, _ts(r, date(2025, 6, 1) + timedelta(days=r.randrange(300)))),
                   'inspectionstatus': r.choice(['Passed', 'Pending', 'Overdue']),
                   'possystemname': r.choice(list(systems)), 'inventorysystemname': _maybe(r, 0.88, 'SHELFWISE'),
                   'inventorysystemref': _maybe(r, 0.88, 'INV%d' % r.randint(1, 6)),
                   'specialistdepartment': _maybe(r, 0.2, r.choice(['Bakery', 'Deli', 'Pharmacy'])),
                   'compliancestatustype': r.choice(['Compliant', 'Exempt', 'Pending', None]),
                   'districtname': dist_name[a['dist']],
                   'closurecomment': 'n/a' if r.random() < 0.0004 else None, 'renovationindicator': 'Y' if r.random() < 0.0004 else None,
                   'franchisecomment': None, 'legacystoretype': None}
            row.update(person_block('escalationcontact', a['esc'], 0.96))
            row.update(ref('possystem', D_SYS, systems[row['possystemname']]))
            row.update(ref('inventorysystem', D_SYS, systems['SHELFWISE'] if row['inventorysystemname'] else None))
            row.update(ref('paymentsystem', D_SYS, r.choice(list(systems.values()))))
            core.append(row)
    _write(folder, STORES, list(core[0].keys()), core)

    # ---------------- STORE_LINKS: links between stores, and stores and staff ----------------
    links = []
    for m in months:
        for b in stores:
            for k in range(r.choice([2, 4, 6, 8])):
                other = r.choice(stores)
                links.append({'reportingmonth': m, **ref('', D_STORE, b, 1 + months.index(m) // 2),
                              **ref('linkedstore', D_STORE, other if k % 2 else None),
                              **ref('delegatestaff', D_STAFF, r.choice(staff) if not k % 2 else None),
                              'linktype': 'STORE_LINK' if k % 2 else 'DELEGATE', 'effectivefromdate': (date(2024, 1, 1) + timedelta(days=r.randrange(700))).isoformat(),
                              'effectivetodate': None, 'linkcomment': None})
    _write(folder, LINKS, list(links[0].keys()), links)

    # ---------------- STORE_TREE: store hierarchy flattened into level1..level13 ----------------
    nodes = _ids(r, max(10, n_store // 3), 50_000, 59_999)
    root = nodes[0]
    tree = []
    for m in months:
        for b in stores:
            depth = r.choice([6, 7, 8, 9, 10, 11, 12, 13])
            path = [root] + [r.choice(nodes) for _ in range(depth - 2)] + [b]
            row = {'reportingmonth': m, **ref('', D_STORE, b, 1), 'level': depth, 'isleaf': 1}
            for k in range(1, 14):
                v = path[k - 1] if k <= depth else None
                row.update({'level%ddomain' % k: (D_STORE if v == b else D_NODE) if v else None, 'level%did' % k: v, 'level%dversion' % k: 1 if v else None})
            tree.append(row)
    _write(folder, TREE, list(tree[0].keys()), tree)

    return {'links': [
        (LINKS + '.identifierid', STORES + '.identifierid'),
        (LINKS + '.linkedstoreidentifierid', STORES + '.identifierid'),
        (LINKS + '.delegatestaffidentifierid', STAFF + '.staffid'),
        (STORES + '.buyeridentifierid', STAFF + '.staffid'),
        (STORES + '.storemanageridentifierid', STAFF + '.staffid'),
        (STORES + '.areamanageridentifierid', STAFF + '.staffid'),
        (STORES + '.lastinspectedbyidentifierid', STAFF + '.staffid'),
        (STORES + '.lastactionbyidentifierid', STAFF + '.staffid'),
        (STORES + '.assortmentidentifierid', ASSORTMENTS + '.identifierid'),
        (STORES + '.primaryzoneidentifierid', ZONES + '.identifierid'),
        (STORES + '.districtname', DISTRICTS + '.districtname'),
        (ASSORTMENTS + '.districtidentifierid', DISTRICTS + '.identifierid'),
        (STAFF + '.managerid', STAFF + '.staffid'),
        (ROTA + '.staffid', STAFF + '.staffid'),
        (TREE + '.identifierid', STORES + '.identifierid'),
    ], 'rows': {'STAFF': len(staff_rows), 'STAFF_ROTA': len(rota), 'STORES': len(core), 'STORE_LINKS': len(links), 'STORE_TREE': len(tree)}}


if __name__ == '__main__':
    import sys
    out = generate(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)
    print(out['rows'])
