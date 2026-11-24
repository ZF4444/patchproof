#!/usr/bin/env python3
"""Compatibility test entry point: uv run --locked --extra http python run_tests.py."""
import sys
from pathlib import Path

import pytest

if __name__ == "__main__":
    sys.exit(pytest.main([str(Path(__file__).resolve().parent / "tests"), *sys.argv[1:]]))
