"""Shared fixtures: load deploy.env before importing the Flask app."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_deploy_env(path: Path) -> None:
    import os

    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


_load_deploy_env(ROOT / "deploy.env")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def app():
    import run_dashboard as rd

    rd.app.config["TESTING"] = True
    return rd.app


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def db_available(app) -> bool:
    import run_dashboard as rd

    try:
        conn = rd.get_conn()
        conn.close()
        return True
    except Exception:
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "db: tests that need a live od_dashboard PostgreSQL")
