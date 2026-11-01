"""Shared fixtures.

Both modules live at the repo root and are imported by path, because the repo
is deliberately flat: `python3 leadbot.py` has to work with no install step.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def leadbot():
    return _load("leadbot")


@pytest.fixture(scope="session")
def localbot():
    return _load("localbot")


@pytest.fixture(scope="session")
def cfg():
    return json.loads((ROOT / "leads.config.json").read_text())


@pytest.fixture
def cfg_copy(cfg):
    """Deep copy so a test that flips a category cannot leak into another."""
    return json.loads(json.dumps(cfg))


@pytest.fixture(scope="session")
def lcfg(cfg):
    """localbot's functions all take the `local` sub-dict, not the whole config.

    `run()` does `lc = cfg["local"]` and passes `lc` to is_chain / audit_site /
    score, so tests must do the same or they exercise an empty config.
    """
    return cfg["local"]


@pytest.fixture
def lcfg_copy(lcfg):
    return json.loads(json.dumps(lcfg))
