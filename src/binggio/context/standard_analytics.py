# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Statistical operations derived from approved numerical MEASURE roles (not business KPIs).

No registration writes. Synthetic, deterministic ids are scoped to the role's approved
field id. A stale/revoked field disappears automatically from governed capabilities.
"""
from __future__ import annotations
import hashlib

STANDARD_OPERATIONS = ('SUM','AVG','MEDIAN','MIN','MAX','COUNT','COUNT_DISTINCT',
    'P10','P25','P50','P75','P90','P95','P99','STDDEV_POP','VAR_POP')
NUMERIC_TYPES = frozenset(('integer','decimal'))

def generate(fields):
    virtual={}
    for fid, field in sorted(fields.items()):
        if field.get('role')!='MEASURE' or field.get('observed_type') not in NUMERIC_TYPES:
            continue
        tid=field['table_id']
        table=tid.split('::',1)[-1]
        for op in STANDARD_OPERATIONS:
            digest=hashlib.sha256((fid+'\0'+op).encode()).hexdigest()[:24]
            mid='technical:'+digest
            description={'AVG':'Mean','MEDIAN':'Median','COUNT_DISTINCT':'Distinct count',
                'STDDEV_POP':'Population standard deviation','VAR_POP':'Population variance'}.get(op,op)
            virtual[mid]={'id':mid,'name':description+' of '+table+'.'+field['name'],
                'domain':'Standard analysis · not a business KPI',
                'aggregation':op,'field':fid,'table_id':tid,'revision':0,
                'owner':'Binggio standard library','authority':'approved_numeric_measure',
                'technical_only':True,'business_approved':False,
                'null_policy':'ignore null values except COUNT, which counts non-null values',
                'calculation':'continuous linear percentile (inclusive)' if op.startswith('P') or op=='MEDIAN' else op}
    return virtual
