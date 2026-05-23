from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from roles import GOVERNANCE_ROLE_NAMES, PHASE_TO_ROLE_NAMES, ROLE_PROFILES, SHARED_RULES, RoleProfile

if TYPE_CHECKING:
    from agent import DuckAgent


@dataclass(frozen=True)
class OrchestratorTurn:
    phase: str
    active_role_names: tuple[str, ...]
    orchestration_context: str


class MultiRoleOrchestrator:
    def __init__(self, roles: tuple[RoleProfile, ...] = ROLE_PROFILES, orchestrator_config_path: str | Path | None = None) -> None:
        self.roles = roles
        self.shared_rules = SHARED_RULES
        self.phase_to_role_names = dict(PHASE_TO_ROLE_NAMES)
        self.governance_role_names = tuple(GOVERNANCE_ROLE_NAMES)
        self.phase_initial = "Understand"
        self.phase_dirty = "Verify"
        self.phase_default = "Build"
        self.switching_logic: tuple[str, ...] = ()
        self.memory_retention: tuple[str, ...] = ()
        self.logging_rules: tuple[str, ...] = ()

        if orchestrator_config_path:
            self.load_from_json(Path(orchestrator_config_path))
        else:
            self.role_index = {role.name: role for role in self.roles}

    def load_from_json(self, config_path: Path) -> None:
        if not config_path.exists():
            raise FileNotFoundError(f"Orchestrator config not found: {config_path}")

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.shared_rules = tuple(config.get("shared_rules", SHARED_RULES))
        self.switching_logic = tuple(config.get("orchestrator", {}).get("switching_logic", ()))
        self.memory_retention = tuple(config.get("memory_policy", {}).get("retention", ()))
        self.logging_rules = tuple(config.get("logging_policy", {}).get("rules", ()))

        loaded_roles: list[RoleProfile] = []
        for role_data in config.get("agents", []):
            loaded_roles.append(
                RoleProfile(
                    name=role_data["name"],
                    phase=self.phase_for_role(role_data["name"], config),
                    personality_style=role_data.get("personality_style", ""),
                    primary_responsibility=role_data.get("primary_responsibility", ""),
                    notices_first=tuple(role_data.get("notices_first", ())),
                    tends_to_miss=tuple(role_data.get("tends_to_miss", ())),
                    asks_for=tuple(role_data.get("asks_for", ())),
                    escalate_when=tuple(role_data.get("escalate_when", ())),
                    handoff_to=tuple(role_data.get("handoff_to", ())),
                    prompt_template=role_data.get("prompt_template", ""),
                )
            )

        if loaded_roles:
            self.roles = tuple(loaded_roles)

        phase_mapping: dict[str, tuple[str, ...]] = {}
        for phase in config.get("collaboration_phases", []):
            phase_mapping[phase["name"]] = tuple(phase.get("primary_agents", ()))

        if phase_mapping:
            self.phase_to_role_names = phase_mapping

        governance = self.phase_to_role_names.get("Govern")
        if governance:
            self.governance_role_names = tuple(governance)

        decision_rules = {item.get("trigger"): item.get("activate") for item in config.get("decision_rules", [])}
        if decision_rules.get("spec_received") in self.phase_to_role_names:
            self.phase_initial = decision_rules["spec_received"]
        if decision_rules.get("tests_needed") in self.phase_to_role_names:
            self.phase_dirty = decision_rules["tests_needed"]
        self.role_index = {role.name: role for role in self.roles}

    def phase_for_role(self, role_name: str, config: dict[str, Any]) -> str:
        for phase in config.get("collaboration_phases", []):
            if role_name in phase.get("primary_agents", []):
                return str(phase.get("name", "Build"))
        return "Build"

    def determine_phase(self, agent: DuckAgent) -> str:
        if agent.state.completed_actions == 0:
            return self.phase_initial

        if agent.state.dirty_since_test and agent.has_validation_target():
            return self.phase_dirty

        return self.phase_default

    def active_roles_for_phase(self, phase: str) -> tuple[str, ...]:
        ordered_names = list(self.phase_to_role_names.get(phase, ()))[:3]
        compact_governance_roles: tuple[str, ...] = ("Coordinator",)
        for role_name in compact_governance_roles:
            if role_name in self.role_index and role_name not in ordered_names:
                ordered_names.append(role_name)
        return tuple(ordered_names)

    def build_constraints(self, agent: DuckAgent) -> list[str]:
        constraints: list[str] = [
            "You are one structured cognition engine that switches reasoning modes internally, not a swarm of worker agents.",
            "Coordinator owns the final decision. Synthesize one final action instead of exposing internal debate.",
            "Only the execution shell may write files, run commands, run tests, or stop.",
            "Keep the final answer within the existing JSON action contract.",
        ]

        if agent.has_validation_target():
            constraints.append("A validation target exists. Prefer RUN_TESTS after material changes or before STOP.")
        else:
            constraints.append("No validation target exists right now. Do not choose RUN_TESTS until a runnable or validatable artifact exists.")

        if agent.state.dirty_since_test and agent.has_validation_target():
            constraints.append("Changes are dirty since the last validation. Validator and Guardrail should block STOP until validation runs.")

        if agent.state.external_log_path:
            constraints.append(
                f"Runtime manages the external log file at {agent.state.external_log_path}. Never write to that path directly."
            )

        if agent.state.primary_artifact_path:
            constraints.append(f"Current primary artifact candidate: {agent.state.primary_artifact_path}.")

        if agent.task_requires_runnable_program():
            constraints.append(
                "The task asks for a runnable program/script/CLI. Produce an executable entrypoint or main flow, not only helper functions."
            )

        if agent.state.last_test_output and agent.state.last_test_output != "No tests run yet.":
            constraints.append("Debugger and Repairer must use the latest validation output as evidence before proposing further edits.")

        return constraints

    def render_role_brief(self, role_name: str) -> str:
        role = self.role_index[role_name]
        summary = role.primary_responsibility or role.prompt_template
        return f"- {role.name}: {summary}"

    def build_orchestration_context(self, agent: DuckAgent, task_description: str, phase: str, active_roles: tuple[str, ...]) -> str:
        del task_description

        shared_rules = "\n".join(f"- {rule}" for rule in self.shared_rules)
        constraints = "\n".join(f"- {rule}" for rule in self.build_constraints(agent))
        role_briefs = "\n".join(self.render_role_brief(role_name) for role_name in active_roles)
        switching_logic = self.switching_logic[0] if self.switching_logic else "Use the current phase and evidence to pick the next internal mode."
        active_roles_str = ", ".join(active_roles)

        template = textwrap.dedent("""\
            Structured cognition engine is enabled.
            Current coordination phase: {phase}
            Active cognitive modes this turn: {active_roles}

            Evidence priority:
            - Task/spec/latest test evidence outrank role preferences.

            Shared rules:
            {shared_rules}

            Deterministic constraints:
            {constraints}

            Switching guide: {switching_logic}

            Active mode briefs:
            {role_briefs}

            Return only one final JSON action object.
            Choose the smallest evidence-backed next step.
        """)

        return template.format(
            phase=phase,
            active_roles=active_roles_str,
            shared_rules=shared_rules,
            constraints=constraints,
            switching_logic=switching_logic,
            role_briefs=role_briefs,
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
