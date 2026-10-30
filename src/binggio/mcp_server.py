# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio as an MCP server: governed answers for AI agents.

    binggio -w C:\\banks\\my-bank mcp

Runs on this computer and talks to one MCP client (Claude Desktop, Claude Code, Cursor, VS Code or
any agent framework) over standard input and output, using the Model Context Protocol.
Standard library only.

The division of labour:
  * the agent interprets the user's question and writes a plan from Binggio's approved vocabulary
    (describe_approved);
  * Binggio validates the plan and answers from approved meanings only (run_plan), or refuses with
    the reason. Nothing unapproved, stale or invented is executed; no SQL is accepted.
  * agents may propose a meaning (propose_term); only people approve, in the Binggio app.
"""
from __future__ import annotations
import json
import platform
import sys
import traceback

from binggio import __version__

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
DEFAULTS = {'select': [], 'filters': [], 'where': None, 'exists': [], 'aggregate': [], 'group_by': [], 'sort': [],
            'limit': 200, 'cohort': None, 'latest': None, 'terms': []}

INSTRUCTIONS = """Binggio answers questions about the user's data using only meanings a person has approved.
Workflow: (1) call describe_approved (use `search` to narrow it) to see the approved tables, columns,
links, business terms and concepts; (2) write a plan using only those IDs and call run_plan;
(3) report the answer with its sources, business terms and as-of date.
If run_plan refuses, tell the user why and what is missing. Never guess a meaning, never substitute
a similar column, never invent IDs. If a word could mean two things, ask the user which.
If the user explains a meaning that is not approved, you may call propose_term; a person must
approve it in the Binggio app before it can be used."""

PLAN_GUIDE = """Plan format (JSON object; omitted keys get defaults):
  entity: table ID the answer is about (rows of this table), e.g. "conn::loans"
  select: [field IDs] to show (any approved table reachable through approved links)
  filters: [{"field": ID, "op": one of eq ne gt gte lt lte in not_in like between is_null not_null, "value": ...}]
  where: optional boolean group {"mode": "all"|"any"|"not", "items": [filters or groups]}
  exists: [{"table": table ID, "filters": [filters on that table], "negate": false}]  related rows exist (or not, negate=true)
  terms: [{"term": term ID, "negate": false}]  apply an approved business term ("delinquent", "default")
  aggregate: [{"fn": count|count_distinct|sum|avg|min|max|median, "field": ID or null for count, "alias": "name"}]
  group_by: [field IDs]; sort: [{"field": ID or aggregate alias, "direction": "asc"|"desc"}]; limit: 1..200
  latest: {"table", "partition", "date", "tie"} to use only the latest row per entity, or null
  cohort: "active" only for follow-ups that refer to the previous answer ("these", "of those"); pass the
          same conversation ID and say "these" in the question.
