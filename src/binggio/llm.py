# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Any model, from any provider, for questions in your own words.

Binggio needs one thing from a model: turn a question into a JSON plan. The plan is then
checked by Binggio's own validator before anything runs, whichever model wrote it, so the
choice of model changes how often a question is understood, never what an answer may contain.

Providers (BINGGIO_LLM_PROVIDER, or worked out from the other settings):

* openai      - OpenAI (default). Key: BINGGIO_LLM_API_KEY or OPENAI_API_KEY.
* anthropic   - Anthropic Claude, its own API. Key: BINGGIO_LLM_API_KEY or ANTHROPIC_API_KEY.
* gemini      - Google Gemini, through Google's OpenAI-compatible address.
                Key: BINGGIO_LLM_API_KEY, GEMINI_API_KEY or GOOGLE_API_KEY.
* azure       - Azure OpenAI. BINGGIO_LLM_BASE_URL is the resource address
                (https://NAME.openai.azure.com), BINGGIO_LLM_MODEL the deployment name.
                Key: BINGGIO_LLM_API_KEY or AZURE_OPENAI_API_KEY.
* compatible  - any service with an OpenAI-style chat API at BINGGIO_LLM_BASE_URL: Mistral, Groq,
                OpenRouter, DeepSeek, Together, xAI, a company gateway, or a model on this computer
                (Ollama, LM Studio, vLLM). A local address needs no key.

BINGGIO_LLM_MODEL names the model; any name the provider accepts. An organisation can limit
the choice with BINGGIO_LLM_ALLOWED_MODELS (comma-separated).

Structured output: the strict JSON schema is used where the service supports it; otherwise
JSON mode, otherwise the schema is given in the instructions and the JSON is read from the
reply. Binggio steps down automatically when a service rejects a feature, and remembers it
for the rest of the session (BINGGIO_LLM_JSON=schema|object|prompt sets it).

Keys are read from the environment (or kept in memory by binggio ui) and never written to disk.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

PROVIDERS = {
    'openai': 'OpenAI',
    'anthropic': 'Anthropic (Claude)',
    'gemini': 'Google Gemini',
    'azure': 'Azure OpenAI',
    'compatible': 'Other (OpenAI-compatible address)',
}
KEY_VARS = {
    'openai': ('BINGGIO_LLM_API_KEY', 'BINGGIO_PHASE10_LLM_API_KEY', 'OPENAI_API_KEY'),
    'anthropic': ('BINGGIO_LLM_API_KEY', 'ANTHROPIC_API_KEY'),
    'gemini': ('BINGGIO_LLM_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY'),
    'azure': ('BINGGIO_LLM_API_KEY', 'AZURE_OPENAI_API_KEY'),
    'compatible': ('BINGGIO_LLM_API_KEY', 'OPENAI_API_KEY'),
}
DEFAULT_MODEL = {'openai': 'gpt-4.1-mini'}          # other providers: the person chooses
GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/openai'
OPENAI_URL = 'https://api.openai.com/v1'
ANTHROPIC_URL = 'https://api.anthropic.com/v1'
AZURE_API_VERSION = '2024-10-21'
MODES = ('schema', 'object', 'prompt')
_mode_cache: dict = {}            # (provider, url, model) -> structured-output mode that works
_no_temperature: set = set()      # services that refuse a temperature setting

# key-shaped text never reaches a log or a message
SECRET = re.compile(r'(sk-[A-Za-z0-9_-]{6,}|sk-ant-[A-Za-z0-9_-]{6,}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]+|'
                    r'(?i:api[-_]?key)["\']?\s*[:=]\s*["\']?[A-Za-z0-9._-]{8,})')


class ModelError(Exception):
    """The model could not be reached or did not return JSON; the message is safe to show."""


def redact(text):
    s = str(text)
    key = _raw_key()
    if key and len(key) >= 6: s = s.replace(key, '[REDACTED]')
    return SECRET.sub('[REDACTED]', s)


# ------------------------------------------------------------------ settings
def _env(name, default=''):
    return (os.environ.get(name) or default).strip()


def base_url_setting():
    return _env('BINGGIO_LLM_BASE_URL').rstrip('/')


def provider():
    p = _env('BINGGIO_LLM_PROVIDER').lower()
    if p in ('claude',): p = 'anthropic'
    if p in ('google',): p = 'gemini'
    if p in ('openai_compatible', 'openai-compatible', 'local', 'ollama', 'other'): p = 'compatible'
    if p in PROVIDERS: return p
    if p: raise ModelError('BINGGIO_LLM_PROVIDER "%s" is not one of: %s' % (p, ', '.join(PROVIDERS)))
    # not set: work it out from the other settings
    url = base_url_setting().lower()
    if url:
        if 'openai.azure.com' in url or 'azure' in url and 'openai' in url: return 'azure'
        if 'anthropic.com' in url: return 'anthropic'
        if 'generativelanguage.googleapis.com' in url: return 'gemini'
        if url.rstrip('/') in (OPENAI_URL, 'https://api.openai.com'): return 'openai'
        return 'compatible'
    m = _env('BINGGIO_LLM_MODEL').lower()
    if m.startswith('claude'): return 'anthropic'
    if m.startswith(('gemini', 'gemma')): return 'gemini'
    if not _env('BINGGIO_LLM_API_KEY') and not _env('OPENAI_API_KEY'):
        if _env('ANTHROPIC_API_KEY'): return 'anthropic'
        if _env('GEMINI_API_KEY') or _env('GOOGLE_API_KEY'): return 'gemini'
        if _env('AZURE_OPENAI_API_KEY'): return 'azure'
    return 'openai'


def _raw_key():
    try: p = provider()
    except ModelError: p = 'openai'
    for k in KEY_VARS[p]:
        if os.environ.get(k): return os.environ[k].strip()
    return ''


def key_source():
    """The environment variable the key comes from (never the key)."""
    try: p = provider()
    except ModelError: return None
    return next((k for k in KEY_VARS[p] if os.environ.get(k)), None)


def is_local(url):
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    return host in ('localhost', '127.0.0.1', '::1', '0.0.0.0') or host.endswith('.local')


def model():
    p = provider()
    m = _env('BINGGIO_LLM_MODEL') or _env('BINGGIO_UNIFIED_V14_MODEL') or _env('BINGGIO_PHASE10_LLM_MODEL') or DEFAULT_MODEL.get(p, '')
    return m


def endpoint():
    p = provider(); url = base_url_setting()
    if p == 'openai': return url or OPENAI_URL
    if p == 'anthropic': return url or ANTHROPIC_URL
    if p == 'gemini': return url or GEMINI_URL
    return url


def problem():
    """Why a question cannot be sent yet, in words for a person; None when ready."""
    try: p = provider()
    except ModelError as e: return str(e)
    url = endpoint()
    if p in ('azure', 'compatible') and not url:
        return ('Set the address of the service (BINGGIO_LLM_BASE_URL), for example https://NAME.openai.azure.com'
                if p == 'azure' else 'Set the address of the service (BINGGIO_LLM_BASE_URL), for example '
                'https://api.mistral.ai/v1, or http://localhost:11434/v1 for Ollama on this computer')
    if url and not url.startswith(('https://', 'http://')): return 'The service address must start with https:// (or http:// for this computer)'
    if url.startswith('http://') and not is_local(url): return 'Use https:// for a service on another computer; http:// is allowed only for this computer'
    if not _raw_key() and not (p == 'compatible' and is_local(url)):
        return 'Asking questions needs a model key: set BINGGIO_LLM_API_KEY (or add one in Settings)'
    if not model(): return 'Choose a model (BINGGIO_LLM_MODEL), for example the model name your provider lists'
    allowed = [x.strip() for x in _env('BINGGIO_LLM_ALLOWED_MODELS').split(',') if x.strip()]
    if allowed and model() not in allowed:
        return 'The model "%s" is not in this organisation\'s allowed list (BINGGIO_LLM_ALLOWED_MODELS)' % model()
    return None


def enabled():
    return problem() is None


def state():
    """What the settings page shows: never the key."""
    try: p = provider(); err = None
    except ModelError as e: p, err = 'openai', str(e)
    url = '' if err else endpoint()
    return {'configured': enabled(), 'problem': err or problem(), 'provider': p, 'provider_name': PROVIDERS[p],
            'model': model() if not err else _env('BINGGIO_LLM_MODEL'), 'base_url': url if p in ('azure', 'compatible') or base_url_setting() else '',
            'local': bool(url) and is_local(url), 'providers': PROVIDERS}


# ------------------------------------------------------------------ calls
def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json', **headers}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _http_message(e):
    body = ''
    try: body = e.read(4000).decode('utf-8', errors='replace')
    except Exception: pass
    msg = ''
    try:
        err = json.loads(body)
        if isinstance(err, list) and err: err = err[0]
        err = err.get('error', err) if isinstance(err, dict) else err
        msg = str(err.get('message') if isinstance(err, dict) else err)
    except (ValueError, AttributeError): msg = body[:300]
    log.warning('Model API error HTTP %s: %s', getattr(e, 'code', '?'), redact(body)[:1500])
    return redact(msg)[:300]


_FEATURE = re.compile(r'response_format|json_schema|json schema|structured|schema|strict|additionalproperties|'
                      r'not supported|unsupported|unknown (?:field|parameter|name)|invalid (?:json )?payload|tool_choice', re.I)


def _schema_text(schema):
    return ('\n\nReturn ONLY one JSON object, with no other text, that matches this JSON Schema exactly:\n'
            + json.dumps(schema, separators=(',', ':')))


def parse_json(text):
    """The first JSON object in a reply (models without JSON mode may add words or ``` fences)."""
    if isinstance(text, dict): return text
    s = str(text or '').strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s)
    try: return json.loads(s)
    except ValueError: pass
    i = s.find('{')
    while i != -1:
        try: return json.JSONDecoder().raw_decode(s[i:])[0]
        except ValueError: i = s.find('{', i + 1)
    raise ModelError('The model did not return JSON')


