"""Run records and run stores: one format and one home for model runs and comparisons.

A run record (``llm-kit.run-record/v1``, :mod:`llm_kit.runs.record`) is one
attempt of one case on one model selection; a comparison is the records that
share one ``group.id``. A run store (:mod:`llm_kit.runs.store`) is a directory
of write-once record events and content-addressed blobs with a content-class
policy, and a derived metadata index (:mod:`llm_kit.runs.index`). A comparison
(:mod:`llm_kit.runs.compare`) runs a set of cases on two or more model
selections and records each attempt; it takes the model caller as an argument
and makes no call of its own. The CLI is ``llm-kit runs`` (:mod:`llm_kit.cli.runs`).

Writers in other languages build records as JSON and hand them to
``llm-kit runs import`` as a ``llm-kit.run-import/v1`` JSON Lines file. The
format contract is ``docs/reference/llm-kit-formats.md``; the store, policy and
CLI are in ``docs/reference/llm-kit-runs.md``.

This package is standard library only.
"""

from __future__ import annotations

from .compare import (
    ACTIONS,
    CALL_STATUSES,
    COMPARE_CASE_V1,
    INPUT_OVERHEAD_TOKENS,
    LABEL_BOUND,
    LABEL_REUSED_FROM,
    AttemptOutcome,
    CallResult,
    Candidate,
    CompareCase,
    CompareError,
    ComparePlan,
    CompareResult,
    PlannedAttempt,
    Price,
    Scorer,
    attempt_bound_usd,
    command_scorer,
    plan_compare,
    read_cases,
    run_compare,
)
from .index import INDEX_COLUMNS, INDEX_VERSION, RunIndex
from .record import (
    CONTENT_CLASSES,
    RECORD_STATUSES,
    RESULT_STATUSES,
    REUSE_KEY_VERSION,
    RUN_RECORD_V1,
    RunRecordError,
    blob_ref,
    blob_refs,
    finish_record,
    reuse_key,
    start_record,
    validate_record,
)
from .store import (
    INDEX_FILE,
    RUN_IMPORT_V1,
    RUN_STORE_V1,
    STORE_ENV,
    PutResult,
    RunRecordStore,
    RunStoreError,
    import_file,
    read_import_file,
    resolve_store_path,
)

__all__ = [
    "ACTIONS",
    "AttemptOutcome",
    "CALL_STATUSES",
    "COMPARE_CASE_V1",
    "CONTENT_CLASSES",
    "CallResult",
    "Candidate",
    "CompareCase",
    "CompareError",
    "ComparePlan",
    "CompareResult",
    "INDEX_COLUMNS",
    "INDEX_FILE",
    "INDEX_VERSION",
    "INPUT_OVERHEAD_TOKENS",
    "LABEL_BOUND",
    "LABEL_REUSED_FROM",
    "PlannedAttempt",
    "Price",
    "PutResult",
    "RECORD_STATUSES",
    "RESULT_STATUSES",
    "REUSE_KEY_VERSION",
    "RUN_IMPORT_V1",
    "RUN_RECORD_V1",
    "RUN_STORE_V1",
    "RunIndex",
    "RunRecordError",
    "RunRecordStore",
    "RunStoreError",
    "STORE_ENV",
    "Scorer",
    "attempt_bound_usd",
    "blob_ref",
    "blob_refs",
    "command_scorer",
    "finish_record",
    "import_file",
    "plan_compare",
    "read_cases",
    "read_import_file",
    "resolve_store_path",
    "reuse_key",
    "run_compare",
    "start_record",
    "validate_record",
]