Use field IDs exactly as listed (connection::table::column). Sums and averages need columns approved as MEASURE."""


# ------------------------------------------------------------------ tools

def _tools():
    obj = lambda props, req=(): {'type': 'object', 'properties': props, 'required': list(req), 'additionalProperties': False}
    return [
        {'name': 'describe_approved', 'title': 'Approved vocabulary',
         'description': 'The tables, columns (with approved roles), links, business terms and concepts Binggio may use, '
                        'plus the plan format. Call this before run_plan. Use `search` (words) to narrow a large workspace.',
         'inputSchema': obj({'search': {'type': 'string', 'description': 'Optional words to match table, column, term or concept names'},
                             'include_plan_guide': {'type': 'boolean', 'default': True}}),
         'annotations': {'readOnlyHint': True}},
        {'name': 'run_plan', 'title': 'Governed answer',
         'description': 'Validate a plan against the approved meanings and run it read-only. Returns rows with sources, '
                        'business terms and as-of date, or a refusal with the reason. No SQL is accepted.',
         'inputSchema': obj({'plan': {'type': 'object', 'description': 'Plan built from describe_approved IDs (see its plan guide)'},
                             'question': {'type': 'string', 'description': "The user's question in their words (recorded with the answer; "
                                                                           'say "these" for a follow-up on the previous answer)'},
                             'conversation': {'type': 'string', 'description': 'Conversation ID from an earlier answer, for follow-ups'}},
                            ('plan', 'question')),
         'annotations': {'readOnlyHint': True}},
        {'name': 'ask', 'title': 'Ask in plain words',
         'description': "Let Binggio interpret the question itself (needs a model key in Binggio's environment). "
                        'Prefer run_plan when you can write the plan.',
         'inputSchema': obj({'question': {'type': 'string'}, 'conversation': {'type': 'string'}}, ('question',)),
         'annotations': {'readOnlyHint': True}},
        {'name': 'status', 'title': 'Workspace status',
         'description': 'Sources, how much is approved, what is waiting for review, and approvals that are stale because data changed.',
         'inputSchema': obj({}), 'annotations': {'readOnlyHint': True}},
        {'name': 'propose_term', 'title': 'Propose a business term',
         'description': 'Suggest what a coded value means (e.g. kept = N means "broken promise"). Creates a draft that a person '
                        'must approve in the Binggio app before it can be used. Does not approve anything.',
         'inputSchema': obj({'table': {'type': 'string', 'description': 'Table ID (connection::table)'},
                             'column': {'type': 'string', 'description': 'Column name in that table'},
                             'value': {'description': 'The coded value, e.g. "N"'},
                             'words': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Business words, first is the name'},
                             'reason': {'type': 'string', 'description': 'Why you believe this (what the user said)'}},
                            ('table', 'column', 'value', 'words', 'reason'))},
    ]


class Server:
    def __init__(self, ws, out=None):
        self.ws, self.out = ws, out or sys.stdout
        self.client = 'MCP client'

    # --------------------------------------------------------------- tool bodies
    def describe_approved(self, search=None, include_plan_guide=True):
        from binggio.context.context import approved_context
        from binggio.catalog import catalog
        ctx = approved_context(self.ws.root)
        names = {c['id']: c['name'] for c in catalog.list_connections(self.ws.root)}
        words = [w for w in (search or '').lower().replace(',', ' ').split() if w]
        hit = lambda *texts: not words or any(w in ' '.join(str(t) for t in texts).lower() for w in words)
        tables = []
        for tid, t in ctx['tables'].items():
            cols = [{'id': f, 'role': ctx['fields'][f]['role']} for f in t['columns'] if f in ctx['fields']]
            if hit(tid, *[c['id'] for c in cols]):
                tables.append({'id': tid, 'source': names.get(t['connection_id'], t['connection_id']), 'columns': cols})
        shown = {t['id'] for t in tables}
        links = [{'from': j['left'], 'to': j['right'], 'cardinality': j['cardinality']} for j in ctx['joins'].values()
                 if j['left_table'] in shown or j['right_table'] in shown]
        terms = [{'id': k, 'name': v['name'], 'aliases': v.get('aliases', []), 'meaning': v.get('description'), 'table': v.get('table_id')}
                 for k, v in (ctx.get('governed_terms') or {}).items() if hit(v['name'], v.get('aliases'), v.get('description'), v.get('table_id'))]
        concepts = [{'name': v['name'], 'aliases': v.get('aliases', []), 'main_table': v.get('primary_table'), 'key': v.get('key_field')}
                    for v in (ctx.get('concepts') or {}).values() if hit(v['name'], v.get('aliases'), v.get('primary_table'))]
        out = {'workspace': self.ws.root.name, 'as_of': (ctx.get('as_of') or {}).get('date'),
               'tables': tables, 'links': links, 'business_terms': terms, 'concepts': concepts,
               'note': 'Only these IDs may be used. Anything else is refused.' + (' Filtered by: ' + search if search else '')}
        if include_plan_guide: out['plan_guide'] = PLAN_GUIDE
        return out

    def run_plan(self, plan, question, conversation=None):
        if not isinstance(plan, dict): raise ValueError('plan must be a JSON object')
        full = {**DEFAULTS, **plan}
        for a in full.get('aggregate') or []:
            if isinstance(a, dict): a.setdefault('alias', a.get('fn', 'value'))
        return self._answer(question, full, conversation)

    def ask(self, question, conversation=None):
        return self._answer(question, None, conversation)

    def _answer(self, question, plan, conversation):
        from binggio.engine.sessions import SessionMissing
        from binggio.ui.server import _answer_public
        conv = conversation or self.ws.new_conversation()
        try:
            a = self.ws.ask(question, plan=plan, conversation=conv)
        except SessionMissing:
            conv = self.ws.new_conversation(); a = self.ws.ask(question, plan=plan, conversation=conv)
        pub = _answer_public(self.ws.root, a)
        pub['conversation'] = conv
        if pub.get('status') == 'refused':
            pub['guidance'] = 'Not answered. Tell the user the reason; do not guess or substitute another column.'
        else:
            pub.pop('fields', None); pub.pop('plan', None)
        return pub

    def status(self):
        from binggio.registry import store
        st = self.ws.status()
        stale = [r for r in store._rows(self.ws.root) if r['effective_status'] == 'stale'][:20]
        return {'sources': [{'id': c['id'], 'name': c['name'], 'tables': c.get('table_count')} for c in self.ws.connections()],
                'answerable': st['answerable'], 'registry': st['registry'],
                'waiting_for_review': store.list_roles(self.ws.root, status='pending', limit=1)['total'] +
                                      store.list_relationships(self.ws.root, status='pending', limit=1)['total'],
                'stale_examples': [r['subject_key'] for r in stale]}

    def propose_term(self, table, column, value, words, reason):
        from binggio.registry import terms
        cid, _, tname = str(table).partition('::')
        words = [w.strip() for w in words if str(w).strip()]
        if not words: raise ValueError('Give at least one word for the value')
        item = {'name': words[0][:100].capitalize(), 'description': '%s.%s = %s (proposed by an agent: %s)' % (tname, column, value, reason),
                'domain': 'Unassigned', 'owner': 'Waiting for review', 'aliases': words[1:],
                'table': {'connection_id': cid, 'table': tname}, 'conditions': [{'column': column, 'op': 'eq', 'value': value}]}
        r = terms.create(self.ws.root, item, actor='Agent via MCP (%s)' % self.client[:50], note='Proposed through MCP: ' + reason[:400])
        return {'id': r['id'], 'status': 'draft',
                'next': 'A person must approve "%s" in the Binggio app (Review > Business terms) before it can be used.' % item['name']}

    # --------------------------------------------------------------- protocol
    def handle(self, msg):
        method, mid, params = msg.get('method'), msg.get('id'), msg.get('params') or {}
        if method == 'initialize':
            info = params.get('clientInfo') or {}
            self.client = ('%s %s' % (info.get('name', ''), info.get('version', ''))).strip() or 'MCP client'
            want = params.get('protocolVersion')
            return {'protocolVersion': want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'binggio', 'title': 'Binggio', 'version': __version__},
                    'instructions': INSTRUCTIONS}
        if method == 'ping': return {}
        if method == 'tools/list': return {'tools': _tools()}
        if method == 'tools/call':
            name, args = params.get('name'), params.get('arguments') or {}
            fn = {'describe_approved': self.describe_approved, 'run_plan': self.run_plan, 'ask': self.ask,
                  'status': self.status, 'propose_term': self.propose_term}.get(name)
            if fn is None: raise _RpcError(-32602, 'Unknown tool: %s' % name)
            try:
                result = fn(**args)
                return {'content': [{'type': 'text', 'text': json.dumps(result, default=str, ensure_ascii=False)}],
                        'structuredContent': result, 'isError': False}
            except TypeError as e:
                return _tool_error('Wrong arguments for %s: %s' % (name, e))
            except Exception as e:  # reported to the agent, which can correct its call
                expected = ('Review', 'Workspace', 'Connector', 'Value', 'Key', 'Session')
                if not any(k in type(e).__name__ for k in expected):
                    print(traceback.format_exc(), file=sys.stderr)
                return _tool_error('%s: %s' % (type(e).__name__, e))
        if method in ('resources/list', 'prompts/list'):
            return {'resources': []} if method == 'resources/list' else {'prompts': []}
        raise _RpcError(-32601, 'Method not found: %s' % method)

    def serve(self, inp=None):
        inp = inp or sys.stdin
        for line in inp:
            line = line.strip()
            if not line: continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}); continue
            batch = msg if isinstance(msg, list) else [msg]
            for m in batch:
                if 'id' not in m or m.get('method') is None:   # notification or a response to us
                    continue
                try:
                    self._send({'jsonrpc': '2.0', 'id': m['id'], 'result': self.handle(m)})
                except _RpcError as e:
                    self._send({'jsonrpc': '2.0', 'id': m['id'], 'error': {'code': e.code, 'message': str(e)}})
                except Exception as e:
                    print(traceback.format_exc(), file=sys.stderr)
                    self._send({'jsonrpc': '2.0', 'id': m['id'], 'error': {'code': -32603, 'message': str(e)}})
        return 0

    def _send(self, obj):
        self.out.write(json.dumps(obj, default=str, ensure_ascii=False) + '\n'); self.out.flush()


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message); self.code = code


def _tool_error(text):
    return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def serve(ws):
    # stdout carries the protocol only; everything else goes to stderr
    print('Binggio %s MCP server on %s (Python %s, %s). Waiting for a client on stdin.' % (
        __version__, ws.root, platform.python_version(), platform.system()), file=sys.stderr, flush=True)
    sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
    sys.stdin.reconfigure(encoding='utf-8') if hasattr(sys.stdin, 'reconfigure') else None
    return Server(ws).serve()
