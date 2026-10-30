# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""binggio connect: merges into an AI client's settings without losing anything, and never writes a broken file."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from binggio.connect import connect


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / 'bank'; w.mkdir(); (w / 'binggio.json').write_text('{}')
    return w


def test_merge_keeps_other_servers_and_backs_up(ws, tmp_path):
    cfg = tmp_path / 'claude_desktop_config.json'
    cfg.write_text(json.dumps({'mcpServers': {'other': {'command': 'x'}}, 'preferences': {'theme': 'dark'}}))
    msg, _ = connect('claude-desktop', ws, path=cfg, python='py')
    out = json.loads(cfg.read_text())
    assert out['mcpServers']['other'] == {'command': 'x'} and out['preferences'] == {'theme': 'dark'}
    assert out['mcpServers']['binggio'] == {'command': 'py', 'args': ['-m', 'binggio', '-w', str(ws.resolve()), 'mcp']}
    assert 'Added' in msg and list(tmp_path.glob('claude_desktop_config.json.bak-*'))
    msg, _ = connect('claude-desktop', ws, path=cfg, python='py')
    assert 'Updated' in msg and len(json.loads(cfg.read_text())['mcpServers']) == 2


def test_broken_json_is_left_untouched(ws, tmp_path):
    cfg = tmp_path / 'c.json'; bad = '{"mcpServers": {"a": 1},, }'
    cfg.write_text(bad)
    with pytest.raises(ValueError, match='not valid JSON'):
        connect('cursor', ws, path=cfg)
    assert cfg.read_text() == bad and not list(tmp_path.glob('c.json.*'))


def test_new_file_vscode_shape_and_print_only(ws, tmp_path):
    cfg = tmp_path / 'sub' / 'mcp.json'
    _, snip = connect('vscode', ws, path=cfg, print_only=True)
    assert not cfg.exists() and json.loads(snip)['servers']['binggio']['type'] == 'stdio'
    connect('vscode', ws, path=cfg)
    assert json.loads(cfg.read_text())['servers']['binggio']['type'] == 'stdio'


def test_bom_file_and_claude_code_and_not_a_workspace(ws, tmp_path):
    cfg = tmp_path / 'c.json'; cfg.write_bytes(b'\xef\xbb\xbf{}')
    connect('cursor', ws, path=cfg)
    assert 'binggio' in json.loads(cfg.read_text())['mcpServers']
    msg, cmd = connect('claude-code', ws)
    assert cmd.startswith('claude mcp add binggio') and str(ws.resolve()) in cmd
    with pytest.raises(ValueError, match='Not a Binggio workspace'):
        connect('cursor', tmp_path / 'nowhere', path=cfg)


def test_cli_uses_binggio_workspace_env(demo):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'), 'BINGGIO_WORKSPACE': str(demo.root)}
    r = subprocess.run([sys.executable, '-m', 'binggio', 'connect', 'cursor', '--print'], capture_output=True, text=True,
                       env=env, cwd=str(Path(demo.root).parent))
    assert r.returncode == 0, r.stderr
    snippet = json.loads(r.stdout[r.stdout.index('{'):])   # Windows paths are escaped inside JSON, so compare parsed values
    assert snippet['mcpServers']['binggio']['args'][3] == str(Path(demo.root).resolve())
