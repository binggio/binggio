# Contributing

Thank you for helping. Binggio is licensed under the AGPL-3.0-or-later and is also offered under a
commercial licence (see COMMERCIAL.md). So that both remain possible, every contribution needs a
signed Contributor Licence Agreement (CLA) before it can be merged: you keep the copyright in your
contribution and grant Ashish Kumar the right to distribute it under the AGPL and under other
licences.

Before opening a pull request:

- run the tests: `pytest -q tests`
- run the user simulation: `python sim/simulate.py`
- keep the package free of required dependencies (the standard library only; optional extras are fine)
- add the licence header to new source files:

```
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
```
