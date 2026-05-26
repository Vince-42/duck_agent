"""
Structured working memory for the orchestrator.

Keeps track of:
- Task objective (clearly stated)
- Current assumptions about solution structure
- Decisions already made
- Errors encountered and lessons
- Recommended next actions

This is shared across iterations so the model doesn't have to re-learn context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkingMemory:
    """Structured working memory for orchestrator reasoning."""

    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    errors_encountered: list[dict[str, Any]] = field(default_factory=list)
    lessons_learned: list[str] = field(default_factory=list)
    current_status: str = ""
    next_actions: list[str] = field(default_factory=list)

    def add_constraint(self, constraint: str) -> None:
        """Record a constraint."""
        if constraint not in self.constraints:
            self.constraints.append(constraint)

    def add_assumption(self, key: str, value: Any) -> None:
        """Record an assumption."""
        self.assumptions[key] = value

    def add_decision(self, decision_id: str, decision: str) -> None:
        """Record a decision made."""
        self.decisions[decision_id] = decision

    def add_error(self, error_type: str, details: str, recovery: str = "") -> None:
        """Record an error and how we recovered."""
        self.errors_encountered.append(
            {
                "type": error_type,
                "details": details,
                "recovery": recovery,
            }
        )

    def add_lesson(self, lesson: str) -> None:
        """Record something learned."""
        if lesson not in self.lessons_learned:
            self.lessons_learned.append(lesson)

    def set_next_actions(self, actions: list[str]) -> None:
        """Set recommended next actions."""
        self.next_actions = actions

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(
            {
                "objective": self.objective,
                "constraints": self.constraints,
                "assumptions": self.assumptions,
                "decisions": self.decisions,
                "errors_encountered": self.errors_encountered,
                "lessons_learned": self.lessons_learned,
                "current_status": self.current_status,
                "next_actions": self.next_actions,
            },
            indent=2,
            default=str,
        )

    def to_prompt_section(self, max_chars: int = 1000) -> str:
        """Format for inclusion in prompts."""
        lines = [f"Objective: {self.objective}"]

        if self.constraints:
            lines.append(f"Constraints: {', '.join(self.constraints[:3])}")

        if self.assumptions:
            assumption_strs = [f"{k}={v}" for k, v in list(self.assumptions.items())[:3]]
            lines.append(f"Assumptions: {', '.join(assumption_strs)}")

        if self.decisions:
            decision_strs = [f"{k}→{v[:30]}" for k, v in list(self.decisions.items())[:2]]
            lines.append(f"Decisions: {', '.join(decision_strs)}")

        if self.errors_encountered:
            error_strs = [f"{e['type']}" for e in self.errors_encountered[-2:]]
            lines.append(f"Recent errors: {', '.join(error_strs)}")

        if self.lessons_learned:
            lines.append(f"Key lesson: {self.lessons_learned[-1]}")

        lines.append(f"Status: {self.current_status}")

        if self.next_actions:
            lines.append(f"Next: {self.next_actions[0]}")

        result = "\n".join(lines)
        if len(result) > max_chars:
            return result[:max_chars] + "..."

        return result
