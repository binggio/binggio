# Build notes: Binggio 0.1 (open-source package)

Built 29 Oct 2026 from components of the POC (`C:\enterprise-intelligence-poc`, read only, never
modified). Eight parts.

| Part | What | Proof |
| --- | --- | --- |
| 1. Package | `binggio` package, standard library only; CLI and Python API; demo bank with its 429 approvals | `test_golden` (10 verified questions), `test_workspace`, `test_cli` |
| 1. Content fingerprints | Approvals bound to file content, not file times; `binggio migrate` re-binds older workspaces without losing approvals | `test_fingerprints`: touching files keeps 429 approvals; editing one file makes only its dependents stale |
| 2. Onboarding fixes | Hard stops removed (100 tables / 600 links); every candidate in the review list; better key suggestions; one-to-one and extension tables; links through non-ID names; auto-approval never takes one-to-one links; `binggio impact` | `test_onboarding` (10-table collections source): 12 links, all real (was 54 with 35 noise); 10 of 10 keys right (was 11 of 50 roles wrong); 0 existing approvals stale |
| 3. Whole-table answers, Excel | Answers read whole tables (up to 1,000,000 rows), not the 2,500-row sample; long lists show 200 rows and the total; queries stop after 120 s; Excel connector | `test_large_tables` (60,000 loans), `test_excel` |
| 4. Databases | SQL Server (pymssql), Oracle (python-oracledb thin), PostgreSQL, SQLite: read-only SELECT with a row limit; declared primary keys kept; password never stored | `test_database` (SQL each dialect sends, value types); live: `tools/check_database.py` |
| 5. Cross-source | The same bank as CSV, Excel and SQLite gives identical answers through the whole first-run path | `test_cross_source`; live SQL Server / Oracle: `tools/check_database.py --cross-source` |
| 6. First-run flow | `binggio onboard`: impact -> roles -> links -> concepts -> words for codes; `--accept-suggestions` | `test_onboard_flow`: onboard collections, define "broken promise" = kept N, ask; 11 loans, matching the independent count |
| 7. The app | `binggio ui`: start screen, data sources (CSV, Excel, SQLite, SQL Server, Oracle, PostgreSQL), explore, onboarding, review (roles, relationships, concepts, terms: approve / reject / revoke / reopen, bulk), relationship map, ask with provenance, activity, settings. Standard library server on 127.0.0.1 with a session token and host check; secrets in memory only | `test_ui` (token and host checks, overview, 10 sample questions match the verified answers, full new-user flow); simulator journey `web app` (installed command, API, and all 11 screens in a browser when Playwright is present) |
| 8. Speed | Within one status, review or answer call each table's profile and freshness are worked out once (`evaluation_scope`); nothing is reused across calls | demo `status`: 8.4 s -> 0.7 s, identical results |

## 0.1.7 (6 Nov 2026)

Shaped by the column names and value shapes of nine enterprise extracts (book, book association,
book hierarchy, desk, portfolio, reporting hierarchy, employee, matrix assignment, instrument).
Test data is random (`tests/fixtures/trading_schema.py`); no real values were used.

| What | 0.1.6 | 0.1.7 |
| --- | --- | --- |
| Relationships found on the nine-table test data | 0 (no column unique: one row per record per month and version) | 8 masters proposed (Employee with 13 member columns in 6 tables, Book, Desk, Portfolio, UBR, Cost centre, Legal entity, Calendar); 40 links once approved |
| Distinct / empty counts | first 2,500 rows | every row (1.5 million rows profiled in 51 s, prepared copies included) |
| Versions, levels, codes, flags as Measure | yes | no |
| Approvals after a profiler improvement | could go stale | kept: column approvals bind to the column and the table's content (signature rule 4; older workspaces are migrated before re-profiling, only approvals current under the old rule are carried) |

New: `masters.py` (recommend / approve / build / link), `profiling/shapes.py` (name endings and
groups), snapshot guard in the engine, data fix "Add a date column", Review › Masters, masters at
the centre of the map. Tests: `tests/test_masters.py`.

## 0.1.4 (6 Nov 2026)

Measured on a 100-table workspace with 10 million rows (372 MB of CSV), standard library only:

| What | 0.1.3 | 0.1.4 |
| --- | --- | --- |
| Profile 100 tables | 88 s (date detection tried six formats on every value) | 4 s |
| Find relationships | 125 s, 2,333 candidates (counters such as row_id linked to each other) and none of the 96 real account links (parent keys read on the first 200,000 rows only) | 24 s to prepare the tables once, then 30 s; 98 candidates, all real |
| Sum of 3,000,000 transactions by channel | refused (over 1,000,000 rows) | 24 s the first time, 2 s after |
| Same, grouped through a join to 600,000 accounts | refused | 11 s |
| Data story for the source | minutes; dimensions of linked tables from partial lookups | 10 s, then instant until data or decisions change |

New: data fixes (`prepared.py`), whole-table key checks, links a person adds (`links.py`), what
discovery tried (`graph.attempts`), answer cache (`engine/answer_cache.py`), navigation guard in
the app (late replies never write into the next page). Tests: `test_steward_tools`.

## Before publishing

- [ ] Run the UAT agent (`python uat\run_uat.py --with-model --sqlserver "host=localhost;database=master;windows"`),
      decide every AMBIGUOUS case in `uat\reports\UAT_results_<time>.xlsx`, and release when its
      Summary tab says "Ready to release: YES".

- [ ] Build the release from a clean checkout, or delete `build\` first: a leftover `build\lib`
      once put a stale `migrate.py` into the wheel (the simulator now checks every installed file
      against the source).

- [ ] Licence: AGPL-3.0-or-later. Add the official text, unchanged, as `LICENSE`:
      `Invoke-WebRequest https://www.gnu.org/licenses/agpl-3.0.txt -OutFile C:\binggio\LICENSE`
      (the UAT agent fails LIC-01 until it is there).
- [x] Copyright and trademark owner: Ashish Kumar (individual), in every header and notice. For
      dual licensing you must own all the code: use the CLA in CONTRIBUTING.md for outside contributions.
- [ ] Check the name `binggio` is free on PyPI; register it, and the matching GitHub organisation.
- [x] Trademark: device mark (logo + "BINGGIO - Enterprise Context Intelligence") filed in India,
      class 42 (temp. ref. 15548209) and class 9 (temp. ref. 15548358). Use ™, never ® until granted.
- [ ] Ask the attorney about a separate word mark BINGGIO (the device marks protect the logo design).
      Keep dated evidence of use from launch (PyPI release, repository, screenshots). Policy: TRADEMARKS.md.
- [ ] Run `pip install -e .[all]` and `pytest -q` on Windows, macOS and Linux.
- [ ] Run `tools/check_database.py sqlserver ... --cross-source` against SQL Server 2019 (installed
      on the development PC) and against an Oracle Free container.
- [ ] Decide which demo-bank data may be published (it is synthetic).

## Known limits in 0.1

- Ratios, relative time windows, durations, period change, conditions on counts: refused.
- A table over 1,000,000 rows is refused for answers (profiling and discovery still work).
- Composite primary keys are not used as declared keys.
- `binggio refresh` re-reads Excel and database sources in place; CSV folders are re-added.
