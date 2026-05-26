from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class RequirementStatus(str, Enum):
    UNSEEN = "unseen"
    EXTRACTED = "extracted"
    PLANNED = "planned"
    IMPLEMENTED = "implemented"
    VALIDATED = "validated"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RequirementProvenance:
    source_ref: str
    section_heading: str = ""
    line_start: int = 0
    line_end: int = 0
    snippet: str = ""


@dataclass
class AcceptanceCriterion:
    criterion_id: str
    text: str
    priority: str = "medium"


@dataclass
class AmbiguityRecord:
    ambiguity_id: str
    text: str
    related_node_ids: list[str] = field(default_factory=list)
    provenance: RequirementProvenance | None = None


@dataclass(frozen=True)
class RequirementEdge:
    source_id: str
    target_id: str
    relation: str = "depends_on"


@dataclass
class RequirementNode:
    stable_id: str
    kind: str
    normalized_text: str
    priority: str = "medium"
    source_ref: str = ""
    source_section_heading: str = ""
    confidence: float = 1.0
    status: RequirementStatus = RequirementStatus.EXTRACTED
    dependencies: list[str] = field(default_factory=list)
    related_acceptance_criteria: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    provenance: list[RequirementProvenance] = field(default_factory=list)


@dataclass
class RequirementGraph:
    nodes: dict[str, RequirementNode] = field(default_factory=dict)
    edges: list[RequirementEdge] = field(default_factory=list)
    acceptance_criteria: dict[str, AcceptanceCriterion] = field(default_factory=dict)
    ambiguities: dict[str, AmbiguityRecord] = field(default_factory=dict)

    PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    STATUS_ORDER = {
        RequirementStatus.UNSEEN: 0,
        RequirementStatus.EXTRACTED: 1,
        RequirementStatus.PLANNED: 2,
        RequirementStatus.IMPLEMENTED: 3,
        RequirementStatus.VALIDATED: 4,
        RequirementStatus.FAILED: 5,
        RequirementStatus.AMBIGUOUS: 6,
    }

    @staticmethod
    def normalize_text(text: str) -> str:
        return " ".join(text.strip().split())

    @classmethod
    def make_stable_id(cls, kind: str, text: str) -> str:
        normalized = cls.normalize_text(text).lower()
        digest = hashlib.sha1(f"{kind}:{normalized}".encode("utf-8")).hexdigest()[:12]
        return f"req_{digest}"

    @staticmethod
    def make_acceptance_id(text: str) -> str:
        digest = hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:12]
        return f"ac_{digest}"

    @staticmethod
    def make_ambiguity_id(text: str) -> str:
        digest = hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:12]
        return f"amb_{digest}"

    def add_node(self, node: RequirementNode) -> RequirementNode:
        existing = self.nodes.get(node.stable_id)
        if existing is None:
            self.nodes[node.stable_id] = node
            return node

        existing.priority = self._higher_priority(existing.priority, node.priority)
        existing.source_ref = existing.source_ref or node.source_ref
        existing.source_section_heading = existing.source_section_heading or node.source_section_heading
        existing.confidence = max(existing.confidence, node.confidence)
        if self.STATUS_ORDER[node.status] > self.STATUS_ORDER[existing.status]:
            existing.status = node.status
        existing.dependencies = self._merge_unique(existing.dependencies, node.dependencies)
        existing.related_acceptance_criteria = self._merge_unique(
            existing.related_acceptance_criteria, node.related_acceptance_criteria
        )
        existing.evidence_ids = self._merge_unique(existing.evidence_ids, node.evidence_ids)
        existing.provenance = self._merge_unique(existing.provenance, node.provenance)
        return existing

    def create_node(
        self,
        *,
        kind: str,
        normalized_text: str,
        priority: str = "medium",
        source_ref: str = "",
        source_section_heading: str = "",
        confidence: float = 1.0,
        status: RequirementStatus = RequirementStatus.EXTRACTED,
        dependencies: list[str] | None = None,
        related_acceptance_criteria: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        provenance: list[RequirementProvenance] | None = None,
    ) -> RequirementNode:
        node = RequirementNode(
            stable_id=self.make_stable_id(kind, normalized_text),
            kind=kind,
            normalized_text=self.normalize_text(normalized_text),
            priority=priority,
            source_ref=source_ref,
            source_section_heading=source_section_heading,
            confidence=confidence,
            status=status,
            dependencies=list(dependencies or []),
            related_acceptance_criteria=list(related_acceptance_criteria or []),
            evidence_ids=list(evidence_ids or []),
            provenance=list(provenance or []),
        )
        return self.add_node(node)

    def add_edge(self, edge: RequirementEdge) -> None:
        if edge not in self.edges:
            self.edges.append(edge)
        source = self.nodes.get(edge.source_id)
        if source is not None and edge.relation == "depends_on" and edge.target_id not in source.dependencies:
            source.dependencies.append(edge.target_id)

    def link_dependency(self, source_id: str, target_id: str) -> None:
        self.add_edge(RequirementEdge(source_id=source_id, target_id=target_id, relation="depends_on"))

    def add_acceptance_criterion(self, criterion: AcceptanceCriterion) -> AcceptanceCriterion:
        existing = self.acceptance_criteria.get(criterion.criterion_id)
        if existing is None:
            self.acceptance_criteria[criterion.criterion_id] = criterion
            return criterion
        existing.priority = self._higher_priority(existing.priority, criterion.priority)
        return existing

    def attach_acceptance_criterion(self, node_id: str, text: str, priority: str = "medium") -> str:
        criterion_id = self.make_acceptance_id(text)
        self.add_acceptance_criterion(AcceptanceCriterion(criterion_id=criterion_id, text=text, priority=priority))
        node = self.nodes[node_id]
        if criterion_id not in node.related_acceptance_criteria:
            node.related_acceptance_criteria.append(criterion_id)
        return criterion_id

    def record_ambiguity(self, text: str, related_node_ids: list[str] | None = None, provenance: RequirementProvenance | None = None) -> AmbiguityRecord:
        ambiguity_id = self.make_ambiguity_id(text)
        record = self.ambiguities.get(ambiguity_id)
        if record is None:
            record = AmbiguityRecord(
                ambiguity_id=ambiguity_id,
                text=self.normalize_text(text),
                related_node_ids=list(related_node_ids or []),
                provenance=provenance,
            )
            self.ambiguities[ambiguity_id] = record
        else:
            record.related_node_ids = self._merge_unique(record.related_node_ids, list(related_node_ids or []))
            if record.provenance is None:
                record.provenance = provenance
        return record

    def transition_status(self, node_id: str, new_status: RequirementStatus) -> None:
        node = self.nodes[node_id]
        current_order = self.STATUS_ORDER[node.status]
        new_order = self.STATUS_ORDER[new_status]
        if new_status in {RequirementStatus.FAILED, RequirementStatus.AMBIGUOUS} or new_order >= current_order:
            node.status = new_status

    def summary(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "acceptance_criteria": len(self.acceptance_criteria),
            "ambiguities": len(self.ambiguities),
        }

    @classmethod
    def _higher_priority(cls, left: str, right: str) -> str:
        return left if cls.PRIORITY_ORDER.get(left, 1) >= cls.PRIORITY_ORDER.get(right, 1) else right

    @staticmethod
    def _merge_unique(existing: list, incoming: list) -> list:
        merged = list(existing)
        for item in incoming:
            if item not in merged:
                merged.append(item)
        return merged
