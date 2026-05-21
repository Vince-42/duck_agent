from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING

from roles import GOVERNANCE_ROLE_NAMES, PHASE_TO_ROLE_NAMES, ROLE_INDEX, ROLE_PROFILES, SHARED_RULES, RoleProfile

if TYPE_CHECKING:
    from agent import DuckAgent


@dataclass(frozen=True)
class OrchestratorTurn:
    phase: str
    active_role_names: tuple[str, ...]
    orchestration_context: str


class MultiRoleOrchestrator:
    def __init__(self, roles: tuple[RoleProfile, ...] = ROLE_PROFILES) -> None:
        self.roles = roles

    def determine_phase(self, agent: DuckAgent) -> str:
        if agent.state.completed_actions == 0:
            return "Understand"

        if agent.state.dirty_since_test or (agent.state.last_test_exit_code not in {None, 0}):
            return "Verify"

        return "Build"

    def active_roles_for_phase(self, phase: str) -> tuple[str, ...]:
        ordered_names = list(PHASE_TO_ROLE_NAMES[phase])
        for role_name in GOVERNANCE_ROLE_NAMES:
            if role_name not in ordered_names:
                ordered_names.append(role_name)
        return tuple(ordered_names)

    def build_constraints(self, agent: DuckAgent) -> list[str]:
        constraints: list[str] = [
            "Coordinator owns the final decision. Synthesize one final action instead of exposing internal debate.",
            "Only the execution shell may write files, run commands, run tests, or stop.",
            "Keep the final answer within the existing JSON action contract.",
        ]

        if agent.has_test_target():
            constraints.append("A test target exists. Prefer evidence-producing RUN_TESTS after material changes.")
        else:
            constraints.append("No test target exists right now. Do not choose RUN_TESTS unless built-in validation is applicable.")

        if agent.state.dirty_since_test and agent.has_test_target():
            constraints.append("Changes are dirty since the last test run. Validator and Guardrail should block STOP until tests run.")

        if agent.state.external_log_path:
            constraints.append(
                f"Runtime manages the external log file at {agent.state.external_log_path}. Never write to that path directly."
            )

        if agent.state.last_test_output and agent.state.last_test_output != "No tests run yet.":
            constraints.append("Debugger and Repairer must use the latest test output as evidence before proposing further edits.")

        return constraints

    def render_role_brief(self, role_name: str) -> str:
        role = ROLE_INDEX[role_name]
        return textwrap.dedent(
            f"""
            - {role.name} [{role.phase}]
              style: {role.personality_style}
              responsibility: {role.primary_responsibility}
              notices first: {', '.join(role.notices_first)}
              asks for: {', '.join(role.asks_for)}
              escalate when: {', '.join(role.escalate_when)}
              handoff to: {', '.join(role.handoff_to)}
            """
        ).strip()

    def build_orchestration_context(self, agent: DuckAgent, task_description: str, phase: str, active_roles: tuple[str, ...]) -> str:
        del task_description

        shared_rules = "\n".join(f"- {rule}" for rule in SHARED_RULES)
        constraints = "\n".join(f"- {rule}" for rule in self.build_constraints(agent))
        role_briefs = "\n".join(self.render_role_brief(role_name) for role_name in active_roles)

        return textwrap.dedent(
            f"""
            Multi-role system: internal 16-agent collaboration is enabled.
            Current coordination phase: {phase}
            Active roles this turn: {', '.join(active_roles)}

            Shared rules:
            {shared_rules}

            Deterministic constraints:
            {constraints}

            Role briefs:
            {role_briefs}

            Collaboration protocol:
            - Use the active roles internally to inspect the task, current repo state, logs, and evidence.
            - Summarize their best combined judgment into one final action.
            - Do not emit chain-of-thought or role-by-role transcripts.
            - Return exactly one JSON object matching the required action schema.
            - The final action should be the smallest high-confidence next step for the current phase.
            """
        ).strip()

    def prepare_turn(self, agent: DuckAgent, task_description: str) -> OrchestratorTurn:
        phase = self.determine_phase(agent)
        active_roles = self.active_roles_for_phase(phase)
        orchestration_context = self.build_orchestration_context(agent, task_description, phase, active_roles)
        return OrchestratorTurn(
            phase=phase,
            active_role_names=active_roles,
            orchestration_context=orchestration_context,
        )
