# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""What a column's name says, and the groups columns form inside a table.

Enterprise extracts often write names as one lowercase word (traderidentifierid,
accountingcategorycode, isleaf). These helpers read the end of such names (id, code, version,
domain, flag, email ...) and find the structures a table is made of:

* reference   - a reference stored as …domain / …id / …version (one record, its system, its version)
* person      - a block of columns about one person (…firstname, …lastname, …emailaddress, …location)
* code_name   - a code with its name or description (businessownercode / businessownername)
* hierarchy   - a path flattened into numbered levels (level1id … level13id, cc_l1code … cc_l10code)
* optional    - columns that are empty on the same rows (an optional part of the record)

All of this is a suggestion shown to a person; nothing here approves anything.
"""
from __future__ import annotations
import re

# words that end in "id" but are not identifiers
_NOT_ID = ('paid', 'valid', 'invalid', 'void', 'avoid', 'hybrid', 'grid', 'fluid', 'liquid', 'rapid', 'solid', 'squid',
           'android', 'humid', 'vivid', 'candid', 'acid', 'lipid', 'orchid', 'pyramid', 'said', 'laid', 'raid', 'maid',
           'afraid', 'braid', 'unpaid', 'prepaid', 'overpaid', 'rigid', 'timid', 'torrid', 'lucid', 'covid', 'mid', 'bid')
ID_ENDINGS = ('identifierid', 'identifier', 'uuid', 'guid', 'id', 'key', 'nbr')
PERSON_PARTS = ('firstname', 'lastname', 'fullname', 'displayname', 'middlename', 'emailaddress', 'email', 'citycode',
                'location', 'phone', 'title', 'name')
_LEVEL = re.compile(r'^(.*?)(level|lvl|l)(\d{1,2})(?![0-9])(.*)$', re.I)
_PERIOD = re.compile(r'(?:reporting|report|as_?of|snapshot|business|cob|valuation|position)_?(?:month|date|period|day)$|(?:^|_)(?:month|period|yearmonth|cob_?date)$', re.I)


def low(name: str) -> str:
    return re.sub(r'([a-z])([A-Z])', r'\1_\2', str(name)).casefold()


def flat(name: str) -> str:
    """The name without separators: trader_identifier_id -> traderidentifierid."""
    return re.sub(r'[^a-z0-9]', '', low(name))


def is_email(name: str) -> bool:
    return 'email' in flat(name) or flat(name).endswith('mail')


def is_flag(name: str) -> bool:
    f, s = flat(name), low(name)
    return (bool(re.match(r'^(?:is|has|can|was|should|allow)[a-z]', f) and len(f) > 4 and not f.startswith(('issue', 'isin', 'isolat', 'island', 'hash')))
            or f.endswith(('flag', 'indicator', 'yn')) or bool(re.search(r'(?:^|_)(?:ind|flg)$', s)))


def is_version(name: str) -> bool:
    f = flat(name)
    return f == 'version' or f.endswith(('version', 'versionnumber', 'versionno'))


def is_domain(name: str) -> bool:
    return flat(name).endswith('domain')


def is_level(name: str) -> bool:
    f = flat(name)
    return f in ('level', 'depth', 'tier') or f.endswith(('hierarchylevel', 'level', 'depth')) and not f.endswith(('servicelevel', 'risklevel'))


def is_period(name: str) -> bool:
    return bool(_PERIOD.search(low(name))) or flat(name) in ('reportingmonth', 'reportmonth', 'month', 'period', 'yearmonth', 'cobdate', 'asofdate')


def is_code(name: str) -> bool:
    f = flat(name)
    return f.endswith('code') and f not in ('postcode', 'zipcode', 'barcode', 'qrcode')


def id_ending(name: str) -> str | None:
    """'id' for traderidentifierid, 'key' for customer_key ...; None when the name is not identifier-like."""
    f = flat(name)
    if f.endswith(_NOT_ID) or is_email(name): return None
    for e in ID_ENDINGS:
        if f.endswith(e): return e
    return None


def subject(name: str) -> str:
    """The thing a reference names: traderidentifierid -> trader, employee_id -> employee,
    level10id -> level10, identifierid -> '' (the table's own identifier)."""
    f = flat(name)
    for e in ('identifierdomain', 'identifierversion', 'identifierid', 'identifier', 'domain', 'version', 'uuid', 'guid', 'id', 'key',
              'code', 'nbr', 'number', 'no'):
        if f.endswith(e) and (e not in ('no', 'id') or len(f) > len(e)):
            return f[:-len(e)]
    return f


def groups(columns: list[dict], masks: dict | None = None, rows: int = 0) -> list[dict]:
    """Structures among a table's columns. `columns` are profile column dicts in table order;
    `masks` maps a column name to the set of sample row numbers where it is empty."""
    names = [c['name'] for c in columns]
    by_flat = {flat(n): n for n in names}
    used = set()
    out = []

    # hierarchy paths: the same names with a level number (level1id, level2id ...)
    levels = {}
    for n in names:
        m = _LEVEL.match(flat(n))
        if not m: continue
        prefix, token, num, rest = m.groups()
        levels.setdefault((prefix, token), {}).setdefault(int(num), []).append(n)
    for (prefix, token), by_num in levels.items():
        if len(by_num) < 3: continue
        cols = [n for k in sorted(by_num) for n in by_num[k]]
        lo, hi = min(by_num), max(by_num)
        out.append({'kind': 'hierarchy', 'name': '%s … %s' % (by_num[lo][0], by_num[hi][-1]), 'columns': cols,
                    'detail': 'A hierarchy path flattened into %d numbered levels (%d to %d). Deeper levels are empty for shorter '
                              'branches, so their empty cells are expected, not missing data.' % (len(by_num), lo, hi)})
        used.update(cols)

    # references: …domain / …id / …version
    for n in names:
        if n in used or not is_version(n): continue
        base = flat(n)[:-len('version')]
        if not base: continue
        idc = by_flat.get(base + 'id') or by_flat.get(base + 'key') or by_flat.get(base + 'code')
        if not idc or idc in used: continue
        dom = by_flat.get(base + 'domain')
        cols = [x for x in (dom, idc, n) if x]
        label = re.sub(r'identifier$', '', base) or 'own'
        out.append({'kind': 'reference', 'name': label, 'columns': cols, 'id_column': idc, 'version_column': n,
                    'domain_column': dom,
                    'detail': ('One reference stored in %d columns: %s. Use them together; the version is not a number to add up.'
                               % (len(cols), ', '.join(cols)))})

    # people: …firstname, …lastname, …emailaddress, …citycode, …location
    bases = {}
    for n in names:
        f = flat(n)
        for part in PERSON_PARTS:
            if f.endswith(part) and len(f) > len(part):
                bases.setdefault(f[:-len(part)], []).append(n); break
    for base, cols in bases.items():
        parts = {flat(c)[len(base):] for c in cols}
        if len(parts & {'firstname', 'lastname', 'fullname', 'displayname', 'emailaddress', 'email'}) < 2 or len(cols) < 3: continue
        ref = next((g for g in out if g['kind'] == 'reference' and flat(g['id_column']).startswith(base)), None)
        allc = (ref['columns'] if ref else []) + cols
        out.append({'kind': 'person', 'name': base, 'columns': allc, 'id_column': ref['id_column'] if ref else None,
                    'detail': 'Details of one person (%s) stored in %d columns%s.' % (
                        base, len(allc), '; %s identifies them' % ref['id_column'] if ref else '')})

    # code with its name or description
    for n in names:
        if n in used or not is_code(n): continue
        base = flat(n)[:-4]
        pair = next((by_flat[base + s] for s in ('name', 'desc', 'description', 'label', 'text') if base + s in by_flat), None)
        if pair:
            out.append({'kind': 'code_name', 'name': base or n, 'columns': [n, pair],
                        'detail': '%s is a code and %s its name: a small lookup kept inside this table.' % (n, pair)})

    # optional parts: three or more columns empty on exactly the same sample rows
    if masks and rows:
        same = {}
        in_hierarchy = {n for g in out if g['kind'] == 'hierarchy' for n in g['columns']}
        for n in names:
            m = masks.get(n)
            if m is None or n in in_hierarchy or not (0.05 * rows <= len(m) < rows): continue
            same.setdefault(frozenset(m), []).append(n)
        for m, cols in same.items():
            if len(cols) < 3: continue
            # already a person or a reference: say on that group that it is optional
            home = next((g for g in out if g['kind'] in ('person', 'reference') and set(cols) <= set(g['columns'])), None)
            if home is not None:
                home['optional_pct'] = float(_pct(len(m) / rows))
                home['detail'] += ' Empty on %s%% of rows (optional).' % _pct(len(m) / rows)
                continue
            out.append({'kind': 'optional', 'name': _common(cols) or cols[0], 'columns': cols,
                        'detail': '%d columns are empty on the same %s%% of rows: an optional part of the record (for example '
                                  'rows without a %s), not %d separate gaps.' % (len(cols), _pct(len(m) / rows), _common(cols) or 'value', len(cols))})
    return out


def _pct(x):
    return ('%.1f' % (100 * x)).rstrip('0').rstrip('.')


def _common(cols):
    fs = [flat(c) for c in cols]
    p = fs[0]
    for f in fs[1:]:
        while not f.startswith(p): p = p[:-1]
    return p if len(p) >= 4 else ''
