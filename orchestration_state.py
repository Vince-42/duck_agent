"""
Structured orchestrator state management.

This module provides explicit, clear state representation for the orchestrator
to decide next steps based on:
- Current plan and completed vs pending steps
- Progress metrics (not just boolean)
- Failure reasons and retry count
- Working memory and context

Prevents infinite loops and improves decision quality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    """Status of a planned step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class StopReason(str, Enum):
    """Why the orchestrator is stopping."""

    SUCCESS = "success"
    MAX_ITERATIONS = "max_iterations"
    MAX_RETRIES = "max_retries"
    NO_PROGRESS = "no_progress"
    FAILURE_UNREPAIRABLE = "failure_unrepairable"
    USER_STOP = "user_stop"
    STAGNATION = "stagnation"
    INVALID_RESPONSE = "invalid_response"


@dataclass
class ExecutedStep:
    """Record of an executed step."""

    name: str
    iteration: int
    action_type: str  # WRITE_FILE, RUN_COMMAND, RUN_TESTS, STOP
    result: str  # Brief description of result
    success: bool
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    fingerprint: str = ""  # For deduplication


@dataclass
class FailureRecord:
    """Record of a failure to enable intelligent retry."""

    failure_type: str  # "validation", "syntax", "test", "api_error", etc.
    iteration: int
    details: str  # Description of failure
    evidence: str  # Log excerpt or error message
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.failure_type,
            "iteration": self.iteration,
            "details": self.details,
            "evidence": self.evidence[:200],  # Truncate for summary
            "timestamp": self.timestamp,
        }


