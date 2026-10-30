# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""binggio connect: add Binggio to an AI client's MCP settings, safely.

    binggio connect claude-desktop -w C:\\banks\\my-bank
    binggio connect cursor | vscode | claude-code
    binggio connect claude-desktop --print        # show what would be added, change nothing

The client's existing settings are kept: the file is read, checked, backed up, merged and written
back. If the file is not valid JSON, nothing is written and the line and column are reported.
"""
from __future__ import annotations
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

CLIENTS = ('claude-desktop', 'cursor', 'vscode', 'claude-code')


def config_path(client):
    home = Path.home(); system = platform.system()
    if client == 'claude-desktop':
        if system == 'Windows': return Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming')) / 'Claude' / 'claude_desktop_config.json'
        if system == 'Darwin': return home / 'Library' / 'Application Support' / 'Claude' / 'claude_desktop_config.json'
        return home / '.config' / 'Claude' / 'claude_desktop_config.json'
    if client == 'cursor':
        return home / '.cursor' / 'mcp.json'
    if client == 'vscode':
        if system == 'Windows': return Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming')) / 'Code' / 'User' / 'mcp.json'
        if system == 'Darwin': return home / 'Library' / 'Application Support' / 'Code' / 'User' / 'mcp.json'
        return home / '.config' / 'Code' / 'User' / 'mcp.json'
    raise ValueError('Unknown client %r; choose one of: %s' % (client, ', '.join(CLIENTS)))


def server_entry(workspace, python=None):
    return {'command': str(python or sys.executable), 'args': ['-m', 'binggio', '-w', str(workspace), 'mcp']}


def connect(client, workspace, name='binggio', print_only=False, path=None, python=None):
    """Returns (message, snippet). Raises ValueError with a plain explanation on problems."""
    ws = Path(workspace).expanduser().resolve()
    if not (ws / 'binggio.json').is_file():
        raise ValueError('Not a Binggio workspace: %s (create one with "binggio init" or "binggio demo")' % ws)
    entry = server_entry(ws, python)
    if client == 'claude-code':
        cmd = 'claude mcp add %s --scope user -- "%s" -m binggio -w "%s" mcp' % (name, entry['command'], ws)
        return 'Run this in a terminal (Claude Code manages its own settings):\n\n    ' + cmd, cmd
    key, value = ('servers', {'type': 'stdio', **entry}) if client == 'vscode' else ('mcpServers', entry)
    snippet = json.dumps({key: {name: value}}, indent=2)
    target = Path(path) if path else config_path(client)
    if print_only:
        return 'Add this to %s:\n\n%s' % (target, snippet), snippet
    cfg = {}
    if target.exists() and target.read_text(encoding='utf-8-sig').strip():
        raw = target.read_text(encoding='utf-8-sig')
        try:
            cfg = json.loads(raw)
        except ValueError as e:
            raise ValueError('%s is not valid JSON (%s), so nothing was changed. Fix that line, or restore an earlier copy, '
                             'and run this again.' % (target, e))
        if not isinstance(cfg, dict):
            raise ValueError('%s does not contain a JSON object, so nothing was changed.' % target)
        backup = target.with_name(target.name + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(target, backup)
    else:
        backup = None
    servers = cfg.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ValueError('"%s" in %s is not an object, so nothing was changed.' % (key, target))
    replaced = name in servers
    servers[name] = value
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    json.loads(tmp.read_text(encoding='utf-8'))          # never leave a broken file behind
    os.replace(tmp, target)
    lines = ['%s Binggio in %s (workspace %s).' % ('Updated' if replaced else 'Added', target, ws)]
    if backup: lines.append('Previous settings saved as %s.' % backup.name)
    lines.append({'claude-desktop': 'Quit Claude Desktop completely (including from the system tray) and open it again.',
                  'cursor': 'Restart Cursor, or reload its MCP servers in Settings > MCP.',
                  'vscode': 'In VS Code, open the Command Palette and run "MCP: List Servers" to start it.'}[client])
    return '\n'.join(lines), snippet