def complete_json(messages, schema, name='binggio_plan', timeout=None):
    """Send chat messages (system first) and return the JSON object the model wrote."""
    why = problem()
    if why: raise ModelError(why)
    p, url, m = provider(), endpoint(), model()
    timeout = float(_env('BINGGIO_LLM_TIMEOUT') or timeout or (120 if is_local(url) else 45))
    forced = _env('BINGGIO_LLM_JSON').lower()
    cache_key = (p, url, m)
    start = forced if forced in MODES else _mode_cache.get(cache_key, 'schema')
    modes = list(MODES[MODES.index(start):]) if p != 'anthropic' else ['schema']
    last = None
    for mode in modes:
        for _ in range(2):                         # second try only without temperature
            try:
                out = (_anthropic if p == 'anthropic' else _openai_style)(p, url, m, messages, schema, name, mode, timeout)
                _mode_cache[cache_key] = mode
                return out
            except urllib.error.HTTPError as e:
                msg = _http_message(e); last = 'HTTP %s - %s' % (e.code, msg)
                if e.code in (400, 404, 422) and 'temperature' in msg.lower() and cache_key not in _no_temperature:
                    _no_temperature.add(cache_key); continue
                if e.code in (400, 422) and _FEATURE.search(msg) and mode != modes[-1]:
                    log.info('Model service refused %s output (%s); trying the next way', mode, msg[:160]); break
                raise ModelError(_explain(e.code, msg, p)) from e
            except ModelError as e:
                last = str(e)
                if mode != modes[-1]: break
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                raise ModelError('Could not reach the model service at %s: %s' % (url, redact(getattr(e, 'reason', e)))) from e
    raise ModelError('The model service did not return a usable JSON answer: ' + redact(last or 'no answer'))


