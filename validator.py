"""
Validation and result analysis.

Provides:
- Progress scoring (not just boolean success/failure)
- Failure pattern detection
- Stop condition evaluation
- Suggestion of next actions based on results
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ValidationLevel(str, Enum):
    """Severity of validation issues."""

    PASS = "pass"
    MINOR_ISSUE = "minor_issue"
    MAJOR_ISSUE = "major_issue"
    CRITICAL_FAILURE = "critical_failure"


@dataclass
class ValidationResult:
    """Result of validating an action outcome."""

    level: ValidationLevel
    score: float  # 0.0 to 1.0
    issues: list[str] = None
    evidence: str = ""
    recommended_next_action: str = ""

    def __post_init__(self) -> None:
        if self.issues is None:
            self.issues = []

    def is_success(self) -> bool:
        return self.level == ValidationLevel.PASS

    def is_failure(self) -> bool:
        return self.level == ValidationLevel.CRITICAL_FAILURE

    def is_repairable(self) -> bool:
        return self.level in {ValidationLevel.MINOR_ISSUE, ValidationLevel.MAJOR_ISSUE}


class ResultValidator:
    """Analyze and score action results."""

    @staticmethod
    def validate_test_result(exit_code: int, output: str, previous_score: float = 0.0) -> ValidationResult:
        """Validate a test run result."""
        if exit_code == 0:
            return ValidationResult(
                level=ValidationLevel.PASS,
                score=1.0,
                evidence="Tests passed",
                recommended_next_action="Consider STOP if all requirements met",
            )

        if exit_code == 124:
            return ValidationResult(
                level=ValidationLevel.CRITICAL_FAILURE,
                score=0.0,
                issues=["timeout"],
                evidence="Exceeded time limit",
                recommended_next_action="Check for infinite loops or heavy computations",
            )

        if exit_code == 1:
            import re

            match = re.search(r"(\d+)/(\d+) passed", output)
            if match:
                passed = int(match.group(1))
                total = int(match.group(2))
                score = passed / total if total > 0 else 0.0

                if score > previous_score:
                    return ValidationResult(
                        level=ValidationLevel.MAJOR_ISSUE,
                        score=score,
                        issues=[f"{total - passed} tests still failing"],
                        evidence=f"{passed}/{total} tests passing (improved)",
                        recommended_next_action="Continue repairs; progress detected",
                    )
                else:
                    return ValidationResult(
                        level=ValidationLevel.MAJOR_ISSUE,
                        score=score,
                        issues=[f"{total - passed} tests failing (no improvement)"],
                        evidence=f"{passed}/{total} tests passing (stalled)",
                        recommended_next_action="Try different fix strategy",
                    )

            return ValidationResult(
                level=ValidationLevel.CRITICAL_FAILURE,
                score=0.0,
                issues=["Test failed with exit code 1"],
                evidence=output[:200],
                recommended_next_action="Analyze error output; fix identified issues",
            )

        return ValidationResult(
            level=ValidationLevel.CRITICAL_FAILURE,
            score=0.0,
            issues=[f"Unexpected exit code: {exit_code}"],
            evidence=output[:200],
            recommended_next_action="Investigate root cause",
        )

    @staticmethod
    def validate_file_write(path: str, previous_size: int = 0, new_size: int = 0) -> ValidationResult:
        """Validate a file write operation."""
        if new_size == previous_size and previous_size > 0:
            return ValidationResult(
                level=ValidationLevel.MINOR_ISSUE,
                score=0.3,
                issues=["File size unchanged"],
                evidence=f"File size: {new_size} bytes",
                recommended_next_action="Verify if write was necessary",
            )

        if new_size == 0:
            return ValidationResult(
                level=ValidationLevel.CRITICAL_FAILURE,
                score=0.0,
                issues=["File is empty"],
                evidence="0 bytes written",
                recommended_next_action="Provide file content",
            )

        return ValidationResult(
            level=ValidationLevel.PASS,
            score=0.8,
            evidence=f"File written: {new_size} bytes",
            recommended_next_action="Run validation if tests available",
        )

    @staticmethod
    def validate_command_output(
        exit_code: int, stdout: str, stderr: str, command_type: str = "generic"
    ) -> ValidationResult:
        """Validate a command execution result."""
        if exit_code == 0 and stdout.strip():
            return ValidationResult(
                level=ValidationLevel.PASS,
                score=0.9,
                evidence=f"Command succeeded, output: {stdout[:100]}",
                recommended_next_action="Use output as evidence for next decision",
            )

        if exit_code == 0 and not stdout.strip():
            return ValidationResult(
                level=ValidationLevel.MINOR_ISSUE,
                score=0.5,
                issues=["Command succeeded but produced no output"],
                evidence="Exit code 0, empty stdout",
                recommended_next_action="Verify command was correct",
            )

        if exit_code == 124:
            return ValidationResult(
                level=ValidationLevel.CRITICAL_FAILURE,
                score=0.0,
                issues=["Command timed out"],
                evidence="Exceeded time limit",
                recommended_next_action="Check for infinite loops or user input",
            )

        return ValidationResult(
            level=ValidationLevel.MAJOR_ISSUE,
            score=0.2,
            issues=[f"Command failed with exit code {exit_code}"],
            evidence=f"Error: {stderr[:200]}",
            recommended_next_action="Fix command or investigate error",
        )

    @staticmethod
    def detect_infinite_loop(
        recent_actions: list[tuple[str, bool]], consecutive_no_progress: int
    ) -> bool:
        """Detect if agent is in an infinite loop."""
        if consecutive_no_progress > 10:
            return True

        if len(recent_actions) >= 5:
            recent_types = [a[0] for a in recent_actions[-5:]]
            if len(set(recent_types)) == 1:
                return True

        return False

    @staticmethod
    def detect_stagnation(
        validation_scores: list[float], window_size: int = 5
    ) -> bool:
        """Detect if validation scores aren't improving."""
        if len(validation_scores) < window_size:
            return False

        recent = validation_scores[-window_size:]
        return all(s == recent[0] for s in recent)

    @staticmethod
    def should_give_up(
        iteration: int,
        max_iterations: int,
        failure_count: int,
        max_retries: int,
        is_stagnated: bool,
        is_looping: bool,
    ) -> tuple[bool, str]:
        """Determine if we should stop trying."""
        if iteration >= max_iterations:
            return True, f"Reached max iterations ({iteration}/{max_iterations})"

        if failure_count >= max_retries:
            return True, f"Exceeded max retries ({failure_count}/{max_retries})"

        if is_stagnated:
            return True, "Validation score stagnated"

        if is_looping:
            return True, "Detected infinite loop pattern"

        return False, ""
