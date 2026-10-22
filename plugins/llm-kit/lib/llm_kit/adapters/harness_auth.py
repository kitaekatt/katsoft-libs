"""Harness authentication probes (adapters layer; performs I/O; never raises).

Answers one question per harness: is it running on a subscription session pool
(SUBSCRIPTION), on metered billing (METERED), or is that unknown (UNKNOWN, which
callers treat as spend). Reasons name fields and variables, never secret values.

* ``claude_auth``: any diverting variable set and non-empty -> METERED naming the
  variable; otherwise ``claude auth status`` JSON: ``authMethod == "claude.ai"``
  with ``apiProvider == "firstParty"`` -> SUBSCRIPTION, any other value -> METERED;
  launch failure, timeout or parse failure -> UNKNOWN.
* ``codex_auth``: asks Codex for its own resolved configuration with
  ``codex doctor --json`` in the run's working directory and environment. An auth
  environment variable gives METERED (or UNKNOWN for ``CODEX_ACCESS_TOKEN`` alone)
  before doctor runs. SUBSCRIPTION only when doctor shows provider ``openai``,
  ChatGPT auth and no stored API key; another provider or auth mode is METERED;
  anything else (missing doctor, non-zero exit, timeout, unknown schema, missing
  field, a check status other than ok) is UNKNOWN. No file-reading fallback.
  Results are cached per process, keyed by ``(cwd, CODEX_AUTH_ENV values, CODEX_HOME)``.
  If the backend ever adds a ``-c`` key starting with ``model_provider``,
  ``model_providers`` or ``profile``, it must be forwarded to doctor too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional

from llm_kit.registry.cost import HarnessAuth

CODEX_AUTH_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")
DOCTOR_SCHEMA = 1


def claude_auth(
    env: Optional[Mapping[str, str]] = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: float = 10,
) -> HarnessAuth:
    """Claude authentication state; ``env`` defaults to ``os.environ``."""
    raise NotImplementedError("U1b")


def codex_auth(
    env: Optional[Mapping[str, str]] = None,
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: float = 30.0,
) -> HarnessAuth:
    """Codex authentication state via ``codex doctor --json`` run in ``cwd`` with ``env``."""
    raise NotImplementedError("U1b")


def probe_harness_auth(
    harnesses: Iterable[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    cwd: Path,
) -> Dict[str, HarnessAuth]:
    """Probe only the claude/codex harnesses named (case-insensitive); keyed by lower-case name."""
    raise NotImplementedError("U1b")
