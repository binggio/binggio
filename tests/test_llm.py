# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""0.1.10: any model from any provider. Small local stand-ins for the services check the requests
Binggio sends (address, key header, structured output) and how it steps down when a service does
not support a feature. Whatever the model, the plan still goes through validate()."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from binggio import llm
from conftest import DEFAULTS

ENV = ('BINGGIO_LLM_PROVIDER', 'BINGGIO_LLM_BASE_URL', 'BINGGIO_LLM_MODEL', 'BINGGIO_LLM_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
       'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'AZURE_OPENAI_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY', 'BINGGIO_LLM_ALLOWED_MODELS',
       'BINGGIO_LLM_JSON', 'BINGGIO_LLM_API_VERSION', 'BINGGIO_UNIFIED_V14_MODEL', 'BINGGIO_PHASE10_LLM_MODEL', '_BINGGIO_UI_KEY')
SCHEMA = {'type': 'object', 'properties': {'status': {'type': 'string'}}, 'required': ['status'], 'additionalProperties': False}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ENV: monkeypatch.delenv(k, raising=False)
    llm._mode_cache.clear(); llm._no_temperature.clear()
    yield
    import os
    for k in ENV: os.environ.pop(k, None)          # the settings page writes os.environ directly; monkeypatch restores the rest
    llm._mode_cache.clear(); llm._no_temperature.clear()


class Fake:
    """A model service on this computer. `rules` decide the reply to each request."""

    def __init__(self, reply, refuse=()):
        self.seen, self.reply, self.refuse = [], reply, set(refuse)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fake.seen.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
                rf = (body.get('response_format') or {}).get('type')
                for what, msg in (('json_schema', "Invalid JSON payload: unknown field 'response_format.json_schema' is not supported"),
                                  ('json_object', 'response_format json_object is not supported by this model'),
                                  ('temperature', "Unsupported parameter: 'temperature' is not supported with this model.")):
                    if what in fake.refuse and (rf == what or (what == 'temperature' and 'temperature' in body)):
                        return self._send(400, {'error': {'message': msg}})
                if 'bad_key' in fake.refuse:
                    return self._send(401, {'error': {'message': 'Incorrect API key provided: AIzaSyD-THIS-IS-THE-SECRET-KEY-123456'}})
                self._send(200, fake.reply(body))

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)

        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self): self.srv.shutdown(); self.srv.server_close()


def chat_reply(content):
    return lambda body: {'choices': [{'message': {'role': 'assistant', 'content': content}}]}


MSGS = [{'role': 'system', 'content': 'Write the plan.'}, {'role': 'user', 'content': 'a question'}]


def test_openai_compatible_any_model_and_steps_down_when_a_feature_is_missing(monkeypatch):
    f = Fake(chat_reply('```json\n{"status": "plan"}\n```'), refuse={'json_schema', 'json_object', 'temperature'})
    try:
        monkeypatch.setenv('BINGGIO_LLM_BASE_URL', f.url + '/v1'); monkeypatch.setenv('BINGGIO_LLM_MODEL', 'llama3.1:8b')
        assert llm.provider() == 'compatible' and llm.enabled()            # a model on this computer needs no key
        assert llm.complete_json(MSGS, SCHEMA) == {'status': 'plan'}
        modes = [(r['body'].get('response_format') or {}).get('type') for r in f.seen]
        assert modes[0] == 'json_schema' and 'json_object' in modes and modes[-1] is None
        last = f.seen[-1]['body']
        assert 'temperature' not in last and 'JSON Schema' in last['messages'][0]['content']   # schema given in the instructions
        assert all(r['path'] == '/v1/chat/completions' for r in f.seen)
        n = len(f.seen); llm.complete_json(MSGS, SCHEMA)
        assert len(f.seen) == n + 1                                           # remembered: no second round of refusals
    finally:
        f.close()


def test_gemini_key_and_model_work(monkeypatch):
    f = Fake(chat_reply('{"status": "plan"}'))
    try:
        monkeypatch.setenv('GEMINI_API_KEY', 'AIzaSyTESTKEY000000000000000000000')
        monkeypatch.setenv('BINGGIO_LLM_MODEL', 'gemini-2.5-flash')
        assert llm.provider() == 'gemini' and llm.endpoint() == llm.GEMINI_URL and llm.enabled()
        monkeypatch.setenv('BINGGIO_LLM_BASE_URL', f.url)                    # stand-in for Google's address
        monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'gemini')
        assert llm.complete_json(MSGS, SCHEMA) == {'status': 'plan'}
        r = f.seen[0]
        assert r['headers']['authorization'] == 'Bearer AIzaSyTESTKEY000000000000000000000' and r['body']['model'] == 'gemini-2.5-flash'
        assert r['body']['response_format']['type'] == 'json_schema'
    finally:
        f.close()


def test_claude_uses_its_own_api_with_a_tool_for_the_json(monkeypatch):
    f = Fake(lambda body: {'content': [{'type': 'tool_use', 'name': body['tool_choice']['name'], 'input': {'status': 'plan'}}]})
    try:
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test-123456789')
        monkeypatch.setenv('BINGGIO_LLM_MODEL', 'claude-any-model')
        assert llm.provider() == 'anthropic'
        monkeypatch.setenv('BINGGIO_LLM_BASE_URL', f.url + '/v1'); monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'anthropic')
        assert llm.complete_json(MSGS, SCHEMA, 'binggio_plan') == {'status': 'plan'}
        r = f.seen[0]
        assert r['path'] == '/v1/messages' and r['headers']['x-api-key'] == 'sk-ant-test-123456789' and 'anthropic-version' in r['headers']
        assert r['body']['system'] == 'Write the plan.' and [m['role'] for m in r['body']['messages']] == ['user']
        assert r['body']['tools'][0]['input_schema'] == SCHEMA and r['body']['tool_choice'] == {'type': 'tool', 'name': 'binggio_plan'}
    finally:
        f.close()


