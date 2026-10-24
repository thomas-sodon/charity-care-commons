"""The policy compiler (design section 5 mechanism 1, step S4): LLM calls only.

:func:`compile_request` builds the Sonnet-class structured-output call whose
static prefix (field guide + few-shots) and documents sit behind the two cache
breakpoints that :mod:`charitycare.llm` sets. :func:`reask_request` reuses the
same system prefix and documents, so a targeted re-ask reads them from cache and
only the short volatile instruction is new. Neither function decides anything;
the gate in :mod:`charitycare.compile.pipeline` does.
"""

from __future__ import annotations

from typing import Any

from charitycare.compile.prompts import (
    COMPILE_VERSION,
    REASK_VERSION,
    compile_system,
    reask_user,
)
from charitycare.compile.textlayer import (
    DocText,
    RoutingPlan,
    build_documents,
    plan_routing,
)
from charitycare.llm import LLMBackend, LLMRequest, LLMResult
from charitycare.schemas import CompiledPolicy, Field, ModelTier


def field_schema(key: str) -> type[Field[Any]]:
    """The concrete ``Field[...]`` model of one CompiledPolicy field (re-ask output schema)."""
    return CompiledPolicy.model_fields[key].annotation  # type: ignore[return-value]


def compile_user(doc: DocText, plan: RoutingPlan) -> str:
    images = f" Page(s) {', '.join(map(str, plan.image_pages))} attached as page images." if plan.image_pages else ""
    return (
        f"Compile the financial assistance policy in the attached document (sha256 {doc.sha256}, "
        f"{doc.n_pages} pages).{images} Use doc_sha256 \"{doc.sha256}\" in every Evidence item and "
        "return one CompiledPolicy JSON object."
    )


def compile_request(doc: DocText, *, tier: ModelTier = "sonnet", batch: bool = False) -> LLMRequest[CompiledPolicy]:
    """The compile call for one hashed FAP document, routed text-first."""
    plan = plan_routing(doc)
    return LLMRequest(
        prompt_version=COMPILE_VERSION,
        model_tier=tier,
        doc_sha256=doc.sha256,
        system=compile_system(),
        documents=build_documents(doc, plan),
        user=compile_user(doc, plan),
        output_schema=CompiledPolicy,
        max_tokens=16000,
        effort="medium",
        batch=batch,
    )


def reask_request(doc: DocText, key: str, previous: Field[Any], problems: list[str], *, tier: ModelTier = "sonnet") -> LLMRequest[Any]:
    """A targeted re-ask for one field (fixture variant = field key)."""
    return LLMRequest(
        prompt_version=REASK_VERSION,
        model_tier=tier,
        doc_sha256=doc.sha256,
        system=compile_system(),
        documents=build_documents(doc),
        user=reask_user(key, doc.sha256, problems, previous.model_dump(mode="json")),
        output_schema=field_schema(key),
        variant=key,
        max_tokens=4000,
        effort="medium",
    )


def compile_document(doc: DocText, backend: LLMBackend, *, tier: ModelTier = "sonnet") -> LLMResult[CompiledPolicy]:
    """Run the compile call. Raises :class:`charitycare.llm.LLMError` on failure."""
    return backend.complete(compile_request(doc, tier=tier))


def reask_field(doc: DocText, key: str, previous: Field[Any], problems: list[str], backend: LLMBackend) -> LLMResult[Any]:
    """Run one targeted re-ask. Raises :class:`charitycare.llm.LLMError` on failure."""
    return backend.complete(reask_request(doc, key, previous, problems))
