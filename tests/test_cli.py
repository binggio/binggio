# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
from binggio.cli import main


def test_cli_demo_status_review(tmp_path, capsys):
    assert main(['demo', str(tmp_path / 'bank')]) == 0
    assert main(['-w', str(tmp_path / 'bank'), 'status']) == 0
    assert '"approved": 429' in capsys.readouterr().out
    assert main(['-w', str(tmp_path / 'bank'), 'review', 'relationships']) == 0
    assert 'rel_' in capsys.readouterr().out
    assert main(['-w', str(tmp_path / 'bank'), 'migrate']) == 0
    assert 'Nothing to do' in capsys.readouterr().out


def test_cli_outside_workspace(tmp_path, capsys):
    assert main(['-w', str(tmp_path), 'status']) == 2
    assert 'No Binggio workspace' in capsys.readouterr().err
