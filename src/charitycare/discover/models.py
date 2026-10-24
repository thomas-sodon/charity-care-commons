"""Discovery-stage models: ``DiscoveryResult`` (design section 8, S2) and the agent's action schema.

These are stage-local (not part of the Open FAP Schema). Every candidate
carries its URL, anchor text and the page it was linked from, so a reviewer
can retrace how it was found.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from charitycare.schemas import SourceKind


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveredDoc(_M):
    """One candidate policy document found for a discovery target."""

    url: str
    kind: SourceKind
    anchor_text: str = ""
    found_on: str | None = PField(default=None, description="Page the link was found on (None for Schedule H / sitemap seeds)")
    discovered_via: str = PField(description="schedule_h_16a|16b|16c|narrative, crawl, sitemap, wayback, agent, external_list")
    http_status: int | None = None
    blocked: bool = False
    provisional: bool = PField(default=False, description="True for archived (Wayback) copies until a live copy is confirmed")
    captured_at: datetime | None = None
    archived_url: str | None = None
    sha256: str | None = None
    language: str | None = None
    classifier: Literal["llm", "keyword", "none"] = "none"
    classifier_confidence: float = 0.0
    score: float = 0.0


class DiscoveryResult(_M):
    """Outcome of discovering the policy documents for one organization's facilities."""

    target_id: str = PField(description='"ein:{ein}" or a facility id')
    ein: str | None = None
    facility_ids: list[str] = PField(default_factory=list)
    candidates: list[DiscoveredDoc] = PField(default_factory=list)
    tool_calls: int = 0
    budget: int = 25
    method: Literal["agent", "heuristic", "agent+heuristic"] = "heuristic"
    pages_visited: list[str] = PField(default_factory=list)
    blocked_urls: list[str] = PField(default_factory=list)
    dead_urls: list[str] = PField(default_factory=list)
    llm_cost_usd: float = 0.0
    notes: list[str] = PField(default_factory=list)

    def best(self, kind: SourceKind) -> DiscoveredDoc | None:
        """Best candidate of a kind: live before archived, then by score."""
        docs = [c for c in self.candidates if c.kind == kind and c.sha256]
        docs.sort(key=lambda c: (c.provisional, -c.score, c.url))
        return docs[0] if docs else None

    @property
    def reachable(self) -> bool:
        """True when a live (non-archived) FAP was retrieved."""
        return any(c.kind == "fap" and c.sha256 and not c.provisional for c in self.candidates)

    @property
    def status_label(self) -> str:
        if self.reachable:
            return "policy found"
        if any(c.kind == "fap" and c.sha256 for c in self.candidates):
            return "policy not currently reachable (archived copy, provisional)"
        return "policy not currently reachable"


AgentTool = Literal["fetch_page", "sitemap_lookup", "wayback_lookup", "schedule_h_context", "record_candidate", "finish"]


class AgentAction(_M):
    """One step of the discovery agent: a single read-only tool call (or finish)."""

    tool: AgentTool
    url: str | None = PField(default=None, description="fetch_page / wayback_lookup / record_candidate target")
    domain: str | None = PField(default=None, description="sitemap_lookup site, e.g. https://www.example.org")
    ein: str | None = PField(default=None, description="schedule_h_context organization EIN")
    kind: SourceKind | None = PField(default=None, description="record_candidate document kind")
    anchor_text: str | None = None
    found_on: str | None = PField(default=None, description="record_candidate: page where the link appeared")
    rationale: str = PField(default="", max_length=300)
