from __future__ import annotations

import re
from dataclasses import dataclass, field

from requirement_graph import RequirementEdge, RequirementGraph, RequirementNode, RequirementProvenance, RequirementStatus


@dataclass
class RequirementDelta:
    nodes: list[RequirementNode] = field(default_factory=list)
    edges: list[RequirementEdge] = field(default_factory=list)
    ambiguities: list[tuple[str, list[str], RequirementProvenance | None]] = field(default_factory=list)


@dataclass
class ExtractionReport:
    chunk_id: str
    attempted: bool
    completeness_marked: bool
    section_headings: list[str] = field(default_factory=list)
    merged_node_ids: list[str] = field(default_factory=list)
    ambiguity_ids: list[str] = field(default_factory=list)
    duplicate_count: int = 0


@dataclass
class ChunkExtractionResult:
    delta: RequirementDelta
    report: ExtractionReport


class RequirementExtractor:
    CLI_PATTERN = re.compile(r"(?:^|\b)(python\d*\s+[^\n`]+\.py[^\n`]*)", re.IGNORECASE)
    AMBIGUITY_MARKERS = ("either", "one of", "unclear", "ambiguous", "tbd", "todo", "?", "may ", "should ")

    def extract_from_text(self, text: str, chunk_id: str = "full-spec") -> ChunkExtractionResult:
        lines = text.splitlines()
        current_heading = ""
        headings: list[str] = []
        delta = RequirementDelta()
        report = ExtractionReport(chunk_id=chunk_id, attempted=True, completeness_marked=True)
        previous_node_id = ""

        for line_number, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue

            if stripped.startswith("#"):
                current_heading = stripped.lstrip("#").strip()
                headings.append(current_heading)
                continue

            candidates = self._extract_line_candidates(
                stripped=stripped,
                current_heading=current_heading,
                chunk_id=chunk_id,
                line_number=line_number,
            )
            for node in candidates:
                delta.nodes.append(node)
                if previous_node_id and node.kind in {"json_schema_rule", "output_rule", "error_rule", "completion_criterion"}:
                    delta.edges.append(RequirementEdge(source_id=node.stable_id, target_id=previous_node_id, relation="depends_on"))
                previous_node_id = node.stable_id

            ambiguity_text = self._detect_ambiguity(stripped, current_heading)
            if ambiguity_text:
                related_ids = [node.stable_id for node in candidates]
                delta.ambiguities.append(
                    (
                        ambiguity_text,
                        related_ids,
                        RequirementProvenance(
                            source_ref=chunk_id,
                            section_heading=current_heading,
                            line_start=line_number,
                            line_end=line_number,
                            snippet=stripped,
                        ),
                    )
                )

        report.section_headings = headings
        return ChunkExtractionResult(delta=delta, report=report)

    def build_graph(self, text: str, chunk_id: str = "full-spec") -> RequirementGraph:
        graph = RequirementGraph()
        result = self.extract_from_text(text, chunk_id=chunk_id)
        self.merge_delta(graph, result.delta, result.report)
        return graph

    def merge_delta(
        self,
        graph: RequirementGraph,
        delta: RequirementDelta,
        report: ExtractionReport | None = None,
    ) -> ExtractionReport:
        if report is None:
            report = ExtractionReport(chunk_id="merge", attempted=True, completeness_marked=True)

        duplicate_count = 0
        for node in delta.nodes:
            existed = node.stable_id in graph.nodes
            merged = graph.add_node(node)
            if merged.stable_id not in report.merged_node_ids:
                report.merged_node_ids.append(merged.stable_id)
            if existed:
                duplicate_count += 1
            graph.attach_acceptance_criterion(merged.stable_id, merged.normalized_text, priority=merged.priority)

        for edge in delta.edges:
            graph.add_edge(edge)

        for text, related_node_ids, provenance in delta.ambiguities:
            record = graph.record_ambiguity(text, related_node_ids=related_node_ids, provenance=provenance)
            if record.ambiguity_id not in report.ambiguity_ids:
                report.ambiguity_ids.append(record.ambiguity_id)

        report.duplicate_count = duplicate_count
        return report

    def _extract_line_candidates(
        self,
        *,
        stripped: str,
        current_heading: str,
        chunk_id: str,
        line_number: int,
    ) -> list[RequirementNode]:
        candidates: list[RequirementNode] = []
        heading_lower = current_heading.lower()
        lowered = stripped.lower()
        provenance = RequirementProvenance(
            source_ref=chunk_id,
            section_heading=current_heading,
            line_start=line_number,
            line_end=line_number,
            snippet=stripped,
        )

        cli_match = self.CLI_PATTERN.search(stripped.replace("```", " "))
        if cli_match:
            candidates.append(self._make_node("cli_contract", cli_match.group(1).strip(), current_heading, chunk_id, provenance, priority="high"))

        if current_heading and "example" in heading_lower:
            candidates.append(self._make_node("example", stripped, current_heading, chunk_id, provenance, priority="low"))

        if stripped.startswith(("-", "*")) and ("json" in heading_lower or "output" in heading_lower):
            normalized = self._normalize_bullet_requirement(stripped)
            kind = "json_schema_rule" if "json" in heading_lower else "output_rule"
            candidates.append(self._make_node(kind, normalized, current_heading, chunk_id, provenance, priority="high"))
            return candidates

        if any(token in lowered for token in ("must not", "do not", "forbidden", "never")):
            candidates.append(self._make_node("forbidden_behavior", stripped, current_heading, chunk_id, provenance, priority="high"))

        if any(token in lowered for token in ("exit with code", "stderr", "does not exist", "json error", "error object")):
            candidates.append(self._make_node("error_rule", stripped, current_heading, chunk_id, provenance, priority="high"))

        if any(token in lowered for token in ("json", "stdout", "print exactly one", "required key")):
            kind = "json_schema_rule" if "json" in lowered else "output_rule"
            candidates.append(self._make_node(kind, stripped, current_heading, chunk_id, provenance, priority="high"))

        if "input" in heading_lower or any(token in lowered for token in ("input file", "<input", "reads a utf-8", "read a utf-8")):
            candidates.append(self._make_node("input_rule", stripped, current_heading, chunk_id, provenance, priority="medium"))

        if any(token in lowered for token in ("must", "exactly", "always", "invariant", "preserve")) and not any(node.kind == "forbidden_behavior" for node in candidates):
            kind = "invariant" if any(token in lowered for token in ("always", "invariant", "preserve")) else "output_rule"
            candidates.append(self._make_node(kind, stripped, current_heading, chunk_id, provenance, priority="high"))

        if any(token in lowered for token in ("done when", "complete when", "completion", "success when")):
            candidates.append(self._make_node("completion_criterion", stripped, current_heading, chunk_id, provenance, priority="high"))

        return self._dedupe_candidates(candidates)

    def _make_node(
        self,
        kind: str,
        text: str,
        current_heading: str,
        chunk_id: str,
        provenance: RequirementProvenance,
        *,
        priority: str,
    ) -> RequirementNode:
        graph = RequirementGraph()
        return RequirementNode(
            stable_id=graph.make_stable_id(kind, text),
            kind=kind,
            normalized_text=graph.normalize_text(text),
            priority=priority,
            source_ref=chunk_id,
            source_section_heading=current_heading,
            confidence=0.9,
            status=RequirementStatus.AMBIGUOUS if kind == "ambiguity" else RequirementStatus.EXTRACTED,
            provenance=[provenance],
        )

    @staticmethod
    def _normalize_bullet_requirement(stripped: str) -> str:
        key_match = re.search(r"`([^`]+)`", stripped)
        if key_match:
            return f"Required field `{key_match.group(1).strip()}` must be present"
        return stripped.lstrip("-* ").strip()

    def _detect_ambiguity(self, stripped: str, current_heading: str) -> str:
        heading_lower = current_heading.lower()
        lowered = stripped.lower()
        if "ambigu" in heading_lower:
            return stripped
        if any(marker in lowered for marker in self.AMBIGUITY_MARKERS):
            return stripped
        return ""

    @staticmethod
    def _dedupe_candidates(candidates: list[RequirementNode]) -> list[RequirementNode]:
        seen: set[str] = set()
        ordered: list[RequirementNode] = []
        for node in candidates:
            if node.stable_id in seen:
                continue
            seen.add(node.stable_id)
            ordered.append(node)
        return ordered
