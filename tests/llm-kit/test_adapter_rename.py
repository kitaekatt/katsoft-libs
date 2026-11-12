"""The transport adapter is named ``openai-compat``; ``openrouter`` is still read.

The adapter is the OpenAI-compatible HTTP completion. OpenRouter is one endpoint
it serves, so the family name says what the adapter is. Before llm-kit 1.17.0 the
family was named ``openrouter``. These tests pin three things:

* every new write and every report uses ``openai-compat``, including for a real
  OpenRouter registry entry;
* the old name is accepted wherever a name is looked up or read back
  (capabilities, requirements, factory specs, ledger rows);
* the old Python names resolve for the deprecation window, with a warning.

A real OpenRouter reference (the ``openrouter`` registry entry, openrouter.ai
URLs, the account check) is not the adapter and keeps its name.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import warnings
from pathlib import Path

import pytest

from llm_kit.adapters import backends as adapter_backends
from llm_kit.adapters import capabilities as adapter_capabilities_module
from llm_kit.adapters.backends import OpenAICompatBackend
from llm_kit.adapters.capabilities import (
    ADAPTER_CAPABILITIES,
    OPENAI_COMPAT_CAPABILITIES,
    adapter_capabilities,
)
from llm_kit.completion.factory_protocols import BackendSpec
from llm_kit.dispatch.factory import create_backend
from llm_kit.dispatch.pipeline_backends import (
    BACKEND_OPENAI_COMPAT,
    BACKEND_OPENROUTER,
    ConfiguredBackendFactory,
    build_backend,
    construction_kwargs,
)
from llm_kit.jobs.model import Attempt, Contract, Job, Prompt, Usage
from llm_kit.jobs.run import _capabilities_for
from llm_kit.jobs.store import JobStore
from llm_kit.pipeline.execution.store import ExecutionStore
from llm_kit.pipeline.runtime import backends as runtime_backends
from llm_kit.pipeline.runtime import platform
from llm_kit.registry import catalog
from llm_kit.registry.adapter_names import (
    ADAPTER_OPENAI_COMPAT,
    canonical_adapter_name,
    canonical_adapter_name_or_none,
    lookup_by_adapter,
)
from llm_kit.registry.project_models import EndpointInfo

OLD = "openrouter"
NEW = "openai-compat"


# --------------------------------------------------------------------------
# The new name
# --------------------------------------------------------------------------


def test_the_family_is_named_openai_compat_everywhere_it_is_advertised():
    assert ADAPTER_OPENAI_COMPAT == NEW == BACKEND_OPENAI_COMPAT
    assert OPENAI_COMPAT_CAPABILITIES.adapter == NEW
    assert ADAPTER_CAPABILITIES[NEW] is OPENAI_COMPAT_CAPABILITIES
    assert OLD not in adapter_capabilities()
    assert catalog.TRANSPORT_ADAPTER == NEW
    assert OpenAICompatBackend().name == NEW
    assert runtime_backends.OpenAICompatBackend().name == NEW
    assert OpenAICompatBackend.capabilities is OPENAI_COMPAT_CAPABILITIES


def test_a_real_openrouter_registry_entry_resolves_and_reports_openai_compat(tmp_path):
    info = EndpointInfo(OLD, "transport", route="openrouter")
    assert catalog.adapter_of(info) == NEW
    selection = create_backend(OLD, project_root=tmp_path)
    assert selection.endpoint == OLD  # the registry entry keeps its real name
    assert selection.kind == "transport"
    assert selection.backend.name == NEW
    assert selection.capabilities.adapter == NEW
    assert selection.backend.endpoint == OLD


def test_the_harness_adapters_keep_their_names():
    assert catalog.ADAPTER_BY_HARNESS == {
        "claude": "claude-cli", "codex": "codex-cli", "opencode": "opencode-cli",
    }
    assert sorted(adapter_capabilities()) == ["claude-cli", "codex-cli", "openai-compat", "opencode-cli"]


# --------------------------------------------------------------------------
# The old name is accepted on lookup
# --------------------------------------------------------------------------


def test_the_old_name_maps_to_the_new_one_and_nothing_else_changes():
    assert canonical_adapter_name(OLD) == NEW
    assert canonical_adapter_name(NEW) == NEW
    assert canonical_adapter_name("claude-cli") == "claude-cli"
    assert canonical_adapter_name("mock") == "mock"
    assert canonical_adapter_name_or_none(None) is None
    assert canonical_adapter_name_or_none(OLD) == NEW


def test_a_capability_table_is_served_under_either_spelling():
    table = adapter_capabilities()
    assert lookup_by_adapter(table, OLD) is OPENAI_COMPAT_CAPABILITIES
    assert lookup_by_adapter(table, NEW) is OPENAI_COMPAT_CAPABILITIES
    assert lookup_by_adapter(table, "claude-cli") is table["claude-cli"]
    assert lookup_by_adapter(table, "ghost") is None
    assert lookup_by_adapter(table, None) is None
    # A table keyed by the old name (an injected one) still serves the new name.
    legacy = {OLD: "legacy-record", "claude-cli": "claude-record"}
    assert lookup_by_adapter(legacy, NEW) == "legacy-record"
    assert lookup_by_adapter(legacy, OLD) == "legacy-record"
    # The exact key wins when both spellings are present.
    both = {OLD: "old", NEW: "new"}
    assert lookup_by_adapter(both, OLD) == "old" and lookup_by_adapter(both, NEW) == "new"


def test_the_default_provider_serves_the_old_name():
    factory = ConfiguredBackendFactory()
    assert factory.capability_record(OLD) is OPENAI_COMPAT_CAPABILITIES
    assert factory.capability_record(NEW) is OPENAI_COMPAT_CAPABILITIES


def test_jobs_find_the_record_of_a_backend_that_still_says_openrouter():
    class Legacy:
        name = OLD

    class Current:
        name = NEW

    table = adapter_capabilities()
    for backend in (Legacy(), Current()):
        selection = type("S", (), {"backend": backend})()
        assert _capabilities_for(selection, table) is OPENAI_COMPAT_CAPABILITIES
    legacy_table = {OLD: OPENAI_COMPAT_CAPABILITIES}
    selection = type("S", (), {"backend": Current()})()
    assert _capabilities_for(selection, legacy_table) is OPENAI_COMPAT_CAPABILITIES


def test_a_factory_spec_naming_openrouter_builds_the_openai_compat_backend():
    assert BACKEND_OPENROUTER == OLD
    for name in (OLD, NEW):
        spec = BackendSpec(backend=name, endpoint="local")
        assert construction_kwargs(spec)["endpoint"] == "local"
        built = build_backend(spec)
        assert isinstance(built, OpenAICompatBackend) and built.name == NEW


def test_an_injected_constructor_table_keyed_by_the_old_name_still_serves():
    marker = object()
    for key in (OLD, NEW):
        factory = ConfiguredBackendFactory({key: lambda **kwargs: marker})
        assert factory.create(BackendSpec(backend=NEW)) is marker
        assert factory.create(BackendSpec(backend=OLD)) is marker


def test_an_unknown_backend_spec_is_still_refused_naming_the_current_names():
    with pytest.raises(ValueError, match="unknown backend 'ghost'.*openai-compat"):
        construction_kwargs(BackendSpec(backend="ghost"))


def test_the_catalog_accepts_modes_keyed_by_the_old_name():
    assert catalog._modes_for({OLD: ["completion"]}, NEW) == ["completion"]
    assert catalog._modes_for({NEW: ["completion"]}, OLD) == ["completion"]
    with pytest.raises(KeyError):
        catalog._modes_for({"claude-cli": ["completion"]}, NEW)


def test_the_response_cache_key_does_not_move_with_the_rename():
    """A persisted cache is addressed by this key; renaming must not orphan it."""
    args = dict(model="a/bare", system="s", user="u")
    old = platform.build_cache_key(backend=OLD, **args)
    assert platform.build_cache_key(backend=NEW, **args) == old
    assert platform.build_cache_key(backend="claude-cli", **args) != old


# --------------------------------------------------------------------------
# The old Python names resolve, with a warning
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, old, new",
    [
        (adapter_backends, "OpenRouterBackend", "OpenAICompatBackend"),
        (runtime_backends, "OpenRouterBackend", "OpenAICompatBackend"),
        (adapter_capabilities_module, "OPENROUTER_CAPABILITIES", "OPENAI_COMPAT_CAPABILITIES"),
    ],
)
def test_a_retired_python_name_resolves_to_the_renamed_object_with_a_warning(module, old, new):
    with pytest.warns(DeprecationWarning, match=f"{old} is deprecated since 1.17.0.*{new}") as seen:
        value = getattr(module, old)
    assert value is getattr(module, new)
    assert seen[0].filename == __file__  # stacklevel points at the caller


def test_the_package_level_retired_name_resolves_with_a_warning():
    import llm_kit.pipeline.runtime as runtime

    with pytest.warns(DeprecationWarning, match="OpenRouterBackend is deprecated"):
        value = runtime.OpenRouterBackend
    assert value is runtime.OpenAICompatBackend
    with pytest.warns(DeprecationWarning):
        from llm_kit.pipeline.runtime import OpenRouterBackend  # noqa: F401


def test_the_retired_names_are_not_exported():
    import llm_kit.pipeline.runtime as runtime

    for module in (adapter_backends, runtime_backends, runtime):
        assert "OpenRouterBackend" not in module.__all__
        assert "OpenAICompatBackend" in module.__all__
    assert "OPENROUTER_CAPABILITIES" not in adapter_capabilities_module.__all__
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for module in (adapter_backends, runtime_backends, runtime):
            vars(module).get("OpenRouterBackend")  # a plain read of __dict__ never warns
    with pytest.raises(AttributeError):
        adapter_backends.NoSuchName  # noqa: B018


def test_the_deprecated_entries_are_listed_in_the_public_surface():
    surface = json.loads(
        (Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "public-surface.json")
        .read_text(encoding="utf-8")
    )
    entries = {entry["name"]: entry for entry in surface["entries"]}
    for module in ("llm_kit.pipeline.runtime", "llm_kit.pipeline.runtime.backends"):
        assert entries[f"{module}.OpenAICompatBackend"] == {
            "name": f"{module}.OpenAICompatBackend", "status": "public",
        }
        old = entries[f"{module}.OpenRouterBackend"]
        assert old["status"] == "deprecated"
        assert (old["since"], old["remove_after"]) == ("1.17.0", "1.19.0")
        assert old["replacement"] == f"{module}.OpenAICompatBackend"


# --------------------------------------------------------------------------
# Ledger rows
# --------------------------------------------------------------------------


def _job(directory: Path) -> Job:
    return Job(
        id="job",
        prompt=Prompt(user="run"),
        models=("fake",),
        directory=directory,
        max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def _attempt(backend: str) -> Attempt:
    return Attempt(
        run_id="run", job_id="job", attempt_no=1, endpoint="fake-endpoint", backend=backend,
        model="fake-model", status="completed", started_at="2026-09-01T00:00:01Z",
        ended_at="2026-09-01T00:00:02Z", usage=Usage(input_tokens=1, output_tokens=1),
        response_text="answer",
    )


def _raw(store: JobStore, sql: str, *args):
    connection = sqlite3.connect(str(store.db_path))
    try:
        rows = connection.execute(sql, args).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


def test_a_new_ledger_write_stores_openai_compat_even_for_the_old_spelling(tmp_path):
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    reservation = store.reserve_attempt(
        "run", "job", endpoint="e", backend=OLD, model="m", reserved_at="2026-09-01T00:00:00Z",
    )
    assert reservation.backend == NEW
    assert _raw(store, "SELECT backend FROM reservations") == [(NEW,)]
    store.arm_reservation("run", "job", 1, invoke_armed_at="2026-09-01T00:00:01Z")
    store.append_attempt(_attempt(OLD))
    assert _raw(store, "SELECT backend FROM attempts") == [(NEW,)]
    assert {row[0] for row in _raw(store, "SELECT adapter FROM events") if row[0]} == {NEW}


def test_ledger_rows_written_before_the_rename_read_back_as_openai_compat(tmp_path):
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    store.reserve_attempt(
        "run", "job", endpoint="e", backend="fake-backend", model="m",
        reserved_at="2026-09-01T00:00:00Z",
    )
    store.arm_reservation("run", "job", 1, invoke_armed_at="2026-09-01T00:00:01Z")
    store.append_attempt(_attempt("fake-backend"))
    # Rows an earlier release wrote: the adapter column says the old name.
    _raw(store, "UPDATE reservations SET backend = ?", OLD)
    _raw(store, "UPDATE attempts SET backend = ?", OLD)
    _raw(store, "UPDATE events SET adapter = ? WHERE adapter IS NOT NULL", OLD)
    assert {row[0] for row in _raw(store, "SELECT backend FROM attempts")} == {OLD}

    assert [a.backend for a in store.list_attempts("run")] == [NEW]
    assert store.get_attempt(store.list_attempts("run")[0].id).backend == NEW
    assert store.get_reservation("run", "job", 1).backend == NEW
    adapters = {
        event["source"].get("adapter") for event in store.list_events("run")
        if event["source"].get("adapter")
    }
    assert adapters == {NEW}


def test_a_pipeline_run_recorded_before_the_rename_reads_back_as_openai_compat(tmp_path):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    store.create_run("r1", driver="inline", backend=OLD, model="m", adapter_version="1")
    store.create_run("r2", driver="inline", backend="mock", model="m", adapter_version="1")
    assert store.get_run("r1").backend == NEW
    assert store.get_run("r2").backend == "mock"
