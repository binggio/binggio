# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Synthetic automotive data for testing "Add data" in Binggio. Seeded, so every run is identical.

    python samples\\automotive\\make_automotive.py          # writes the two source folders + ANSWER_KEY.md

Two source systems, as a carmaker would have them:

  plant_and_sales/        models, plants, vehicles, dealers, customers, vehicle_sales
  aftersales_quality/     parts, suppliers, service_visits, warranty_claims, recalls,
                          recall_completions, vehicle_telematics, dealer_directory, fuel_prices

Built-in traps (what Binggio should cope with):
  * keys not named *_id: vin, model_code, part_number; links through dealer_code, dealer_ref,
    vehicle_vin and part_no;
  * links across the two systems (service visits -> vehicles and dealers; recalls -> models);
  * messy values: VINs in lower case with spaces in service_visits, amounts with thousands
    separators ("1,234.50"), day-first dates in one file, a byte-order mark, a semicolon-separated
    file, Windows line endings;
  * vehicle_telematics: one row per vehicle (an extension of vehicles, not a new entity);
  * dealer_directory: the dealers table again under other column names (a duplicate);
  * fuel_prices: linked to nothing (standalone);
  * coded columns that need business words: vehicle status, visit type, claim status, severity,
    completed Y/N, connected Y/N.
