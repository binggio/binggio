# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""User acceptance test cases for Binggio 0.1: the single source for the UAT spreadsheet and the agent.

Each case says what a person would do, what should happen, how the agent decides on its own, and
when the agent cannot decide and hands the case to a person (`ambiguous_when`).

How the case is run:
  Agent              fully automatic, no extra setup
  Agent + browser    needs Playwright for this Python (pip install playwright; playwright install chromium)
  Agent + model      needs BINGGIO_LLM_API_KEY; run with --with-model
  Agent + server     needs a database server; run with --sqlserver / --oracle / --postgres
"""

A, AB, AM, AS = 'Agent', 'Agent + browser', 'Agent + model', 'Agent + server'


def C(id, area, title, steps, expected, how=A, decides='', ambiguous_when='', priority='P1'):
    return {'id': id, 'area': area, 'title': title, 'steps': steps, 'expected': expected, 'how': how,
            'decides': decides, 'ambiguous_when': ambiguous_when, 'priority': priority}


CASES = [
    # ------------------------------------------------------------------ install and launch
    C('INS-01', 'Install', 'Install from the built package into a clean Python',
      '1. Build the wheel from a clean copy of the source\n2. Create a new virtual environment\n3. pip install the wheel',
      'Install succeeds with no extra dependencies', decides='pip exit code 0; binggio has no required dependencies'),
    C('INS-02', 'Install', 'Installed files are exactly the source',
      'Compare every installed file with the source tree', 'Every file identical; none missing (UI page and sample bank included)',
      decides='byte comparison of all files'),
    C('INS-03', 'Install', 'binggio --version and --help',
      'Run binggio --version, then binggio --help', 'Version 0.1.0 printed; help lists ui, onboard, ask',
      decides='exit code and text'),
    C('INS-04', 'Install', 'python -m binggio works when the command is not on PATH',
      'Run python -m binggio --version', 'Same output as binggio --version', decides='exit code and text'),
    C('INS-05', 'Install', 'Excel extra installs', 'pip install openpyxl into the environment (binggio[excel])',
      'Excel support available', decides='import works after install',
      ambiguous_when='install fails because there is no internet (cannot tell a packaging fault from a network fault)', priority='P2'),
    C('LIC-01', 'Install', 'Licence is the official AGPL-3.0 text and the package declares it',
      'Check LICENSE in the source and the installed package metadata',
      'LICENSE is the unchanged GNU AGPL v3 text; metadata says AGPL-3.0-or-later; source files carry the SPDX header',
      decides='file heading, version line and length; metadata; headers on every source file'),
    C('INS-06', 'Install', 'Commands outside a workspace explain what to do',
      'In an empty folder run binggio status', 'Clear message "No Binggio workspace found ... Run: binggio init"; exit code 2, no traceback',
      decides='exit code 2 and message; no "Traceback" in output'),

    # ------------------------------------------------------------------ app start
    C('APP-01', 'App', 'binggio ui starts and prints its address',
      'Run binggio ui --no-browser in a folder that is not a workspace', 'Address http://127.0.0.1:<port>/ printed within 10 s; start screen served',
      decides='address printed; page contains the app', ambiguous_when='starts but takes 10-30 s'),
    C('APP-02', 'App', 'API refuses requests without the session token', 'Call the API with no token and a wrong token',
      'HTTP 403 both times', decides='status codes'),
    C('APP-03', 'App', 'API refuses another host name (DNS rebinding)', 'Call the API with Host: evil.example',
      'HTTP 403', decides='status code'),
    C('APP-04', 'App', 'Start screen: create the sample bank', 'Choose a folder, name it, click Create the sample bank',
      'Sample bank created; app opens on Ask', decides='workspace created; status shows 63 answerable tables'),
    C('APP-05', 'App', 'Start screen: open an existing workspace and the recent list',
      'Open a workspace by folder; restart the app; check Recent', 'Workspace opens; it appears in Recent after restart',
      decides='open succeeds; recent list contains it', priority='P2'),
    C('APP-06', 'App', 'Opening a folder that is not a workspace is explained', 'Open /not/a/workspace',
      'Clear error, app stays usable', decides='HTTP 400 with a message containing "workspace"'),

    # ------------------------------------------------------------------ sample bank
    C('DEMO-01', 'Sample bank', 'Overview numbers', 'Open Overview on the sample bank',
      '10 sources, 85 tables, 63 ready for questions, 429 approvals current, 0 changed', decides='exact numbers'),
    C('DEMO-02', 'Sample bank', 'No POC names or personal names visible', 'Look at sources, concepts and the activity list',
      'No "Phase1", "POC", "decision round" or personal names', decides='text search of every listed name, description and actor'),
    C('DEMO-03', 'Sample bank', 'Ten sample questions give the verified answers', 'Click each sample question on Ask',
      'Each answer matches the verified answer (tests/golden)', decides='rows compared with the verified answers'),
    C('DEMO-04', 'Sample bank', 'Follow-up questions use the previous answer', 'Run the 3-step sample (top 10 loans -> which are delinquent -> their customers)',
      '10 loans, then 9, then the 9 customers', decides='row counts and ids'),
    C('DEMO-05', 'Sample bank', 'Every answer shows where it came from', 'Open an answer\'s details',
      'Source tables, business terms with meanings, as-of date, steps in plain words, the query', decides='fields present and non-empty'),
    C('DEMO-06', 'Sample bank', 'Asking in your own words without a key is explained', 'Type a question with no model key set',
      'Not answered: needs a model key, with a link to Settings', decides='refusal code MODEL_NOT_CONFIGURED'),
    C('DEMO-07', 'Sample bank', 'Overview opens quickly', 'Open Overview on the sample bank',
      'Under 2 seconds', decides='timed API call', ambiguous_when='2-5 seconds (acceptable on a slow PC?)', priority='P2'),

    # ------------------------------------------------------------------ adding data
    C('SRC-01', 'Data sources', 'Add a messy CSV export', 'Data sources -> CSV folder -> Browse to the export -> Add and profile',
      '4 tables added, profiled, relationships found; no errors', decides='job finishes; 4 tables; 0 profile failures'),
    C('SRC-02', 'Data sources', 'Folder picker lists folders and counts CSV files', 'Browse to the export folder',
      'Shows the folder and "4 CSV files in this folder"', decides='API listing'),
    C('SRC-03', 'Data sources', 'Add an Excel workbook with title rows above the header', 'Excel workbook -> choose the file -> Add',
      'Header found on row 3 for every sheet; 4 tables', decides='notes and table count'),
    C('SRC-04', 'Data sources', 'Old .xls is refused with advice', 'Add an .xls file',
      'Refused: "save as .xlsx"; nothing added', decides='job fails with that message; source count unchanged'),
    C('SRC-05', 'Data sources', 'Add a SQLite database', 'SQLite file -> choose the .db -> Add',
      '4 tables; declared primary keys kept', decides='table count; declared keys on the profile'),
    C('SRC-06', 'Data sources', 'Test connection lists tables before copying', 'SQLite: Test connection and list tables',
      'The 4 table names listed; nothing copied yet', decides='list returned; source count unchanged'),
    C('SRC-07', 'Data sources', 'Broken files are reported, not fatal', 'Add a folder with an empty CSV, a CSV with duplicate headers and one good CSV',
      'Good table profiled; the two broken ones reported', decides='job done; 2 profile failures reported', priority='P2'),
    C('SRC-08', 'Data sources', 'Original files are never changed', 'Hash the CSV, Excel and SQLite files before and after adding and refreshing',
      'Hashes identical', decides='sha256 before and after'),
    C('SRC-09', 'Data sources', 'Refresh after the source changes', 'Change one row in the SQLite file -> Refresh',
      'Only the changed table reported as changed', decides='changed_tables == [accounts]'),
    C('SRC-10', 'Data sources', 'SQL Server: test connection, add, refresh', 'SQL Server tab -> host, database, Windows sign-in -> Test -> Add',
      'Tables listed and copied read-only; declared keys kept; answers same as CSV', how=AS,
      decides='with --sqlserver: list, add and answer checks', ambiguous_when='connects but some tables are skipped or keys missing (rights?)'),
    C('SRC-11', 'Data sources', 'Oracle: test connection and add', 'Oracle tab -> host, service, user, password -> Test -> Add',
      'Tables listed and copied read-only', how=AS, decides='with --oracle', priority='P2'),
    C('SRC-12', 'Data sources', 'PostgreSQL: test connection and add', 'PostgreSQL tab -> details -> Test -> Add',
      'Tables listed and copied read-only', how=AS, decides='with --postgres', priority='P2'),
    C('SRC-13', 'Data sources', 'A missing database driver is explained', 'Open the SQL Server tab without pymssql installed',
      'Message: pip install binggio[sqlserver]; Add is disabled', decides='state reports driver missing; API error text', priority='P2'),

    # ------------------------------------------------------------------ profiling and discovery quality
    C('DISC-01', 'Discovery', 'Own keys suggested correctly', 'After adding the CSV export, look at suggested roles',
      'customers.customer_id, accounts.account_id, loans.loan_id, repayments.repayment_id suggested as primary keys', decides='exact set'),
    C('DISC-02', 'Discovery', 'Links found, including a column not named *_id', 'Look at candidate relationships',
      'accounts -> customers, repayments -> loans, loans.cust_ref -> customers', decides='required links present'),
    C('DISC-03', 'Discovery', 'No noise links', 'Look at candidate relationships for the CSV export',
      'No links other than the 3 real ones', decides='extra links counted',
      ambiguous_when='an extra link scores 0.85-0.90 (pre-ticked for approval, could be wrong)'),
    C('DISC-04', 'Discovery', 'Collections source (10 tables): links and keys', 'Add the 10-table collections fixture to a workspace with the CSV bank',
      'The 11 verified links found; anything else scores under 0.85 and is left for a person; 10 of 10 own keys right',
      decides='compared with the verified lists',
      ambiguous_when='a link outside the verified list scores 0.85-0.90 (it would be pre-ticked for approval)'),
    C('DISC-05', 'Discovery', 'Messy values match: " c0007 ", "1,234.50", BOM, semicolons', 'Answers on the messy CSV export',
      'Answers equal the independent counts', decides='compared with the independent counts'),

    # ------------------------------------------------------------------ onboarding
    C('ONB-01', 'Onboarding', 'Impact report classifies each table', 'Onboard the CSV export',
      'Each table classified (new entity / lookup list / records) with a reason', decides='every table has a kind and a reason'),
    C('ONB-02', 'Onboarding', 'A copy of an existing table is flagged as a duplicate and left out', 'Onboard the collections source into the sample bank (borrower_profile is a copy of customer_master)',
      'Classified Duplicate; its columns are not offered for approval', decides='kind == duplicate; table absent from step 1'),
    C('ONB-03', 'Onboarding', 'Approving the suggestions', 'Onboard -> Approve selected (with your name)',
      '21 roles, 3 links, 4 concepts approved; no errors', decides='counts'),
    C('ONB-04', 'Onboarding', 'Words for a coded value become a business term', 'Give "closed account" for accounts.status = CLOSED',
      'Term approved; a question using it filters status = CLOSED', decides='term approved; plan with the term returns the CLOSED accounts'),
    C('ONB-05', 'Onboarding', 'Weak links are left for a person', 'Onboard the collections source (cust_no -> customer_kyc_profiles scores about 0.77)',
      'That link is unticked and stays waiting', decides='suggested == false for scores < 0.85; still pending after apply'),
    C('ONB-06', 'Onboarding', 'Decisions need a name', 'Approve without entering a name', 'Asked for a name; nothing recorded without one',
      decides='HTTP 400; no new events'),

    # ------------------------------------------------------------------ review
    C('REV-01', 'Review', 'Approve, revoke, reopen and re-approve a column role', 'Review -> Column roles -> tick one -> each action in turn',
      'Status moves Approved -> Revoked -> Waiting -> Approved; each recorded', decides='status after each action; history has 4 entries'),
    C('REV-02', 'Review', 'Change a role before approving', 'Pick a different role in the list, then Approve', 'The chosen role is stored',
      decides='approved role equals the chosen one'),
    C('REV-03', 'Review', 'Same cycle for a relationship, with a kind', 'Review -> Relationships -> revoke, reopen, approve as Many to one',
      'Recorded with the chosen kind', decides='status and cardinality'),
    C('REV-04', 'Review', 'Same cycle for a business term and a concept', 'Review -> Business terms / concepts -> Revoke, Reopen, Approve',
      'Each status change recorded', decides='status after each action'),
    C('REV-05', 'Review', 'Bulk approve', 'Tick 5 waiting roles -> Approve', '5 recorded', decides='done == 5'),
    C('REV-06', 'Review', 'Impossible actions are refused per item', 'Reopen an item that is approved', 'That item not changed, with the reason; others unaffected',
      decides='done == 0 and an error per item'),
    C('REV-07', 'Review', 'Revoking a link stops answers that need it', 'Revoke accounts -> customers, ask open balance by segment',
      'Not answered (the link is not approved)', decides='refused', ambiguous_when='answered through another approved path (is that path right?)'),
    C('REV-08', 'Review', 'Activity shows every decision with name and note', 'Open Activity', 'All decisions from this run, newest first, with names and notes',
      decides='events from this run present with actor and note'),

    # ------------------------------------------------------------------ data changes
    C('CHG-01', 'Data changes', 'Changed data makes its approvals stale', 'Change the source, refresh, profile',
      'Only the changed table\'s approvals show Data changed', decides='stale count > 0 and only for that table'),
    C('CHG-02', 'Data changes', 'Stale approvals are not used for answers', 'Ask a question needing the changed table',
      'Not answered, with the reason', decides='refused'),
    C('CHG-03', 'Data changes', 'Re-approving restores answers', 'Approve the stale items again, ask again', 'Answered, using the new data',
      decides='answer equals the count on the new data'),
    C('CHG-04', 'Data changes', 'Touching files without changing them keeps approvals', 'Change file times only', 'Nothing becomes stale',
      decides='stale == 0'),

    # ------------------------------------------------------------------ asking (saved plans, no model)
    C('ASK-01', 'Ask', 'Answers on your own data equal independent counts', 'Ask the 7 checked questions on the CSV export',
      'All 7 equal the independent counts', decides='compared with the independent counts'),
    C('ASK-02', 'Ask', 'CSV, Excel and SQLite give identical answers', 'Ask the same 7 on each source', 'Identical', decides='equality'),
    C('ASK-03', 'Ask', 'Long lists show 200 rows and the total', 'Ask for all loans of a 300,000-row table',
      '200 rows shown, total given', decides='rows == 200 and row_count == total'),
    C('ASK-04', 'Ask', 'Nothing approved: the message says what to do', 'Ask in a new workspace with nothing approved',
      'Not answered: approve column roles and relationships first', decides='refusal code NOTHING_APPROVED'),
    C('ASK-05', 'Ask', 'Follow-up without a previous answer is refused', 'Start a new conversation and ask "which of these ..."',
      'Not answered: no previous result', decides='refusal code COHORT_NOT_AVAILABLE'),

    # ------------------------------------------------------------------ asking in your own words (model)
    C('NL-01', 'Ask (own words)', 'Clear questions on the sample bank', 'Ask 8 clearly worded questions with a model key',
      'Each answer equals the verified answer', how=AM, decides='rows compared with the verified answers',
      ambiguous_when='the right rows plus extra rows or columns, or refused with a reason (wording or engine?)'),
    C('NL-02', 'Ask (own words)', 'Badly worded questions (typos, shorthand, no punctuation)', 'Ask 6 sloppy versions of verified questions',
      'Same answers as the clean versions', how=AM, decides='rows compared with the verified answers',
      ambiguous_when='refused, or partly right'),
    C('NL-03', 'Ask (own words)', 'Questions with two possible meanings are not guessed', 'Ask "show revenue", "KYC status of customers"',
      'Not answered; asks which meaning', how=AM, decides='refused as ambiguous', ambiguous_when='answered (which meaning did it choose, and is that acceptable?)'),
    C('NL-04', 'Ask (own words)', 'Questions the data cannot answer are refused', 'Ask about data that does not exist (credit card limits, employee salaries)',
      'Not answered, with the reason', how=AM, decides='refused', ambiguous_when='answered from a column that might be related'),
    C('NL-05', 'Ask (own words)', 'The same question twice gives the same answer', 'Ask one question twice in new conversations',
      'Identical rows', how=AM, decides='equality', ambiguous_when='different rows'),
    C('NL-06', 'Ask (own words)', 'Follow-ups in your own words', '"Top 10 loans by outstanding balance" then "which of these are delinquent"',
      '10 then 9 loans', how=AM, decides='ids', ambiguous_when='second step refused'),

    # ------------------------------------------------------------------ AI agents (MCP)
    C('MCP-01', 'AI agents', 'An agent gets governed answers through binggio mcp',
      'Start the installed binggio mcp on the sample bank; as an MCP client: initialize, list tools, describe, run a plan, try an invented column, propose a term',
      'Verified answer with sources; invented column refused; proposal saved as a draft; no tool can approve',
      decides='protocol replies, rows compared with the verified answer, registry state'),

    # ------------------------------------------------------------------ safety
    C('SAFE-01', 'Safety', 'A model key typed in the app is not written to disk', 'Settings -> paste a key -> use it; search the workspace and home folder',
      'Key not found in any file', decides='byte search of every file'),
    C('SAFE-02', 'Safety', 'A database password typed in the app is not written to disk', 'Add a database with a password; search the workspace',
      'Password not found in any file', decides='byte search of every file'),
    C('SAFE-03', 'Safety', 'Moving the workspace keeps every approval', 'Copy the workspace folder elsewhere and open it',
      'Same approvals, same answers', decides='status and answers equal'),
    C('SAFE-04', 'Safety', 'Uninstall leaves workspaces alone', 'pip uninstall binggio', 'Command gone; workspaces intact', decides='files present'),

    # ------------------------------------------------------------------ performance
    C('PERF-01', 'Performance', '300,000-row table end to end', 'Add, profile, discover, onboard, ask a total by segment',
      'Exact totals; each step under 30 s', decides='totals exact; timings', ambiguous_when='a step takes 30-90 s'),

    # ------------------------------------------------------------------ screens (browser)
    C('UI-01', 'Screens', 'Every screen opens without errors', 'Open all 11 screens', 'No script errors; each shows content',
      how=AB, decides='page errors and content'),
    C('UI-02', 'Screens', 'Works at phone width', 'Open Review at 390 px wide', 'No sideways scrolling of the page', how=AB,
      decides='page width equals window width', priority='P2'),
    C('UI-03', 'Screens', 'Start screen to first answer by clicking', 'Create a workspace, add the CSV export, onboard, ask a sample-bank question',
      'Completes by clicking only', how=AB, decides='each step reaches its expected screen'),
    C('UI-04', 'Screens', 'Download an answer as CSV', 'Ask a sample question -> Download CSV', 'A CSV with the same rows', how=AB,
      decides='downloaded rows equal the answer', priority='P2'),
    C('UI-05', 'Screens', 'Keyboard: the question box and dialogs', 'Press Enter to ask; Esc closes a dialog', 'Both work', how=AB,
      decides='answer appears; dialog closes', priority='P2'),
]


def by_id():
    return {c['id']: c for c in CASES}
