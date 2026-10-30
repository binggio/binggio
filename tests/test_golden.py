# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""The demo bank's ten verified questions, run as canonical plans (no model calls).
Expected answers were computed independently from the raw files when the set was made."""
import json
from pathlib import Path
from conftest import full_plan

GOLD = json.loads((Path(__file__).parent / 'golden' / 'demo_plans.json').read_text())
K = lambda v: str(v).strip().upper()


def column(ans, name):
    cols = [str(c).split('::')[-1] for c in ans['columns']]
    i = cols.index(name)
    return [r[i] for r in ans['rows']]


def run(ws, case):
    ws.new_conversation()
    out = []
    for i, plan in enumerate(case['plans']):
        q = case['question'] if i == 0 else 'Which of these are needed? (follow-up %d)' % i
        ans = ws.ask(q, plan=full_plan(plan))
        assert ans.get('status') != 'refused', ans
        out.append(ans)
    return out


def test_ten_golden_questions(demo):
    for case in GOLD:
        ans = run(demo, case)
        exp = case['expected']
        last = ans[-1]
        if case['id'] in ('Q4', 'Q7'):
            got = {r[0]: round(float(r[1]), 2) for r in last['rows']}
            assert got == {k: round(float(v), 2) for k, v in exp.items()}, case['id']
        elif case['id'] == 'Q9':
            assert column(last, 'loan_id') == exp, case['id']
        elif case['id'] == 'Q10':
            assert len(ans[0]['rows']) == exp[0]
            assert sorted(column(ans[1], 'loan_id')) == exp[1]
            assert sorted(K(x) for x in column(ans[2], 'CUSTOMER_ID')) == exp[2]
        else:
            key = 'loan_id' if 'loan_id' in [str(c).split('::')[-1] for c in last['columns']] else 'CUSTOMER_ID'
            assert sorted(K(x) for x in column(last, key)) == exp, case['id']


def test_unapproved_reference_without_prior_result_is_refused(demo):
    demo.new_conversation()
    plan = full_plan({**GOLD[9]['plans'][1]})
    ans = demo.ask('Which of these loans are delinquent?', plan=plan)
    assert ans['status'] == 'refused' and ans['code'] == 'COHORT_NOT_AVAILABLE'
