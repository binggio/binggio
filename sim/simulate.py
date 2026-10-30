# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Simulate a new user installing and using Binggio, end to end.

    python sim/simulate.py                 # build, install into a fresh virtual environment, run every journey
    python sim/simulate.py --with-model    # also ask the demo questions in plain words (needs BINGGIO_LLM_API_KEY; ~15 model calls)
    python sim/simulate.py --keep          # keep the temporary folder to look around afterwards
    python sim/simulate.py --only demo,csv # run some journeys

What it imitates:
  * a clean machine: a new virtual environment, a separate home folder, no PYTHONPATH, no access
    to this repository's source (the installed package is what runs);
  * the install a user does: build the wheel, `pip install binggio-*.whl`, `pip install openpyxl`
    for Excel (Excel journeys are skipped, and reported, when it cannot be installed);
  * what a user types: every step runs the installed `binggio` command, including the interactive
    `onboard` and `ask`, with answers fed through the keyboard;
  * folders with spaces and non-English characters; messy files; moving the workspace; uninstall.

Every answer is compared with counts computed independently from the generated data
(sim/datasets.py). The report goes to sim/reports/ as Markdown and JSON.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import datasets  # noqa: E402

WINDOWS = os.name == 'nt'


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond: raise Fail(msg)


class Env:
    """A fresh 'user machine': virtual environment + home folder + working folder."""

    def __init__(self, root: Path, mode: str):
        self.root, self.mode = root, mode
        self.venv = root / 'venv'
        self.home = root / 'home user'                      # a space in the path, like C:\Users\First Last
        self.work = self.home / 'Documents' / 'बिंगियो work'  # and non-English characters
        self.work.mkdir(parents=True)
        self.notes = []
        self.excel = False

    @property
    def py(self):
        return self.venv / ('Scripts/python.exe' if WINDOWS else 'bin/python')

    @property
    def exe(self):
        return self.venv / ('Scripts/binggio.exe' if WINDOWS else 'bin/binggio')

    def env(self, extra=None):
        e = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'BINGGIO_LLM_API_KEY', 'OPENAI_API_KEY',
                                                               'BINGGIO_PHASE10_LLM_API_KEY', 'VIRTUAL_ENV')}
        e.update({'HOME': str(self.home), 'USERPROFILE': str(self.home), 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'})
        e.update(extra or {})
        return e

    # ---------------------------------------------------------------- install
    def install(self):
        if self.mode == 'wheel':
            # Build from a clean copy, like a release: a leftover build/ folder in the repository can
            # put stale files into the wheel (setuptools keeps a build/lib copy whose time is newer).
            dist, clean = self.root / 'dist', self.root / 'clean source'
            shutil.copytree(REPO / 'src', clean / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', '*.pyc'))
            for f in ('pyproject.toml', 'README.md', 'LICENSE'):
                if (REPO / f).exists(): shutil.copy2(REPO / f, clean / f)
            r = subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(clean), '--no-deps', '-w', str(dist), '-q'],
                               capture_output=True, text=True)
            if r.returncode:
                r = subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(clean), '--no-deps', '--no-build-isolation', '-w', str(dist), '-q'],
                                   capture_output=True, text=True)
            check(r.returncode == 0, 'building the wheel failed:\n' + r.stderr[-2000:])
            wheels = list(dist.glob('binggio-*.whl'))
            check(wheels, 'no binggio wheel was built (got %s). The local setuptools is too old for pyproject.toml '
                          '(needs 68+: pip install -U setuptools), or pip could not reach PyPI to fetch it. '
                          'Use --mode copy on machines without internet.' % [p.name for p in dist.glob('*.whl')])
            wheel = wheels[0]
            venv.EnvBuilder(with_pip=True, clear=True).create(self.venv)
            r = subprocess.run([str(self.py), '-m', 'pip', 'install', '-q', str(wheel)], capture_output=True, text=True, env=self.env())
            check(r.returncode == 0, 'pip install failed:\n' + r.stderr[-2000:])
            r = subprocess.run([str(self.py), '-m', 'pip', 'install', '-q', 'openpyxl'], capture_output=True, text=True, env=self.env())
            self.excel = r.returncode == 0
            if not self.excel: self.notes.append('openpyxl could not be installed (offline?): Excel journeys skipped')
            self.notes.append('Installed %s into a new virtual environment' % wheel.name)
        else:
            # Sandbox fallback: no network for pip. Same layout as an install: the package copied
            # into the environment's site-packages and a `binggio` launcher; nothing else on the path.
            venv.EnvBuilder(with_pip=False, clear=True, system_site_packages=True).create(self.venv)
            site = next(self.venv.glob('lib/python*/site-packages')) if not WINDOWS else self.venv / 'Lib' / 'site-packages'
            shutil.copytree(REPO / 'src' / 'binggio', site / 'binggio', ignore=shutil.ignore_patterns('__pycache__'))
            self.exe.write_text('#!%s\nimport sys\nfrom binggio.cli import main\nsys.exit(main())\n' % self.py)
            self.exe.chmod(0o755)
            try:
                import openpyxl  # noqa: F401
                self.excel = True
            except ImportError:
                self.notes.append('openpyxl not available: Excel journeys skipped')
            self.notes.append('Copy-install (sandbox mode): package copied into site-packages, launcher created')
        r = self.run(['--version'])
        check(r.returncode == 0 and 'binggio' in r.stdout, 'binggio --version failed')
        r = subprocess.run([str(self.py), '-c', 'import binggio,sys;print(binggio.__file__)'], capture_output=True, text=True,
                           env=self.env(), cwd=str(self.work))
        check(str(REPO / 'src') not in r.stdout, 'the repository source was imported instead of the installed package')
        # every installed file must be byte-identical to the source (catches stale builds)
        installed = Path(r.stdout.strip()).parent
        src = REPO / 'src' / 'binggio'
        missing, differ = [], []
        for f in src.rglob('*'):
            if f.is_dir() or '__pycache__' in f.parts or f.suffix == '.pyc': continue
            g = installed / f.relative_to(src)
            if not g.exists(): missing.append(f.relative_to(src).as_posix())
            elif g.read_bytes() != f.read_bytes(): differ.append(f.relative_to(src).as_posix())
        check(not missing and not differ, 'installed package differs from the source. Missing: %s. Different: %s. '
              '(Delete the build\\ folder before building a release.)' % (missing[:10], differ[:10]))
        self.notes.append('all %d installed files identical to the source' % sum(1 for f in src.rglob('*') if f.is_file() and '__pycache__' not in f.parts))

    # ---------------------------------------------------------------- running commands
    def run(self, args, cwd=None, input=None, extra_env=None, timeout=900):
        cmd = [str(self.exe)] + [str(a) for a in args]
        if not WINDOWS and self.mode == 'copy':
            cmd = [str(self.py), str(self.exe)] + [str(a) for a in args]
        return subprocess.run(cmd, cwd=str(cwd or self.work), input=input, capture_output=True, text=True,
                              encoding='utf-8', env=self.env(extra_env), timeout=timeout)

    def ok(self, args, cwd=None, **kw):
        r = self.run(args, cwd=cwd, **kw)
        check(r.returncode == 0, 'binggio %s failed (exit %d):\n%s\n%s' % (' '.join(map(str, args)), r.returncode, r.stdout[-1500:], r.stderr[-1500:]))
        return r


