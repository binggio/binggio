# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Builds the folder for the Claude Desktop bundle (.mcpb). Binggio has no dependencies, so the bundle
is just the package plus a small launcher; the person needs Python 3.10 or later installed.

    python tools/build_mcpb.py --out build/mcpb
    npx -y @anthropic-ai/mcpb validate build/mcpb/manifest.json
    npx -y @anthropic-ai/mcpb pack build/mcpb binggio.mcpb
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

LAUNCHER = '''# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from pathlib import Path
if sys.version_info < (3, 10):
    sys.exit('Binggio needs Python 3.10 or later; this is ' + sys.version.split()[0])
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from binggio.cli import main
sys.exit(main(sys.argv[1:]))
'''


def manifest():
    from binggio import __version__
    from binggio.mcp_server import _tools
    return {
        'manifest_version': '0.3',
        'name': 'binggio',
        'display_name': 'Binggio',
        'version': __version__,
        'description': 'Governed answers from your data: approved meanings only, refusals instead of guesses.',
        'long_description': 'Binggio answers questions from a workspace whose columns, relationships and business terms '
                            'have been reviewed and approved by people. Anything not approved is refused rather than guessed. '
                            'Set up and review the workspace with "pip install binggio" and "binggio ui".',
        'author': {'name': 'Ashish Kumar', 'email': 'ashish@binggio.com', 'url': 'https://www.binggio.com'},
        'homepage': 'https://www.binggio.com',
        'repository': {'type': 'git', 'url': 'https://github.com/binggio/binggio'},
        'support': 'https://github.com/binggio/binggio/issues',
        'icon': 'icon.png',
        'license': 'AGPL-3.0-or-later',
        'keywords': ['data', 'governance', 'semantic layer', 'sql', 'csv'],
        'server': {
            'type': 'python',
            'entry_point': 'server/main.py',
            'mcp_config': {
                'command': 'python',
                'args': ['${__dirname}/server/main.py', '-w', '${user_config.workspace}', 'mcp'],
                'env': {'PYTHONIOENCODING': 'utf-8'},
            },
        },
        'user_config': {
            'workspace': {'type': 'directory', 'title': 'Binggio workspace',
                          'description': 'The folder that contains binggio.json (made with "binggio init" or "binggio demo").',
                          'required': True},
        },
        'tools': [{'name': t['name'], 'description': re.split(r'(?<!e\.g)\. ', t['description'])[0].rstrip('.') + '.'} for t in _tools()],
        'compatibility': {'platforms': ['darwin', 'win32', 'linux'], 'runtimes': {'python': '>=3.10'}},
    }


def build(out):
    out = Path(out)
    if out.exists(): shutil.rmtree(out)
    lib = out / 'server' / 'lib'
    shutil.copytree(ROOT / 'src' / 'binggio', lib / 'binggio',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', 'demo_bank'))  # the MCP server never needs the sample bank
    (out / 'server' / 'main.py').write_text(LAUNCHER, encoding='utf-8')
    shutil.copy2(ROOT / 'docs' / 'assets' / 'binggio.png', out / 'icon.png')
    shutil.copy2(ROOT / 'LICENSE', out / 'LICENSE')
    (out / 'manifest.json').write_text(json.dumps(manifest(), indent=2) + '\n', encoding='utf-8')
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--out', default=str(ROOT / 'build' / 'mcpb'))
    print('Bundle folder ready:', build(ap.parse_args().out))
