# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""binggio mcp: an AI agent's view of Binggio over the Model Context Protocol (stdio)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GOLD = json.loads((Path(__file__).parent / 'golden' / 'demo_plans.json').read_text())


class Client:
    def __init__(self, ws):
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        for k in ('BINGGIO_LLM_API_KEY', 'OPENAI_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY'): env.pop(k, None)
        self.p = subprocess.Popen([sys.executable, '-m', 'binggio', '-w', str(ws), 'mcp'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
        self.n = 0

    def rpc(self, method, params=None, notify=False):
        msg = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify: self.n += 1; msg['id'] = self.n
        self.p.stdin.write(json.dumps(msg) + '\n'); self.p.stdin.flush()
        return None if notify else json.loads(self.p.stdout.readline())

    def tool(self, name, **args):
        return self.rpc('tools/call', {'name': name, 'arguments': args})['result']

    def close(self):
        self.p.stdin.close(); self.p.wait(10)


@pytest.fixture(scope='module')
def agent(demo):
    c = Client(demo.root)
    r = c.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test-agent', 'version': '1'}})
    assert r['result']['serverInfo']['name'] == 'binggio' and r['result']['protocolVersion'] == '2025-06-18'
    c.rpc('notifications/initialized', notify=True)
    yield c
    c.close()


def test_tools_offered_and_none_can_approve(agent):
    names = {t['name'] for t in agent.rpc('tools/list')['result']['tools']}
    assert names == {'describe_approved', 'run_plan', 'ask', 'status', 'propose_term'}


def test_describe_lists_only_approved_vocabulary(agent):
    d = agent.tool('describe_approved', search='delinquent')['structuredContent']
    assert any(t['name'] == 'Delinquent loan' for t in d['business_terms'])
    full = agent.tool('describe_approved')['structuredContent']
    assert len(full['tables']) == 63 and full['as_of'] == '2026-10-19' and 'plan_guide' in full


def test_run_plan_gives_the_verified_answer_with_provenance(agent):
    g = GOLD[0]
    a = agent.tool('run_plan', plan=g['plans'][0], question=g['question'])['structuredContent']
    assert a['status'] == 'answered' and sorted(r[0] for r in a['rows']) == g['expected']
    assert a['sources'] and a['business_terms'] and a['as_of'] and a['conversation']


def test_follow_up_in_the_same_conversation(agent):
    g = next(x for x in GOLD if x['id'] == 'Q10')
    conv = None
    for i, plan in enumerate(g['plans']):
        q = 'Top 10 loans by balance' if i == 0 else 'Which of these ... ?'
        a = agent.tool('run_plan', plan=plan, question=q, **({'conversation': conv} if conv else {}))['structuredContent']
        conv = a['conversation']
    assert sorted(str(r[0]) for r in a['rows']) == g['expected'][2]


def test_invented_or_unapproved_ids_are_refused(agent):
    plan = dict(GOLD[0]['plans'][0]); plan['select'] = ['csv_clean::customer_master::NOT_A_COLUMN']
    a = agent.tool('run_plan', plan=plan, question='x')['structuredContent']
    assert a['status'] == 'refused' and 'NOT_A_COLUMN' in a['message'] and 'guidance' in a


def test_ask_without_key_and_bad_arguments_are_explained(agent):
    assert agent.tool('ask', question='how many loans?')['structuredContent']['code'] == 'MODEL_NOT_CONFIGURED'
    r = agent.tool('run_plan', question='missing plan')
    assert r['isError'] and 'Wrong arguments' in r['content'][0]['text']
    assert agent.rpc('nope/nothing')['error']['code'] == -32601


def test_proposed_term_is_a_draft_for_a_person(agent, demo):
    from binggio.registry import terms
    r = agent.tool('propose_term', table='conn_31ca3d841ed240::loan_accounts', column='loan_product', value='HOME',
                   words=['home loan'], reason='the user said HOME means home loan')['structuredContent']
    item = next(t for t in terms.list_terms(demo.root) if t['id'] == r['id'])
    assert item['status'] == 'draft' and item['actor'].startswith('Agent via MCP (test-agent')
