# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Checks that every place carrying the version agrees (and with the release tag, if given).

    python tools/check_version.py            # pyproject, __init__, server.json agree
    python tools/check_version.py v0.1.0     # ...and match this tag
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def versions():
    py = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M).group(1)
    init = re.search(r"__version__\s*=\s*'([^']+)'", (ROOT / 'src' / 'binggio' / '__init__.py').read_text()).group(1)
    srv = json.loads((ROOT / 'server.json').read_text())
    found = {'pyproject.toml': py, 'src/binggio/__init__.py': init, 'server.json': srv['version']}
    for i, p in enumerate(srv.get('packages', [])): found['server.json packages[%d]' % i] = p['version']
    return found


if __name__ == '__main__':
    found = versions()
    if len(sys.argv) > 1: found['release tag'] = sys.argv[1].lstrip('v')
    if len(set(found.values())) != 1:
        sys.exit('Versions differ:\n' + '\n'.join('  %-28s %s' % kv for kv in found.items()))
    print('Version', next(iter(found.values())), 'everywhere.')
