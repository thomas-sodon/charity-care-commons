"""Deterministic eligibility engine: household + verified policy fields -> cited Determination.

Pure code (no LLM). See :mod:`charitycare.engine.core` for the rules,
:func:`screen` for the API entry point and :mod:`charitycare.engine.households`
for the 300-household evaluation set.
"""

from charitycare.engine.core import (
    CONDITION_KEYS,
    Condition,
    EngineResult,
    evaluate,
    no_policy_determination,
)
from charitycare.engine.deadlines import (
    FEDERAL_APPLICATION_DAYS,
    compute_deadlines,
    federal_deadline,
)
from charitycare.engine.policy import (
    PolicyView,
    demo_policies,
    demo_policy_records,
    records_from_policy,
)
from charitycare.engine.render import disclaimer, render_text
from charitycare.engine.screen import UnknownFacility, screen, screen_detailed

__all__ = [
    "CONDITION_KEYS", "Condition", "EngineResult", "FEDERAL_APPLICATION_DAYS", "PolicyView", "UnknownFacility",
    "compute_deadlines", "demo_policies", "demo_policy_records", "disclaimer", "evaluate", "federal_deadline",
    "no_policy_determination", "records_from_policy", "render_text", "screen", "screen_detailed",
]