# ==================================================================== journeys

def names_and_conn(env, ws):
    rows = json.loads(env.ok(['review', 'roles', '--status', 'all', '--json'], cwd=ws).stdout)
    conns = sorted({r['connection'] for r in rows})
    out = {}
    for r in rows:
        out[r['table'].lower()] = r['table']; out[(r['table'].lower(), r['column'].lower())] = r['column']
    return conns, out


def ask_plan(env, ws, plan, question='saved plan'):
    f = ws / ('_plan_%d.json' % (abs(hash(json.dumps(plan, default=str))) % 10 ** 8))
    f.write_text(json.dumps(plan), encoding='utf-8')
    r = env.run(['ask', question, '--plan', f, '--json'], cwd=ws)
    out = json.loads(r.stdout) if r.stdout.strip().startswith(('{', '[')) else {'status': 'error', 'stderr': r.stderr}
    return out


def first_col(ans):
    return [str(row[0]).strip().upper() for row in ans.get('rows', [])]


def answers_for(env, ws, conn):
    _, names = names_and_conn(env, ws)
    P = datasets.plans(conn, names)
    got = {}
    a = ask_plan(env, ws, P['open balance by segment'])
    got['open balance by segment'] = {r[0]: round(float(r[1]), 2) for r in a.get('rows', [])}
    for k in ('customers with a MISSED repayment', 'customers with no account', 'Mumbai with PARTIAL'):
        got[k] = sorted(first_col(ask_plan(env, ws, P[k])))
    got['top 5 loans by amount'] = first_col(ask_plan(env, ws, P['top 5 loans by amount']))
    conv = ask_plan(env, ws, P['conversation'])
    if isinstance(conv, list):
        got['top 10 by balance'] = first_col(conv[0])
        got['top 10 then MISSED'] = sorted(first_col(conv[1])) if conv[1].get('status') != 'refused' else ('refused', conv[1].get('message'))
    else:
        got['top 10 by balance'] = got['top 10 then MISSED'] = ('error', conv)
    return got


