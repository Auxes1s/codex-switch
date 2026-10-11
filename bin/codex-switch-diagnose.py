#!/usr/bin/env python3
"""Thin delegation wrapper for consolidated auth diagnostics."""

import importlib.util
from pathlib import Path
import sys

_dashboard_path = Path(__file__).resolve().parent / "codex-switch-dashboard.py"
_spec = importlib.util.spec_from_file_location("dashboard", _dashboard_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

has_saved_credentials = _mod.has_saved_credentials
diagnose = _mod.diagnose

if __name__ == "__main__":
    try:
        diagnose(*sys.argv[1:])
    except (OSError, ValueError, TypeError):
        pass
