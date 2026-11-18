"""The tracked project model file may not widen the Codex sandbox.

``.llm-kit/models.yaml`` is repository content: a pull request or a cloned repository
can change it. The Codex extras ``add_dirs`` and ``scratch_dir`` (further writable
roots) and ``network: true`` (network inside the sandbox) widen what a run may write
or reach, so the file is refused at validation when a row's ``request_defaults.extras``
or a request profile's ``extras`` holds one. ``network: false`` only narrows the
sandbox and stays allowed. A call (the request layer) and the code that calls the
backend set all three as before.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from llm_kit.adapters.codex_backend import CodexCliBackend
from llm_kit.completion.types import BackendOptions
from llm_kit.registry import project_models as pm
from llm_kit.registry import request_settings
from test_cli_harness import home  # noqa: F401 -- fixture the world fixture needs
from test_invoke_protocol import _DOC, World, allow, call, world  # noqa: F401 -- world is a fixture

ABSOLUTE = str(Path(__file__).resolve().parent)
WIDENING = [
    ("add_dirs", [ABSOLUTE]),
    ("scratch_dir", ABSOLUTE),
    ("network", True),
    ("network", None),
    ("network", "false"),
]
NETWORK_OFF = "sandbox_workspace_write.network_access=false"


def _luna(document: dict) -> dict:
    return next(row for row in document["catalog"] if row["id"] == "luna")


def _row_document(key: str, value: Any) -> dict:
    document = copy.deepcopy(_DOC)
    _luna(document)["request_defaults"] = {"extras": {key: value}}
    return document


def _profile_document(key: str, value: Any) -> dict:
    document = copy.deepcopy(_DOC)
    document["requests"] = {"wide": {"extras": {key: value}}}
    document["selections"]["draft.generate.primary"] = {"model": "luna", "effort": "high", "request": "wide"}
    return document


def _tracked_file_text(document: dict) -> bytes:
    """The file bytes of ``document`` as a repository would carry them (portable rendering, no binding)."""
    return pm.render_yaml(pm.validate_portable_document(document))


def _write_tracked(world: World, document: dict) -> None:
    path = world.root / pm.DEFAULT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_tracked_file_text(document))


@pytest.mark.parametrize(("key", "value"), WIDENING)
def test_a_row_extra_that_widens_the_codex_sandbox_is_refused(world, key, value):
    document = _row_document(key, value)
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_document(document, project_root=str(world.root))
    error = caught.value
    assert error.code == pm.CODE_INVALID
    assert error.where == f"catalog[0].request_defaults.extras.{key}"
    assert repr(key) in str(error) and "tracked project file" in str(error) and "BackendOptions.extras" in str(error)
    with pytest.raises(pm.ProjectModelsError):
        pm.write_project_models(document, project_root=str(world.root))


@pytest.mark.parametrize(("key", "value"), WIDENING)
def test_a_profile_extra_that_widens_the_codex_sandbox_is_refused(world, key, value):
    document = _profile_document(key, value)
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_document(document, project_root=str(world.root))
    assert caught.value.code == pm.CODE_INVALID
    assert caught.value.where == f"requests['wide'].extras.{key}"
    assert repr(key) in str(caught.value)


def test_network_false_narrows_the_sandbox_and_stays_allowed_in_a_row_and_a_profile(world):
    row = pm.validate_document(_row_document("network", False), project_root=str(world.root))
    assert _luna(row)["request_defaults"]["extras"] == {"network": False}
    profile = pm.validate_document(_profile_document("network", False), project_root=str(world.root))
    assert profile["requests"]["wide"]["extras"] == {"network": False}


def test_portable_validation_keeps_its_reach(world):
    """Binding refuses, as it does the other reserved extras; the portable check is unchanged."""
    document = _row_document("add_dirs", [ABSOLUTE])
    assert _luna(pm.validate_portable_document(document))["request_defaults"]["extras"] == {"add_dirs": [ABSOLUTE]}


@pytest.mark.parametrize(("key", "value"), [("add_dirs", [ABSOLUTE]), ("scratch_dir", ABSOLUTE), ("network", True)])
@pytest.mark.parametrize("make", [_row_document, _profile_document])
def test_a_call_over_a_tracked_file_with_such_an_extra_dispatches_nothing(world, capsys, make, key, value):
    allow()
    (world.root / "work").mkdir()
    _write_tracked(world, make(key, value))
    code, _doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work")
    assert code != 0 and err["error"]["kind"] == "project-file", (code, err)
    assert repr(key) in err["error"]["message"]
    assert world.runner_calls == 0
    assert not any("--add-dir" in argv for argv in world.codex)


def test_a_tracked_network_false_still_reaches_the_codex_argv(world, capsys):
    allow()
    (world.root / "work").mkdir()
    pm.write_project_models(_row_document("network", False), project_root=str(world.root))
    code, _doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work")
    assert code == 0, err
    argv = world.codex[-1]
    assert NETWORK_OFF in {argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "-c"}


def _flag_values(argv: list, flag: str) -> list:
    return [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == flag]


def test_the_calling_code_still_sets_every_widening_extra(tmp_path):
    """The code that calls the backend sets ``add_dirs``, ``scratch_dir`` and ``network`` as before."""
    first, second, scratch = ((tmp_path / name).resolve() for name in ("first", "second", "scratch"))
    calls: list = []

    def runner(argv, request, cwd, **kwargs):
        calls.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_text("receipt", encoding="utf-8")
        return "", "", 0

    backend = CodexCliBackend(argv_prefix=("codex",), runner=runner)
    backend.complete("System", "Work", model="gpt-6-luna", options=BackendOptions(
        cwd=tmp_path.resolve(),
        extras={"add_dirs": [str(first), str(second)], "scratch_dir": str(scratch), "network": True},
    ))
    argv = calls[0]
    assert _flag_values(argv, "--add-dir") == [str(first), str(second), str(scratch)]
    assert "sandbox_workspace_write.network_access=true" in _flag_values(argv, "-c")


def test_a_call_layer_still_carries_the_widening_extras(world):
    """The request layer (``call``) is not the tracked file: the three extras resolve from it unchanged."""
    extras = {"add_dirs": [ABSOLUTE], "scratch_dir": ABSOLUTE, "network": True}
    document = pm.validate_document(copy.deepcopy(_DOC), project_root=str(world.root))
    wanted = request_settings.request_for(document, {"model": "luna", "effort": None}, call={"extras": extras})
    assert wanted["extras"] == extras
    assert pm.validate_profile({"extras": extras}, "call", call=True) == {"extras": extras}


INTERNAL = [
    ("__llm_kit_image_paths", [ABSOLUTE]),
    ("__llm_kit_codex_config", {"shell_environment_policy.inherit": "core"}),
    ("__llm_kit_anything", 1),
]


@pytest.mark.parametrize(("key", "value"), INTERNAL)
@pytest.mark.parametrize("make", [_row_document, _profile_document])
def test_an_internal_extra_in_the_tracked_file_is_refused(world, make, key, value):
    """``__llm_kit_`` names are set by the library's own code; a tracked file may not carry one."""
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_document(make(key, value), project_root=str(world.root))
    assert caught.value.code == pm.CODE_INVALID and caught.value.where.endswith(f"extras.{key}")
    assert repr(key) in str(caught.value) and "internal" in str(caught.value)