"""
from __future__ import annotations
import csv
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = random.Random(2026)

MODELS = [('AX1', 'Axion City', 'HATCHBACK', 'PETROL', 2019, 690000), ('AX2', 'Axion City E', 'HATCHBACK', 'EV', 2022, 1290000),
          ('SD3', 'Sedra', 'SEDAN', 'PETROL', 2018, 980000), ('SD4', 'Sedra Hybrid', 'SEDAN', 'HYBRID', 2021, 1450000),
          ('TR5', 'Terra', 'SUV', 'DIESEL', 2017, 1650000), ('TR6', 'Terra E', 'SUV', 'EV', 2023, 2390000),
          ('VN7', 'Cargo Van', 'VAN', 'DIESEL', 2016, 1180000), ('PK8', 'Ridge Pickup', 'PICKUP', 'DIESEL', 2020, 1720000)]
PLANTS = [('PL01', 'Chakan Assembly', 'Pune', 'India', 180000), ('PL02', 'Sanand Works', 'Ahmedabad', 'India', 150000),
          ('PL03', 'Hosur Plant', 'Hosur', 'India', 120000)]
CITIES = [('Mumbai', 'WEST'), ('Pune', 'WEST'), ('Delhi', 'NORTH'), ('Jaipur', 'NORTH'), ('Chennai', 'SOUTH'),
          ('Bengaluru', 'SOUTH'), ('Kolkata', 'EAST'), ('Hyderabad', 'SOUTH')]
FIRST = ['Aarav', 'Diya', 'Kabir', 'Meera', 'Rohan', 'Ishita', 'Vikram', 'Ananya', 'Arjun', 'Sara', 'Dev', 'Nisha']
LAST = ['Sharma', 'Iyer', 'Khan', 'Patel', 'Reddy', 'Das', 'Gupta', 'Menon', 'Singh', 'Rao']
PARTS = [('Brake pad set', 'BRAKES', 2400), ('Brake disc', 'BRAKES', 5200), ('Alternator', 'ELECTRICAL', 14500),
         ('Battery 12V', 'ELECTRICAL', 7800), ('Traction battery module', 'EV_SYSTEM', 185000), ('Charging port', 'EV_SYSTEM', 22000),
         ('Fuel injector', 'ENGINE', 9800), ('Turbocharger', 'ENGINE', 64000), ('Clutch kit', 'TRANSMISSION', 18500),
         ('Gearbox sensor', 'TRANSMISSION', 4300), ('Shock absorber', 'SUSPENSION', 6100), ('Wheel bearing', 'SUSPENSION', 3900),
         ('Infotainment unit', 'ELECTRICAL', 38000), ('Airbag module', 'SAFETY', 27500), ('Seat belt pretensioner', 'SAFETY', 8900)]


def d(start, days):
    return start + timedelta(days=days)


def build():
    t = {}
    t['models'] = [dict(model_code=m[0], model_name=m[1], segment=m[2], fuel_type=m[3], launch_year=m[4], base_price=m[5]) for m in MODELS]
    t['plants'] = [dict(plant_id=p[0], plant_name=p[1], city=p[2], country=p[3], capacity_per_year=p[4]) for p in PLANTS]
    t['dealers'] = []
    for i in range(1, 25):
        city, region = CITIES[i % len(CITIES)]
        t['dealers'].append(dict(dealer_id='DL%03d' % i, dealer_name='%s Motors %d' % (city, i), city=city, region=region,
                                 tier=['GOLD', 'SILVER', 'BRONZE'][i % 3], opened_on=str(d(date(2010, 1, 1), 120 * i))))
    t['customers'] = []
    for i in range(1, 401):
        city, _ = R.choice(CITIES)
        fleet = R.random() < 0.12
        t['customers'].append(dict(customer_id='CU%05d' % i, full_name=('%s Logistics Pvt Ltd' % LAST[i % 10]) if fleet else '%s %s' % (FIRST[i % 12], LAST[(i * 7) % 10]),
                                   city=city, customer_type='FLEET' if fleet else 'RETAIL', joined_on=str(d(date(2018, 1, 1), (i * 5) % 2200))))
    t['vehicles'] = []
    for i in range(1, 901):
        m = R.choices(MODELS, weights=[22, 8, 16, 9, 18, 5, 10, 12])[0]
        status = ['SOLD'] * 7 + ['AT_DEALER', 'IN_TRANSIT', 'BUILT']
        t['vehicles'].append(dict(vin='MA1%s%08d' % (m[0], i), model_code=m[0], plant_id=R.choices(PLANTS, weights=[5, 3, 2])[0][0],
                                  build_date=str(d(date(2021, 1, 1), R.randrange(1300))),
                                  colour=R.choice(['WHITE', 'SILVER', 'BLACK', 'RED', 'BLUE', 'GREY']),
                                  trim=R.choice(['BASE', 'MID', 'TOP']), status=R.choice(status)))
    sold = [v for v in t['vehicles'] if v['status'] == 'SOLD']
    t['vehicle_sales'] = []
    for i, v in enumerate(sold, 1):
        m = next(x for x in MODELS if x[0] == v['model_code'])
        cu = R.choice(t['customers'])
        t['vehicle_sales'].append(dict(sale_id='SL%06d' % i, vin=v['vin'], dealer_code=R.choices(t['dealers'], weights=[1 + (j * 7) % 5 for j in range(24)])[0]['dealer_id'],
                                       customer_id=cu['customer_id'],
                                       sale_date=str(d(date.fromisoformat(v['build_date']), R.randint(15, 90))),
                                       sale_price=round(m[5] * R.uniform(0.9, 1.04), 2),
                                       finance_type=('LEASE' if cu['customer_type'] == 'FLEET' and R.random() < 0.6 else R.choice(['CASH', 'LOAN', 'LOAN']))))
    t['suppliers'] = [dict(supplier_id='SP%02d' % i, supplier_name=n, country=c, quality_rating=q) for i, (n, c, q) in enumerate(
        [('Sundaram Brakes', 'India', 4.6), ('Bosch Mobility', 'Germany', 4.8), ('Lucas Electrical', 'India', 4.1),
         ('Exide Energy', 'India', 4.3), ('CATL Modules', 'China', 4.5), ('Denso Systems', 'Japan', 4.7), ('Valeo Transmission', 'France', 4.2)], 1)]
    t['parts'] = [dict(part_number='PN-%04d' % (1000 + i), part_name=p[0], category=p[1], unit_cost=p[2],
                       supplier_id=t['suppliers'][i % 7]['supplier_id']) for i, p in enumerate(PARTS)]
    t['service_visits'] = []
    for i in range(1, 1601):
        v = R.choice(sold)
        vt = R.choices(['ROUTINE', 'REPAIR', 'WARRANTY', 'RECALL'], weights=[50, 28, 14, 8])[0]
        t['service_visits'].append(dict(visit_id='SV%06d' % i, vehicle_vin=v['vin'], dealer_ref=R.choice(t['dealers'])['dealer_id'],
                                        visit_date=str(d(date(2023, 1, 1), R.randrange(650))), odometer_km=R.randint(800, 95000),
                                        visit_type=vt, labour_hours=round(R.choice([0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6.5]) * (1.6 if vt == 'REPAIR' else 1), 1)))
    t['warranty_claims'] = []
    k = 0
    for v in t['service_visits']:
        if v['visit_type'] not in ('WARRANTY', 'REPAIR'): continue
        if v['visit_type'] == 'REPAIR' and R.random() < 0.55: continue
        k += 1
        p = R.choices(t['parts'], weights=[9, 4, 3, 6, 2, 3, 5, 2, 4, 5, 6, 5, 3, 1, 2])[0]
        t['warranty_claims'].append(dict(claim_id='WC%06d' % k, visit_id=v['visit_id'], part_no=p['part_number'],
                                         claim_amount=round(p['unit_cost'] * R.uniform(1.0, 1.35) + R.choice([450, 900, 1350]), 2),
                                         claim_status=R.choices(['OPEN', 'APPROVED', 'PAID', 'REJECTED'], weights=[15, 20, 50, 15])[0],
                                         failure_code='F%02d' % R.randrange(1, 18)))
    t['recalls'] = [dict(recall_id='RC%03d' % i, model_code=m, issue=iss, announced_on=str(dd), severity=sev) for i, (m, iss, dd, sev) in enumerate([
        ('AX2', 'Charging port overheating', date(2024, 3, 4), 'HIGH'), ('TR5', 'Turbo hose clamp', date(2023, 9, 18), 'MEDIUM'),
        ('SD3', 'Seat belt pretensioner', date(2024, 6, 1), 'HIGH'), ('VN7', 'Rear lamp wiring', date(2023, 2, 11), 'LOW'),
        ('TR6', 'Battery module software', date(2025, 1, 20), 'HIGH')], 1)]
    t['recall_completions'] = []
    n = 0
    for r in t['recalls']:
        for v in [v for v in t['vehicles'] if v['model_code'] == r['model_code']]:
            n += 1
            done = R.random() < (0.55 if r['severity'] == 'HIGH' else 0.75)
            t['recall_completions'].append(dict(completion_id='CP%06d' % n, recall_id=r['recall_id'], vin=v['vin'],
                                                completed_on=str(d(date.fromisoformat(r['announced_on']), R.randint(7, 220))) if done else '',
                                                completed='Y' if done else 'N'))
    t['vehicle_telematics'] = [dict(vin=v['vin'], last_odometer_km=R.randint(1500, 98000), avg_daily_km=round(R.uniform(12, 85), 1),
                                    last_ping=str(d(date(2025, 6, 1), R.randrange(120))), connected='Y' if R.random() < 0.85 else 'N')
                               for i, v in enumerate(sold, 1)]
    t['dealer_directory'] = [dict(code=x['dealer_id'], name=x['dealer_name'], town=x['city'], area=x['region'], grade=x['tier'])
                             for x in reversed(t['dealers'])]
    t['fuel_prices'] = [dict(month='2025-%02d' % m, fuel_type=f, price_per_litre=round(p + m * 0.4, 2))
                        for m in range(1, 13) for f, p in (('PETROL', 102.5), ('DIESEL', 89.2))]
    return t


SYSTEMS = {'plant_and_sales': ['models', 'plants', 'vehicles', 'dealers', 'customers', 'vehicle_sales'],
           'aftersales_quality': ['parts', 'suppliers', 'service_visits', 'warranty_claims', 'recalls', 'recall_completions',
                                  'vehicle_telematics', 'dealer_directory', 'fuel_prices']}


def messy(table, col, v, i):
    if table == 'service_visits' and col == 'vehicle_vin' and i % 6 == 0: return ' %s ' % v.lower()
    if table in ('warranty_claims', 'vehicle_sales') and col in ('claim_amount', 'sale_price'): return '{:,.2f}'.format(v)
    if table == 'recall_completions' and col == 'completed_on' and v:
        y, m, dd = v.split('-'); return '%s/%s/%s' % (dd, m, y)   # day-first dates in this file
    return v


def write(t, base=HERE):
    for system, tables in SYSTEMS.items():
        folder = Path(base) / system; folder.mkdir(parents=True, exist_ok=True)
        for name in tables:
            rows = t[name]; cols = list(rows[0])
            enc = 'utf-8-sig' if name == 'vehicle_sales' else 'utf-8'          # one file with a byte-order mark
            delim = ';' if name == 'warranty_claims' else ','                   # one semicolon-separated file
            with open(folder / (name + '.csv'), 'w', newline='', encoding=enc) as f:
                w = csv.writer(f, delimiter=delim, lineterminator='\r\n')
                w.writerow(cols)
                for i, r in enumerate(rows):
                    w.writerow([messy(name, c, r[c], i) for c in cols])


# ------------------------------------------------------------------ independent answers

def answers(t):
    dealer = {x['dealer_id']: x for x in t['dealers']}
    vehicle = {x['vin']: x for x in t['vehicles']}
    model = {x['model_code']: x for x in t['models']}
    part = {x['part_number']: x for x in t['parts']}
    cust = {x['customer_id']: x for x in t['customers']}
    a = {}
    by_region = {}
    for s in t['vehicle_sales']: by_region[dealer[s['dealer_code']]['region']] = by_region.get(dealer[s['dealer_code']]['region'], 0) + 1
    a['Vehicles sold per dealer region'] = dict(sorted(by_region.items()))
    paid = {}
    for c in t['warranty_claims']:
        if c['claim_status'] == 'PAID':
            cat = part[c['part_no']]['category']; paid[cat] = round(paid.get(cat, 0) + c['claim_amount'], 2)
    a['Total PAID warranty claim amount per part category'] = dict(sorted(paid.items()))
    high = {r['recall_id'] for r in t['recalls'] if r['severity'] == 'HIGH'}
    open_high = sorted({c['vin'] for c in t['recall_completions'] if c['recall_id'] in high and c['completed'] == 'N'})
    a['Vehicles with a HIGH-severity recall not yet completed (count)'] = len(open_high)
    per_dealer = {}
    for s in t['vehicle_sales']: per_dealer[s['dealer_code']] = per_dealer.get(s['dealer_code'], 0) + 1
    top = sorted(per_dealer.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    a['Top 5 dealers by number of sales'] = ['%s (%s): %d' % (k, dealer[k]['dealer_name'], v) for k, v in top]
    ev_fleet = sorted({s['customer_id'] for s in t['vehicle_sales']
                       if model[vehicle[s['vin']]['model_code']]['fuel_type'] == 'EV' and cust[s['customer_id']]['customer_type'] == 'FLEET'})
    a['FLEET customers who bought an EV'] = ev_fleet
    hours = {}
    for v in t['service_visits']:
        if v['visit_type'] == 'REPAIR':
            m = vehicle[v['vehicle_vin']]['model_code']; hours.setdefault(m, []).append(v['labour_hours'])
    a['Average labour hours of REPAIR visits per model'] = {m: round(sum(h) / len(h), 2) for m, h in sorted(hours.items())}
    sold_by_model = {}
    for s in t['vehicle_sales']:
        m = model[vehicle[s['vin']]['model_code']]['model_name']; sold_by_model[m] = round(sold_by_model.get(m, 0) + s['sale_price'], 2)
    a['Sales revenue per model'] = dict(sorted(sold_by_model.items()))
    a['Vehicles sold that have no service visit (count)'] = len({s['vin'] for s in t['vehicle_sales']} - {v['vehicle_vin'] for v in t['service_visits']})
    return a


KEY_TEXT = """# Automotive test data: answer key

