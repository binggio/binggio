# User simulation

Imitates a new user on a clean machine: builds the wheel, installs it into a brand-new virtual
environment (separate home folder, no PYTHONPATH, folders with spaces and Hindi characters), then
drives the installed `binggio` command through real journeys and checks every answer against counts
computed independently in `datasets.py`.

```
python sim\simulate.py                 # everything (needs internet once for pip install openpyxl)
python sim\simulate.py --keep          # keep the simulated machine to look around
python sim\simulate.py --only demo,csv # some journeys
python sim\simulate.py --with-model    # also 8 plain-word questions (set BINGGIO_LLM_API_KEY; ~8-15 model calls)
python sim\simulate.py --mode copy     # no pip/network: copy the package into the venv instead of a wheel
```

| Journey | What the user does |
| --- | --- |
| install | build wheel, new venv, `pip install`, `binggio --version`; confirms the installed copy runs, not the repo |
| first contact | `--help`; commands outside a workspace give a clear message |
| demo | `binggio demo`; 10 verified questions; asking with no model key; interactive `ask` with new/quit |
| csv | messy export (BOM, semicolons, CRLF, "1,234.50", " c0007 ", `cust_ref`) → profile → discover → impact → onboard → 7 answers |
| excel | same data as a workbook with title rows → same answers |
| sqlite | same data as a database via `add-db` → same answers; change a row, `refresh`, only that table goes stale and is refused |
| interactive onboard | the guided `onboard` driven from the keyboard |
| python api | `import binggio` from the user's own script |
| move workspace | copy the workspace folder elsewhere: approvals and answers unchanged |
| bad inputs | empty file, duplicate headers, `.xls`, missing folder, re-init, nothing approved |
| big table | 300,000 loans: exact totals, time per step |
| web app | `binggio ui` started outside a workspace: create one, add CSVs, onboard, revoke / reopen / approve, ask; every screen in a browser if `pip install playwright` + `playwright install chromium` were run |
| plain words (model) | optional, `--with-model` |
| uninstall | `pip uninstall` removes the command, workspaces untouched |

Reports: `sim\reports\sim_<time>.md` and `.json`. Exit code 1 if anything failed.
