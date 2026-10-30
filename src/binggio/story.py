# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Data story (prototype): the few facts that matter in one source, from approved meanings only.

Measures are columns approved as MEASURE; dimensions are approved CLASSIFICATION / STATUS columns
of the table itself or of tables it points to through approved many-to-one links inside the same
source (up to two steps: sales -> vehicles -> models). Dates give trends. Keys, IDs and free text
are never charted.

Ranking (standard library only):
  * driver facts: Kruskal-Wallis on each measure x dimension; effect size epsilon-squared (share of
    the measure's variation explained by the groups, robust to skew), p-values corrected for the
    number of pairs tested (Benjamini-Hochberg). Ranked by effect size, not p-value.
  * concentration: share of the total held by the top 3 of an approved reference (dealers, parts).
  * trend: monthly totals over an approved date; change between the first and last third.
  * gaps: rows of a referenced table that nothing points to (dealers with no sales).
"""
from __future__ import annotations
import math
import re
from collections import defaultdict
from pathlib import Path

from binggio.catalog import catalog
from binggio.discovery.discovery import _read_sample, _value
from binggio.registry import store

SUM_HINT = re.compile(r'amount|price|cost|revenue|fee|sales|income|spend|paid|value|premium|budget', re.I)
AVG_HINT = re.compile(r'(?:^|_)per(?:_|$)|rate|ratio|pct|percent|score|rating|avg|average|unit_|(?:^|_)km|hours|age$|days|odometer|year', re.I)
DIM_ROLES = ('CLASSIFICATION', 'STATUS')
MIN_GROUP, MAX_GROUPS = 5, 25


# ------------------------------------------------------------------ statistics

def _gammq(a, x):
    """Regularised upper incomplete gamma Q(a, x)."""
    if x <= 0: return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        s = term = 1.0 / a; ap = a
        for _ in range(500):
            ap += 1; term *= x / ap; s += term
            if abs(term) < abs(s) * 1e-12: break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - gln))
    b = x + 1 - a; c = 1 / 1e-300; d = 1 / b; h = d
    for i in range(1, 500):
        an = -i * (i - a); b += 2
        d = an * d + b; d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c; c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d; delta = d * c; h *= delta
        if abs(delta - 1) < 1e-12: break
    return min(1.0, math.exp(-x + a * math.log(x) - gln) * h)


def kruskal(groups):
    """H, p, epsilon-squared for a list of numeric lists."""
    allv = sorted((v, g) for g, vals in enumerate(groups) for v in vals)
    n = len(allv)
    if n < 3 or len(groups) < 2: return None
    ranks = [0.0] * n; ties = 0.0; i = 0
    while i < n:
        j = i
        while j + 1 < n and allv[j + 1][0] == allv[i][0]: j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1): ranks[k] = r
        t = j - i + 1; ties += t ** 3 - t; i = j + 1
    rs = defaultdict(float); ns = defaultdict(int)
    for (v, g), r in zip(allv, ranks): rs[g] += r; ns[g] += 1
    h = 12 / (n * (n + 1)) * sum(rs[g] ** 2 / ns[g] for g in rs) - 3 * (n + 1)
    c = 1 - ties / (n ** 3 - n) if n > 1 else 1
    if c <= 0: return None
    h /= c
    return h, _gammq((len(groups) - 1) / 2, h / 2), max(0.0, h / (n - 1))


def eta_squared(groups):
    allv = [v for g in groups for v in g]; m = sum(allv) / len(allv)
    sst = sum((v - m) ** 2 for v in allv)
    if sst == 0: return 0.0
    ssb = sum(len(g) * (sum(g) / len(g) - m) ** 2 for g in groups if g)
    return ssb / sst


def bh(pvals):
    order = sorted(range(len(pvals)), key=lambda i: pvals[i]); m = len(pvals); out = [1.0] * m; prev = 1.0
    for rank, i in reversed(list(enumerate(order, 1))):
        prev = min(prev, pvals[i] * m / rank); out[i] = prev
    return out


# ------------------------------------------------------------------ data

def _num(v):
    if v is None: return None
    s = str(v).strip().replace(',', '')
    if not s: return None
    try: return float(s)
    except ValueError: return None


def _words(name):
    return name.replace('_', ' ').strip().lower()


def _fmt(x):
    a = abs(x)
    for unit, d in (('B', 1e9), ('M', 1e6), ('K', 1e3)):
        if a >= d: return '%.1f%s' % (x / d, unit)
    return ('%.2f' % x).rstrip('0').rstrip('.') if a < 100 else '{:,.0f}'.format(x)


class Source:
    def __init__(self, root, cid):
        self.root = Path(root).resolve(); self.cid = cid
        self.conn = next(c for c in catalog.list_connections(self.root) if c['id'] == cid)
        roles = [r for r in store.list_roles(self.root, connection_id=cid, status='approved', limit=10 ** 6)['items']]
        self.roles = defaultdict(dict)
        for r in roles:
            self.roles[r['table']][r['column']] = (r['decision']['approved'] or {}).get('role') or r['candidate_role']
        self.links = defaultdict(list)   # child table -> [(child col, parent table, parent col)]
        for x in store.list_relationships(self.root, status='approved', limit=10 ** 6)['items']:
            a = x['decision']['approved']
            if a['child_connection_id'] == cid == a['parent_connection_id'] and a['cardinality'] in ('many_to_one', 'one_to_one'):
                self.links[a['child_table']].append((a['child_column'], a['parent_table'], a['parent_column']))
        self._data = {}

    def table(self, t):
        if t not in self._data:
            h, rows, _ = _read_sample(self.root, self.conn, t, 1_000_000)
            self._data[t] = (h, rows)
        return self._data[t]

    def col(self, t, c):
        h, rows = self.table(t); i = h.index(c)
        return [r[i] if i < len(r) else None for r in rows]

    def lookup(self, t, key, col):
        keys, vals = self.col(t, key), self.col(t, col)
        return {_value(k): v for k, v in zip(keys, vals) if _value(k) is not None}

    def dimensions(self, t):
        """[(label, values aligned with t's rows, path)]: t's own categories, and those of tables it
        points to through approved links in this source, up to two steps (sales -> vehicles -> models)."""
        out = [(_words(c), self.col(t, c), [t]) for c, r in self.roles[t].items() if r in DIM_ROLES]
        for ccol, pt, pcol in self.links.get(t, []):
            kv = [_value(k) for k in self.col(t, ccol)]
            for c, r in self.roles.get(pt, {}).items():
                if r in DIM_ROLES:
                    m = self.lookup(pt, pcol, c)
                    out.append((self._label(pt, c), [m.get(k) for k in kv], [t, pt]))
            for ccol2, pt2, pcol2 in self.links.get(pt, []):          # second step
                if pt2 == t: continue
                bridge = self.lookup(pt, pcol, ccol2)
                sk = [_value(bridge.get(k)) for k in kv]
                for c, r in self.roles.get(pt2, {}).items():
                    if r in DIM_ROLES:
                        m = self.lookup(pt2, pcol2, c)
                        out.append((self._label(pt2, c), [m.get(k) for k in sk], [t, pt, pt2]))
        return out

    @staticmethod
    def _label(table, col):
        tw = _words(table); tw = tw[:-1] if tw.endswith('s') and not tw.endswith('ss') else tw
        return _words(col) if _words(col).startswith(tw.split()[-1]) else '%s %s' % (tw.split()[-1], _words(col))


# ------------------------------------------------------------------ facts

def build(root, connection_id, top=6, table=None):
    """The strongest facts in one source; with `table`, the facts about that table (its own
    measures and dates, split by its categories and those of tables it links to)."""
    src = Source(root, connection_id)
    all_tables = sorted(src.roles)
    if table is not None and table not in src.roles:
        raise ValueError('No approved columns in table %s of this source' % table)
    tables = [table] if table else all_tables
    if not tables:
        return {'source': src.conn['name'], 'connection': connection_id, 'facts': [], 'asks': [], 'tables': [], 'table': table,
                'considered': {'measures': 0, 'pairs_tested': 0, 'facts_found': 0},
                'empty': 'Approve the column roles of this source first: the story uses approved measures and categories only.'}
    facts, drivers = [], []
    measures = []
    for t in tables:
        for c, r in src.roles[t].items():
            if r == 'MEASURE':
                vals = [_num(v) for v in src.col(t, c)]
                if sum(v is not None for v in vals) >= 10 and len({v for v in vals if v is not None}) > 2:
                    measures.append((t, c, vals, 'sum' if SUM_HINT.search(c) and not AVG_HINT.search(c) else 'average'))
    # headline: the busiest table with a summable measure (for one table without one: its row count)
    sums = [m for m in measures if m[3] == 'sum']
    if table and not sums:
        h, rows = src.table(table)
        facts.append({'kind': 'headline', 'score': 10, 'title': '%s %s' % ('{:,}'.format(len(rows)), _words(table)),
                      'detail': 'All %s rows of %s. Approve an amount or count column as a measure to see totals and what drives them.' % (
                          '{:,}'.format(len(rows)), table), 'chart': None, 'tables': [table]})
    if sums:
        t, c, vals, _ = max(sums, key=lambda m: len(m[2]))
        tot = sum(v for v in vals if v is not None)
        facts.append({'kind': 'headline', 'score': 10, 'title': '%s %s, %s in total %s' % ('{:,}'.format(len(vals)), _words(t), _fmt(tot), _words(c)),
                      'detail': 'All %s rows of %s; total of the approved measure %s.' % ('{:,}'.format(len(vals)), t, c),
                      'chart': None, 'tables': [t]})
    # drivers
    for t, c, vals, agg in measures:
        for label, dvals, path in src.dimensions(t):
            groups = defaultdict(list)
            for v, d in zip(vals, dvals):
                if v is not None and d not in (None, ''): groups[str(d).strip()].append(v)
            groups = {k: g for k, g in groups.items() if len(g) >= MIN_GROUP}
            if not 2 <= len(groups) <= MAX_GROUPS: continue
            res = kruskal(list(groups.values()))
            if not res: continue
            eta = eta_squared(list(groups.values()))
            if eta > 0.98: continue      # the dimension just restates the measure (one value per group)
            h, p, eps = res
            stat = {k: (sum(g) if agg == 'sum' else sum(g) / len(g)) for k, g in groups.items()}
            order = sorted(stat, key=lambda k: -stat[k])
            drivers.append({'t': t, 'c': c, 'agg': agg, 'label': label, 'path': path, 'p': p, 'eps': eps, 'eta': eta,
                            'n': sum(len(g) for g in groups.values()), 'order': order, 'stat': stat, 'k': len(groups)})
    if drivers:
        adj = bh([d['p'] for d in drivers])
        for d, q in zip(drivers, adj): d['q'] = q
        seen = set()
        for d in sorted((d for d in drivers if d['q'] < 0.05), key=lambda d: -d['eps']):
            if (d['t'], d['c']) in seen: continue
            seen.add((d['t'], d['c']))
            top1 = d['order'][0]; share = d['stat'][top1] / sum(d['stat'].values()) if d['agg'] == 'sum' else None
            what = '%s %s' % ('Total' if d['agg'] == 'sum' else 'Average', _words(d['c']))
            title = ('%s differs most by %s: %s leads with %s%s' % (what, d['label'], top1, _fmt(d['stat'][top1]),
                     ' (%d%% of the total)' % round(100 * share) if share else ''))
            weight = min(1.0, math.log10(max(d['n'], 2)) / 3)      # 1,000+ rows count fully; 24 rows about half
            facts.append({'kind': 'driver', 'score': 5 + 10 * d['eps'] * weight, 'title': title,
                          'detail': '%s explains %d%% of the variation in %s across %s %s (Kruskal-Wallis, adjusted p %s; ANOVA eta-squared %.2f).' % (
                              d['label'].capitalize(), round(100 * d['eps']), _words(d['c']), '{:,}'.format(d['n']), _words(d['t']),
                              '< 0.001' if d['q'] < 0.001 else '%.3f' % d['q'], d['eta']),
                          'chart': {'type': 'bar', 'labels': d['order'][:12], 'values': [round(d['stat'][k], 2) for k in d['order'][:12]],
                                    'unit': '%s %s' % (d['agg'], _words(d['c']))},
                          'tables': d['path'], 'ask': 'What is the %s %s by %s?' % ('total' if d['agg'] == 'sum' else 'average', _words(d['c']), d['label'])})
    # concentration: top 3 references by row count (and by the table's summable measure)
    for t in tables:
        for ccol, pt, pcol in src.links.get(t, []):
            keys = [_value(k) for k in src.col(t, ccol)]
            nparent = len({_value(k) for k in src.col(pt, pcol)} - {None})
            if nparent < 8: continue
            m = next((x for x in sums if x[0] == t), None)
            w = m[2] if m else [1.0] * len(keys)
            tot = defaultdict(float)
            for k, v in zip(keys, w):
                if k is not None and v is not None: tot[k] += v
            if not tot: continue
            ranked = sorted(tot.items(), key=lambda kv: -kv[1]); allsum = sum(tot.values())
            share = sum(v for _, v in ranked[:3]) / allsum; fair = 3 / nparent
            names = src.lookup(pt, pcol, next((c for c, r in src.roles[pt].items() if r == 'TEXT'), pcol))
            what = _words(m[1]) if m else _words(t)
            facts.append({'kind': 'concentration', 'score': 3 + 6 * max(0.0, share - fair) / (1 - fair),
                          'title': 'The top 3 of %s %s hold %d%% of %s' % ('{:,}'.format(nparent), _words(pt), round(100 * share), what),
                          'detail': 'An even spread would give the top 3 about %d%%.' % round(100 * fair),
                          'chart': {'type': 'bar', 'labels': [str(names.get(k, k)) for k, _ in ranked[:8]], 'values': [round(v, 2) for _, v in ranked[:8]],
                                    'unit': what}, 'tables': [t, pt]})
    # trend over an approved date
    for t in tables:
        dates = [c for c, r in src.roles[t].items() if r == 'DATE']
        if not dates: continue
        dcol = dates[0]
        m = next((x for x in sums if x[0] == t), None)
        months = defaultdict(float)
        for i, dv in enumerate(src.col(t, dcol)):
            mm = _month(dv)
            if mm: months[mm] += (m[2][i] or 0) if m else 1
        keys = sorted(months)
        if len(keys) < 6: continue
        vals = [months[k] for k in keys]; third = max(1, len(vals) // 3)
        a, b = sum(vals[:third]) / third, sum(vals[-third:]) / third
        change = (b - a) / a if a else 0
        what = ('total %s' % _words(m[1])) if m else ('%s per month' % _words(t))
        facts.append({'kind': 'trend', 'score': 2 + min(6, 10 * abs(change)),
                      'title': '%s %s %d%% from the first to the last third of the period' % (what.capitalize(), 'rose' if change >= 0 else 'fell', round(100 * abs(change))),
                      'detail': '%s to %s, by month of %s.' % (keys[0], keys[-1], dcol),
                      'chart': {'type': 'line', 'labels': keys, 'values': [round(v, 2) for v in vals], 'unit': what}, 'tables': [t]})
    # gaps: referenced rows that nothing points to
    for t in tables:
        for ccol, pt, pcol in src.links.get(t, []):
            used = {_value(k) for k in src.col(t, ccol)}
            allk = {_value(k) for k in src.col(pt, pcol)} - {None}
            unused = allk - used
            if allk and 0.02 <= len(unused) / len(allk) <= 0.8:
                used_n = len(allk) - len(unused)
                title = ('Only %s of %s %s have any %s' % ('{:,}'.format(used_n), '{:,}'.format(len(allk)), _words(pt), _words(t))) if len(unused) > len(allk) / 2 \
                    else '%s of %s %s have no %s' % ('{:,}'.format(len(unused)), '{:,}'.format(len(allk)), _words(pt), _words(t))
                facts.append({'kind': 'gap', 'score': 2.5, 'title': title,
                              'detail': 'Keys in %s.%s that never appear in %s.%s. Worth checking: inactive, new, or missing data?' % (pt, pcol, t, ccol),
                              'chart': None, 'tables': [t, pt]})
    # choose: headline, best drivers, best concentration, best trend, best gap
    pick = [f for f in facts if f['kind'] == 'headline'][:1]
    pick += sorted((f for f in facts if f['kind'] == 'driver'), key=lambda f: -f['score'])[:2]
    for kind in ('concentration', 'trend', 'gap'):
        pick += sorted((f for f in facts if f['kind'] == kind), key=lambda f: -f['score'])[:1]
    rest = sorted((f for f in facts if f not in pick), key=lambda f: -f['score'])
    pick = (pick + rest)[:top if not table else max(top, 8)]
    return {'source': src.conn['name'], 'connection': connection_id, 'facts': pick, 'tables': all_tables, 'table': table,
            'considered': {'measures': len(measures), 'pairs_tested': len(drivers), 'facts_found': len(facts)},
            'asks': [f['ask'] for f in pick if f.get('ask')][:3]}


def _month(v):
    s = str(v or '').strip()
    m = re.match(r'(\d{4})[-/](\d{1,2})', s) or None
    if m: return '%s-%02d' % (m.group(1), int(m.group(2)))
    m = re.match(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', s)
    if m: return '%s-%02d' % (m.group(3), int(m.group(2)))
    return None
