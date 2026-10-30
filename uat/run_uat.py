# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio UAT agent: runs the acceptance cases end to end and reports only what needs a person.

    python uat\\run_uat.py                       # every case it can run on this PC
    python uat\\run_uat.py --with-model          # + questions in your own words (BINGGIO_LLM_API_KEY; ~40 model calls)
    python uat\\run_uat.py --with-model --judge  # + a second opinion from the model on each ambiguous answer
    python uat\\run_uat.py --sqlserver "host=localhost;database=master;windows"
    python uat\\run_uat.py --oracle "host=h;service=ORCLPDB1;user=u"  --postgres "host=h;database=d;user=u"
    python uat\\run_uat.py --keep                # keep the simulated machine for a look afterwards

Like the simulator, it installs the package into a brand-new virtual environment and works the way
a user does: the installed `binggio` command and the app (`binggio ui`) through its API, and through
a browser when Playwright is installed. Every case ends as one of:

  PASS       the expected result, checked against independent counts or exact values
  FAIL       clearly wrong
  AMBIGUOUS  the agent cannot decide on its own (each case says when); a person must look
  NOT RUN    needs something this PC does not have (a server, Playwright, a model key)

Only AMBIGUOUS cases are reported in detail, with failures (they block a release) and the cases
that did not run listed after them. Passes are counted, not listed. Results also go into a copy of
the UAT spreadsheet: uat/reports/UAT_results_<time>.xlsx (when openpyxl is available).
"""
from __future__ import annotations
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / 'sim')); sys.path.insert(0, str(HERE))
import datasets  # noqa: E402
import simulate  # noqa: E402
from cases import CASES, by_id  # noqa: E402

WINDOWS = os.name == 'nt'
GOLD = {g['id']: g for g in json.loads((REPO / 'tests' / 'golden' / 'demo_plans.json').read_text(encoding='utf-8'))}
FIXTURE = REPO / 'tests' / 'fixtures' / 'collections'
BY = 'UAT Agent'


class Fail(Exception): pass
class Ambiguous(Exception):
    def __init__(self, msg, evidence=None): super().__init__(msg); self.evidence = evidence
class NotRun(Exception): pass


def check(cond, msg):
    if not cond: raise Fail(msg)


# ====================================================================== the app over HTTP

class App:
    def __init__(self, env):
        self.env = env
        cmd = [str(env.exe), 'ui', '--no-browser', '--port', '0']
        if not WINDOWS and env.mode == 'copy': cmd = [str(env.py)] + cmd
        self.t0 = time.time()
        self.proc = subprocess.Popen(cmd, cwd=str(env.home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', env=env.env())
        self.url = None
        while time.time() - self.t0 < 30:
            line = self.proc.stdout.readline()
            m = re.search(r'(http://127\.0\.0\.1:\d+/)', line or '')
            if m: self.url = m.group(1); break
            if self.proc.poll() is not None: break
        self.started_in = time.time() - self.t0
        if self.url:
            html = urllib.request.urlopen(self.url).read().decode()
            self.html = html
            self.token = re.search(r"const TOKEN = '([^']+)'", html).group(1)

    def call(self, path, body=None, token=None, headers=None, timeout=900):
        h = {'X-Binggio-Token': self.token if token is None else token, 'Content-Type': 'application/json', **(headers or {})}
        req = urllib.request.Request(self.url.rstrip('/') + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=timeout); return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def ok(self, path, body=None):
        code, d = self.call(path, body)
        check(code == 200, '%s -> HTTP %s: %s' % (path, code, d.get('error') if isinstance(d, dict) else d))
        return d

    def wait(self, job, limit=1800):
        t0 = time.time()
        while time.time() - t0 < limit:
            j = self.ok('/api/jobs/' + job['id'])
            if j['status'] != 'running': return j
            time.sleep(0.2)
        raise Fail('job %s did not finish in %d s' % (job['kind'], limit))

    def open(self, path, action='open'):
        return self.ok('/api/workspace', {'action': action, 'path': str(path)})

    def add(self, body):
        return self.wait(self.ok('/api/sources', body))

    def onboard_all(self, cid, by=BY, terms=None):
        props = self.ok('/api/onboard?connection=' + cid)
        body = {'connection': cid, 'by': by,
                'roles': {t: [{'column': x['column'], 'role': x['role']} for x in cols] for t, cols in props['roles'].items()},
                'links': [{'edge_id': l['edge_id'], 'cardinality': l['cardinality']} for l in props['links'] if l['suggested']],
                'concepts': [c['table'] for c in props['concepts']], 'terms': terms or []}
        return props, self.ok('/api/onboard', body)

    def names(self):
        out = {}
        for x in self.ok('/api/review/roles?status=all&limit=100000')['items']:
            out[x['table'].lower()] = x['table']; out[(x['table'].lower(), x['column'].lower())] = x['column']
        return out

    def ask(self, question, plan=None, conversation=None):
        body = {'question': question, 'plan': plan}
        if conversation: body['conversation'] = conversation
        return self.ok('/api/ask', body)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(10)
            except subprocess.TimeoutExpired: self.proc.kill()


def ids(ans):
    return [str(r[0]).strip().upper() for r in ans.get('rows', [])]


def answers(app, cid, data_oracle_keys=None):
    """The 7 checked questions on the retail bank, through the app."""
    P = datasets.plans(cid, app.names())
    got = {}
    a = app.ask('open balance by segment', P['open balance by segment'])['answer']
    got['open balance by segment'] = {r[0]: round(float(r[1]), 2) for r in a.get('rows', [])} if a['status'] == 'answered' else ('refused', a.get('message'))
    for k in ('customers with a MISSED repayment', 'customers with no account', 'Mumbai with PARTIAL'):
        a = app.ask(k, P[k])['answer']; got[k] = sorted(ids(a)) if a['status'] == 'answered' else ('refused', a.get('message'))
    a = app.ask('top 5 loans by amount', P['top 5 loans by amount'])['answer']; got['top 5 loans by amount'] = ids(a)
    conv = app.ok('/api/ask/new', {})['conversation']
    s1, s2 = P['conversation']
    a1 = app.ask(s1['question'], s1['plan'], conv)['answer']; a2 = app.ask(s2['question'], s2['plan'], conv)['answer']
    got['top 10 by balance'] = ids(a1)
    got['top 10 then MISSED'] = sorted(ids(a2)) if a2['status'] == 'answered' else ('refused', a2.get('message'))
    return got


def compare(got, exp):
    bad = []
    for k, e in exp.items():
        g = got.get(k)
        ok = (isinstance(g, dict) and set(g) == set(e) and all(abs(g[x] - e[x]) < 0.01 for x in e)) if isinstance(e, dict) else g == e
        if not ok: bad.append('%s: got %s, expected %s' % (k, str(g)[:160], str(e)[:160]))
    return bad


def golden_ok(case_id, rows):
    exp = GOLD[case_id]['expected']
    if case_id in ('Q4', 'Q7'):
        return {r[0]: round(float(r[1]), 2) for r in rows} == {k: round(float(v), 2) for k, v in exp.items()}
    if case_id == 'Q10': return sorted(str(r[0]) for r in rows) == exp[2]
    if case_id == 'Q9': return [r[0] for r in rows] == exp
    return sorted(str(r[0]) for r in rows) == exp


def sha_tree(path):
    path = Path(path)
    files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def files_containing(roots, needle):
    hits = []
    b = needle.encode()
    for root in roots:
        for f in Path(root).rglob('*'):
            try:
                if f.is_file() and f.stat().st_size < 200_000_000 and b in f.read_bytes(): hits.append(str(f))
            except OSError:
                pass
    return hits


# ====================================================================== the agent

class Agent:
    def __init__(self, args):
        self.args = args
        self.root = Path(tempfile.mkdtemp(prefix='binggio-uat-'))
        self.env = simulate.Env(self.root, args.mode)
        self.results = {}
        self.ctx = {}
        self.app = None
        self.key = os.environ.get('BINGGIO_LLM_API_KEY') or os.environ.get('OPENAI_API_KEY')

    # --------------------------------------------------------------- bookkeeping
    def run(self, cid, fn, *a):
        t0 = time.time()
        try:
            detail = fn(*a) or ''
            status, evidence = 'PASS', None
        except Ambiguous as e:
            status, detail, evidence = 'AMBIGUOUS', str(e), e.evidence
        except Fail as e:
            status, detail, evidence = 'FAIL', str(e), None
        except NotRun as e:
            status, detail, evidence = 'NOT RUN', str(e), None
        except Exception as e:
            status, detail, evidence = 'FAIL', 'Unexpected error: %s: %s' % (type(e).__name__, e), traceback.format_exc()[-1500:]
        self.results[cid] = {'id': cid, 'status': status, 'detail': str(detail), 'evidence': evidence, 'seconds': round(time.time() - t0, 1)}
        print('%-8s %-9s %6.1fs  %s' % (cid, status, time.time() - t0, str(detail).splitlines()[0][:100] if detail else ''), flush=True)
        return status

    def skip(self, cid, why):
        if cid not in self.results:
            self.results[cid] = {'id': cid, 'status': 'NOT RUN', 'detail': why, 'evidence': None, 'seconds': 0}
            print('%-8s %-9s %6.1fs  %s' % (cid, 'NOT RUN', 0, why[:100]), flush=True)

    # --------------------------------------------------------------- main
    def go(self):
        env = self.env
        if self.run('INS-01', self.ins_install) != 'PASS':
            for c in CASES: self.skip(c['id'], 'installation failed (INS-01)')
            return
        self.run('INS-02', self.ins_files)
        self.run('INS-03', self.ins_version)
        self.run('INS-04', self.ins_module)
        self.run('INS-05', self.ins_excel)
        self.run('INS-06', self.ins_outside)
        self.run('LIC-01', self.ins_licence)
        if self.run('APP-01', self.app_start) not in ('PASS', 'AMBIGUOUS'):
            for c in CASES: self.skip(c['id'], 'the app did not start (APP-01)')
            return
        try:
            for cid, fn in [('APP-02', self.app_token), ('APP-03', self.app_host), ('APP-06', self.app_badopen),
                            ('APP-04', self.app_demo), ('DEMO-01', self.demo_overview), ('DEMO-02', self.demo_names),
                            ('DEMO-03', self.demo_samples), ('DEMO-04', self.demo_followups), ('DEMO-05', self.demo_provenance),
                            ('DEMO-06', self.demo_nokey), ('DEMO-07', self.demo_speed),
                            ('ASK-04', self.ask_nothing), ('SRC-02', self.src_picker), ('SRC-01', self.src_csv),
                            ('DISC-01', self.disc_keys), ('DISC-02', self.disc_links), ('DISC-03', self.disc_noise),
                            ('ONB-01', self.onb_impact), ('ONB-06', self.onb_name), ('ONB-03', self.onb_apply),
                            ('ONB-04', self.onb_term), ('ASK-01', self.ask_own), ('DISC-05', self.disc_messy), ('ASK-05', self.ask_nocohort),
                            ('REV-01', self.rev_role_cycle), ('REV-02', self.rev_role_change), ('REV-03', self.rev_rel_cycle),
                            ('REV-04', self.rev_term_concept), ('REV-05', self.rev_bulk), ('REV-06', self.rev_impossible),
                            ('REV-07', self.rev_revoke_link), ('REV-08', self.rev_activity), ('CHG-04', self.chg_touch),
                            ('SAFE-03', self.safe_move), ('SRC-03', self.src_excel), ('SRC-04', self.src_xls),
                            ('SRC-06', self.src_test_conn), ('SRC-05', self.src_sqlite), ('SAFE-02', self.safe_password),
                            ('SRC-09', self.src_refresh), ('CHG-01', self.chg_stale), ('CHG-02', self.chg_refused),
                            ('CHG-03', self.chg_reapprove), ('ASK-02', self.ask_same), ('SRC-07', self.src_broken),
                            ('SRC-13', self.src_driver), ('DISC-04', self.disc_collections), ('ONB-02', self.onb_duplicate),
                            ('ONB-05', self.onb_weak), ('PERF-01', self.perf_big), ('ASK-03', self.ask_long)]:
                self.run(cid, fn)
            self.run('MCP-01', self.mcp_agent)
            for cid, kind in (('SRC-10', 'sqlserver'), ('SRC-11', 'oracle'), ('SRC-12', 'postgres')):
                spec = getattr(self.args, kind)
                if spec: self.run(cid, self.src_server, kind, spec)
                else: self.skip(cid, 'needs a %s server: run with --%s "host=...;database=..."' % (kind, kind))
            if self.args.with_model:
                if not self.key:
                    for c in CASES:
                        if c['how'] == 'Agent + model': self.skip(c['id'], 'set BINGGIO_LLM_API_KEY to run questions in your own words')
                else:
                    self.app.open(self.ctx['demo'])
                    self.app.ok('/api/settings/model', {'key': self.key})
                    for cid, fn in [('NL-01', self.nl_clear), ('NL-02', self.nl_sloppy), ('NL-03', self.nl_two_meanings),
                                    ('NL-04', self.nl_unanswerable), ('NL-05', self.nl_repeat), ('NL-06', self.nl_followup)]:
                        self.run(cid, fn)
            else:
                for c in CASES:
                    if c['how'] == 'Agent + model': self.skip(c['id'], 'run with --with-model (needs BINGGIO_LLM_API_KEY)')
            self.run('SAFE-01', self.safe_key)
            self.run('SRC-08', self.src_untouched)
            self.browser_cases()
            self.run('APP-05', self.app_recent)
        finally:
            if self.app: self.app.stop()
        self.run('SAFE-04', self.safe_uninstall)

    # =============================================================== install
    def ins_install(self):
        try:
            self.env.install()
        except simulate.Fail as e:
            if 'differs from the source' in str(e):
                self.ctx['install_differs'] = str(e)
                return 'installed, but the files differ from the source (see INS-02)'
            raise Fail(str(e))
        deps = subprocess.run([str(self.env.py), '-c', 'import importlib.metadata as m;print(m.requires("binggio") or [])'],
                              capture_output=True, text=True, env=self.env.env()).stdout
        check(all('extra ==' in d for d in re.findall(r"'([^']+)'", deps)), 'binggio declares required dependencies: ' + deps)
        return '; '.join(self.env.notes[-2:])

    def ins_files(self):
        check('install_differs' not in self.ctx, self.ctx.get('install_differs', ''))
        return self.env.notes[-1]

    def ins_version(self):
        r = self.env.run(['--version']); check(r.returncode == 0 and '0.1.0' in r.stdout, 'version: ' + r.stdout + r.stderr)
        r = self.env.run(['--help']); check(all(w in r.stdout for w in ('ui', 'onboard', 'ask')), 'help text incomplete')
        return r.stdout.splitlines()[0] if r.stdout else ''

    def ins_module(self):
        r = subprocess.run([str(self.env.py), '-m', 'binggio', '--version'], capture_output=True, text=True, env=self.env.env(), cwd=str(self.env.work))
        check(r.returncode == 0 and '0.1.0' in r.stdout, 'python -m binggio --version: ' + r.stdout + r.stderr)

    def ins_excel(self):
        if self.env.mode == 'copy' and not self.env.excel: raise NotRun('sandbox mode without openpyxl')
        if not self.env.excel:
            raise Ambiguous('openpyxl could not be installed into the new environment. If this PC has internet, binggio[excel] is broken; if not, it is the network.')
        return 'openpyxl available in the user environment'

    def ins_licence(self):
        lic = REPO / 'LICENSE'
        check(lic.exists(), 'no LICENSE file: download the official text (see docs/BUILD_NOTES.md)')
        text = lic.read_text(encoding='utf-8', errors='replace')
        check(text.lstrip().startswith('GNU AFFERO GENERAL PUBLIC LICENSE') and 'Version 3, 19 November 2007' in text,
              'LICENSE is not the GNU AGPL v3 text')
        check(30000 < len(text) < 40000 and 'Remote Network Interaction' in text, 'LICENSE looks incomplete or modified (%d characters)' % len(text))
        if self.env.mode == 'copy':      # no installed metadata in sandbox mode: read the project file
            meta = (REPO / 'pyproject.toml').read_text(encoding='utf-8')
        else:
          meta = subprocess.run([str(self.env.py), '-c', 'import importlib.metadata as m;md=m.metadata("binggio");print(md.get("License"));print(md.get_all("Classifier"))'],
                              capture_output=True, text=True, env=self.env.env()).stdout
        check('AGPL-3.0-or-later' in meta and 'AGPLv3+' in meta, 'installed metadata does not declare the AGPL: ' + meta)
        missing = [str(f.relative_to(REPO)) for d in ('src', 'sim', 'uat', 'tools') for f in (REPO / d).rglob('*.py')
                   if 'SPDX-License-Identifier: AGPL-3.0-or-later' not in f.read_text(encoding='utf-8')[:300]]
        check(not missing, 'source files without the licence header: %s' % missing[:5])

    def ins_outside(self):
        empty = self.env.home / 'empty folder'; empty.mkdir(exist_ok=True)
        r = self.env.run(['status'], cwd=empty)
        out = r.stdout + r.stderr
        check(r.returncode == 2 and 'No Binggio workspace' in out and 'Traceback' not in out, 'outside a workspace: exit %s %s' % (r.returncode, out[-300:]))

    # =============================================================== app
    def app_start(self):
        self.app = App(self.env)
        check(self.app.url, 'binggio ui did not print an address within 30 s')
        check('const TOKEN' in self.app.html and 'Binggio' in self.app.html, 'the page served is not the app')
        if self.app.started_in > 10: raise Ambiguous('started, but took %.0f s (expected under 10 s)' % self.app.started_in)
        return 'started in %.1f s at %s' % (self.app.started_in, self.app.url)

    def app_token(self):
        check(self.app.call('/api/state', token='')[0] == 403, 'no token accepted')
        check(self.app.call('/api/state', token='wrong')[0] == 403, 'wrong token accepted')

    def app_host(self):
        check(self.app.call('/api/state', headers={'Host': 'evil.example:80'})[0] == 403, 'foreign Host header accepted')

    def app_badopen(self):
        code, d = self.app.call('/api/workspace', {'action': 'open', 'path': str(self.env.home / 'not a workspace')})
        check(code == 400 and 'workspace' in d.get('error', '').lower(), 'got %s %s' % (code, d))
        check(self.app.ok('/api/state')['version'], 'app unusable after the error')

    def app_demo(self):
        st = self.app.ok('/api/state'); check(st['workspace'] is None, 'should start on the start screen')
        self.ctx['demo'] = self.env.work / 'sample bank'
        self.app.open(self.ctx['demo'], 'demo')
        o = self.app.ok('/api/overview'); check(o['status']['answerable']['tables'] == 63, 'answerable tables: %s' % o['status']['answerable'])

    def app_recent(self):
        self.app.stop(); self.app = App(self.env)
        check(self.app.url, 'restart failed')
        recent = self.app.ok('/api/state')['recent']
        check(any(Path(r).name == 'sample bank' for r in recent), 'sample bank not in Recent: %s' % recent)
        self.app.open(self.ctx['demo'])

    # =============================================================== sample bank
    def demo_overview(self):
        o = self.app.ok('/api/overview'); st = o['status']
        got = (st['sources'], st['tables'], st['answerable']['tables'], st['registry']['approved'], st['registry']['stale'])
        check(got == (10, 85, 63, 429, 0), 'sources, tables, answerable, approved, stale = %s' % (got,))

    def demo_names(self):
        pat = re.compile(r'Phase ?\d|\bPOC\b|decision round|Ashish|superuser', re.I)
        texts = [s['name'] for s in self.app.ok('/api/sources')['items']]
        for kind in ('concepts', 'terms'):
            for x in self.app.ok('/api/review/' + kind)['items']:
                texts += [x['name'] or '', x['definition'].get('description') or '', x['actor'] or '']
        db = sqlite3.connect(self.ctx['demo'] / 'data' / 'phase7_governance' / 'registry.sqlite3')
        texts += [a for a, in db.execute('SELECT DISTINCT actor FROM events')] + [n for n, in db.execute('SELECT DISTINCT note FROM events')]
        db.close()
        hits = sorted({t for t in texts if pat.search(t)})
        check(not hits, 'visible POC or personal wording: %s' % hits[:5])

    def demo_samples(self):
        s = self.app.ok('/api/samples')['items']; check(len(s) == 10, '%d sample questions' % len(s))
        bad = []
        for q in s:
            conv = self.app.ok('/api/ask/new', {})['conversation']
            for st in q['steps']: a = self.app.ask(st['question'], st['plan'], conv)['answer']
            if a['status'] != 'answered' or not golden_ok(q['id'], a['rows']): bad.append(q['id'])
            if q['id'] == 'Q1': self.ctx['demo_answer'] = a
        check(not bad, 'wrong answers: %s' % bad)
        return '10 of 10 correct'

    def demo_followups(self):
        q = next(x for x in self.app.ok('/api/samples')['items'] if x['id'] == 'Q10')
        conv = self.app.ok('/api/ask/new', {})['conversation']
        counts = []
        for st in q['steps']:
            a = self.app.ask(st['question'], st['plan'], conv)['answer']; counts.append(a.get('row_count'))
        check(counts == [10, 9, 9], 'row counts per step %s (expected [10, 9, 9])' % counts)

    def demo_provenance(self):
        a = self.ctx.get('demo_answer') or {}
        missing = [k for k in ('sources', 'business_terms', 'as_of', 'explanation', 'sql') if not a.get(k)]
        check(not missing, 'answer details missing: %s' % missing)
        check(all(t.get('meaning') for t in a['business_terms']), 'a business term has no meaning shown')
        check(not any(re.search(r'term:[0-9a-f]{8}', s) for s in a['explanation']), 'explanation shows internal term ids: %s' % a['explanation'])

    def demo_nokey(self):
        a = self.app.ask('Which customers have a delinquent loan?')['answer']
        check(a['status'] == 'refused' and a['code'] == 'MODEL_NOT_CONFIGURED', 'got %s %s' % (a.get('status'), a.get('code')))

    def demo_speed(self):
        t0 = time.time(); self.app.ok('/api/overview'); s = time.time() - t0
        if s > 5: raise Fail('overview took %.1f s' % s)
        if s > 2: raise Ambiguous('overview took %.1f s (target under 2 s; acceptable on a slow PC?)' % s)
        return '%.2f s' % s

    # =============================================================== own data (CSV)
    def ask_nothing(self):
        self.app.open(self.env.work / 'empty bank', 'create')
        plan = {'entity': 'x::y', 'select': [], 'filters': [], 'where': None, 'exists': [], 'aggregate': [], 'group_by': [],
                'sort': [], 'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
        a = self.app.ask('How many customers?', plan)['answer']
        check(a['status'] == 'refused' and a['code'] == 'NOTHING_APPROVED', 'got %s %s' % (a.get('code'), a.get('message')))

    def src_picker(self):
        self.ctx['data'] = datasets.build(); self.ctx['oracle'] = datasets.oracle(self.ctx['data'])
        self.ctx['csv_src'] = datasets.write_csv(self.ctx['data'], self.env.home / 'Downloads' / 'core export (Oct)')
        self.ctx['src_hash_before'] = sha_tree(self.ctx['csv_src'])
        d = self.app.ok('/api/fs?want=csvdir&path=' + urllib.request.quote(str(self.ctx['csv_src'])))
        check(d['csv_here'] == 4, 'picker counts %s CSV files' % d['csv_here'])

    def src_csv(self):
        self.ctx['csv_ws'] = self.env.work / 'retail bank'
        self.app.open(self.ctx['csv_ws'], 'create')
        j = self.app.add({'type': 'csv', 'name': 'Core export', 'path': str(self.ctx['csv_src'])})
        check(j['status'] == 'done', 'job failed: %s' % j.get('error'))
        r = j['result']; self.ctx['csv_cid'] = r['connection']
        check(r['tables'] == 4 and not r['profile_failures'], 'tables %s, failures %s' % (r['tables'], r['profile_failures']))
        return '%s s' % j['seconds']

    def _roles(self, status='all'):
        return self.app.ok('/api/review/roles?status=%s&limit=100000' % status)['items']

    def disc_keys(self):
        pk = {x['table']: x['column'] for x in self._roles() if x['suggested'] == 'PRIMARY_KEY_CANDIDATE'}
        exp = {'customers': 'customer_id', 'accounts': 'account_id', 'loans': 'loan_id', 'repayments': 'repayment_id'}
        check(pk == exp, 'suggested keys %s' % pk)

    def _rels(self, status='all'):
        return self.app.ok('/api/review/relationships?status=%s&limit=100000' % status)['items']

    REAL = {('accounts', 'customer_id', 'customers', 'customer_id'), ('repayments', 'loan_id', 'loans', 'loan_id'),
            ('loans', 'cust_ref', 'customers', 'customer_id')}

    def disc_links(self):
        got = {(x['child_table'], x['child_column'], x['parent_table'], x['parent_column']) for x in self._rels()}
        check(self.REAL <= got, 'missing links: %s' % (self.REAL - got))

    def disc_noise(self):
        extra = [x for x in self._rels() if (x['child_table'], x['child_column'], x['parent_table'], x['parent_column']) not in self.REAL]
        hard = [x for x in extra if x['score'] >= 0.9]
        mid = [x for x in extra if 0.85 <= x['score'] < 0.9]
        lbl = lambda x: '%s.%s -> %s.%s (%.2f)' % (x['child_table'], x['child_column'], x['parent_table'], x['parent_column'], x['score'])
        check(not hard, 'unexpected strong links: %s' % [lbl(x) for x in hard])
        if mid: raise Ambiguous('links outside the expected set that would be pre-ticked: %s' % [lbl(x) for x in mid],
                                evidence=[{'link': lbl(x), 'reasons': x['reasons']} for x in mid])
        return '%d extra weak link(s), left for a person' % len(extra) if extra else 'no extra links'

    def onb_impact(self):
        p = self.app.ok('/api/onboard?connection=' + self.ctx['csv_cid'])
        bad = [t['table'] for t in p['impact']['tables'] if not t.get('kind') or not t.get('detail')]
        check(len(p['impact']['tables']) == 4 and not bad, 'tables without a classification: %s' % bad)
        return ', '.join('%s=%s' % (t['table'], t['kind']) for t in p['impact']['tables'])

    def _event_count(self):
        return len(self.app.ok('/api/history?limit=500')['items'])

    def onb_name(self):
        before = self._event_count()
        code, d = self.app.call('/api/onboard', {'connection': self.ctx['csv_cid'], 'roles': {'customers': [{'column': 'customer_id', 'role': 'PRIMARY_KEY_CANDIDATE'}]}})
        check(code == 400 and 'name' in d.get('error', '').lower(), 'got %s %s' % (code, d))
        check(self._event_count() == before, 'something was recorded without a name')

    def onb_apply(self):
        terms = [{'table': 'accounts', 'column': 'status', 'value': 'CLOSED', 'words': 'closed account, account closed'}]
        _, r = self.app.onboard_all(self.ctx['csv_cid'], terms=terms)
        check(not r['errors'], 'errors: %s' % r['errors'])
        check((r['roles'], r['links'], r['concepts'], r['terms']) == (21, 3, 4, 1), 'approved roles, links, concepts, terms = %s' % ((r['roles'], r['links'], r['concepts'], r['terms']),))

    def onb_term(self):
        term = next((t for t in self.app.ok('/api/review/terms')['items'] if t['kind'] == 'term' and t['name'].lower().startswith('closed account')), None)
        check(term and term['status'] == 'approved', 'term not approved: %s' % term)
        cid, n = self.ctx['csv_cid'], self.app.names()
        plan = {'entity': cid + '::' + n['accounts'], 'select': [cid + '::' + n['accounts'] + '::' + n[('accounts', 'account_id')]],
                'filters': [], 'where': None, 'exists': [], 'aggregate': [], 'group_by': [], 'sort': [], 'limit': 200,
                'cohort': None, 'latest': None, 'terms': [{'term': term['id'], 'negate': False}]}
        a = self.app.ask('closed accounts', plan)['answer']
        exp = sorted(x['account_id'] for x in self.ctx['data']['accounts'] if x['status'] == 'CLOSED')
        check(a['status'] == 'answered' and sorted(ids(a)) == exp, 'closed accounts via the term: %s rows, expected %s' % (a.get('row_count'), len(exp)))

    def ask_own(self):
        got = answers(self.app, self.ctx['csv_cid']); self.ctx['csv_answers'] = got
        bad = compare(got, self.ctx['oracle']); check(not bad, '\n'.join(bad))
        return '7 of 7 equal the independent counts'

    def disc_messy(self):
        check(self.ctx.get('csv_answers') and not compare(self.ctx['csv_answers'], self.ctx['oracle']), 'see ASK-01')
        return 'BOM, CRLF, semicolons, "1,234.50", " c0007 " and cust_ref handled (ASK-01 answers exact)'

    def ask_nocohort(self):
        conv = self.app.ok('/api/ask/new', {})['conversation']
        plan = datasets.plans(self.ctx['csv_cid'], self.app.names())['conversation'][1]['plan']
        a = self.app.ask('Which of these customers have a MISSED repayment?', plan, conv)['answer']
        check(a['status'] == 'refused' and a['code'] == 'COHORT_NOT_AVAILABLE', 'got %s %s' % (a.get('code'), a.get('message')))

    # =============================================================== review
    def _role_item(self, x): return {'connection': x['connection'], 'table': x['table'], 'column': x['column']}

    def _role_status(self, item):
        return next(x for x in self._roles() if (x['table'], x['column']) == (item['table'], item['column']))

    def rev_role_cycle(self):
        x = next(x for x in self._roles('approved') if x['table'] == 'repayments' and x['column'] == 'due_date')
        item = self._role_item(x); seen = []
        for act, want in (('revoke', 'revoked'), ('reopen', 'pending'), ('approve', 'approved')):
            r = self.app.ok('/api/review/roles', {'action': act, 'by': BY, 'note': 'UAT ' + act, 'items': [item]})
            check(r['done'] == 1, '%s: %s' % (act, r['errors']))
            seen.append(self._role_status(item)['status'])
        check(seen == ['revoked', 'pending', 'approved'], 'statuses %s' % seen)
        hist = self.app.ok('/api/history?artifact=' + self._role_status(item)['decision']['id'])['items']
        check(len(hist) >= 4, 'history has %d entries' % len(hist))

    def rev_role_change(self):
        x = next(x for x in self._roles('approved') if x['table'] == 'repayments' and x['column'] == 'amount_due')
        item = self._role_item(x)
        self.app.ok('/api/review/roles', {'action': 'revoke', 'by': BY, 'items': [item]})
        self.app.ok('/api/review/roles', {'action': 'reopen', 'by': BY, 'items': [item]})
        r = self.app.ok('/api/review/roles', {'action': 'approve', 'by': BY, 'items': [{**item, 'role': 'IDENTIFIER'}]})
        stored = self._role_status(item)['decision']['approved']['role']
        # put it back as a measure so later answers stay right
        self.app.ok('/api/review/roles', {'action': 'approve', 'by': BY, 'items': [{**item, 'role': 'MEASURE'}]})
        check(r['done'] == 1 and stored == 'IDENTIFIER', 'stored role %s' % stored)

    def _rel(self, child, parent):
        return next(x for x in self._rels() if x['child_table'] == child and x['parent_table'] == parent)

    def rev_rel_cycle(self):
        x = self._rel('repayments', 'loans'); seen = []
        for act in ('revoke', 'reopen', 'approve'):
            it = {'edge_id': x['edge_id'], 'cardinality': 'many_to_one'}
            check(self.app.ok('/api/review/relationships', {'action': act, 'by': BY, 'items': [it]})['done'] == 1, act + ' failed')
            seen.append(self._rel('repayments', 'loans')['status'])
        y = self._rel('repayments', 'loans')
        check(seen == ['revoked', 'pending', 'approved'] and y['decision']['approved']['cardinality'] == 'many_to_one', 'statuses %s' % seen)

    def rev_term_concept(self):
        out = []
        for kind in ('terms', 'concepts'):
            x = next(t for t in self.app.ok('/api/review/' + kind)['items'] if t['status'] == 'approved' and t['kind'] in ('term', 'concept'))
            seen = [self.app.ok('/api/review/' + kind, {'id': x['id'], 'action': a, 'by': BY})['status'] for a in ('revoke', 'reopen', 'approve')]
            check(seen == ['revoked', 'pending', 'approved'], '%s: statuses %s' % (kind, seen))
            out.append('%s "%s"' % (kind[:-1], x['name']))
        return ', '.join(out)

    def rev_bulk(self):
        five = [x for x in self._roles('approved') if x['table'] == 'customers'][:5]
        items = [self._role_item(x) for x in five]
        self.app.ok('/api/review/roles', {'action': 'revoke', 'by': BY, 'items': items})
        self.app.ok('/api/review/roles', {'action': 'reopen', 'by': BY, 'items': items})
        r = self.app.ok('/api/review/roles', {'action': 'approve', 'by': BY, 'items': items})
        check(r['done'] == 5, 'bulk approve recorded %s of 5: %s' % (r['done'], r['errors']))

    def rev_impossible(self):
        x = self._roles('approved')[0]
        r = self.app.ok('/api/review/roles', {'action': 'reopen', 'by': BY, 'items': [self._role_item(x)]})
        check(r['done'] == 0 and r['errors'], 'reopen of an approved item: %s' % r)

    def rev_revoke_link(self):
        x = self._rel('accounts', 'customers'); it = {'edge_id': x['edge_id'], 'cardinality': 'many_to_one'}
        self.app.ok('/api/review/relationships', {'action': 'revoke', 'by': BY, 'items': [it]})
        try:
            a = self.app.ask('open balance by segment', datasets.plans(self.ctx['csv_cid'], self.app.names())['open balance by segment'])['answer']
        finally:
            self.app.ok('/api/review/relationships', {'action': 'reopen', 'by': BY, 'items': [it]})
            self.app.ok('/api/review/relationships', {'action': 'approve', 'by': BY, 'items': [it]})
        if a['status'] == 'answered':
            raise Ambiguous('answered with accounts -> customers revoked. Another approved path must exist; check it is right.',
                            evidence={'sources': a.get('sources'), 'steps': a.get('explanation')})
        return 'refused: %s' % a.get('message', '')[:100]

    def rev_activity(self):
        ev = [e for e in self.app.ok('/api/history?limit=500')['items'] if e['actor'] == BY]
        check(len(ev) >= 20 and all(e['note'] for e in ev), '%d events by the agent' % len(ev))
        ats = [e['at'] for e in ev]; check(ats == sorted(ats, reverse=True), 'not newest first')

    def chg_touch(self):
        for f in (self.ctx['csv_ws'] / 'data' / 'sources').rglob('*.csv'):
            os.utime(f, (time.time() + 60, time.time() + 60))
        st = self.app.ok('/api/overview')['status']['registry']
        check(st['stale'] == 0, 'touching files made %s approvals stale' % st['stale'])

    def safe_move(self):
        dest = self.env.home / 'Desktop' / 'moved bank (copy)'
        shutil.copytree(self.ctx['csv_ws'], dest)
        before = self.app.ok('/api/overview')['status']['registry']
        self.app.open(dest)
        after = self.app.ok('/api/overview')['status']['registry']
        got = answers(self.app, self.ctx['csv_cid'])
        self.app.open(self.ctx['csv_ws'])
        check(before == after and got == self.ctx['csv_answers'], 'after moving: %s vs %s' % (after, before))

    # =============================================================== Excel and SQLite
    def src_excel(self):
        if not self.env.excel: raise NotRun('openpyxl is not installed in the user environment (INS-05)')
        book = self.env.home / 'Downloads' / 'Bank report.xlsx'
        dj = self.root / 'excel_data.json'; dj.write_text(json.dumps(self.ctx['data']), encoding='utf-8')
        r = subprocess.run([str(self.env.py), '-c', 'import sys,json,pathlib;sys.path.insert(0,sys.argv[1]);import datasets;'
                            'datasets.write_excel(json.load(open(sys.argv[2],encoding="utf-8")),pathlib.Path(sys.argv[3]))',
                            str(REPO / 'sim'), str(dj), str(book)], capture_output=True, text=True, env=self.env.env())
        check(r.returncode == 0, 'could not write the test workbook: ' + r.stderr[-500:])
        self.ctx['xlsx'] = book; self.ctx['xlsx_hash'] = sha_tree(book)
        self.app.open(self.env.work / 'excel bank', 'create')
        j = self.app.add({'type': 'excel', 'name': 'Bank report', 'path': str(book)})
        check(j['status'] == 'done', 'job failed: %s' % j.get('error'))
        notes = ' '.join(j['result']['notes'])
        check(j['result']['tables'] == 4 and notes.count('row 3') >= 4, 'tables %s; notes: %s' % (j['result']['tables'], notes[:300]))
        self.app.onboard_all(j['result']['connection'])
        self.ctx['xlsx_answers'] = answers(self.app, j['result']['connection'])

    def src_xls(self):
        old = self.env.home / 'Downloads' / 'old report.xls'; old.write_bytes(b'\xd0\xcf\x11\xe0' + b'\0' * 64)
        n = len(self.app.ok('/api/sources')['items'])
        code, d = self.app.call('/api/sources', {'type': 'excel', 'name': 'Old', 'path': str(old)})
        msg = d.get('error', '') if code != 200 else (self.app.wait(d).get('error') or '')
        check('save as .xlsx' in msg, 'message: %s' % msg)
        check(len(self.app.ok('/api/sources')['items']) == n, 'a source was added anyway')

    def src_test_conn(self):
        self.ctx['db'] = datasets.write_sqlite(self.ctx['data'], self.env.home / 'Downloads' / 'bank.db')
        self.ctx['db_hash'] = sha_tree(self.ctx['db'])
        self.app.open(self.env.work / 'db bank', 'create')
        r = self.app.ok('/api/sources/test', {'kind': 'sqlite', 'path': str(self.ctx['db'])})
        check(sorted(t['table'] for t in r['tables']) == ['accounts', 'customers', 'loans', 'repayments'], 'listed %s' % r['tables'])
        check(not self.app.ok('/api/sources')['items'], 'testing the connection added a source')

    def src_sqlite(self):
        j = self.app.add({'type': 'sqlite', 'name': 'Core DB', 'path': str(self.ctx['db'])})
        check(j['status'] == 'done' and j['result']['tables'] == 4, 'job: %s' % (j.get('error') or j['result']))
        self.ctx['db_cid'] = j['result']['connection']
        d = self.app.ok('/api/table?connection=%s&table=accounts' % self.ctx['db_cid'])
        check(any(c['declared_key'] and c['name'] == 'account_id' for c in d['columns']), 'declared primary key not kept')
        self.app.onboard_all(self.ctx['db_cid'])
        self.ctx['db_answers'] = answers(self.app, self.ctx['db_cid'])

    def safe_password(self):
        pw = 'uat-pw-' + secrets.token_hex(6)
        j = self.app.add({'type': 'database', 'kind': 'sqlite', 'name': 'Password check', 'database': str(self.ctx['db']),
                          'user': 'reader', 'password': pw, 'tables': ['customers']})
        check(j['status'] == 'done', 'add failed: %s' % j.get('error'))
        hits = files_containing([self.env.home, self.env.work], pw)
        check(not hits, 'the password was written to: %s' % hits[:3])

    def src_refresh(self):
        con = sqlite3.connect(self.ctx['db']); con.execute("UPDATE accounts SET status='CLOSED' WHERE account_id='A00001'"); con.commit(); con.close()
        self.ctx['data2'] = json.loads(json.dumps(self.ctx['data']))
        next(a for a in self.ctx['data2']['accounts'] if a['account_id'] == 'A00001')['status'] = 'CLOSED'
        self.ctx['db_hash'] = sha_tree(self.ctx['db'])   # the change above is ours; refresh must not change it again
        j = self.app.wait(self.app.ok('/api/sources/refresh', {'connection': self.ctx['db_cid']}))
        check(j['status'] == 'done', 'refresh failed: %s' % j.get('error'))
        check(j['result']['changed_tables'] == ['accounts'], 'changed tables %s' % j['result']['changed_tables'])

    def chg_stale(self):
        stale = self._roles('stale')
        check(stale, 'no approvals became stale')
        tables = {x['table'] for x in stale}
        check(tables == {'accounts'}, 'stale roles in tables %s (expected accounts only)' % tables)
        return '%d column roles stale, all in accounts' % len(stale)

    def chg_refused(self):
        a = self.app.ask('open balance by segment', datasets.plans(self.ctx['db_cid'], self.app.names())['open balance by segment'])['answer']
        check(a['status'] == 'refused', 'answered from stale approvals')

    def chg_reapprove(self):
        items = [self._role_item(x) for x in self._roles('stale')]
        self.app.ok('/api/review/roles', {'action': 'approve', 'by': BY, 'note': 'Re-confirmed after the data changed', 'items': items})
        rels = [{'edge_id': x['edge_id'], 'cardinality': 'many_to_one'} for x in self._rels('stale')]
        if rels: self.app.ok('/api/review/relationships', {'action': 'approve', 'by': BY, 'items': rels})
        got = answers(self.app, self.ctx['db_cid'])
        bad = compare(got, datasets.oracle(self.ctx['data2']))
        check(not bad, 'after re-approving: ' + '; '.join(bad))

    def ask_same(self):
        csv, db = self.ctx.get('csv_answers'), self.ctx.get('db_answers')
        x = self.ctx.get('xlsx_answers')
        check(csv and db and csv == db, 'SQLite answers differ from CSV: %s' % compare(db or {}, csv or {}))
        if x is None: return 'CSV = SQLite (Excel not run)'
        check(x == csv, 'Excel answers differ from CSV: %s' % compare(x, csv))
        return 'CSV = Excel = SQLite'

    def src_broken(self):
        bad = self.env.home / 'Downloads' / 'broken files'; bad.mkdir(parents=True, exist_ok=True)
        (bad / 'empty.csv').write_text(''); (bad / 'dupes.csv').write_text('id,name,name\n1,a,b\n')
        (bad / 'good.csv').write_text('code_id,label\nX1,a\nX2,b\nX3,c\n')
        self.app.open(self.env.work / 'messy files', 'create')
        j = self.app.add({'type': 'csv', 'name': 'Broken', 'path': str(bad)})
        check(j['status'] == 'done', 'job failed: %s' % j.get('error'))
        check(len(j['result']['profile_failures'] or []) == 2, 'profile failures reported: %s' % j['result']['profile_failures'])

    def src_driver(self):
        st = self.app.ok('/api/state')
        if st['drivers']['sqlserver']: raise NotRun('pymssql is installed in the user environment, so the missing-driver message cannot be shown')
        code, d = self.app.call('/api/sources/test', {'kind': 'sqlserver', 'host': 'localhost', 'database': 'master'})
        check(code == 400 and 'binggio[sqlserver]' in d.get('error', ''), 'got %s %s' % (code, d))

    # =============================================================== collections source on the sample bank
    def disc_collections(self):
        self.ctx['col_ws'] = self.env.work / 'bank with collections'
        self.app.open(self.ctx['col_ws'], 'demo')
        j = self.app.add({'type': 'csv', 'name': 'Collections system', 'path': str(FIXTURE)})
        check(j['status'] == 'done', 'job failed: %s' % j.get('error'))
        self.ctx['col_cid'] = cid = j['result']['connection']
        rels = [x for x in self._rels() if cid in (x['child_connection'], x['parent_connection'])]
        got = {(x['child_table'], x['child_column'], x['parent_table'], x['parent_column']): x for x in rels}
        must = {('collection_cases', 'loan_ref', 'loan_accounts', 'loan_id'), ('collection_cases', 'cust_no', 'customer_master', 'CUSTOMER_ID'),
                ('collection_cases', 'assigned_agent', 'collection_agents', 'agent_id'), ('promises_to_pay', 'case_id', 'collection_cases', 'case_id'),
                ('collection_actions', 'case_id', 'collection_cases', 'case_id'), ('legal_notices', 'case_id', 'collection_cases', 'case_id'),
                ('recoveries', 'loan_id', 'loan_accounts', 'loan_id'), ('settlements', 'loan_id', 'loan_accounts', 'loan_id'),
                ('loan_collateral', 'loan_id', 'loan_accounts', 'loan_id'), ('customer_contacts', 'customer_id', 'customer_master', 'CUSTOMER_ID'),
                ('borrower_profile', 'CUST_NO', 'customer_master', 'CUSTOMER_ID')}
        check(must <= set(got), 'missing links: %s' % (must - set(got)))
        roles = {(x['table'], x['column']): x['suggested'] for x in self._roles() if x['connection'] == cid}
        pk = {t: c for (t, c), r in roles.items() if r == 'PRIMARY_KEY_CANDIDATE'}
        exp = {'collection_cases': 'case_id', 'collection_actions': 'action_id', 'collection_agents': 'agent_id', 'promises_to_pay': 'ptp_id',
               'recoveries': 'recovery_id', 'settlements': 'settlement_id', 'legal_notices': 'notice_id', 'loan_collateral': 'collateral_id',
               'customer_contacts': 'customer_id', 'borrower_profile': 'CUST_NO'}
        check(pk == exp, 'key suggestions differ: %s' % {t: (pk.get(t), c) for t, c in exp.items() if pk.get(t) != c})
        extra = {k: v for k, v in got.items() if k not in must}
        strong = {k: v['score'] for k, v in extra.items() if v['score'] >= 0.9}
        mid = {k: v['score'] for k, v in extra.items() if 0.85 <= v['score'] < 0.9}
        check(not strong, 'unexpected strong links: %s' % strong)
        if mid: raise Ambiguous('links outside the verified list that would be pre-ticked: %s' % mid)
        return '11 verified links, 10 of 10 keys; %d weaker link(s) left for a person' % len(extra)

    def onb_duplicate(self):
        p = self.app.ok('/api/onboard?connection=' + self.ctx['col_cid']); self.ctx['col_props'] = p
        kinds = {t['table']: t['kind'] for t in p['impact']['tables']}
        check(kinds.get('borrower_profile') == 'duplicate', 'borrower_profile classified as %s' % kinds.get('borrower_profile'))
        check('borrower_profile' not in p['roles'], 'duplicate table offered for approval')

    def onb_weak(self):
        p = self.ctx['col_props']
        weak = [l for l in p['links'] if l['score'] < 0.85]
        if not weak: raise NotRun('no link under 0.85 in this run')
        check(not any(l['suggested'] for l in weak), 'a weak link is pre-ticked')
        self.app.onboard_all(self.ctx['col_cid'])
        still = {x['edge_id']: x['status'] for x in self._rels()}
        check(all(still.get(l['edge_id']) == 'pending' for l in weak), 'a weak link was approved')
        return '%d weak link(s) left waiting' % len(weak)

    # =============================================================== big table
    def perf_big(self):
        if 'big_cid' in self.ctx: return 'already run'
        src, exp = datasets.big_csv(self.env.home / 'Downloads' / 'big export')
        self.ctx['big_rows'] = 300_000
        self.app.open(self.env.work / 'big bank', 'create')
        t = {}
        t0 = time.time(); j = self.app.add({'type': 'csv', 'name': 'Big', 'path': str(src)}); t['add, profile, discover'] = time.time() - t0
        check(j['status'] == 'done', 'job failed: %s' % j.get('error'))
        cid = self.ctx['big_cid'] = j['result']['connection']
        t0 = time.time(); self.app.onboard_all(cid); t['onboard'] = time.time() - t0
        n = self.app.names(); T = lambda tb, c=None: cid + '::' + n[tb] + ('::' + n[(tb, c)] if c else '')
        plan = {'entity': T('loans'), 'select': [], 'filters': [{'field': T('loans', 'status'), 'op': 'eq', 'value': 'ACTIVE'}],
                'where': None, 'exists': [], 'aggregate': [{'fn': 'sum', 'field': T('loans', 'amount'), 'alias': 't'}],
                'group_by': [T('customers', 'segment')], 'sort': [], 'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
        t0 = time.time(); a = self.app.ask('total by segment', plan)['answer']; t['ask'] = time.time() - t0
        got = {r[0]: float(r[1]) for r in a.get('rows', [])}
        check(got == {k: float(v) for k, v in exp.items()}, 'totals wrong: %s vs %s' % (got, exp))
        limits = {'add, profile, discover': 90, 'onboard': 30, 'ask': 30}
        slow = {k: round(v, 1) for k, v in t.items() if v > limits[k]}
        if any(v > 3 * limits[k] for k, v in t.items()): raise Fail('far too slow: %s' % slow)
        if slow: raise Ambiguous('correct, but slower than the target: %s seconds (targets %s)' % (slow, limits))
        return 'exact; seconds %s' % {k: round(v, 1) for k, v in t.items()}

    def ask_long(self):
        if 'big_cid' not in self.ctx: self.perf_big()
        cid, n = self.ctx['big_cid'], self.app.names()
        plan = {'entity': cid + '::' + n['loans'], 'select': [cid + '::' + n['loans'] + '::' + n[('loans', 'loan_id')]], 'filters': [],
                'where': None, 'exists': [], 'aggregate': [], 'group_by': [], 'sort': [], 'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
        a = self.app.ask('all loans', plan)['answer']
        check(a['status'] == 'answered' and len(a['rows']) == 200, 'rows shown %s' % len(a.get('rows', [])))
        if a['row_count'] != 300_000:
            raise Ambiguous('200 rows shown; the total given is %s, not 300,000. Is it the full count or the cut-off?' % a['row_count'])

    # =============================================================== MCP
    def mcp_agent(self):
        cmd = [str(self.env.exe), '-w', str(self.ctx['demo']), 'mcp']
        if not WINDOWS and self.env.mode == 'copy': cmd = [str(self.env.py)] + cmd
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding='utf-8', env=self.env.env())
        n = [0]
        def rpc(method, params=None):
            n[0] += 1
            p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': n[0], 'method': method, 'params': params or {}}) + '\n'); p.stdin.flush()
            return json.loads(p.stdout.readline())
        tool = lambda name, **a: rpc('tools/call', {'name': name, 'arguments': a})['result']
        try:
            init = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'uat-agent', 'version': '1'}})
            check(init['result']['serverInfo']['name'] == 'binggio', 'initialize: %s' % init)
            p.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n'); p.stdin.flush()
            names = {t['name'] for t in rpc('tools/list')['result']['tools']}
            check(names == {'describe_approved', 'run_plan', 'ask', 'status', 'propose_term'}, 'tools: %s' % names)
            check(len(tool('describe_approved')['structuredContent']['tables']) == 63, 'describe_approved does not list the 63 approved tables')
            g = GOLD['Q1']
            a = tool('run_plan', plan=g['plans'][0], question=g['question'])['structuredContent']
            check(a['status'] == 'answered' and golden_ok('Q1', a['rows']) and a['sources'], 'run_plan: %s' % str(a)[:300])
            bad = dict(g['plans'][0]); bad['select'] = ['csv_clean::customer_master::INVENTED']
            r = tool('run_plan', plan=bad, question='x')['structuredContent']
            check(r['status'] == 'refused', 'an invented column was not refused')
            t = tool('propose_term', table='conn_31ca3d841ed240::loan_accounts', column='loan_product', value='AUTO',
                     words=['car loan'], reason='UAT')['structuredContent']
            check(t['status'] == 'draft', 'proposal not saved as a draft')
        finally:
            p.stdin.close()
            try: p.wait(10)
            except subprocess.TimeoutExpired: p.kill()
        return 'initialize, 5 tools, verified answer, invented column refused, proposal left as a draft'

    # =============================================================== servers
    def src_server(self, kind, spec):
        kv = dict(p.split('=', 1) if '=' in p else (p, '1') for p in spec.split(';') if p)
        body = {'kind': kind, 'host': kv.get('host'), 'port': kv.get('port'), 'database': kv.get('database') or kv.get('service'),
                'schema': kv.get('schema'), 'user': kv.get('user'), 'windows_auth': 'windows' in kv}
        if kv.get('user') and 'windows' not in kv:
            body['password'] = os.environ.get('BINGGIO_DB_PASSWORD') or getpass.getpass('%s password for %s (not stored): ' % (kind, kv['user']))
        st = self.app.ok('/api/state')
        if not st['drivers'][kind]:
            raise NotRun('the %s driver is not installed in the user environment (pip install binggio[%s])' % (kind, kind))
        listed = self.app.ok('/api/sources/test', body)['tables']
        check(listed, 'connected but no tables listed')
        tables = [t['table'] for t in listed][:int(kv.get('max', 10))]
        self.app.open(self.env.work / ('%s bank' % kind), 'create')
        j = self.app.add({**body, 'type': 'database', 'name': kind, 'tables': tables})
        check(j['status'] == 'done', 'add failed: %s' % j.get('error'))
        if j['result']['tables'] != len(tables):
            raise Ambiguous('listed %d tables but copied %d (permissions?)' % (len(tables), j['result']['tables']))
        return '%d tables listed, %d copied read-only' % (len(listed), j['result']['tables'])

    # =============================================================== own words (model)
    CLEAR = [
        ('Q1', 'Which customers in the PREMIUM segment have a delinquent loan and have also raised a complaint that is currently ESCALATED? Show their customer ID and name.'),
        ('Q2', 'Which delinquent loans have an outstanding balance above 5,000,000 and belong to customers in the PREMIUM segment? Show the loan ID, loan product and outstanding balance, with the largest balance first.'),
        ('Q3', 'Which customers have a risk score above 50, hold at least one account with status OPEN and have raised at least one complaint, but have never had a service ticket with status ESCALATED?'),
        ('Q4', 'What is the total outstanding balance of all delinquent loans for each loan product?'),
        ('Q5', 'Which loans are both restructured and delinquent? List each loan with its customer reference and outstanding balance, sorted from the highest balance to the lowest.'),
        ('Q6', 'Which customers living in Mumbai have a delinquent loan and have raised at least one complaint? Show their customer ID and name.'),
        ('Q7', 'How many customers in each segment have at least one delinquent loan?'),
        ('Q8', 'Which customers with a delinquent loan also have a fraud signal score above 80 and a risk score above 50?')]
    SLOPPY = [
        ('Q1', 'premium custmers with delinquent loan + escalated complaint'),
        ('Q4', 'total outstanding delinquent loans by product'),
        ('Q7', 'how many cust per segment w/ a delinquent loan'),
        ('Q6', 'mumbai ppl with delinquent loans who complained'),
        ('Q5', 'restructured and delinquent loans biggest balance first'),
        ('Q8', 'delinquent loan customers fraud score >80 risk >50')]

    def _judge_nl(self, gid, question, a):
        """correct / partly / wrong / refused, comparing with the verified answer."""
        if a['status'] != 'answered': return 'refused', a.get('message', '')
        rows = a['rows']; exp = GOLD[gid]['expected']
        if golden_ok(gid, rows): return 'correct', ''
        if isinstance(exp, dict):
            return 'wrong', 'got %s' % rows[:6]
        expected = set(exp if gid != 'Q10' else exp[2])
        found = {str(c).strip() for r in rows for c in r} & expected
        if found == expected: return 'partly', 'all %d expected rows present, plus %d other rows / columns %s' % (len(expected), max(0, len(rows) - len(expected)), a['columns'])
        if found: return 'partly', '%d of %d expected rows found' % (len(found), len(expected))
        return 'wrong', 'none of the expected rows'

    def _nl_batch(self, items):
        res = []
        for gid, q in items:
            conv = self.app.ok('/api/ask/new', {})['conversation']
            a = self.app.ask(q, None, conv)['answer']
            v, why = self._judge_nl(gid, q, a)
            res.append({'question': q, 'expected': GOLD[gid]['question'], 'verdict': v, 'why': why,
                        'steps': a.get('explanation'), 'message': a.get('message')})
        wrong = [r for r in res if r['verdict'] == 'wrong']
        unsure = [r for r in res if r['verdict'] in ('partly', 'refused')]
        if wrong: raise Fail('%d wrong: %s' % (len(wrong), [r['question'][:60] for r in wrong]))
        if unsure: raise Ambiguous('%d of %d need a look' % (len(unsure), len(res)), evidence=unsure)
        return '%d of %d correct' % (len(res), len(res))

    def nl_clear(self): return self._nl_batch(self.CLEAR)
    def nl_sloppy(self): return self._nl_batch(self.SLOPPY)

    def _expect_refusal(self, questions, label):
        answered = []
        for q in questions:
            conv = self.app.ok('/api/ask/new', {})['conversation']
            a = self.app.ask(q, None, conv)['answer']
            if a['status'] == 'answered': answered.append({'question': q, 'steps': a.get('explanation'), 'rows': a.get('row_count'), 'columns': a.get('columns')})
        if answered: raise Ambiguous('%d of %d were answered instead of refused (%s); check the meaning it chose' % (len(answered), len(questions), label), evidence=answered)
        return 'all refused'

    def nl_two_meanings(self): return self._expect_refusal(['Show revenue', 'What is the KYC status of our customers?'], 'two possible meanings')
    def nl_unanswerable(self): return self._expect_refusal(['What is the credit card limit of each customer?', 'List employee salaries by branch'], 'data not present')

    def nl_repeat(self):
        q = self.CLEAR[3][1]; out = []
        for _ in range(2):
            conv = self.app.ok('/api/ask/new', {})['conversation']
            out.append(self.app.ask(q, None, conv)['answer'].get('rows'))
        if out[0] != out[1]: raise Ambiguous('the same question gave different rows', evidence={'first': out[0], 'second': out[1]})

    def nl_followup(self):
        conv = self.app.ok('/api/ask/new', {})['conversation']
        a1 = self.app.ask('Show the top 10 loans by outstanding balance.', None, conv)['answer']
        a2 = self.app.ask('Which of these loans are delinquent?', None, conv)['answer']
        if a1['status'] != 'answered': raise Ambiguous('first step refused: %s' % a1.get('message'))
        if a2['status'] != 'answered': raise Ambiguous('follow-up refused: %s' % a2.get('message'))
        check(a1['row_count'] == 10 and a2['row_count'] == 9, 'row counts %s then %s (expected 10 then 9)' % (a1['row_count'], a2['row_count']))

    # =============================================================== safety and wrap-up
    def safe_key(self):
        key = self.key if (self.args.with_model and self.key) else 'sk-uat-' + secrets.token_hex(16)
        self.app.ok('/api/settings/model', {'key': key})
        st = self.app.ok('/api/state')['model']
        check(st['configured'], 'key not taken')
        hits = files_containing([self.env.home, self.env.work], key)
        self.app.ok('/api/settings/model', {'clear': True})
        check(not hits, 'the model key was written to: %s' % hits[:3])

    def src_untouched(self):
        changed = []
        for k in ('src_hash_before', 'xlsx_hash', 'db_hash'):
            before = self.ctx.get(k)
            if not before: continue
            for f, h in before.items():
                if hashlib.sha256(Path(f).read_bytes()).hexdigest() != h: changed.append(f)
        check(not changed, 'original files changed: %s' % changed)
        return 'CSV folder, workbook and database unchanged'

    def safe_uninstall(self):
        if self.env.mode != 'wheel': raise NotRun('sandbox (copy) mode has nothing to uninstall')
        r = subprocess.run([str(self.env.py), '-m', 'pip', 'uninstall', '-y', 'binggio'], capture_output=True, text=True, env=self.env.env())
        check(r.returncode == 0 and not self.env.exe.exists(), 'uninstall failed or the command remains')
        check((self.ctx['csv_ws'] / 'binggio.json').exists(), 'a workspace was removed')

    # =============================================================== browser
    def browser_cases(self):
        ids_ = ['UI-01', 'UI-02', 'UI-03', 'UI-04', 'UI-05']
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            for c in ids_: self.skip(c, 'needs Playwright: pip install playwright, then playwright install chromium')
            return
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch()
            except Exception as e:
                for c in ids_: self.skip(c, 'Playwright installed but no browser: playwright install chromium (%s)' % str(e).splitlines()[0][:60])
                return
            try:
                self.app.open(self.ctx['demo'])
                self.run('UI-01', self.ui_screens, b)
                self.run('UI-02', self.ui_phone, b)
                self.run('UI-04', self.ui_download, b)
                self.run('UI-05', self.ui_keyboard, b)
                self.run('UI-03', self.ui_clickthrough, b)
            finally:
                b.close()

    def _page(self, b, **kw):
        p = b.new_page(viewport={'width': 1400, 'height': 900}, accept_downloads=True, **kw)
        p._errors = []; p.on('pageerror', lambda e: p._errors.append(str(e)))
        p.goto(self.app.url); p.evaluate("localStorage.setItem('binggio.by', JSON.stringify('UAT Agent'))")
        return p

    def ui_screens(self, b):
        p = self._page(b); empty = []
        shots = self.root / 'screens'; shots.mkdir(exist_ok=True)
        for h in ('home', 'sources', 'explore', 'onboard', 'review/roles/all', 'review/relationships/all', 'review/terms', 'graph', 'ask', 'activity', 'settings'):
            p.goto(self.app.url + '#' + h)
            # poll from Python: the app's security policy forbids evaluating strings in the page
            for _ in range(75):
                txt = p.inner_text('#page') if p.query_selector('#page') else ''
                if txt.strip() and not txt.strip().startswith('Loading'): break
                p.wait_for_timeout(200)
            else:
                empty.append(h)
            p.screenshot(path=str(shots / (h.replace('/', '_') + '.png')))
        check(not p._errors, 'script errors: %s' % p._errors[:3])
        check(not empty, 'screens still loading after 15 s: %s' % empty)
        return 'all 11 screens; screenshots in %s' % shots

    def ui_phone(self, b):
        p = b.new_page(viewport={'width': 390, 'height': 844}); p.goto(self.app.url + '#review/roles'); p.wait_for_timeout(2500)
        w = p.evaluate('document.documentElement.scrollWidth')
        check(w <= 392, 'page is %d px wide at 390 px' % w)

    def ui_download(self, b):
        p = self._page(b); p.goto(self.app.url + '#ask'); p.wait_for_selector('.samples button')
        p.click('.samples button[data-s="0"]'); p.wait_for_selector('[data-csv]', timeout=30000)
        with p.expect_download() as dl: p.click('[data-csv]')
        text = Path(dl.value.path()).read_text(encoding='utf-8-sig').strip().splitlines()
        check(len(text) - 1 == len(GOLD['Q1']['expected']), 'CSV has %d rows (expected %d)' % (len(text) - 1, len(GOLD['Q1']['expected'])))

    def ui_keyboard(self, b):
        p = self._page(b); p.goto(self.app.url + '#ask'); p.wait_for_selector('#qbox')
        p.fill('#qbox', 'Which customers have a delinquent loan?'); p.press('#qbox', 'Enter')
        p.wait_for_selector('.a.refusal, .a .ah b', timeout=30000)
        p.click('#chip-by'); p.wait_for_selector('.modal-bg'); p.keyboard.press('Escape'); p.wait_for_timeout(300)
        check(p.query_selector('.modal-bg') is None, 'Esc did not close the dialog')

    def ui_clickthrough(self, b):
        src = datasets.write_csv(self.ctx['data'], self.env.home / 'Downloads' / 'click export')
        self.app.stop(); self.app = App(self.env)   # back to the start screen
        p = self._page(b); p.goto(self.app.url); p.wait_for_selector('#go-new')
        p.click('#go-new'); p.wait_for_selector('#fs-path'); p.fill('#fs-path', str(self.env.work)); p.click('#fs-go'); p.wait_for_timeout(400)
        p.click('#fs-use'); p.wait_for_selector('#m-in'); p.fill('#m-in', 'clicked bank'); p.click('.modal .btn.primary')
        p.wait_for_selector('#f-browse', timeout=15000)
        p.click('#f-browse'); p.wait_for_selector('#fs-path'); p.fill('#fs-path', str(src)); p.click('#fs-go'); p.wait_for_timeout(400); p.click('#fs-use')
        p.click('#f-add'); p.wait_for_selector('text=Review what this source adds', timeout=120000)
        p.click('text=Review what this source adds'); p.wait_for_selector('#ob-apply', timeout=60000)
        p.click('#ob-apply'); p.wait_for_selector('#ob-res .note-box', timeout=60000)
        txt = p.inner_text('#ob-res')
        check('Approved 21 column roles, 3 links' in txt, 'onboarding result: %s' % txt[:200])
        check(not p._errors, 'script errors: %s' % p._errors[:3])
        self.app.open(self.ctx['demo'])


# ====================================================================== optional second opinion

def judge(key, case, result):
    model = os.environ.get('BINGGIO_LLM_MODEL', 'gpt-4.1-mini')
    prompt = ('You review an automated acceptance test of a data question-answering tool that must never guess. '
              'Say in two sentences whether the behaviour looks acceptable for release, and start with one word: '
              'ACCEPTABLE, UNACCEPTABLE or UNSURE.\n\nCase: %s\nExpected: %s\nWhat happened: %s\nDetails: %s'
              % (case['title'], case['expected'], result['detail'], json.dumps(result.get('evidence'), default=str)[:6000]))
    body = json.dumps({'model': model, 'temperature': 0, 'messages': [{'role': 'user', 'content': prompt}]}).encode()
    req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=body,
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())['choices'][0]['message']['content'].strip()
    except Exception as e:
        return 'second opinion unavailable: %s' % type(e).__name__


# ====================================================================== report

def write_report(agent, started):
    results = agent.results; cases = by_id()
    for c in CASES:
        if c['id'] not in results: results[c['id']] = {'id': c['id'], 'status': 'NOT RUN', 'detail': 'not reached', 'evidence': None, 'seconds': 0}
    if agent.args.judge and agent.key:
        for r in results.values():
            if r['status'] == 'AMBIGUOUS': r['second_opinion'] = judge(agent.key, cases[r['id']], r)
    count = {s: sum(r['status'] == s for r in results.values()) for s in ('PASS', 'FAIL', 'AMBIGUOUS', 'NOT RUN')}
    stamp = time.strftime('%Y%m%d_%H%M%S'); out = HERE / 'reports'; out.mkdir(exist_ok=True)
    order = [c['id'] for c in CASES]
    md = ['# Binggio UAT: cases that need you', '',
          '%s · Python %s · %s mode · %d cases: **%d passed, %d failed, %d ambiguous, %d not run** · %.0f s' % (
              time.strftime('%d %b %Y %H:%M'), sys.version.split()[0], agent.args.mode, len(CASES),
              count['PASS'], count['FAIL'], count['AMBIGUOUS'], count['NOT RUN'], time.time() - started), '']
    amb = [results[i] for i in order if results[i]['status'] == 'AMBIGUOUS']
    md += ['## Needs your judgement (%d)' % len(amb), '']
    if not amb: md += ['Nothing. Every case the agent ran was decided on its own.', '']
    for r in amb:
        c = cases[r['id']]
        md += ['### %s · %s' % (r['id'], c['title']), '', '- **Expected:** %s' % c['expected'],
               '- **What happened:** %s' % r['detail'].replace('\n', ' '),
               '- **Why the agent could not decide:** %s' % (c['ambiguous_when'] or 'outcome outside its rules')]
        if r.get('second_opinion'): md.append('- **Model\'s second opinion:** %s' % r['second_opinion'].replace('\n', ' '))
        if r.get('evidence'): md += ['', '```', json.dumps(r['evidence'], indent=1, default=str, ensure_ascii=False)[:3000], '```']
        md += ['- **Your verdict:** pass / fail (write it in the spreadsheet)', '']
    fails = [results[i] for i in order if results[i]['status'] == 'FAIL']
    md += ['## Failed (%d): these block the release' % len(fails), '']
    md += ['- **%s** %s: %s' % (r['id'], cases[r['id']]['title'], r['detail'].replace('\n', ' ')[:400]) for r in fails] or ['None.']
    nr = [results[i] for i in order if results[i]['status'] == 'NOT RUN']
    md += ['', '## Not run (%d)' % len(nr), '']
    md += ['- **%s** %s: %s' % (r['id'], cases[r['id']]['title'], r['detail']) for r in nr] or ['None.']
    md += ['', '%d cases passed and are not listed. Full results: uat_%s.json' % (count['PASS'], stamp)]
    (out / ('uat_%s.md' % stamp)).write_text('\n'.join(md) + '\n', encoding='utf-8')
    (out / ('uat_%s.json' % stamp)).write_text(json.dumps([results[i] for i in order], indent=1, default=str), encoding='utf-8')
    xlsx = None
    try:
        import make_sheet
        xlsx = make_sheet.build(out / ('UAT_results_%s.xlsx' % stamp), results)
    except ImportError:
        pass
    return out / ('uat_%s.md' % stamp), xlsx, count, amb, fails


def main():
    ap = argparse.ArgumentParser(description='Binggio UAT agent')
    ap.add_argument('--mode', choices=['wheel', 'copy'], default='wheel')
    ap.add_argument('--with-model', action='store_true'); ap.add_argument('--judge', action='store_true')
    ap.add_argument('--sqlserver'); ap.add_argument('--oracle'); ap.add_argument('--postgres')
    ap.add_argument('--keep', action='store_true')
    args = ap.parse_args()
    started = time.time()
    agent = Agent(args)
    print('Simulated machine:', agent.root, flush=True)
    try:
        agent.go()
    finally:
        md, xlsx, count, amb, fails = write_report(agent, started)
        print('\n%d passed, %d failed, %d ambiguous, %d not run.' % (count['PASS'], count['FAIL'], count['AMBIGUOUS'], count['NOT RUN']))
        for r in amb: print('  NEEDS YOU  %s  %s' % (r['id'], r['detail'].splitlines()[0][:110]))
        for r in fails: print('  FAILED     %s  %s' % (r['id'], r['detail'].splitlines()[0][:110]))
        print('Report:', md)
        if xlsx: print('Spreadsheet with results:', xlsx)
        if args.keep: print('Simulated machine kept at', agent.root)
        else: shutil.rmtree(agent.root, ignore_errors=True)
    sys.exit(1 if count['FAIL'] else 0)


if __name__ == '__main__':
    main()
