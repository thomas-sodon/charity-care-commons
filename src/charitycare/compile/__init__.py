"""Compile stage: policy compiler, quote gate, round trip, Schedule H cross-check,
facility mapping, advisory adjudication, review gating and change detection
(design section 5 mechanisms 1-4, section 8 steps S4-S9).

The LLM extracts; code verifies and computes; humans verify fields.
"""

from charitycare.compile.pipeline import (
    CompileError,
    CompileReport,
    compile_facility,
    compile_policy_version,
)

__all__ = ["CompileError", "CompileReport", "compile_facility", "compile_policy_version"]
