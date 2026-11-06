# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Read-only, bounded table profiling for Phase 4 CSV and SQLite connectors.

All percentages are SAMPLE observations unless sample_complete is true. Candidate
semantic roles are heuristics, never approved mappings. Does not import oracle data.
Never exports raw values; diagnostic findings contain aggregates only.
"""
from __future__ import annotations
from contextlib import closing

import csv
import hashlib
import io
import math
import re
import sqlite3
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from binggio.catalog.connectors import (ConnectionErrorSafe, MAX_CSV_BYTES,
    MAX_COLUMNS, _quote_identifier, _readonly, _unwrapped_row,
    list_sqlite_tables, resolve_source)

VERSION = '5.3.0'
SAMPLE_LIMIT = 2500
MISSING = {'', 'null', 'none', 'n/a', 'na', 'nan', '?'}
DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M',
                '%Y-%m-%dT%H:%M', '%d-%b-%Y', '%d %b %Y', '%d-%b-%y', '%d-%b-%Y %H:%M', '%d-%b-%Y %H:%M:%S', '%d %b %Y %H:%M:%S',
                '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d.%m.%Y')
NUMERIC = re.compile(r'^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$|^[+-]?\.\d+$')
INTEGER = re.compile(r'^[+-]?\d+$')
# spellings that mean "no value" but are not already read as empty (MISSING)
PLACEHOLDERS = {'-', '--', '---', '#n/a', 'n.a.', 'n.a', 'unknown', 'not available', 'not applicable', 'nil', '(blank)', '(null)', '#null!', 'tbd', 'missing'}
_FORMATTED_NUMBER = re.compile(r'^\(?[-+]?\s*(?:[$€£₹¥]|usd|eur|gbp|inr|rs\.?)?\s*\d[\d,]*(?:\.\d+)?\s*(?:%|[a-z]{1,3}|usd|eur|gbp|inr)?\)?$', re.I)
ID_HINT = re.compile(r'(?:^|_)(?:id|key|ref|code|uuid|identifier|no|num|nbr|number)$|(?:^|_)(?:id|key|ref|code|uuid)(?:_|$)|[a-z](?:Id|ID|No|Num|Number)$'
                     # well-known business identifiers whose names do not say "id"
                     r'|(?:^|_)(?:vin|sku|imei|isin|cusip|lei|gtin|ean|upc|chassis_no|serial_no|serial_number)$', re.I)
NOT_KEY_NAMES = ('postcode', 'postal_code', 'zip', 'zipcode', 'zip_code', 'phone', 'phone_number', 'mobile_number', 'fax_number',
                 'contact_number', 'house_number', 'street_number', 'flat_number', 'iban', 'swift', 'tax_id')
DATE_HINT = re.compile(r'(?:date|_at$|^as_of|period_|_period$|_dt$|_time$|^dt_)', re.I)
MEASURE_HINT = re.compile(r'(?:amount|revenue|balance|cost|fee|interest|limit|budget|score|rating|threshold|total|price|value|quantity|count|severity|percentage|rate|notional)', re.I)
STATUS_HINT = re.compile(r'(?:status|stage|outcome|resolution|(?:^|_)state$)', re.I)
_PLACE_STATE = re.compile(r'^(?:state|province|county|region_state|state_name)$', re.I)
TEXT_HINT = re.compile(r'(?:text|notes?|description|comment|feedback|message|name|address|contact_value)', re.I)
CLASS_HINT = re.compile(r'(?:type|category|segment|channel|role|city|branch|rating|preference|reason)', re.I)


def _now():return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _is_missing(v: Any) -> bool:
    return v is None or (isinstance(v,str) and v.strip().casefold() in MISSING)


_DATE_SHAPE = re.compile(r'^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$'
                         r'|^\d{1,2}[- ][A-Za-z]{3}[- ]\d{2,4}(?: \d{1,2}:\d{2}(?::\d{2})?)?$')
_FRACTION_TZ = re.compile(r'(?<=:\d\d)[.,]\d{1,9}|(?<=\d)(?:Z|[+-]\d{2}:?\d{2})$')
_DATE_CACHE: dict = {}


def _date_parse(v: str) -> str | None:
    # Only text shaped like a date is tried against the formats (strptime is slow), and each
    # distinct value is worked out once.
    if not _DATE_SHAPE.match(v):return None
    hit=_DATE_CACHE.get(v,0)
    if hit!=0:return hit
    fmt=_date_parse_slow(v)
    if len(_DATE_CACHE)>200_000:_DATE_CACHE.clear()
    _DATE_CACHE[v]=fmt
    return fmt


def _date_parse_slow(v: str) -> str | None:
    # Fractions of a second and a time zone (…T10:22:33.123Z, …+05:30) are part of a timestamp.
    core=_FRACTION_TZ.sub('',v) if (':' in v or v.endswith('Z')) else v
    suffix=' (with fractions of a second / time zone)' if core!=v else ''
    for fmt in DATE_FORMATS:
        try:
            d=datetime.strptime(core,fmt)
            if 1900 <= d.year <= 2100:return fmt+suffix
        except ValueError:pass
    return None


def _type_of(v: Any) -> tuple[str,str | None]:
    if isinstance(v,bool):return 'boolean',None
    if isinstance(v,int):return 'integer',None
    if isinstance(v,float):return 'decimal',None
    s=str(v).strip()
    # Avoid interpreting identifiers such as 000001 or 00123 as numbers.
    if NUMERIC.fullmatch(s):
        plain=s.replace(',','')
        if len(plain.lstrip('+-'))>1 and plain.lstrip('+-').startswith('0') and '.' not in plain:
            return 'text',None
        return ('integer' if INTEGER.fullmatch(plain) else 'decimal'),None
    fmt=_date_parse(s)
    if fmt:return 'date',fmt
    return 'text',None


def _normal(v: Any) -> str:
    return str(v).strip().casefold()


def _sample_csv(path: Path, limit: int) -> tuple[list[str],list[list[Any]],dict]:
    if path.stat().st_size > MAX_CSV_BYTES:
        raise ConnectionErrorSafe('CSV exceeds the 2 GB per-table limit')
    with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
        first=f.read(12000)
        if not first.strip():raise ConnectionErrorSafe('Empty CSV; no columns to profile')
        try:delim=csv.Sniffer().sniff(first,delimiters=',;\t|').delimiter
        except csv.Error:delim=';' if first.splitlines()[0].count(';')>first.splitlines()[0].count(',') else ','
        f.seek(0)
        reader=csv.reader(f,delimiter=delim)
        try:header=[x.strip().lstrip('\ufeff') for x in _unwrapped_row(next(reader),delim)]
        except (StopIteration,csv.Error) as exc:raise ConnectionErrorSafe('Invalid CSV header') from exc
        if len(header)>MAX_COLUMNS or not header:raise ConnectionErrorSafe('Unsupported CSV column count')
        if len({h.casefold() for h in header})!=len(header) or any(not h for h in header):
            raise ConnectionErrorSafe('Duplicate/blank CSV headers require cleanup before profiling')
        rows=[]; malformed=0; replacement=0; read_count=0; has_more=False
        try:
            for raw in reader:
                if not raw or not any(str(x).strip() for x in raw):continue
                row=_unwrapped_row(raw,delim)
                if read_count>=limit:has_more=True;break
                read_count+=1
                if len(row)!=len(header):malformed+=1
                replacement+=sum('\ufffd' in str(x) for x in row)
                rows.append([row[i] if i<len(row) else None for i in range(len(header))])
        except csv.Error as exc:
            raise ConnectionErrorSafe('Malformed CSV quoting; profiling stopped safely') from exc
    return header,rows,{'sample_complete':not has_more,'malformed_rows':malformed,
                        'encoding_replacement_cells':replacement,'delimiter':'TAB' if delim=='\t' else delim}


def _sample_sqlite(db: Path, table: str, limit: int) -> tuple[list[str],list[list[Any]],dict]:
    with _readonly(db) as conn:
        if table not in set(list_sqlite_tables(db)):
            raise ConnectionErrorSafe('Table not found in read-only SQLite source')
        cols=conn.execute(f'PRAGMA table_info({_quote_identifier(table)})').fetchall()
        header=[r[1] for r in cols]
        if len(header)>MAX_COLUMNS or not header:raise ConnectionErrorSafe('Unsupported SQLite column count')
        cursor=conn.execute(f'SELECT * FROM {_quote_identifier(table)} LIMIT ?', (limit+1,))
        rows=[list(x) for x in cursor.fetchall()]
    has_more=len(rows)>limit
    return header,rows[:limit],{'sample_complete':not has_more,'malformed_rows':0,
        'encoding_replacement_cells':0,'declared_types':{r[1]:r[2] for r in cols},
        'declared_primary_keys':{r[1] for r in cols if r[5]}}


def _semantic_role(name: str, table: str, kind: str, observed: str, uniqueness: float,
                   missing: int, n: int, cardinality: int, declared_pk: bool) -> tuple[str,str,list[str]]:
    """Heuristic classification, confidence labels are *not* probabilities."""
    s=name.casefold();t=table.casefold(); evidence=[]
    primary_names={'record_id','transaction_id','txn_id','account_id','customer_id','complaint_id','product_id','case_id',
                   'assessment_id','alert_id','ticket_id','survey_id','period_id','cost_center_id'}
    from . import shapes
    is_id=bool(ID_HINT.search(s)) or s.endswith(('_ref','_key','_code')) or bool(shapes.id_ending(name)) or shapes.is_code(name)
    if s in NOT_KEY_NAMES:
        return 'IDENTIFIER','moderate',['Identifier-like business field; numeric-looking values are not measures']
    if declared_pk:
        return 'PRIMARY_KEY_CANDIDATE','high',['Source declares primary key; still a candidate until verified']
    # What the end of the name says comes before what the values look like: a version, a level or
    # a code is never a number to add up, and an email address is never a key.
    if shapes.is_email(name):
        return 'TEXT','moderate',['Email address: descriptive text, not a key (even when the name ends in "id")']
    if shapes.is_flag(name):
        return 'CLASSIFICATION','moderate',['Yes/No flag (name starts with is/has or ends in flag/indicator); not a measure']
    if shapes.is_version(name):
        return 'CLASSIFICATION','moderate',['Version number of a record: read it with its identifier, never add it up']
    if shapes.is_domain(name):
        return 'CLASSIFICATION','moderate',['Domain of a reference (which system or kind of record it points to)']
    if shapes.is_period(name):
        return 'DATE','high' if observed=='date' else 'moderate',['Reporting period: the table holds one snapshot per period']
    if shapes.is_level(name) and not shapes.id_ending(name):
        return 'CLASSIFICATION','moderate',['Hierarchy level or depth; not a measure']
    if _PLACE_STATE.match(s):
        return 'CLASSIFICATION','moderate',['A place (state or region), not a status']
    if is_id:
        evidence.append('Identifier-like column name')
        # Parent-table key or generic row surrogate; uniqueness alone is not proof.
        # Generic ownership inference; NO business-domain allowlist.  Accept
        # singular/plural naming (loan_accounts -> loan_id) only when the
        # observed key is fully unique and non-null.  Structural names alone
        # never establish a certified primary key.
        tokens=[x for x in re.split(r'[_\W]+', t) if x]
        def singular(word: str) -> str:
            if word.endswith('ies') and len(word)>4:return word[:-3]+'y'
            if word.endswith('sses'):return word[:-2]
            if word.endswith('s') and not word.endswith(('ss','us')):return word[:-1]
            return word
        family=singular(tokens[0]) if tokens else ''
        root=singular(s.split('_')[0])
        looks_own=(s in ('record_id','id') or
                   (family and root==family and s.endswith(('_id','_key','_uuid','_identifier'))))
        if shapes.is_code(name) and observed in ('integer','decimal'):
            evidence.append('A code: numbers here are labels, not quantities')
        if looks_own and uniqueness==1.0 and missing==0 and n>=2:
            evidence.append('Unique and non-null in observed rows')
            return 'PRIMARY_KEY_CANDIDATE','moderate',evidence
        if s in ('record_id','id') and uniqueness<1.0:
            evidence.append('Identifier duplicates observed; cannot be a primary key')
            return 'IDENTIFIER','moderate',evidence
        evidence.append('Relationship target not yet checked; Phase 6 required')
        return 'FOREIGN_KEY_CANDIDATE','moderate',evidence
    if re.search(r'(?:^|_)(?:reference|identifier|ref|uuid)(?:$|_)', s):
        return 'IDENTIFIER','moderate',['Identifier/reference naming signal; join target requires independent verification']
    if DATE_HINT.search(s):
        return 'DATE','high' if observed=='date' else 'moderate',['Date/time naming signal',f'Observed type: {observed}']
    if STATUS_HINT.search(s):
        return 'STATUS','high' if cardinality<=20 else 'moderate',['Status naming signal']
    if MEASURE_HINT.search(s) and observed in ('integer','decimal'):
        return 'MEASURE','high',['Numeric observations and measure-like name']
    if TEXT_HINT.search(s):
        return 'TEXT','moderate',['Free-text or descriptive naming signal']
    if CLASS_HINT.search(s) or (0<cardinality<=min(20,max(2,n//3)) and observed=='text'):
        return 'CLASSIFICATION','moderate',['Category-like name or low observed cardinality']
    if observed=='date':return 'DATE','moderate',['Date-like observed values']
    if observed in ('integer','decimal'):return 'MEASURE','low',['Numeric observed values; business meaning unverified']
    return 'TEXT','low',['Fallback: no strong semantic evidence']


def _column(name: str, values: list[Any], table: str, kind: str, declared_pk: bool=False) -> dict:
    n=len(values)
    present=[v for v in values if not _is_missing(v)]
    missing=n-len(present)
    types=Counter(); formats=Counter(); whitespace=0; norm=Counter(); raw=Counter();nums=[];formatted=0;placeholders=Counter()
    for v in present:
        s=str(v)
        if s!=s.strip():whitespace+=1
        t=s.strip()
        if t.casefold() in PLACEHOLDERS:placeholders[t]+=1
        elif _FORMATTED_NUMBER.fullmatch(t) and not NUMERIC.fullmatch(t):formatted+=1
        raw[s]+=1;norm[_normal(v)]+=1
        typ,fmt=_type_of(v);types[typ]+=1
        if fmt:formats[fmt]+=1
        if typ in ('integer','decimal'):
            try:nums.append(float(str(v).strip().replace(',','')))
            except (ValueError,OverflowError):pass
    if not types:observed='unknown'
    elif set(types)<= {'integer','decimal'}:observed='decimal' if 'decimal' in types else 'integer'
    elif len(types)==1:observed=next(iter(types))
    else:observed='mixed'
    distinct=len(raw);norm_distinct=len(norm)
    unique=round(distinct/len(present),4) if present else 0.0
    role,confidence,evidence=_semantic_role(name,table,kind,observed,unique,missing,n,distinct,declared_pk)
    findings=[]
    numbers_as_text=False
    if observed=='mixed' and role!='MEASURE' and set(types)<={'integer','decimal','text'}:
        # identifiers and names where some values happen to be numbers (12345 next to BK_RATES_12):
        # the column is text; that is not a parsing problem
        observed='text';numbers_as_text=True
    if missing:findings.append({'code':'missing_values','severity':'warning' if missing/max(n,1)>=.1 else 'info',
                                'count':missing,'detail':'Missing/null-like cells observed'})
    if numbers_as_text:
        findings.append({'code':'numbers_in_text','severity':'info','count':sum(types.get(t,0) for t in ('integer','decimal')),
                         'detail':'Some values are numbers and some are text; read as text'})
    elif len(types)>1 and not (set(types)<= {'integer','decimal'}):
        findings.append({'code':'mixed_types','severity':'warning','count':len(present),
                         'detail':'Mixed observed types; review parsing and business meaning'})
    if whitespace:findings.append({'code':'whitespace','severity':'info','count':whitespace,
                                    'detail':'Values with leading/trailing whitespace'})
    if formatted and formatted>=0.5*max(1,len(present)-sum(placeholders.values())):
        findings.append({'code':'formatted_numbers','severity':'warning','count':formatted,
                         'detail':'Numbers written with currency signs, separators, units or brackets ($1,234.50, 12 kg, (500)); read as text'})
    if placeholders:
        # only the fixed list of placeholder spellings is kept, never other values
        findings.append({'code':'placeholder_values','severity':'info','count':sum(placeholders.values()),
                         'detail':'Placeholders that mean "no value": %s' % ', '.join(sorted(placeholders)),'values':sorted(placeholders)})
    if norm_distinct<distinct:
        findings.append({'code':'normalized_collisions','severity':'info','count':distinct-norm_distinct,
                         'detail':'Distinct raw values collapse after trim/case normalization'})
    if len(formats)>1:
        findings.append({'code':'date_format_drift','severity':'warning','count':sum(formats.values()),
                         'detail':'Multiple observed date formats'})
    if formats and role=='DATE':
        fmt=formats.most_common(1)[0][0]
        if not (fmt.startswith('%Y-%m-%d')):
            findings.append({'code':'date_as_text','severity':'info','count':sum(formats.values()),
                             'detail':'Dates written as %s; add the data fix "Standardise dates" to compare and filter by date' % _fmt_label(fmt)})
    if (name.casefold() in ('record_id','id') or declared_pk) and (unique<1.0 or missing):
        findings.append({'code':'key_uniqueness_violation','severity':'warning',
                         'count':len(present)-distinct+missing,'detail':'Observed duplicate or null values in a key-like column'})
    numeric_stats=None
    if nums:
        nums.sort()
        numeric_stats={'min':nums[0],'max':nums[-1],'median':statistics.median(nums)}
        if len(nums)>=8 and role=='MEASURE':     # outliers only mean something for quantities
            q=statistics.quantiles(nums,n=4,method='inclusive');lo=q[0]-1.5*(q[2]-q[0]);hi=q[2]+1.5*(q[2]-q[0]);out=sum(v<lo or v>hi for v in nums)
            numeric_stats['iqr_outlier_candidates']=out
            if out:findings.append({'code':'numeric_outlier_candidate','severity':'info','count':out,
                'detail':'IQR outlier candidates; not automatically errors'})
    return {'name':name,'observed_type':observed,'type_counts':dict(types),
        'semantic_role_candidate':role,'role_confidence_label':confidence,'role_evidence':evidence,
        'observed_rows':n,'non_null_count':len(present),'missing_count':missing,
        'missing_pct':round(100*missing/max(n,1),2),'distinct_count':distinct,
        'distinct_pct':round(100*unique,2),'normalized_distinct_count':norm_distinct,
        'duplicate_value_count':len(present)-distinct,'leading_trailing_whitespace_count':whitespace,
        'date_formats':dict(formats),'numeric_stats':numeric_stats,'findings':findings,
        'declared_primary_key':declared_pk,'counts_scope':'sample'}


def _fmt_label(fmt):
    out=fmt.replace('%Y','YYYY').replace('%y','YY').replace('%m','MM').replace('%d','DD').replace('%b','Mon')
    return out.replace('%H','hh').replace('%M','mm').replace('%S','ss')


FINGERPRINT_ALGORITHM = 'sha256-content-v1'
_HASH_CACHE: dict = {}


def _content_sha256(path: Path, st) -> str:
    """Hash of the file's bytes; cached per (path, size, mtime) so unchanged files are read once.

    Approvals are bound to content, not to file times: copying a workspace, a git clone or a
    package install keeps every approval current, while any change to the bytes makes the
    affected approvals stale."""
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    hit = _HASH_CACHE.get(key)
    if hit: return hit
    with _hash_lock(key[0]):          # concurrent page requests wait for one read, not one each
        hit = _HASH_CACHE.get(key) or _disk_cache_get(key)
        if not hit:
            h = hashlib.sha256()
            with open(path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 22), b''):
                    h.update(chunk)
            hit = h.hexdigest()
            _disk_cache_put(key, hit)
        if len(_HASH_CACHE) > 20000: _HASH_CACHE.clear()
        _HASH_CACHE[key] = hit
        return hit


_LOCKS: dict = {}
_LOCKS_GUARD = __import__('threading').Lock()


def _hash_lock(name):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, __import__('threading').Lock())


def _disk_cache():
    """~/.binggio/fingerprints.sqlite3: file hashes by (path, size, modified time), so a restart
    does not re-read gigabytes. A changed file has a new size or time and is hashed again."""
    import sqlite3
    d = Path.home() / '.binggio'
    d.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(d / 'fingerprints.sqlite3', timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS hashes (path TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, PRIMARY KEY(path, size, mtime_ns))')
    return db


def _disk_cache_get(key):
    try:
        with _disk_cache() as db:
            r = db.execute('SELECT sha256 FROM hashes WHERE path=? AND size=? AND mtime_ns=?', key).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _disk_cache_put(key, digest):
    try:
        with _disk_cache() as db:
            db.execute('DELETE FROM hashes WHERE path=?', (key[0],))
            db.execute('INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)', (*key, digest))
    except Exception:
        pass


def _fingerprint(source: Path, kind: str, table: str) -> dict:
    if kind=='csv_folder':
        source=source/(table+'.csv')
        if not source.is_file() or not source.resolve().is_relative_to(source.parent.resolve()):
            raise ConnectionErrorSafe('CSV table missing or path escaped')
    st=source.stat()
    return {'size_bytes':st.st_size,'sha256':_content_sha256(source,st),'algorithm':FINGERPRINT_ALGORITHM,
            'path_kind':kind,'scope':'table' if kind=='csv_folder' else 'database'}


_TERMINAL = {'master','history','log','directory','lines','line','table','data','details','detail','list','register','records','record','tbl','dim','fact'}
_GENERIC_KEY = {'id','key','code','ref','reference','identifier','uuid','no','num','nbr','number','record','row','line','seq','sequence','serial','sr','sno','idx','index'}


def _words(name: str) -> list:
    s = re.sub(r'([a-z])([A-Z])', r'\1_\2', str(name))
    return [w for w in re.split(r'[^a-z0-9]+', s.lower()) if w]


def _sing(w: str) -> str:
    if w.endswith('ies') and len(w) > 4: return w[:-3] + 'y'
    if w.endswith('sses'): return w[:-2]
    if w.endswith('s') and not w.endswith(('ss', 'us')) and len(w) > 3: return w[:-1]
    return w


def _key_subject(column: str) -> list:
    words = [_sing(w) for w in _words(column)]
    while words and words[-1] in _GENERIC_KEY: words.pop()
    return words


def _refine_key_roles(table: str, cols: list) -> None:
    """Table-level choice of the table's own key (v0.2 heuristic; still only a suggestion).

    A table normally has one own key. Prefer the unique, non-null identifier whose name names the
    table's subject (case_id in collection_cases, collateral_id in loan_collateral); otherwise one
    whose name is made of the table's words (customer_id in customer_contacts); otherwise the
    first column when it is a unique, non-null identifier. Other unique identifiers are references
    to other tables, so they are suggested as foreign keys."""
    tw = [_sing(w) for w in _words(table)]
    core = [w for w in tw if w not in _TERMINAL] or tw
    subject = core[-1] if core else ''
    def idish(c):
        n = c['name'].casefold()
        if n in NOT_KEY_NAMES:
            return False
        return bool(ID_HINT.search(c['name'])) or c['semantic_role_candidate'] in ('PRIMARY_KEY_CANDIDATE', 'FOREIGN_KEY_CANDIDATE', 'IDENTIFIER')
    def full_unique(c):
        return c['missing_count'] == 0 and c['non_null_count'] >= 2 and c['distinct_count'] == c['non_null_count']
    if any(c.get('declared_primary_key') for c in cols): return
    cands = [c for c in cols if idish(c) and full_unique(c)]
    own = None
    for c in cands:
        ks = _key_subject(c['name'])
        if ks and (ks[-1] == subject or ''.join(ks) == subject): own = c; break
    if own is None:
        for c in cands:
            ks = _key_subject(c['name'])
            if ks and all(w in tw for w in ks): own = c; break
    if own is None and cols and cols[0] in cands and _key_subject(cols[0]['name']) != []:
        own = cols[0]
    if own is None and cols and cols[0] in cands and (cols[0]['name'].casefold() in ('id', 'record_id', 'code', 'key', 'no', 'number', 'ref', 'uuid') or _key_subject(cols[0]['name']) == []):
        own = cols[0]   # a lookup list keyed by a plain "code" or "id" in its first column
    for c in cols:
        if c is own:
            if c['semantic_role_candidate'] != 'PRIMARY_KEY_CANDIDATE':
                c['semantic_role_candidate'], c['role_confidence_label'] = 'PRIMARY_KEY_CANDIDATE', 'moderate'
                c['role_evidence'] = ['Identifier-like column name', 'Unique and non-null in observed rows',
                                      'Chosen as the table\'s own key (name or position)']
        elif c['semantic_role_candidate'] == 'PRIMARY_KEY_CANDIDATE' and own is not None:
            c['semantic_role_candidate'], c['role_confidence_label'] = 'FOREIGN_KEY_CANDIDATE', 'moderate'
            c['role_evidence'] = ['Identifier-like column name', 'Unique in observed rows',
                                  'The table\'s own key appears to be %s, so this is suggested as a reference to another table' % own['name']]


def _manifest_primary_key(folder: Path, table: str) -> set:
    """A single-column primary key declared in the source database, recorded by the connector."""
    import json
    try:
        info=json.loads((folder/'_binggio_source.json').read_text(encoding='utf-8'))['tables'].get(table) or {}
    except (OSError,ValueError,KeyError):
        return set()
    pk=info.get('primary_key') or []
    return {pk[0]} if len(pk)==1 else set()


def profile_table(project: Path, connection: dict, table: str, *, limit: int=SAMPLE_LIMIT) -> dict:
    if not (1<=int(limit)<=SAMPLE_LIMIT):raise ConnectionErrorSafe('Sample limit outside 1..2500')
    kind=connection['kind'];source=resolve_source(project,connection['source_path'],kind)
    from binggio import prepared
    if kind=='csv_folder':
        if table not in set(p.stem for p in source.glob('*.csv') if p.is_file()):
            raise ConnectionErrorSafe('Table not found in registered CSV folder')
        csv_path=source/(table+'.csv')
        if not csv_path.resolve().is_relative_to(source):
            raise ConnectionErrorSafe('CSV source escaped registered folder')
        # Data fixes (if any) are applied: the profile describes the data as Binggio reads it.
        header,rows,meta=prepared.read(project,connection,table,limit)
        meta['declared_primary_keys']=_manifest_primary_key(source,table)
    elif kind=='sqlite':header,rows,meta=prepared.read(project,connection,table,limit)
    else:raise ConnectionErrorSafe('Unsupported connector')
    fp=_with_fixes(project,connection,table,_fingerprint(source,kind,table))
    cols=[_column(name,[row[i] for row in rows],table,kind,
                  name in meta.get('declared_primary_keys',set())) for i,name in enumerate(header)]
    total_rows=len(rows) if meta['sample_complete'] else None
    counts_scope='all_rows' if meta['sample_complete'] else 'sample'
    full={}
    if not meta['sample_complete']:
        # Distinct values and empty cells are counted on every row of the prepared copy, so a key
        # is not judged from the first rows of a large table.
        try:
            full,total_rows=_full_counts(project,connection,table,header,cols)
            if full:counts_scope='all_rows' if len(full)==len(header) else 'partial'
        except Exception:
            full={}
    for c in cols:
        f=full.get(c['name'])
        if f:_apply_full(c,f,total_rows)
    if meta['sample_complete']:
        for c in cols:c['counts_scope']='all_rows'
    _refine_key_roles(table,cols)
    masks={name:{j for j,row in enumerate(rows) if _is_missing(row[i])} for i,name in enumerate(header)}
    from . import shapes
    groups=shapes.groups(cols,masks,len(rows))
    _refine_shapes(project,connection,table,cols,groups,total_rows)
    # Hash row content only in memory for duplicate counts; never persist rows.
    hashes=Counter(hashlib.sha256(repr(tuple(str(v) if v is not None else None for v in row)).encode('utf-8')).digest() for row in rows)
    duplicates=sum(count-1 for count in hashes.values() if count>1)
    issues=[]
    if duplicates:issues.append({'code':'duplicate_rows','severity':'warning','count':duplicates,
                                 'detail':'Exact duplicate rows in the observed sample'})
    if meta['malformed_rows']:issues.append({'code':'malformed_row_width','severity':'warning',
        'count':meta['malformed_rows'],'detail':'CSV rows with different width than header'})
    if meta['encoding_replacement_cells']:issues.append({'code':'encoding_replacement','severity':'warning',
        'count':meta['encoding_replacement_cells'],'detail':'Invalid encoding replacement characters'})
    if not meta['sample_complete']:issues.append({'code':'sample_limited','severity':'info','count':len(rows),
        'detail':'Only the first bounded sample was examined; metrics are not full-table estimates'})
    if not rows:issues.append({'code':'empty_table','severity':'warning','count':0,'detail':'No rows observed'})
    warning_count=sum(1 for col in cols for issue in col['findings'] if issue['severity']=='warning')+sum(1 for i in issues if i['severity']=='warning')
    return {'profile_version':VERSION,'connection_id':connection['id'],'schema':'files' if kind=='csv_folder' else 'main',
        'table':table,'source_kind':kind,'sample_rows':len(rows),'sample_limit':limit,
        'table_rows':total_rows,'counts_scope':counts_scope,'groups':groups,
        'sample_complete':meta['sample_complete'],'sample_strategy':'first_n_rows',
        'fingerprint':fp,'profiled_at':_now(),'columns':cols,'column_count':len(cols),
        'duplicate_rows_observed':duplicates,'table_findings':issues,
        'warning_count':warning_count,'approval_status':'unverified',
        'disclaimer':'Sample-based technical evidence and heuristic semantic role candidates; no relationship, business definition or key is approved.'}


FULL_COUNT_SECONDS = 240     # time allowed for whole-table counts of one table; the rest stay sample counts


def _needs_exact_distinct(c):
    """Columns whose whole-table distinct count decides something: keys, references, codes, and
    anything unique in the rows read (a key whatever its name). Other columns keep the distinct
    count of the rows read; their empty count and "one value only" are still whole-table."""
    from . import shapes
    n=c['name']
    if shapes.is_version(n) or shapes.is_domain(n) or shapes.is_email(n):return False
    if c['semantic_role_candidate'] in ('PRIMARY_KEY_CANDIDATE','FOREIGN_KEY_CANDIDATE','IDENTIFIER'):return True
    if shapes.id_ending(n) or shapes.is_code(n):return True
    return c['non_null_count']>=20 and c['distinct_count']>=0.9*c['non_null_count'] and c['observed_type'] in ('integer','text')


SPLIT_ROWS = 300_000     # tables with more rows have their columns counted by several threads at once


def _full_counts(project, connection, table, header, cols=None):
    """{column: (non_missing, distinct or None, one_value)} on every row of the prepared copy, and the
    row count. One pass counts empty cells and checks for a single value in every column; a second
    pass counts distinct values of the columns that need it (see _needs_exact_distinct). On a large
    table the columns are shared out between threads, each reading the table with its own connection."""
    import time
    from binggio import prepared, parallel
    from binggio.prepared import missing_sql as miss
    path=prepared.ensure(project,connection,table)
    q=_quote_identifier;out={};started=time.time()
    by={c['name']:c for c in cols or []}

    def connect():
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,check_same_thread=False)
        db.execute('PRAGMA temp_store=MEMORY');db.execute('PRAGMA cache_size=-131072')
        return db

    with closing(connect()) as db:
        total=db.execute('SELECT COUNT(*) FROM t').fetchone()[0]
        present=[r[1] for r in db.execute('PRAGMA table_info(t)')]
    names=[h for h in header if h in present]
    threads=min(8,parallel.workers()) if total>=SPLIT_ROWS else 1

    def empties(part):
        # "one value only" can only hold where the rows read show at most one value
        check=[c for c in part if not cols or (c in by and by[c]['distinct_count']<=1)]
        sql=', '.join(['SUM(CASE WHEN %s THEN 0 ELSE 1 END)' % miss(q(c)) for c in part]+
                      ['MIN(CASE WHEN %s THEN NULL ELSE %s END), MAX(CASE WHEN %s THEN NULL ELSE %s END)' % (miss(q(c)),q(c),miss(q(c)),q(c)) for c in check])
        with closing(connect()) as db:
            r=db.execute('SELECT '+sql+' FROM t').fetchone()
        res={c:[r[j] or 0,None,False] for j,c in enumerate(part)}
        for k,c in enumerate(check):
            lo,hi=r[len(part)+2*k],r[len(part)+2*k+1]
            res[c][2]=res[c][0]>0 and lo==hi
        return res

    def distincts(part):
        if time.time()-started>FULL_COUNT_SECONDS:return {}
        with closing(connect()) as db:
            r=db.execute('SELECT '+', '.join('COUNT(DISTINCT %s)' % q(c) for c in part)+' FROM t').fetchone()
        return {c:r[j] or 0 for j,c in enumerate(part)}

    def chunks(xs,n):
        size=max(1,-(-len(xs)//n))
        return [xs[i:i+size] for i in range(0,len(xs),size)]

    for _,res,err in parallel.run(chunks(names,threads) if names else [],empties,n_workers=threads):
        if err:raise err
        out.update(res)
    exact=[c for c in names if c in by and _needs_exact_distinct(by[c]) and out[c][0]>0 and not out[c][2]] if cols else names
    # each COUNT(DISTINCT) keeps its own sorted list, so a few columns per query
    parts=[exact[i:i+4] for i in range(0,len(exact),4)] if threads>1 else [exact[i:i+16] for i in range(0,len(exact),16)]
    for _,res,err in parallel.run(parts,distincts,n_workers=threads):
        if err:raise err
        for c,v in res.items():out[c][1]=v
    for c,v in out.items():
        if v[2]:v[1]=1
        elif v[0]==0:v[1]=0
    return {c:tuple(v) for c,v in out.items()},total


def _apply_full(c,f,total):
    present,distinct,one=f
    missing=total-present
    c.update({'observed_rows':total,'non_null_count':present,'missing_count':missing,'missing_pct':round(100*missing/max(total,1),2),
              'counts_scope':'all_rows'})
    if distinct is not None:
        c.update({'distinct_count':distinct,'distinct_pct':round(100*distinct/present,2) if present else 0.0,
                  'duplicate_value_count':present-distinct,'distinct_scope':'all_rows'})
    else:
        c['distinct_scope']='sample'          # empty count is whole-table; distinct is of the rows read
    for x in c['findings']:
        if x['code']=='missing_values':
            x['count']=missing;x['severity']='warning' if missing/max(total,1)>=.1 else 'info'
    if not missing:c['findings']=[x for x in c['findings'] if x['code']!='missing_values']


def _refine_shapes(project,connection,table,cols,groups,total_rows):
    """Table-level suggestions from the groups: codes kept with their names are labels, the
    numbered levels of a hierarchy are not measures, the table's own identifier is named even when
    it repeats (and why), and columns with no information are set aside."""
    from . import shapes
    by={c['name']:c for c in cols}
    for g in groups:
        for n in g['columns']:
            by[n].setdefault('groups',[]).append(g['kind']+':'+g['name'])
        if g['kind']=='code_name':
            code=by[g['columns'][0]]
            if code['semantic_role_candidate'] not in ('PRIMARY_KEY_CANDIDATE',):
                code['semantic_role_candidate'],code['role_confidence_label']='CLASSIFICATION','moderate'
                code['role_evidence']=['A code kept with its name (%s): a label to group by, not a quantity' % g['columns'][1]]
        if g['kind']=='hierarchy':
            for n in g['columns']:
                c=by[n]
                if c['semantic_role_candidate']=='MEASURE':
                    c['semantic_role_candidate'],c['role_confidence_label']='CLASSIFICATION','moderate'
                    c['role_evidence']=['Part of a hierarchy path (%s); not a quantity' % g['name']]
    # short code lists (city, currency, region codes) are labels to group by
    for c in cols:
        if shapes.is_code(c['name']) and c['semantic_role_candidate']=='FOREIGN_KEY_CANDIDATE' and 0<c['distinct_count']<=30:
            c['semantic_role_candidate'],c['role_confidence_label']='CLASSIFICATION','moderate'
            c['role_evidence']=['A short list of codes (%d different values): a label to group by' % c['distinct_count']]
    # no information: empty on every row, or one value only
    for c in cols:
        if c['non_null_count']==0 and c['observed_rows']>0:
            c['no_information']='empty'
        elif c['distinct_count']==1 and c['observed_rows']>1 and not shapes.is_domain(c['name']):
            c['no_information']='one_value'
    # the table's own identifier, when its name says so but it repeats
    if any(c['semantic_role_candidate']=='PRIMARY_KEY_CANDIDATE' for c in cols):return
    own=next((c for c in cols if shapes.id_ending(c['name']) and shapes.subject(c['name'])=='' and c['missing_count']==0),None)
    if own is None:
        ids=[c for c in cols if shapes.id_ending(c['name']) and c['missing_count']==0 and c['semantic_role_candidate'] in
             ('FOREIGN_KEY_CANDIDATE','IDENTIFIER') and not c.get('no_information')]
        own=max(ids,key=lambda c:(c['distinct_count'],-cols.index(c)),default=None)
    if own is None or own['distinct_count']<2:return
    period=next((c['name'] for c in cols if shapes.is_period(c['name'])),None)
    version=next((g['version_column'] for g in groups if g['kind']=='reference' and g.get('id_column')==own['name']),None)
    why=[]
    from binggio import prepared
    for extra in [x for x in (period,version) if x]+([[period,version]] if period and version else []):
        parts=[own['name']]+(extra if isinstance(extra,list) else [extra])
        try:unique=prepared.is_unique(project,connection,table,parts)
        except Exception:break
        if unique:
            why=parts;break
    if own['distinct_count']==own['non_null_count']:
        if own['semantic_role_candidate']!='PRIMARY_KEY_CANDIDATE' and own['counts_scope']=='all_rows':
            own['semantic_role_candidate'],own['role_confidence_label']='PRIMARY_KEY_CANDIDATE','moderate'
            own['role_evidence']=['Identifier-like column name','Unique and non-null on every row',
                                  'Chosen as the table\'s own key (the identifier with the most different values)']
        return
    own['semantic_role_candidate'],own['role_confidence_label']='IDENTIFIER','moderate'
    ev=['The identifier of this table\'s rows (by its name and position), but it repeats: %s of %s rows' % (
        '{:,}'.format(own['non_null_count']-own['distinct_count']),'{:,}'.format(own['non_null_count']))]
    if why:
        ev.append('%s together are unique on every row: one row per %s' % (' + '.join(why),' per '.join(why)))
    ev.append('A master table (Review > Masters) gives it one row per %s to link to' % own['name'])
    own['role_evidence']=ev
    own['own_identifier']=True
    own['unique_with']=why


def _with_fixes(project, connection, table, fp):
    # A table's data fixes are part of what was profiled and approved: adding or removing one
    # changes the fingerprint, so approvals made on the other version become stale.
    from binggio import prepared
    sig=prepared.signature(project,connection['id'],table)
    if sig:fp={**fp,'fixes':sig}
    return fp


def current_fingerprint(project: Path, connection: dict, table: str) -> dict:
    source=resolve_source(project,connection['source_path'],connection['kind'])
    return _with_fixes(project,connection,table,_fingerprint(source,connection['kind'],table))
