"""Keep each test run independent of ambient user state and old bytecode."""
from __future__ import annotations

import atexit
import shutil
import sys
import tempfile

if sys.pycache_prefix is None:
    _cache = tempfile.mkdtemp(prefix="katsoft-libs-pycache-")
    sys.pycache_prefix = _cache
    atexit.register(shutil.rmtree, _cache)
