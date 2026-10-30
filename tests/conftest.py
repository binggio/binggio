# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import binggio  # noqa: E402

DEFAULTS = {'select': [], 'filters': [], 'where': None, 'exists': [], 'aggregate': [], 'group_by': [], 'sort': [],
            'limit': 200, 'cohort': None, 'latest': None, 'terms': []}


def full_plan(p):
    return {**DEFAULTS, **p}


@pytest.fixture(scope='module')
def demo(tmp_path_factory):
    return binggio.Workspace.demo(tmp_path_factory.mktemp('demo') / 'bank')