Two sources. Add them one after the other (Data sources -> CSV folder), first `plant_and_sales`, then
`aftersales_quality`, and onboard each. Generated by `make_automotive.py` (seeded; identical every run).

## Row counts

{counts}

## Own keys Binggio should suggest

| Table | Key |
| --- | --- |
{keys}

## Links Binggio should find

| From | To | Note |
| --- | --- | --- |
{links}

## How the second source should be classified (onboarding, step "How this source fits your data")

| Table | Expected | Why |
| --- | --- | --- |
| vehicle_telematics | Adds columns | one row per sold vehicle, keyed by vin |
| dealer_directory | Duplicate | the dealers table under other column names; left out of approval |
| fuel_prices | Not linked | no key shared with anything |
| parts, recalls, service_visits, warranty_claims, recall_completions | New entity / records | own keys, linked to existing tables |
| suppliers | Lookup list | parts point to it |

## Coded columns that need words before questions can use them

vehicles.status (BUILT, IN_TRANSIT, AT_DEALER, SOLD), service_visits.visit_type (ROUTINE, REPAIR, WARRANTY, RECALL),
warranty_claims.claim_status (OPEN, APPROVED, PAID, REJECTED), recalls.severity (LOW, MEDIUM, HIGH),
recall_completions.completed (Y, N), vehicle_telematics.connected (Y, N), vehicle_sales.finance_type (CASH, LOAN, LEASE),
customers.customer_type (RETAIL, FLEET). Example words: completed = N means "open recall"; claim_status = PAID means "paid claim".

