# User acceptance test

`cases.py` is the single list of acceptance cases. The spreadsheet and the agent are both made from it.

```
python uat\make_sheet.py                 # the blank UAT spreadsheet: uat\UAT_Binggio_0.1.xlsx
python uat\run_uat.py                    # the agent: every case it can run on this PC
python uat\run_uat.py --with-model       # + questions in your own words (set BINGGIO_LLM_API_KEY first)
python uat\run_uat.py --with-model --judge
python uat\run_uat.py --sqlserver "host=localhost;database=master;windows"
```

The agent installs the package into a new virtual environment and uses it as a person would: the
installed `binggio` command, the app through its API, and the screens through a browser if
Playwright is installed (`pip install playwright` then `playwright install chromium`).

Each case ends as PASS, FAIL, AMBIGUOUS (the agent cannot decide on its own; each case says when)
or NOT RUN (needs a server, Playwright or a model key). The report, `uat\reports\uat_<time>.md`,
lists only the ambiguous cases in detail, then failures and cases that did not run. Passes are
counted, not listed. `uat\reports\UAT_results_<time>.xlsx` is the spreadsheet with the agent's
results filled in; add your verdicts in the yellow columns and the Summary tab says whether the
release is ready.

`--judge` asks the model for a second opinion on each ambiguous case. It is advice in the report;
it never turns a case into a pass.