def test_azure_uses_the_deployment_address_and_api_key_header(monkeypatch):
    f = Fake(chat_reply('{"status": "plan"}'))
    try:
        monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'azure'); monkeypatch.setenv('BINGGIO_LLM_BASE_URL', f.url)
        monkeypatch.setenv('AZURE_OPENAI_API_KEY', 'azure-key-123456'); monkeypatch.setenv('BINGGIO_LLM_MODEL', 'my-gpt-deployment')
        assert llm.complete_json(MSGS, SCHEMA) == {'status': 'plan'}
        r = f.seen[0]
        assert r['path'] == '/openai/deployments/my-gpt-deployment/chat/completions?api-version=' + llm.AZURE_API_VERSION
        assert r['headers']['api-key'] == 'azure-key-123456' and 'authorization' not in r['headers']
    finally:
        f.close()


def test_a_refused_key_is_explained_and_never_shown(monkeypatch):
    f = Fake(chat_reply('{}'), refuse={'bad_key'})
    try:
        key = 'AIzaSyD-THIS-IS-THE-SECRET-KEY-123456'
        monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'compatible'); monkeypatch.setenv('BINGGIO_LLM_BASE_URL', f.url)
        monkeypatch.setenv('BINGGIO_LLM_API_KEY', key); monkeypatch.setenv('BINGGIO_LLM_MODEL', 'm')
        with pytest.raises(llm.ModelError) as e:
            llm.complete_json(MSGS, SCHEMA)
        assert 'refused (HTTP 401)' in str(e.value) and key not in str(e.value) and 'SECRET' not in str(e.value)
    finally:
        f.close()


def test_settings_that_cannot_work_say_why(monkeypatch):
    assert 'model key' in llm.problem()                                     # nothing set
    monkeypatch.setenv('GEMINI_API_KEY', 'AIzaSyTESTKEY000000000000000000000')
    assert llm.provider() == 'gemini' and 'Choose a model' in llm.problem()  # no default model guessed for Gemini
    monkeypatch.setenv('BINGGIO_LLM_MODEL', 'gemini-2.5-flash'); monkeypatch.setenv('BINGGIO_LLM_ALLOWED_MODELS', 'gpt-4.1, claude-x')
    assert 'allowed list' in llm.problem()
    monkeypatch.delenv('BINGGIO_LLM_ALLOWED_MODELS')
    monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'compatible'); monkeypatch.setenv('BINGGIO_LLM_BASE_URL', 'http://models.example.com/v1')
    assert 'https://' in llm.problem()                                      # plain http only for this computer
    monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'nonsense')
    assert 'not one of' in llm.problem()
    monkeypatch.delenv('BINGGIO_LLM_PROVIDER'); monkeypatch.delenv('BINGGIO_LLM_BASE_URL'); monkeypatch.delenv('GEMINI_API_KEY')
    monkeypatch.delenv('BINGGIO_LLM_MODEL'); monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-12345678')
    assert llm.provider() == 'openai' and llm.model() == 'gpt-4.1-mini' and llm.enabled()   # unchanged default


def test_a_question_end_to_end_through_another_provider(demo, monkeypatch):
    """The demo bank answered through a Claude-style service: same answer as the saved plan."""
    gold = {g['id']: g for g in json.loads((Path(__file__).parent / 'golden' / 'demo_plans.json').read_text())}
    g = next(x for x in gold.values() if x['question'] == 'total outstanding of delinquent loans by product')
    plan = {**DEFAULTS, **g['plans'][0]}
    plan['aggregate'] = [{**a, 'percentile': None} for a in plan['aggregate']]
    f = Fake(lambda body: {'content': [{'type': 'tool_use', 'name': 'x', 'input': {'status': 'plan', 'reason': None, 'plan': plan}}]})
    try:
        monkeypatch.setenv('BINGGIO_LLM_PROVIDER', 'anthropic'); monkeypatch.setenv('BINGGIO_LLM_BASE_URL', f.url + '/v1')
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test-123456789'); monkeypatch.setenv('BINGGIO_LLM_MODEL', 'claude-any-model')
        a = demo.ask('What is the total outstanding balance of all delinquent loans for each loan product?')
        assert a['status'] == 'answered', a
        got = {r[0]: r[1] for r in a['rows']}
        assert got == g['expected']
        assert 'approved_catalog' in f.seen[0]['body']['messages'][0]['content']      # names only were sent
    finally:
        f.close()


def test_settings_page_keeps_everything_in_memory(tmp_path, monkeypatch):
    import binggio
    from binggio.ui.server import App, api
    ws = binggio.Workspace.create(tmp_path / 'ws'); app = App(ws)
    st = api(app, 'POST', '/api/settings/model', {}, {'provider': 'gemini', 'key': 'AIzaSyTESTKEY000000000000000000000', 'model': 'gemini-2.5-flash'})
    assert st['configured'] and st['provider'] == 'gemini' and st['from'] == 'this session' and 'AIza' not in json.dumps(st)
    assert not any(b'AIzaSyTESTKEY' in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())
    st = api(app, 'POST', '/api/settings/model', {}, {'provider': 'compatible', 'base_url': 'http://localhost:11434/v1', 'model': 'llama3.1'})
    assert st['configured'] and st['local']
    st = api(app, 'POST', '/api/settings/model', {}, {'clear': True})