## Traps in the files

- `service_visits.vehicle_vin`: every 6th VIN is lower case with spaces (" ma1ax200000012 "). Links must still be found.
- `vehicle_sales.sale_price` and `warranty_claims.claim_amount`: thousands separators ("1,234,567.00").
- `recall_completions.completed_on`: day-first dates (31/12/2024); empty when not completed.
- `vehicle_sales.csv` has a byte-order mark; `warranty_claims.csv` is separated by semicolons; all files use Windows line endings.

## Questions with known answers

Ask these after approving both sources (in your own words with a model key, or check the numbers against Explore).
Some need business words first, for example "open recall" (completed = N) or "paid claim" (claim_status = PAID).

{answers}
"""


EXPECTED_KEYS = {'models': 'model_code', 'plants': 'plant_id', 'vehicles': 'vin', 'dealers': 'dealer_id', 'customers': 'customer_id',
                 'vehicle_sales': 'sale_id', 'parts': 'part_number', 'suppliers': 'supplier_id', 'service_visits': 'visit_id',
                 'warranty_claims': 'claim_id', 'recalls': 'recall_id', 'recall_completions': 'completion_id', 'vehicle_telematics': 'vin',
                 'dealer_directory': 'code'}
EXPECTED_LINKS = [('vehicles.model_code', 'models.model_code', ''), ('vehicles.plant_id', 'plants.plant_id', ''),
                  ('vehicle_sales.vin', 'vehicles.vin', 'key not named *_id'), ('vehicle_sales.dealer_code', 'dealers.dealer_id', 'different column name'),
                  ('vehicle_sales.customer_id', 'customers.customer_id', ''),
                  ('service_visits.vehicle_vin', 'vehicles.vin', 'across systems; messy values'),
                  ('service_visits.dealer_ref', 'dealers.dealer_id', 'across systems; different name'),
                  ('warranty_claims.visit_id', 'service_visits.visit_id', ''), ('warranty_claims.part_no', 'parts.part_number', 'different name'),
                  ('parts.supplier_id', 'suppliers.supplier_id', ''), ('recalls.model_code', 'models.model_code', 'across systems'),
                  ('recall_completions.recall_id', 'recalls.recall_id', ''), ('recall_completions.vin', 'vehicles.vin', 'across systems'),
                  ('vehicle_telematics.vin', 'vehicles.vin', 'one to one (extension)'),
                  ('dealer_directory.code', 'dealers.dealer_id', 'one to one: the copy; shown as evidence, left out of approval')]


def fmt(v):
    if isinstance(v, dict): return '\n'.join('  - %s: %s' % (k, '{:,}'.format(x) if isinstance(x, (int, float)) else x) for k, x in v.items())
    if isinstance(v, list): return '\n'.join('  - %s' % x for x in v) if v else '  - (none)'
    return '  - %s' % v


def main():
    t = build(); write(t)
    a = answers(t)
    text = KEY_TEXT.format(
        counts='\n'.join('- %s / %s: %d rows' % (s, n, len(t[n])) for s, ns in SYSTEMS.items() for n in ns),
        keys='\n'.join('| %s | %s |' % kv for kv in EXPECTED_KEYS.items()),
        links='\n'.join('| %s | %s | %s |' % l for l in EXPECTED_LINKS),
        answers='\n\n'.join('**%d. %s**\n\n%s' % (i, q, fmt(v)) for i, (q, v) in enumerate(a.items(), 1)))
    (HERE / 'ANSWER_KEY.md').write_text(text, encoding='utf-8')
    print('Wrote', HERE)


if __name__ == '__main__':
    main()
