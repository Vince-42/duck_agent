import json
import os
import tempfile
import unittest
from io import BytesIO
from io import StringIO
from contextlib import contextmanager
from pathlib import Path
from urllib import error
from unittest.mock import MagicMock, patch

from agent import AgentConfig, AgentLogger, DuckAgent, build_parser, config_from_args, run_doctor


@contextmanager
def temporary_cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class AgentLoggerTests(unittest.TestCase):
    def test_logger_creates_required_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logs_dir = Path(tmp_dir) / "agent_logs"
            logger = AgentLogger(logs_dir)

            self.assertEqual(logger.logs_dir, logs_dir)

            for path in logger.paths.values():
                self.assertTrue(path.exists())


class CliTests(unittest.TestCase):
    def test_config_from_args_uses_cli_values(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--spec-path",
                "custom/spec.md",
                "--logs-dir",
                "custom-logs",
                "--task",
                "ship a cli tool",
                "--task-file",
                "custom/task.md",
                "--model",
                "demo-model",
                "--ollama-url",
                "http://localhost:9999/api/generate",
                "--max-iterations",
                "7",
                "--solution-command",
                "python3 alt_solution.py",
            ]
        )

        config = config_from_args(args)

        self.assertEqual(config.spec_path, Path("custom/spec.md"))
        self.assertEqual(config.logs_dir, Path("custom-logs"))
        self.assertEqual(config.task, "ship a cli tool")
        self.assertEqual(config.task_file, Path("custom/task.md"))
        self.assertEqual(config.model, "demo-model")
        self.assertEqual(config.ollama_url, "http://localhost:9999/api/generate")
        self.assertEqual(config.max_iterations, 7)
        self.assertEqual(config.solution_command, "python3 alt_solution.py")

    def test_run_doctor_accepts_direct_task_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            config = AgentConfig(
                spec_path=base / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=base / "agent_logs",
                task="build a parser",
            )
            stdout = StringIO()

            with patch("agent.request.urlopen", side_effect=RuntimeError("offline")):
                with patch("sys.stdout", stdout):
                    exit_code = run_doctor(config)

            self.assertEqual(exit_code, 1)
            self.assertIn("task provided: yes", stdout.getvalue())

    def test_run_doctor_reports_missing_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            config = AgentConfig(
                spec_path=base / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=base / "agent_logs",
            )
            stdout = StringIO()

            with patch("agent.request.urlopen", side_effect=RuntimeError("offline")):
                with patch("sys.stdout", stdout):
                    exit_code = run_doctor(config)

            self.assertEqual(exit_code, 1)
            self.assertIn("hidden spec present: no", stdout.getvalue())
            self.assertIn("model endpoint reachable: no", stdout.getvalue())


