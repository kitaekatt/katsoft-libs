"""Keep each test run independent of ambient user state and old bytecode."""
from __future__ import annotations

import atexit
import shutil
import sys
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_model_overrides(monkeypatch, tmp_path):
    """Selection tests never read the real user's usage policy."""
    monkeypatch.setenv("LLM_KIT_MODEL_OVERRIDES_FILE", str(tmp_path / "model-overrides.yaml"))

if sys.pycache_prefix is None:
    _cache = tempfile.mkdtemp(prefix="katsoft-libs-pycache-")
    sys.pycache_prefix = _cache
    atexit.register(shutil.rmtree, _cache)