@dataclass
class OrchestratorState:
    """
    Core orchestrator state for explicit, deterministic decision-making.

    This is different from RuntimeState (in agent.py) which tracks execution details.
    OrchestratorState tracks PLANNING and DECISION logic.
    """

    # --- Task Identity ---
    task_id: str = ""
    user_goal: str = ""
    task_deadline: int = 100  # max iterations

    # --- Planning ---
    current_plan: str = ""  # High-level plan text
    planned_steps: list[str] = field(default_factory=list)  # ["Step 1", "Step 2", ...]
    current_step_index: int = 0  # Which step are we on?

    # --- Execution History ---
    completed_steps: list[ExecutedStep] = field(default_factory=list)
    failed_steps: list[FailureRecord] = field(default_factory=list)
    consecutive_no_progress_iterations: int = 0

    # --- Progress Metrics (not just boolean) ---
    iteration_count: int = 0
    actions_executed: int = 0
    files_written: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    validation_score: float = 0.0  # 0.0 to 1.0

    # --- Failure & Retry ---
    current_retry_count: int = 0
    max_retries_per_step: int = 3
    failure_patterns: dict[str, int] = field(
        default_factory=dict
    )  # {"syntax_error": 2, "test_failed": 3}

    # --- Working Memory (structured context) ---
    context_summary: str = ""  # Brief summary of current state
    assumptions: dict[str, Any] = field(default_factory=dict)  # {"primary_artifact": "solution.py", ...}
    decisions_made: dict[str, str] = field(default_factory=dict)  # {"architecture": "simple single file", ...}
    next_actions_suggested: list[str] = field(default_factory=list)  # Recommendations

    # --- Stop Condition ---
    should_stop: bool = False
    stop_reason: StopReason | None = None
    stop_evidence: str = ""

    # --- Chunk Processing ---
    chunks_processed: int = 0
    chunks_total: int = 0
    current_chunk_summary: str = ""

    # --- Requirement Graph Summary (shadow-mode only for now) ---
    requirement_graph_summary: str = ""
    requirement_graph_node_count: int = 0
    open_ambiguity_count: int = 0
    validated_requirement_count: int = 0
    implemented_requirement_count: int = 0
    pending_requirement_count: int = 0
    requirement_coverage_summary: str = ""
    semantic_validation_summary: str = ""
    repair_strategy_summary: str = ""

    # --- Phase Tracking ---
    current_phase: str = "Understand"  # Understand, Build, Verify, Govern
    phase_transitions: list[tuple[int, str]] = field(default_factory=list)  # [(iteration, phase), ...]

    def current_step_description(self) -> str:
        """Get description of current step."""
        if not self.planned_steps or self.current_step_index >= len(self.planned_steps):
            return "(no step defined)"
        return self.planned_steps[self.current_step_index]

    def record_step_execution(
        self, name: str, action_type: str, result: str, success: bool, fingerprint: str = ""
    ) -> None:
        """Record that a step was executed."""
        step = ExecutedStep(
            name=name,
            iteration=self.iteration_count,
            action_type=action_type,
            result=result,
            success=success,
            fingerprint=fingerprint,
        )
        self.completed_steps.append(step)

    def record_failure(
        self, failure_type: str, details: str, evidence: str = ""
    ) -> None:
        """Record that a step failed."""
        failure = FailureRecord(
            failure_type=failure_type,
            iteration=self.iteration_count,
            details=details,
            evidence=evidence,
        )
        self.failed_steps.append(failure)

        # Track failure patterns
        self.failure_patterns[failure_type] = self.failure_patterns.get(failure_type, 0) + 1

    def can_retry_current_step(self) -> bool:
        """Check if we can retry the current step."""
        return self.current_retry_count < self.max_retries_per_step

    def detect_stagnation(self) -> bool:
        """Detect if we're stagnating (repeating same action)."""
        if len(self.completed_steps) < 3:
            return False

        # If last 3 steps all failed and have similar fingerprints, we're stagnating
        recent = self.completed_steps[-3:]
        if all(not s.success for s in recent):
            fingerprints = [s.fingerprint for s in recent]
            if len(set(fingerprints)) == 1:
                return True

        # If validation score hasn't improved in last N iterations
        if self.iteration_count > 10:
            old_score = self.validation_score
            recent_failures = [f for f in self.failed_steps if f.iteration > self.iteration_count - 5]
            if recent_failures and old_score == 0.0:
                return True

        return False

    def detect_infinite_loop(self) -> bool:
        """Detect if we're in an infinite loop."""
        # More than 10 consecutive no-progress iterations
        if self.consecutive_no_progress_iterations >= 10:
            return True

        # Same failure repeated 5+ times
        for count in self.failure_patterns.values():
            if count >= 5:
                return True

        return False

    def is_complete(self) -> bool:
        """Check if the orchestration should be complete."""
        if not self.planned_steps:
            return False

        # All steps completed successfully
        completed_count = sum(1 for s in self.completed_steps if s.success)
        return completed_count >= len(self.planned_steps)

    def update_phase(self, new_phase: str) -> None:
        """Transition to a new phase."""
        if new_phase != self.current_phase:
            self.phase_transitions.append((self.iteration_count, new_phase))
            self.current_phase = new_phase

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for logging."""
        return {
            "task_id": self.task_id,
            "user_goal": self.user_goal,
            "current_plan": self.current_plan[:200],  # Truncate
            "current_step": self.current_step_description(),
            "step_progress": f"{self.current_step_index}/{len(self.planned_steps)}",
            "iteration_count": self.iteration_count,
            "actions_executed": self.actions_executed,
            "validation_score": self.validation_score,
            "consecutive_no_progress": self.consecutive_no_progress_iterations,
            "failure_patterns": self.failure_patterns,
            "current_phase": self.current_phase,
            "should_stop": self.should_stop,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "chunk_progress": f"{self.chunks_processed}/{self.chunks_total}"
            if self.chunks_total
            else "(no chunks)",
            "requirement_graph_node_count": self.requirement_graph_node_count,
            "open_ambiguity_count": self.open_ambiguity_count,
            "validated_requirement_count": self.validated_requirement_count,
            "implemented_requirement_count": self.implemented_requirement_count,
            "pending_requirement_count": self.pending_requirement_count,
            "requirement_coverage_summary": self.requirement_coverage_summary[:120],
            "semantic_validation_summary": self.semantic_validation_summary[:120],
            "repair_strategy_summary": self.repair_strategy_summary[:120],
        }

    def summary_for_prompt(self) -> str:
        """Generate a concise summary for use in prompts."""
        lines = [
            f"Current task: {self.user_goal[:100]}",
            f"Progress: step {self.current_step_index + 1}/{len(self.planned_steps)}",
            f"Iteration: {self.iteration_count}/{self.task_deadline}",
            f"Validation score: {self.validation_score:.1%}",
            f"Phase: {self.current_phase}",
        ]

        if self.failure_patterns:
            recent_failures = ", ".join(f"{k}({v})" for k, v in list(self.failure_patterns.items())[:3])
            lines.append(f"Recent failures: {recent_failures}")

        if self.next_actions_suggested:
            lines.append(f"Suggested next: {self.next_actions_suggested[0][:60]}")

        if self.requirement_graph_node_count:
            lines.append(
                f"Requirements: {self.requirement_graph_node_count} nodes, {self.open_ambiguity_count} ambiguities"
            )
        if self.requirement_coverage_summary:
            lines.append(f"Coverage: {self.requirement_coverage_summary}")
        if self.semantic_validation_summary:
            lines.append(f"Semantic: {self.semantic_validation_summary}")
        if self.repair_strategy_summary:
            lines.append(f"Repair: {self.repair_strategy_summary}")

        return "\n".join(lines)