def compare(got, exp, keys=None):
    bad = []
    for k in keys or exp:
        if k not in got: continue
        g, e = got[k], exp[k]
        if isinstance(e, dict):
            ok = isinstance(g, dict) and set(g) == set(e) and all(abs(g[x] - e[x]) < 0.01 for x in e)
        else:
            ok = g == e
        if not ok: bad.append('%s: got %s, expected %s' % (k, str(g)[:200], str(e)[:200]))
    return bad


def j_first_contact(env, ctx):
    r = env.run(['--help']); check(r.returncode == 0 and 'onboard' in r.stdout, '--help does not list onboard')
    r = env.run(['status']); check(r.returncode == 2 and 'No Binggio workspace' in r.stderr, 'status outside a workspace: unclear error')
    r = env.run(['add-csv', 'x', env.work / 'missing']); check(r.returncode == 2, 'add-csv outside a workspace should fail cleanly')
    return 'help, version and outside-workspace errors are clear'


def j_demo(env, ctx):
    env.ok(['demo', 'my bank'])
    ws = env.work / 'my bank'
    st = json.loads(env.ok(['status'], cwd=ws).stdout)
    check(st['registry']['approved'] == 429 and st['registry']['stale'] == 0, 'demo approvals: %s' % st['registry'])
    check(st['answerable']['tables'] == 63, 'demo answerable tables: %s' % st['answerable'])
    r = env.run(['ask', 'Which customers have a delinquent loan?'], cwd=ws)
    check(r.returncode == 3 and 'model key' in r.stdout, 'asking without a key should say a model key is needed: ' + r.stdout[-300:])
    r = env.run(['ask'], cwd=ws, input='new\nquit\n')
    check(r.returncode == 0 and 'New conversation' in r.stdout, 'interactive ask: new/quit did not work')
    gold = json.loads((REPO / 'tests' / 'golden' / 'demo_plans.json').read_text())
    D = {'select': [], 'filters': [], 'where': None, 'exists': [], 'aggregate': [], 'group_by': [], 'sort': [],
         'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
    bad = []
    for case in gold:
        steps = [{'question': case['question'] if i == 0 else 'Which of these? (step %d)' % i, 'plan': {**D, **p}} for i, p in enumerate(case['plans'])]
        a = ask_plan(env, ws, steps if len(steps) > 1 else steps[0]['plan'], case['question'])
        last = a[-1] if isinstance(a, list) else a
        if last.get('status') == 'refused' or 'rows' not in last: bad.append(case['id'] + ': ' + str(last)[:200]); continue
        exp = case['expected']
        if case['id'] in ('Q4', 'Q7'):
            ok = {r[0]: round(float(r[1]), 2) for r in last['rows']} == {k: round(float(v), 2) for k, v in exp.items()}
        elif case['id'] == 'Q10':
            ok = sorted(first_col(last)) == exp[2]
        elif case['id'] == 'Q9':
            ok = first_col(last) == exp
        else:
            ok = sorted(first_col(last)) == exp
        if not ok: bad.append(case['id'])
    check(not bad, 'demo questions wrong: %s' % bad)
    ctx['demo'] = ws
    return '429 approvals current; 10 of 10 demo questions correct; no-key and interactive behaviour clear'


def j_csv(env, ctx):
    data = datasets.build(); ctx['data'] = data; ctx['oracle'] = datasets.oracle(data)
    src = datasets.write_csv(data, env.home / 'Downloads' / 'core export (Oct)')
    env.ok(['init', 'retail bank']); ws = env.work / 'retail bank'
    r = env.ok(['add-csv', 'Core export', src], cwd=ws); check('4 tables' in r.stdout, r.stdout)
    r = env.ok(['profile'], cwd=ws); check('failures: 0' in r.stdout, r.stdout)
    env.ok(['discover'], cwd=ws)
    rels = json.loads(env.ok(['review', 'relationships', '--status', 'all', '--json'], cwd=ws).stdout)
    pairs = {(x['child'].split('.')[0], x['parent'].split('.')[0]) for x in rels}
    check({('accounts', 'customers'), ('loans', 'customers'), ('repayments', 'loans')} <= pairs, 'links found: %s' % pairs)
    roles = {(x['table'], x['column']): x['suggested'] for x in json.loads(env.ok(['review', 'roles', '--status', 'all', '--json'], cwd=ws).stdout)}
    pk = {t: c for (t, c), r in roles.items() if r == 'PRIMARY_KEY_CANDIDATE'}
    check(pk == {'customers': 'customer_id', 'accounts': 'account_id', 'loans': 'loan_id', 'repayments': 'repayment_id'}, 'key suggestions: %s' % pk)
    r = env.ok(['impact'], cwd=ws); check('ENTITY' in r.stdout, 'impact report missing classifications')
    r = env.ok(['onboard', '--by', 'Sim Steward', '--accept-suggestions'], cwd=ws)
    check('Approved:' in r.stdout, r.stdout[-500:])
    conns, _ = names_and_conn(env, ws)
    got = answers_for(env, ws, conns[0]); ctx['csv_answers'] = got
    bad = compare(got, ctx['oracle'])
    check(not bad, 'CSV answers wrong:\n  ' + '\n  '.join(bad))
    ctx['csv_ws'] = ws
    return 'messy CSVs (BOM, semicolons, CRLF, "1,234.50", " c0007 ", cust_ref) onboarded; %d answers match' % len(got)


def j_excel(env, ctx):
    if not env.excel: return 'SKIP: openpyxl not installed'
    # The workbook is written by the user's environment (which has openpyxl), not by the Python running the simulator.
    book = env.home / 'Downloads' / 'Bank report.xlsx'; book.parent.mkdir(parents=True, exist_ok=True)
    dj = env.root / 'excel_data.json'; dj.write_text(json.dumps(ctx['data']), encoding='utf-8')
    r = subprocess.run([str(env.py), '-c', 'import sys,json;sys.path.insert(0,sys.argv[1]);import datasets;'
                        'datasets.write_excel(json.load(open(sys.argv[2],encoding="utf-8")),__import__("pathlib").Path(sys.argv[3]))',
                        str(HERE), str(dj), str(book)], capture_output=True, text=True, env=env.env())
    check(r.returncode == 0, 'writing the test workbook failed: ' + r.stderr[-800:])
    env.ok(['init', 'excel bank']); ws = env.work / 'excel bank'
    r = env.ok(['add-excel', 'Bank report', book], cwd=ws); check('header found on row 3' in r.stdout, 'title rows not handled: ' + r.stdout)
    env.ok(['profile'], cwd=ws); env.ok(['discover'], cwd=ws)
    env.ok(['onboard', '--by', 'Sim Steward', '--accept-suggestions'], cwd=ws)
    conns, _ = names_and_conn(env, ws)
    got = answers_for(env, ws, conns[0])
    check(got == ctx['csv_answers'], 'Excel answers differ from CSV: %s' % compare(got, ctx['csv_answers']))
    return 'workbook with title rows gives the same answers as the CSV export'


def j_sqlite(env, ctx):
    db = datasets.write_sqlite(ctx['data'], env.home / 'Downloads' / 'bank.db')
    env.ok(['init', 'db bank']); ws = env.work / 'db bank'
    env.ok(['add-db', 'Core DB', 'sqlite', '--database', db], cwd=ws)
    env.ok(['profile'], cwd=ws); env.ok(['discover'], cwd=ws)
    env.ok(['onboard', '--by', 'Sim Steward', '--accept-suggestions'], cwd=ws)
    conns, _ = names_and_conn(env, ws)
    got = answers_for(env, ws, conns[0])
    check(got == ctx['csv_answers'], 'database answers differ from CSV: %s' % compare(got, ctx['csv_answers']))
    # refresh after a change in one table
    import sqlite3
    con = sqlite3.connect(db); con.execute("UPDATE accounts SET status='CLOSED' WHERE account_id='A00001'"); con.commit(); con.close()
    r = env.ok(['refresh', conns[0]], cwd=ws); check('Changed: accounts' in r.stdout, r.stdout)
    env.ok(['profile'], cwd=ws)
    st = json.loads(env.ok(['status'], cwd=ws).stdout)['registry']
    check(st['stale'] > 0, 'changed table did not make its approvals stale: %s' % st)
    a = ask_plan(env, ws, datasets.plans(conns[0], names_and_conn(env, ws)[1])['open balance by segment'])
    check(a.get('status') == 'refused', 'a stale table must not be answered from: %s' % str(a)[:200])
    return 'SQLite source matches CSV; refresh makes only the changed table stale, and Binggio then refuses it'


def j_interactive_onboard(env, ctx):
    src = datasets.write_csv(ctx['data'], env.home / 'Downloads' / 'second copy')
    env.ok(['init', 'hand reviewed']); ws = env.work / 'hand reviewed'
    env.ok(['add-csv', 'Export', src], cwd=ws); env.ok(['profile'], cwd=ws); env.ok(['discover'], cwd=ws)
    # A person: approves every table's roles, accepts suggested links and concepts, and gives words
    # for the first coded value offered; then presses Enter for the rest.
    answers = 'y\n' * 4 + '\n' * 12 + '\n' * 4 + 'closed account, account closed\n' + '\n' * 80
    r = env.run(['onboard', '--by', 'Sim Person'], cwd=ws, input=answers)
    check(r.returncode == 0, 'interactive onboard failed:\n' + r.stdout[-800:] + r.stderr[-800:])
    st = json.loads(env.ok(['status'], cwd=ws).stdout)
    check(st['answerable']['tables'] == 4 and st['answerable']['relationships'] >= 3, 'after interactive onboard: %s' % st['answerable'])
    return 'keyboard-driven onboard: %s tables, %s links, %s concepts, %s terms answerable' % (
        st['answerable']['tables'], st['answerable']['relationships'], st['answerable']['concepts'], st['answerable']['terms'])


def j_python_api(env, ctx):
    script = env.work / 'my_script.py'
    script.write_text('''import binggio, json, sys
ws = binggio.Workspace.open(sys.argv[1])
st = ws.status()
print(json.dumps({"tables": st["answerable"]["tables"], "approved": st["registry"]["approved"]}))
''', encoding='utf-8')
    r = subprocess.run([str(env.py), str(script), str(ctx['csv_ws'])], capture_output=True, text=True, env=env.env(), cwd=str(env.work))
    check(r.returncode == 0, 'Python API script failed: ' + r.stderr[-800:])
    out = json.loads(r.stdout)
    check(out['tables'] == 4, out)
    return 'import binggio from a user script works (%s)' % out


def j_move_workspace(env, ctx):
    dest = env.home / 'Desktop' / 'moved bank (copy)'
    shutil.copytree(ctx['csv_ws'], dest)
    st = json.loads(env.ok(['status'], cwd=dest).stdout)
    check(st['registry']['stale'] == 0, 'copying the workspace made approvals stale: %s' % st['registry'])
    conns, _ = names_and_conn(env, dest)
    got = answers_for(env, dest, conns[0])
    check(got == ctx['csv_answers'], 'answers changed after moving the workspace')
    return 'a copied workspace keeps every approval and gives the same answers'


def j_bad_inputs(env, ctx):
    bad = env.home / 'Downloads' / 'broken files'; bad.mkdir(parents=True)
    (bad / 'empty.csv').write_text('')
    (bad / 'dupes.csv').write_text('id,name,name\n1,a,b\n')
    (bad / 'good.csv').write_text('code_id,label\nX1,a\nX2,b\nX3,c\n')
    env.ok(['init', 'messy']); ws = env.work / 'messy'
    env.ok(['add-csv', 'Broken', bad], cwd=ws)
    r = env.run(['profile'], cwd=ws)
    check(r.returncode == 0 and 'failures: 2' in r.stdout, 'two broken files should be reported, not crash: ' + r.stdout + r.stderr[-500:])
    (env.home / 'old.xls').write_bytes(b'\xd0\xcf\x11\xe0')
    r = env.run(['add-excel', 'Old', env.home / 'old.xls'], cwd=ws)
    check(r.returncode == 2 and 'save as .xlsx' in r.stderr, 'old .xls: ' + r.stderr)
    r = env.run(['add-csv', 'None', env.home / 'nope'], cwd=ws)
    check(r.returncode == 2, 'missing folder should be a clean error')
    r = env.run(['init', str(ws)]); check(r.returncode == 2 and 'already exists' in r.stderr, 're-init should refuse')
    r = env.run(['ask'], cwd=ws, input='How many?\nquit\n')
    check('Nothing is approved yet' in r.stdout, 'asking with nothing approved should explain the next steps')
    return 'empty/duplicate-header files reported; .xls, missing folder, re-init and empty registry give clear messages'


def j_big(env, ctx):
    src, exp = datasets.big_csv(env.home / 'Downloads' / 'big export')
    env.ok(['init', 'big bank']); ws = env.work / 'big bank'
    t = {}
    for step in (['add-csv', 'Big', src], ['profile'], ['discover'], ['onboard', '--by', 'Sim', '--accept-suggestions']):
        t0 = time.time(); env.ok(step, cwd=ws); t[step[0]] = round(time.time() - t0, 1)
    conns, names = names_and_conn(env, ws)
    T = lambda tb, c=None: conns[0] + '::' + names[tb] + ('::' + names[(tb, c)] if c else '')
    plan = {'entity': T('loans'), 'select': [], 'filters': [{'field': T('loans', 'status'), 'op': 'eq', 'value': 'ACTIVE'}],
            'where': None, 'exists': [], 'aggregate': [{'fn': 'sum', 'field': T('loans', 'amount'), 'alias': 't'}],
            'group_by': [T('customers', 'segment')], 'sort': [], 'limit': 200, 'cohort': None, 'latest': None, 'terms': []}
    t0 = time.time(); a = ask_plan(env, ws, plan); t['ask'] = round(time.time() - t0, 1)
    got = {r[0]: float(r[1]) for r in a.get('rows', [])}
    check(got == {k: float(v) for k, v in exp.items()}, 'big-table totals wrong: %s vs %s' % (got, exp))
    return '300,000 loans: totals exact; seconds per step %s' % t


def j_model(env, ctx):
    if not ctx['with_model']: return 'SKIP: run with --with-model to ask in plain words (uses ~15 model calls)'
    key = os.environ.get('BINGGIO_LLM_API_KEY') or os.environ.get('OPENAI_API_KEY')
    check(key, 'set BINGGIO_LLM_API_KEY to run the model journey')
    questions = [
        ('Which customers in the PREMIUM segment have a delinquent loan and have also raised a complaint that is currently ESCALATED? Show their customer ID and name.', 'Q1'),
        ('Which delinquent loans have an outstanding balance above 5,000,000 and belong to customers in the PREMIUM segment? Show the loan ID, loan product and outstanding balance, with the largest balance first.', 'Q2'),
        ('Which customers have a risk score above 50, hold at least one account with status OPEN and have raised at least one complaint, but have never had a service ticket with status ESCALATED?', 'Q3'),
        ('What is the total outstanding balance of all delinquent loans for each loan product?', 'Q4'),
        ('Which loans are both restructured and delinquent? List each loan with its customer reference and outstanding balance, sorted from the highest balance to the lowest.', 'Q5'),
        ('Which customers living in Mumbai have a delinquent loan and have raised at least one complaint? Show their customer ID and name.', 'Q6'),
        ('How many customers in each segment have at least one delinquent loan?', 'Q7'),
        ('Which customers with a delinquent loan also have a fraud signal score above 80 and a risk score above 50?', 'Q8'),
    ]
    gold = {g['id']: g for g in json.loads((REPO / 'tests' / 'golden' / 'demo_plans.json').read_text())}
    ws = ctx['demo']; results = []
    for q, gid in questions:
        r = env.run(['ask', q, '--json'], cwd=ws, extra_env={'BINGGIO_LLM_API_KEY': key})
        a = json.loads(r.stdout) if r.stdout.strip().startswith('{') else {'status': 'error'}
        exp = gold[gid]['expected']
        if a.get('status') == 'refused' or 'rows' not in a: results.append((gid, 'REFUSED: ' + str(a.get('message'))[:120])); continue
        if gid in ('Q4', 'Q7'):
            ok = {row[0]: round(float(row[1]), 2) for row in a['rows']} == {k: round(float(v), 2) for k, v in exp.items()}
        else:
            ids = sorted({str(c).strip().upper() for row in a['rows'] for c in row if str(c).strip().upper() in set(exp)})
            ok = ids == exp
        results.append((gid, 'correct' if ok else 'WRONG'))
    bad = [x for x in results if x[1] != 'correct']
    check(not bad, 'plain-word questions: %s' % results)
    return '8 of 8 plain-word questions answered correctly'


def j_uninstall(env, ctx):
    if env.mode == 'wheel':
        r = subprocess.run([str(env.py), '-m', 'pip', 'uninstall', '-y', 'binggio'], capture_output=True, text=True, env=env.env())
        check(r.returncode == 0, 'uninstall failed')
        check(not env.exe.exists(), 'binggio command still present after uninstall')
    check((ctx['csv_ws'] / 'binggio.json').exists(), 'uninstall must not touch workspaces')
    return 'uninstall removes the command and leaves workspaces untouched' + ('' if env.mode == 'wheel' else ' (checked workspaces only in sandbox mode)')


def j_web_app(env, ctx):
    """`binggio ui` started as a user would (outside any workspace), then used through its API and,
    when Playwright is installed for this Python, through a real browser."""
    import re, urllib.request, urllib.error
    cmd = [str(env.exe), 'ui', '--no-browser', '--port', '0']
    if not WINDOWS and env.mode == 'copy': cmd = [str(env.py)] + cmd
    proc = subprocess.Popen(cmd, cwd=str(env.home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', env=env.env())
    try:
        url = None; t0 = time.time()
        while time.time() - t0 < 30:
            line = proc.stdout.readline()
            m = re.search(r'(http://127\.0\.0\.1:\d+/)', line or '')
            if m: url = m.group(1); break
            if proc.poll() is not None: break
        check(url, 'binggio ui did not print its address')
        html = urllib.request.urlopen(url).read().decode()
        token = re.search(r"const TOKEN = '([^']+)'", html).group(1)

        def call(path, body=None, tok=None):
            req = urllib.request.Request(url.rstrip('/') + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={'X-Binggio-Token': token if tok is None else tok, 'Content-Type': 'application/json'})
            try:
                r = urllib.request.urlopen(req, timeout=300); return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        def ok(path, body=None):
            code, d = call(path, body); check(code == 200, '%s -> %s %s' % (path, code, d)); return d

        def wait(job):
            for _ in range(3000):
                j = ok('/api/jobs/' + job['id'])
                if j['status'] != 'running': return j
                time.sleep(0.1)
            raise Fail('job never finished')

        check(call('/api/state', tok='wrong')[0] == 403, 'the API must refuse requests without the session token')
        check(ok('/api/state')['workspace'] is None, 'started outside a workspace: should show the start screen')
        src = datasets.write_csv(ctx.get('data') or datasets.build(), env.home / 'Downloads' / 'web upload')
        ok('/api/workspace', {'action': 'create', 'path': str(env.work / 'web bank')})
        job = wait(ok('/api/sources', {'type': 'csv', 'name': 'Web export', 'path': str(src)}))
        check(job['status'] == 'done', 'adding a source from the app failed: %s' % job.get('error'))
        cid = job['result']['connection']
        props = ok('/api/onboard?connection=' + cid)
        r = ok('/api/onboard', {'connection': cid, 'by': 'Sim Web',
                                'roles': {t: [{'column': x['column'], 'role': x['role']} for x in cols] for t, cols in props['roles'].items()},
                                'links': [{'edge_id': l['edge_id'], 'cardinality': l['cardinality']} for l in props['links'] if l['suggested']],
                                'concepts': [c['table'] for c in props['concepts']]})
        check(r['roles'] == 21 and r['links'] == 3 and not r['errors'], 'onboarding from the app: %s' % r)
        one = ok('/api/review/roles?status=approved')['items'][0]
        item = {'connection': one['connection'], 'table': one['table'], 'column': one['column']}
        for act in ('revoke', 'reopen', 'approve'):
            check(ok('/api/review/roles', {'action': act, 'by': 'Sim Web', 'items': [item]})['done'] == 1, act + ' from the app failed')
        names = {}
        for x in ok('/api/review/roles?status=all&limit=1000')['items']:
            names[x['table'].lower()] = x['table']; names[(x['table'].lower(), x['column'].lower())] = x['column']
        P = datasets.plans(cid, names)
        a = ok('/api/ask', {'question': 'customers with no account', 'plan': P['customers with no account']})['answer']
        check(sorted(str(row[0]).strip().upper() for row in a['rows']) == datasets.oracle(ctx.get('data') or datasets.build())['customers with no account'],
              'answer from the app differs from the independent count')
        browser = 'browser check skipped (pip install playwright to include it)'
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sync_playwright = None
        if sync_playwright:
            with sync_playwright() as pw:
                try:
                    b = pw.chromium.launch()
                except Exception as e:
                    b = None; browser = 'browser check skipped (%s)' % str(e).splitlines()[0][:80]
                if b:
                    page = b.new_page(); errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    for h in ('#home', '#sources', '#explore', '#onboard', '#review/roles/all', '#review/relationships/all',
                              '#review/terms', '#graph', '#ask', '#activity', '#settings'):
                        page.goto(url + h); page.wait_for_timeout(1200)
                        check('Loading' not in page.inner_text('#page')[:40] or h == '#graph', 'page %s did not load' % h)
                    b.close()
                    check(not errors, 'browser errors: %s' % errors)
                    browser = 'all 11 screens opened in a browser without errors'
        return 'app started outside a workspace; created one, added CSVs, onboarded, revoke/reopen/approve, answer matches; ' + browser
    finally:
        proc.terminate()
        try: proc.wait(10)
        except subprocess.TimeoutExpired: proc.kill()


JOURNEYS = [('first contact', j_first_contact), ('demo', j_demo), ('csv', j_csv), ('excel', j_excel), ('sqlite', j_sqlite),
            ('interactive onboard', j_interactive_onboard), ('python api', j_python_api), ('move workspace', j_move_workspace),
            ('bad inputs', j_bad_inputs), ('big table', j_big), ('web app', j_web_app), ('plain words (model)', j_model), ('uninstall', j_uninstall)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['wheel', 'copy'], default='wheel')
    ap.add_argument('--with-model', action='store_true'); ap.add_argument('--keep', action='store_true')
    ap.add_argument('--only', help='comma-separated journey names')
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix='binggio-sim-'))
    env = Env(root, a.mode)
    ctx = {'with_model': a.with_model}
    results = []
    t0 = time.time()
    try:
        env.install(); results.append(('install', 'PASS', '; '.join(env.notes), round(time.time() - t0, 1)))
    except Exception as e:
        results.append(('install', 'FAIL', str(e)[:1500], round(time.time() - t0, 1)))
    if results[0][1] == 'PASS':
        wanted = None
        if a.only:
            asked = [x.strip() for x in a.only.split(',')]
            wanted = {n for n, _ in JOURNEYS if n in asked or n.split()[0] in asked}
            needs = {'excel': 'csv', 'sqlite': 'csv', 'interactive onboard': 'csv', 'python api': 'csv',
                     'move workspace': 'csv', 'uninstall': 'csv', 'plain words (model)': 'demo'}
            wanted |= {needs[n] for n in wanted if n in needs}
        for name, fn in JOURNEYS:
            if wanted is not None and name not in wanted: continue
            t1 = time.time()
            try:
                detail = fn(env, ctx)
                status = 'SKIP' if str(detail).startswith('SKIP') else 'PASS'
            except Fail as e:
                status, detail = 'FAIL', str(e)
            except Exception as e:
                status, detail = 'ERROR', '%s: %s\n%s' % (type(e).__name__, e, traceback.format_exc()[-1200:])
            results.append((name, status, detail, round(time.time() - t1, 1)))
            print('%-22s %-5s %6.1fs  %s' % (name, status, results[-1][3], str(detail).splitlines()[0][:110]), flush=True)
    reports = HERE / 'reports'; reports.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    passed = sum(r[1] == 'PASS' for r in results); failed = sum(r[1] in ('FAIL', 'ERROR') for r in results)
    md = ['# Binggio user simulation %s' % stamp, '',
          'Mode: %s · Python %s · %s · %d passed, %d failed, %d skipped · %.0f s' % (
              a.mode, sys.version.split()[0], sys.platform, passed, failed, sum(r[1] == 'SKIP' for r in results), time.time() - t0), '',
          '| Journey | Result | Seconds | Detail |', '| --- | --- | --- | --- |']
    for n, s, d, sec in results:
        md.append('| %s | %s | %s | %s |' % (n, s, sec, str(d).replace('\n', '<br>').replace('|', '/')[:1500]))
    (reports / ('sim_%s.md' % stamp)).write_text('\n'.join(md) + '\n', encoding='utf-8')
    (reports / ('sim_%s.json' % stamp)).write_text(json.dumps(results, indent=1), encoding='utf-8')
    print('\n%d passed, %d failed. Report: %s' % (passed, failed, reports / ('sim_%s.md' % stamp)))
    if a.keep: print('Simulated machine kept at', root)
    else: shutil.rmtree(root, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
