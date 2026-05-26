"""
Integration module for the improved orchestrator components.

Ties together:
- Orchestration state
- Context/chunk management
- Working memory
- Validation
- Structured logging

This module handles the orchestrator's core loop:
1. Load/update state
2. Check for stop conditions
3. Build working memory and context
4. Make decision
5. Execute action
6. Validate result
7. Log decision
8. Update state
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from context_manager import ContentChunker, ContentCache
from orchestration_state import OrchestratorState, StopReason
from structured_logging import StructuredLogger
from validator import ResultValidator, ValidationLevel
from working_memory import WorkingMemory


class ImprovedOrchestrator:
    """Core orchestration loop with explicit state and validation."""

    def __init__(
        self,
        logs_dir: Path | None = None,
        max_iterations: int = 100,
        max_retries_per_step: int = 3,
    ) -> None:
        self.state = OrchestratorState(task_deadline=max_iterations)
        self.state.max_retries_per_step = max_retries_per_step
        self.working_memory = WorkingMemory()
        self.content_cache = ContentCache()
        self.logger = StructuredLogger(logs_dir or Path("agent_logs"))

    def process_long_task_description(self, task_description: str, summarize_fn: Any = None) -> str:
        """
        Process a potentially long task description.

        If large, chunks it and returns summarized version.
        If small, returns as-is.
        """
        if not ContentChunker.needs_chunking(task_description):
            return task_description

        chunks = ContentChunker.smart_chunk_code(task_description)
        self.state.chunks_total = len(chunks)

        for chunk in chunks:
            self.content_cache.add_chunk(chunk)

            if summarize_fn:
                summary = ContentChunker.summarize_chunk(chunk, summarize_fn)
                self.content_cache.add_summary(chunk.index, summary)

        if not summarize_fn:
            return ContentChunker.merge_summaries([chunk.content for chunk in chunks], max_summary_chars=2000)

        return self.content_cache.merged_summary()

    def update_working_memory_from_result(
        self, action_type: str, validation_result: Any, evidence: str = ""
    ) -> None:
        """Update working memory based on action result."""
        if hasattr(validation_result, "is_success") and validation_result.is_success():
            self.working_memory.add_lesson(f"{action_type} succeeded: {validation_result.evidence}")
        elif hasattr(validation_result, "is_failure") and validation_result.is_failure():
            self.working_memory.add_error(
                action_type, validation_result.evidence, recovery=validation_result.recommended_next_action
            )
        elif hasattr(validation_result, "recommended_next_action"):
            if validation_result.recommended_next_action:
                self.working_memory.set_next_actions([validation_result.recommended_next_action])

    def should_stop(self) -> tuple[bool, StopReason | None, str]:
        """Evaluate stop conditions."""
        # Check iteration limit
        if self.state.iteration_count >= self.state.task_deadline:
            return True, StopReason.MAX_ITERATIONS, f"Reached max iterations {self.state.iteration_count}"

        # Check infinite loop
        if self.state.detect_infinite_loop():
            return True, StopReason.STAGNATION, "Detected infinite loop pattern"

        # Check stagnation
        if self.state.detect_stagnation():
            return True, StopReason.NO_PROGRESS, "Validation score stagnated"

        return False, None, ""

    def decide_next_phase(self) -> str:
        """Decide which phase to transition to based on state."""
        if self.state.iteration_count == 0:
            return "Understand"

        if self.state.iteration_count < 3:
            return "Build"

        if self.state.files_written > 0 and self.state.tests_failed > 0:
            return "Verify"

        if self.state.actions_executed > 5 and self.state.validation_score > 0.5:
            return "Govern"

        return self.state.current_phase

    def get_context_for_model(self, max_chars: int = 2000) -> str:
        """Build context string for the model."""
        lines = [
            "--- ORCHESTRATION STATE ---",
            self.state.summary_for_prompt(),
            "--- WORKING MEMORY ---",
            self.working_memory.to_prompt_section(max_chars=500),
        ]

        return "\n\n".join(lines)

    def log_decision(
        self,
        decision_type: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Log a decision."""
        self.logger.log_decision(
            decision_type=decision_type,
            reason=reason,
            evidence=self.state.to_dict(),
            metadata=metadata or {},
        )
