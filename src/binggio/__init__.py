# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""Binggio: governed answers from your data.

Binggio answers questions only from meanings a person has approved (column roles,
relationships, business terms, metrics), and refuses with the reason when it cannot.
"""
__version__ = '0.1.2'

from binggio.workspace import Workspace, WorkspaceError  # noqa: E402,F401