class DuckAgentTests(unittest.TestCase):
    def make_agent(self, tmp_dir: str) -> DuckAgent:
        base = Path(tmp_dir)
        config = AgentConfig(
            spec_path=base / "secret_spec" / "SECRET_SPEC.md",
            logs_dir=base / "agent_logs",
            solution_command="python3 solution.py",
            max_iterations=2,
        )
        return DuckAgent(config)

    def test_read_spec_raises_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with self.assertRaises(FileNotFoundError):
                agent.load_task_description()

    def test_load_task_description_prefers_direct_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.task = "write a tiny cli"

            self.assertEqual(agent.load_task_description(), "write a tiny cli")

    def test_load_task_description_reads_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            task_file = Path(tmp_dir) / "task.md"
            task_file.write_text("implement feature x", encoding="utf-8")
            agent.config.task_file = task_file

            self.assertEqual(agent.load_task_description(), "implement feature x")

    def test_parse_action_normalizes_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            action = agent.parse_action(
                json.dumps(
                    {
                        "action": "write_file",
                        "reason": "create entrypoint",
                        "path": "solution.py",
                        "content": "print('ok')",
                        "command": "",
                    }
                )
            )

            self.assertEqual(action["action"], "WRITE_FILE")
            self.assertEqual(action["path"], "solution.py")

    def test_parse_action_accepts_fenced_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            action = agent.parse_action(
                """```json
{
  \"action\": \"RUN_COMMAND\",
  \"reason\": \"Evaluate the task expression.\",
  \"path\": \"\",
  \"content\": \"\",
  \"command\": \"echo $((1+1))\"
}
```"""
            )

            self.assertEqual(action["action"], "RUN_COMMAND")
            self.assertEqual(action["command"], "echo $((1+1))")

    def test_parse_action_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with self.assertRaises(ValueError):
                agent.parse_action("not-json")

    def test_write_file_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with self.assertRaises(ValueError):
                agent.write_file("../oops.py", "bad")

    def test_run_public_tests_without_runner_returns_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            result = agent.run_public_tests()

            self.assertEqual(result.summary, "No test command configured and public test runner not available yet.")
            self.assertFalse(result.progress)

    def test_run_public_tests_uses_custom_test_command_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.test_command = "python3 -m unittest"

            completed = MagicMock()
            completed.returncode = 0
            completed.stdout = "ok"
            completed.stderr = ""

            with patch.object(agent, "run_command", return_value=completed) as run_command:
                result = agent.run_public_tests()

            run_command.assert_called_once_with("python3 -m unittest", category="test_runs")
            self.assertIn("exit=0", result.summary)
            self.assertEqual(result.test_exit_code, 0)

    def test_build_prompt_reports_when_no_test_target_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            prompt = agent.build_prompt("1+1")

            self.assertIn("Test target available: no", prompt)
            self.assertIn("Last action result:\nNo actions executed yet.", prompt)

    def test_perform_iteration_tracks_command_output_for_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            completed = MagicMock()
            completed.returncode = 0
            completed.stdout = "2\n"
            completed.stderr = ""

            with patch.object(
                agent,
                "call_model",
                return_value=json.dumps(
                    {
                        "action": "RUN_COMMAND",
                        "reason": "evaluate expression",
                        "path": "",
                        "content": "",
                        "command": "echo $((1+1))",
                    }
                ),
            ):
                with patch.object(agent, "run_command", return_value=completed):
                    should_continue = agent.perform_iteration("1+1")

            self.assertTrue(should_continue)
            self.assertEqual(agent.state.completed_actions, 1)
            self.assertIn("STDOUT: 2", agent.state.last_action_summary)

    def test_perform_iteration_refuses_stop_before_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with patch.object(
                agent,
                "call_model",
                return_value=json.dumps(
                    {
                        "action": "STOP",
                        "reason": "done",
                        "path": "",
                        "content": "",
                        "command": "",
                    }
                ),
            ):
                with self.assertRaises(ValueError):
                    agent.perform_iteration("1+1")

    def test_perform_iteration_stops_on_repeated_noop_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            repeated_write = json.dumps(
                {
                    "action": "WRITE_FILE",
                    "reason": "create hello file",
                    "path": "hello.txt",
                    "content": "Hello, World!\n",
                    "command": "",
                }
            )

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(agent, "call_model", return_value=repeated_write):
                    should_continue = agent.perform_iteration("create hello world file")

                self.assertTrue(should_continue)
                self.assertEqual(agent.state.material_changes, 1)

                with patch.object(agent, "call_model", return_value=repeated_write):
                    should_continue = agent.perform_iteration("create hello world file")

                self.assertTrue(should_continue)

                with patch.object(agent, "call_model", return_value=repeated_write):
                    should_continue = agent.perform_iteration("create hello world file")

                self.assertFalse(should_continue)
                self.assertIn("stalled: repeated equivalent write", agent.state.last_action_summary)

    def test_stop_requires_test_after_material_change_when_tests_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.completed_actions = 1
            agent.state.dirty_since_test = True
            agent.config.test_command = "python3 -m unittest"

            with patch.object(
                agent,
                "call_model",
                return_value=json.dumps(
                    {
                        "action": "STOP",
                        "reason": "done",
                        "path": "",
                        "content": "",
                        "command": "",
                    }
                ),
            ):
                with self.assertRaises(ValueError):
                    agent.perform_iteration("create file")

    def test_perform_iteration_writes_file_from_model_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create solution",
                            "path": "solution.py",
                            "content": "print('hello')\n",
                            "command": "",
                        }
                    ),
                ):
                    should_continue = agent.perform_iteration("spec body")

                self.assertTrue(should_continue)
                self.assertEqual(Path("solution.py").read_text(encoding="utf-8"), "print('hello')\n")
                self.assertEqual(agent.state.material_changes, 1)
                Path("solution.py").unlink()

    def test_run_returns_zero_when_model_requests_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text("spec body", encoding="utf-8")

            with patch.object(agent, "perform_iteration", return_value=False):
                exit_code = agent.run()

            self.assertEqual(exit_code, 0)

    def test_call_model_returns_response_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            fake_response = MagicMock()
            fake_response.read.return_value = b'{"response":"done"}'
            fake_response.__enter__.return_value = fake_response
            fake_response.__exit__.return_value = False

            with patch("agent.request.urlopen", return_value=fake_response):
                result = agent.call_model("hello")

            self.assertEqual(result, "done")

    def test_call_model_falls_back_to_openai_compatible_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            def fake_urlopen(http_request, timeout=120):
                if http_request.full_url.endswith("/api/generate"):
                    raise error.HTTPError(
                        http_request.full_url,
                        404,
                        "Not Found",
                        hdrs=None,
                        fp=BytesIO(b'{"error":"model not found"}'),
                    )

                fake_response = MagicMock()
                fake_response.read.return_value = b'{"choices":[{"message":{"content":"STOP"}}]}'
                fake_response.__enter__.return_value = fake_response
                fake_response.__exit__.return_value = False
                return fake_response

            with patch("agent.request.urlopen", side_effect=fake_urlopen):
                result = agent.call_model("hello")

            self.assertEqual(result, "STOP")

    def test_call_model_surfaces_http_error_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            def fake_urlopen(http_request, timeout=120):
                raise error.HTTPError(
                    http_request.full_url,
                    404,
                    "Not Found",
                    hdrs=None,
                    fp=BytesIO(b'{"error":"model \"qwen2.5-coder:7b\" not found, try pulling it first"}'),
                )

            with patch("agent.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(RuntimeError) as exc_info:
                    agent.call_model("hello")

            self.assertIn("qwen2.5-coder:7b", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