def _explain(code, msg, p):
    if code in (401, 403):
        return ('The %s key was refused (HTTP %s). Check that the key is for %s and still valid%s. %s'
                % (PROVIDERS[p], code, PROVIDERS[p], '' if p != 'openai' else ' (for another provider, choose it in Settings)', msg)).strip()
    if code == 404:
        return 'The model or address was not found (HTTP 404). Check the model name and the address. %s' % msg
    if code == 429:
        return 'The model service is limiting requests or the account has no credit left (HTTP 429). %s' % msg
    return 'The model service returned HTTP %s. %s' % (code, msg)


def _openai_style(p, url, m, messages, schema, name, mode, timeout):
    msgs = [dict(x) for x in messages]
    if mode != 'schema' and msgs and msgs[0]['role'] == 'system':
        msgs[0]['content'] += _schema_text(schema)
    body = {'model': m, 'messages': msgs}
    if (p, url, m) not in _no_temperature: body['temperature'] = 0
    if mode == 'schema': body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': name, 'strict': True, 'schema': schema}}
    elif mode == 'object': body['response_format'] = {'type': 'json_object'}
    key = _raw_key()
    if p == 'azure':
        dep = urllib.parse.quote(m, safe='')
        if '/openai/v1' in url:
            target = url + '/chat/completions'
        else:
            target = '%s/openai/deployments/%s/chat/completions?api-version=%s' % (url, dep, _env('BINGGIO_LLM_API_VERSION', AZURE_API_VERSION))
        headers = {'api-key': key}
    else:
        target = url + '/chat/completions'
        headers = {'Authorization': 'Bearer ' + key} if key else {}
    obj = _post(target, body, headers, timeout)
    try:
        msg = obj['choices'][0]['message']
    except (KeyError, IndexError, TypeError):
        raise ModelError('The model service returned an answer without a message')
    if msg.get('refusal'): raise ModelError('The model declined: ' + redact(msg['refusal'])[:200])
    content = msg.get('content')
    if isinstance(content, list): content = ''.join(c.get('text', '') for c in content if isinstance(c, dict))
    return parse_json(content)


def _anthropic(p, url, m, messages, schema, name, mode, timeout):
    system = '\n\n'.join(x['content'] for x in messages if x['role'] == 'system')
    chat = [{'role': x['role'], 'content': x['content']} for x in messages if x['role'] != 'system']
    body = {'model': m, 'max_tokens': 4096, 'system': system, 'messages': chat,
            'tools': [{'name': name, 'description': 'Return the answer as this JSON object.', 'input_schema': schema}],
            'tool_choice': {'type': 'tool', 'name': name}}
    if (p, url, m) not in _no_temperature: body['temperature'] = 0
    obj = _post(url + '/messages', body, {'x-api-key': _raw_key(), 'anthropic-version': _env('BINGGIO_ANTHROPIC_VERSION', '2023-06-01')}, timeout)
    for block in obj.get('content') or []:
        if block.get('type') == 'tool_use' and isinstance(block.get('input'), dict): return block['input']
    text = ''.join(b.get('text', '') for b in obj.get('content') or [] if b.get('type') == 'text')
    return parse_json(text)


def complete_text(prompt, timeout=60):
    """A short free-text answer (used by the acceptance tests' optional second opinion)."""
    schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False}
    out = complete_json([{'role': 'system', 'content': 'Answer in the field "text".'}, {'role': 'user', 'content': prompt}], schema, 'answer', timeout)
    return str(out.get('text', '')).strip()
