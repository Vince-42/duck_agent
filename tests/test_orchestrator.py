import tempfile
import unittest
from pathlib import Path

from agent import AgentConfig, DuckAgent
from orchestrator import MultiRoleOrchestrator
from roles import ROLE_PROFILES


class MultiRoleOrchestratorTests(unittest.TestCase):
    def make_agent(self, tmp_dir: str) -> DuckAgent:
        base = Path(tmp_dir)
        config = AgentConfig(
            spec_path=base / "secret_spec" / "SECRET_SPEC.md",
            logs_dir=base / "agent_logs",
            solution_command="python3 solution.py",
            max_iterations=2,
        )
        return DuckAgent(config)

    def test_role_registry_contains_all_16_roles(self) -> None:
        self.assertEqual(len(ROLE_PROFILES), 16)

    def test_prepare_turn_uses_understand_phase_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            orchestrator = MultiRoleOrchestrator()

            turn = orchestrator.prepare_turn(agent, "create a parser")

            self.assertEqual(turn.phase, "Understand")
            self.assertIn("Scout", turn.active_role_names)
            self.assertIn("Parser", turn.active_role_names)
            self.assertIn("Coordinator", turn.active_role_names)
            self.assertIn("Guardrail", turn.active_role_names)
            self.assertIn("Current coordination phase: Understand", turn.orchestration_context)

    def test_prepare_turn_uses_verify_phase_when_changes_are_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.completed_actions = 1
            agent.state.dirty_since_test = True
            agent.config.test_command = "python3 -m unittest"
            orchestrator = MultiRoleOrchestrator()

            turn = orchestrator.prepare_turn(agent, "fix the failing tests")

            self.assertEqual(turn.phase, "Verify")
            self.assertIn("Tester", turn.active_role_names)
            self.assertIn("Debugger", turn.active_role_names)
            self.assertIn("Repairer", turn.active_role_names)
            self.assertIn("Validator", turn.active_role_names)
            self.assertIn("block STOP until validation runs", turn.orchestration_context)

    def test_build_prompt_accepts_orchestration_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            prompt = agent.build_prompt("ship a small cli", "Current coordination phase: Build\nActive roles this turn: Builder, Coordinator")

            self.assertIn("Multi-role orchestration context:", prompt)
            self.assertIn("Current coordination phase: Build", prompt)
            self.assertIn("return only one final JSON action object", prompt)


if __name__ == "__main__":
    unittest.main()
