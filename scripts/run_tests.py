"""Run the dashboard test suite from the repo root.

Usage (from project root)::

  python scripts/run_tests.py
  python scripts/run_tests.py -k transit
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    try:
        import pytest
    except ImportError:
        print("pytest is not installed. Run:  pip install -r requirements.txt", file=sys.stderr)
        return 2
    args = argv if argv is not None else sys.argv[1:]
    import os

    os.chdir(ROOT)
    return pytest.main(["tests", *args])


if __name__ == "__main__":
    raise SystemExit(main())