@pytest.mark.parametrize(("key", "value"), INTERNAL[:2])
def test_a_call_over_a_tracked_file_with_an_internal_extra_dispatches_nothing(world, capsys, key, value):
    allow()
    (world.root / "work").mkdir()
    _write_tracked(world, _row_document(key, value))
    code, _doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work")
    assert code != 0 and err["error"]["kind"] == "project-file", (code, err)
    assert repr(key) in err["error"]["message"]
    assert world.runner_calls == 0


def test_the_calling_code_still_sets_the_internal_extras(tmp_path):
    image = (tmp_path / "pic.png").resolve()
    calls: list = []

    def runner(argv, request, cwd, **kwargs):
        calls.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_text("receipt", encoding="utf-8")
        return "", "", 0

    backend = CodexCliBackend(argv_prefix=("codex",), runner=runner)
    backend.complete("System", "Work", model="gpt-6-luna", options=BackendOptions(
        cwd=tmp_path.resolve(),
        extras={"__llm_kit_image_paths": (str(image),),
                "__llm_kit_codex_config": {"shell_environment_policy.inherit": "core"}},
    ))
    argv = calls[0]
    assert _flag_values(argv, "-i") == [str(image)]
    assert "shell_environment_policy.inherit=\"core\"" in _flag_values(argv, "-c")
