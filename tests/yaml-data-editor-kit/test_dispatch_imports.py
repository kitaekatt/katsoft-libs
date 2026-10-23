"""Tests for the yaml-data-editor-kit dispatch import boundary."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


PLUGIN_LIB = Path(__file__).resolve().parents[2] / "plugins" / "yaml-data-editor-kit" / "lib"
LLM_KIT_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-kit" / "lib"


def _python(code: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dispatch_exports_agentic_and_question_protocol_surfaces() -> None:
    result = _python(
        """
        from yaml_data_editor_kit.dispatch import (
            AgenticCommentPlanner,
            CommentPlanner,
            PlannerPolicy,
            QuestionProtocolError,
            encode_question_failure,
            build_dispatch_handlers,
            materialize_failed_questions,
            prepare_background_dispatch,
            run_background_wave,
            get_background_dispatch_status,
            finalize_background_dispatch,
        )

        assert AgenticCommentPlanner
        assert CommentPlanner
        assert PlannerPolicy
        assert QuestionProtocolError
        assert encode_question_failure
        assert build_dispatch_handlers
        assert materialize_failed_questions
        assert prepare_background_dispatch
        assert run_background_wave
        assert get_background_dispatch_status
        assert finalize_background_dispatch
        """,
        PLUGIN_LIB,
        LLM_KIT_LIB,
    )
    assert result.returncode == 0, result.stderr


def test_missing_llm_kit_reports_the_bootstrap_action() -> None:
    result = _python(
        """
        import builtins

        original_import = builtins.__import__

        def missing_llm_kit(name, *args, **kwargs):
            if name == "llm_kit":
                raise ModuleNotFoundError(
                    "No module named 'llm_kit'", name="llm_kit"
                )
            return original_import(name, *args, **kwargs)

        builtins.__import__ = missing_llm_kit
        try:
            import yaml_data_editor_kit.dispatch
        except ModuleNotFoundError as exc:
            message = str(exc)
            assert "bootstrap" in message.lower()
            assert "llm_kit" in message
        else:
            raise AssertionError("dispatch import did not fail")
        """,
        PLUGIN_LIB,
    )
    assert result.returncode == 0, result.stderr


def test_nested_module_not_found_is_not_relabelled_as_missing_llm_kit() -> None:
    result = _python(
        """
        import builtins

        original_import = builtins.__import__

        def nested_failure(name, *args, **kwargs):
            if name == "llm_kit":
                return original_import(name, *args, **kwargs)
            if name == "llm_kit.pipeline.artifacts.freshness.hashing":
                raise ModuleNotFoundError(
                    "No module named 'llm_kit.pipeline.artifacts.freshness.hashing'",
                    name="llm_kit.pipeline.artifacts.freshness.hashing",
                )
            return original_import(name, *args, **kwargs)

        builtins.__import__ = nested_failure
        try:
            import yaml_data_editor_kit.dispatch
        except ModuleNotFoundError as exc:
            assert exc.name == "llm_kit.pipeline.artifacts.freshness.hashing"
            assert "katsoft-llm-kit" not in str(exc).lower()
        else:
            raise AssertionError("dispatch import did not fail")
        """,
        PLUGIN_LIB,
        LLM_KIT_LIB,
    )
    assert result.returncode == 0, result.stderr
