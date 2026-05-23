import json
import os
import subprocess
import tempfile
import unittest
from io import BytesIO
from io import StringIO
from contextlib import contextmanager
from pathlib import Path
from urllib import error
from unittest.mock import MagicMock, patch

from agent import ActionOutcome, AgentConfig, AgentLogger, DuckAgent, build_parser, config_from_args, run_doctor


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
                "--provider",
                "groq",
                "--model",
                "demo-model",
                "--ollama-url",
                "http://localhost:9999/api/generate",
                "--groq-url",
                "https://api.groq.com/openai/v1",
                "--groq-model",
                "mixtral-8x7b-32768",
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
        self.assertEqual(config.provider, "groq")
        self.assertEqual(config.model, "demo-model")
        self.assertEqual(config.ollama_url, "http://localhost:9999/api/generate")
        self.assertEqual(config.groq_url, "https://api.groq.com/openai/v1")
        self.assertEqual(config.groq_model, "mixtral-8x7b-32768")
        self.assertEqual(config.max_iterations, 7)
        self.assertEqual(config.solution_command, "python3 alt_solution.py")

    def test_config_from_args_accepts_any_positional_spec_file(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["custom-knitting-input.txt"])

        config = config_from_args(args)

        self.assertEqual(config.spec_path, Path("custom-knitting-input.txt"))

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

    def test_primary_task_text_reads_spec_file_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text("create a command-line program in python", encoding="utf-8")

            self.assertEqual(agent.primary_task_text(), "create a command-line program in python")

    def test_task_requires_runnable_program_uses_spec_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text("Create a Python command-line program called demo.py", encoding="utf-8")

            self.assertTrue(agent.task_requires_runnable_program())

    def test_task_looks_like_file_processing_cli_uses_spec_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text(
                "Create a Python command-line program that reads a UTF-8 text file and prints exactly one JSON object to stdout.",
                encoding="utf-8",
            )

            self.assertTrue(agent.task_looks_like_file_processing_cli())

    def test_task_requests_text_stats_fields_uses_spec_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text(
                "The JSON must contain these keys: line_count, word_count, char_count, top_words.",
                encoding="utf-8",
            )

            self.assertTrue(agent.task_requests_text_stats_fields())

    def test_detect_requested_log_output_path_from_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="give me all the log of everything that you do in a new file called iamthelog",
            )

            agent = DuckAgent(config)

            self.assertEqual(agent.state.external_log_path, "iamthelog")

    def test_detect_primary_artifact_path_for_introduction_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="Generate an introduction message toward Vincent, a student at 42. I want to sell softwares products",
            )

            agent = DuckAgent(config)

            self.assertEqual(agent.detect_primary_artifact_path(), "introduction.txt")

    def test_detect_primary_artifact_path_for_c_hello_world_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a hello world program in C",
            )

            agent = DuckAgent(config)

            self.assertEqual(agent.detect_primary_artifact_path(), "hello.c")

    def test_detect_primary_artifact_path_for_python_calculator_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a calculator program in python",
            )

            agent = DuckAgent(config)

            self.assertEqual(agent.detect_primary_artifact_path(), "calculator.py")

    def test_is_logging_only_task_detects_runtime_log_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="give me all the log of everything that you do in a new file called iamthelog2",
            )

            agent = DuckAgent(config)

            self.assertTrue(agent.is_logging_only_task())

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

    def test_write_file_blocks_prompt_regurgitation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="Create a Python command-line program called text_stats.py.\nIt must print exactly one JSON object to stdout.\nAdd optional support for --top N.",
            )
            agent = DuckAgent(config)

            bad_content = (
                "# Complex Python CLI Requirement\n\n"
                "Current iteration: 4\n"
                "Last test output:\nValidation expected line_count=2\n"
                "Persistent iteration memory:\n"
            )
            outcome = agent.write_file("text_stats.py", bad_content)

            self.assertFalse(outcome.progress)
            self.assertIn("prompt/spec/runtime text", outcome.summary)

    def test_write_file_blocks_lowercase_prompt_regurgitation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="Create a Python command-line program called text_stats.py.\nIt must print exactly one JSON object to stdout.\nAdd optional support for --top N.",
            )
            agent = DuckAgent(config)

            bad_content = (
                "current iteration: 2\n"
                "last test output:\ntraceback\n"
                "persistent iteration memory:\n"
                "write target guidance:\n"
            )
            outcome = agent.write_file("text_stats.py", bad_content)

            self.assertFalse(outcome.progress)
            self.assertIn("prompt/spec/runtime text", outcome.summary)

    def test_run_public_tests_without_runner_returns_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with temporary_cwd(Path(tmp_dir)):
                result = agent.run_public_tests()

            self.assertEqual(
                result.summary,
                "No test command configured, public test runner not available, and no built-in validation applied yet.",
            )
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

    def test_external_spec_path_provides_public_test_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            work_dir = base / "work"
            spec_dir = base / "release" / "secret_spec"
            runner = spec_dir / "test_runner" / "run_tests.py"
            work_dir.mkdir()
            runner.parent.mkdir(parents=True)
            runner.write_text("print('runner')\n", encoding="utf-8")

            agent = self.make_agent(str(work_dir))
            agent.config.spec_path = spec_dir / "SECRET_SPEC.md"

            with temporary_cwd(work_dir):
                self.assertEqual(agent.public_test_runner_path(), runner)
                self.assertTrue(agent.has_external_test_target())

    def test_task_file_run_ignores_hackathon_public_test_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            work_dir = base / "work"
            task_file = base / "task.md"
            local_runner = work_dir / "secret_spec" / "test_runner" / "run_tests.py"
            work_dir.mkdir()
            local_runner.parent.mkdir(parents=True)
            local_runner.write_text("print('runner')\n", encoding="utf-8")
            task_file.write_text(
                "Create a Python command-line program called text_stats.py.\n"
                "It must read a UTF-8 text file and print exactly one JSON object to stdout.\n",
                encoding="utf-8",
            )

            agent = self.make_agent(str(work_dir))
            agent.config.task_file = task_file

            with temporary_cwd(work_dir):
                self.assertIsNone(agent.public_test_runner_path())
                self.assertFalse(agent.has_external_test_target())

    def test_run_public_tests_uses_runner_next_to_external_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            work_dir = base / "work"
            spec_dir = base / "release" / "secret_spec"
            runner = spec_dir / "test_runner" / "run_tests.py"
            work_dir.mkdir()
            runner.parent.mkdir(parents=True)
            runner.write_text("print('runner')\n", encoding="utf-8")

            agent = self.make_agent(str(work_dir))
            agent.config.spec_path = spec_dir / "SECRET_SPEC.md"
            completed = MagicMock()
            completed.returncode = 1
            completed.stdout = "Overall [....] 0/150 passed (0.0%)"
            completed.stderr = ""

            with temporary_cwd(work_dir):
                with patch.object(agent, "run_command", return_value=completed) as run_command:
                    result = agent.run_public_tests()

            command = run_command.call_args.args[0]
            self.assertIn(str(runner), command)
            self.assertIn("--compiler", command)
            self.assertEqual(result.test_exit_code, 1)
            self.assertIn("0/150 passed", result.summary)

    def test_run_public_tests_falls_back_to_builtin_c_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "hello.c"

            with temporary_cwd(Path(tmp_dir)):
                Path("hello.c").write_text('#include <stdio.h>\nint main(){printf("Hello\\n");return 0;}\n', encoding="utf-8")
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = "Hello\n"
                completed.stderr = ""

                with patch.object(agent, "run_command", return_value=completed) as run_command:
                    result = agent.run_public_tests()

            self.assertEqual(result.test_exit_code, 0)
            run_command.assert_called_once()

    def test_build_prompt_reports_when_no_test_target_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with temporary_cwd(Path(tmp_dir)):
                prompt = agent.build_prompt("1+1")

            self.assertIn("Test target available: no", prompt)
            self.assertIn("Last action result:\nNo actions executed yet.", prompt)

    def test_build_prompt_mentions_runtime_managed_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.external_log_path = "iamthelog"

            prompt = agent.build_prompt("log everything to iamthelog")

            self.assertIn("Runtime-managed external log file:\niamthelog", prompt)
            self.assertIn("do not write to that file yourself", prompt)

    def test_build_prompt_truncates_large_runtime_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.last_test_output = "x" * 2000
            agent.state.last_action_summary = "y" * 1000

            prompt = agent.build_prompt("create a calculator in python")

            self.assertLess(len(prompt), 4000)
            self.assertIn("...", prompt)

    def test_build_prompt_stays_generic_without_public_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with temporary_cwd(Path(tmp_dir)):
                prompt = agent.build_prompt("create a small parser")

            self.assertNotIn("Write your code to solution.py", prompt)
            self.assertNotIn("python3 solution.py compile <test_file>", prompt)

    def test_build_prompt_forbids_copying_requirements_text_into_output_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            prompt = agent.build_prompt("create a small parser")

            self.assertIn("Do not copy the task description, requirements markdown, or prompt/rules text into output files.", prompt)

    def test_build_prompt_adds_repair_guidance_after_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "text_stats.py"
            agent.state.last_test_output = "exit=1\nSyntaxError"
            agent.state.last_validation_failures = ["top_n"]

            prompt = agent.build_prompt("create a small parser")

            self.assertIn("Repair guidance:", prompt)
            self.assertIn("Repair text_stats.py in place instead of restarting from scratch.", prompt)
            self.assertIn("The current failing checks are: top_n.", prompt)
            self.assertIn("Do not choose RUN_TESTS again until you have changed the artifact", prompt)

    def test_build_prompt_adds_validation_feedback_and_convergence_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "text_stats.py"
            agent.state.last_test_output = "exit=1\nValidation failed"
            agent.state.last_validation_passes = ["syntax", "stdout_json"]
            agent.state.last_validation_failures = ["semantic_counts", "top_n"]
            agent.state.rewrite_stagnation_count = 2

            prompt = agent.build_prompt("create a parser")

            self.assertIn("Validation feedback:", prompt)
            self.assertIn("Passed checks: syntax, stdout_json.", prompt)
            self.assertIn("Failing checks: semantic_counts, top_n.", prompt)
            self.assertIn("Convergence warning:", prompt)

    def test_build_prompt_includes_persistent_iteration_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "solution.py"
            agent.state.last_action_summary = "WRITE_FILE solution.py -> changed"
            agent.state.last_validation_failures = ["semantic_counts"]
            agent.state.last_test_output = "exit=0\n{}\nValidation expected line_count=2, word_count=9, char_count=49."

            with temporary_cwd(Path(tmp_dir)):
                Path("solution.py").write_text("print('hello')\n", encoding="utf-8")
                prompt = agent.build_prompt("create a command-line program in python")

            self.assertIn("Persistent iteration memory:", prompt)
            self.assertIn("Artifact target: solution.py", prompt)
            self.assertIn("Next repair objective:", prompt)
            self.assertIn("Validation expected line_count=2, word_count=9, char_count=49.", prompt)

    def test_build_prompt_forces_non_test_action_after_rerun_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "text_stats.py"
            agent.state.last_validation_failures = ["semantic_counts"]
            agent.state.last_action_summary = "Action rejected: Refusing to rerun validation before repairing failing checks: semantic_counts"

            with temporary_cwd(Path(tmp_dir)):
                Path("text_stats.py").write_text("print('bad')\n", encoding="utf-8")
                prompt = agent.build_prompt("create a command-line program in python")

            self.assertIn("Do not choose RUN_TESTS next.", prompt)
            self.assertIn("Your next action must be WRITE_FILE", prompt)

    def test_build_prompt_includes_current_artifact_excerpt_after_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "text_stats.py"
            agent.state.last_validation_failures = ["semantic_counts"]

            with temporary_cwd(Path(tmp_dir)):
                Path("text_stats.py").write_text("def main():\n    print('bad')\n", encoding="utf-8")
                prompt = agent.build_prompt("create a command-line program in python")

            self.assertIn("Current artifact excerpt:", prompt)
            self.assertIn("def main():", prompt)
            self.assertIn("Patch this artifact directly", prompt)

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
                should_continue = agent.perform_iteration("1+1")

            self.assertTrue(should_continue)
            self.assertIn("Action rejected: Refusing to STOP before executing any concrete action", agent.state.last_action_summary)

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
                should_continue = agent.perform_iteration("create file")

            self.assertTrue(should_continue)
            self.assertIn("Action rejected: Refusing to STOP while changes have not been validated", agent.state.last_action_summary)

    def test_perform_iteration_refuses_rerun_tests_before_repairing_failed_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.last_validation_failures = ["top_n"]
            agent.state.last_test_fingerprint = "VALIDATE:python:text-stats:text_stats.py"
            agent.state.last_action_fingerprint = "VALIDATE:python:text-stats:text_stats.py"

            with patch.object(
                agent,
                "call_model",
                return_value=json.dumps(
                    {
                        "action": "RUN_TESTS",
                        "reason": "rerun tests",
                        "path": "",
                        "content": "",
                        "command": "",
                    }
                ),
            ):
                should_continue = agent.perform_iteration("create a command-line program in python")

            self.assertTrue(should_continue)
            self.assertIn("Refusing to rerun validation before repairing failing checks: top_n", agent.state.last_action_summary)

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

                self.assertFalse(should_continue)
                self.assertEqual(Path("solution.py").read_text(encoding="utf-8"), "print('hello')\n")
                self.assertEqual(agent.state.material_changes, 1)
                Path("solution.py").unlink()

    def test_perform_iteration_sets_primary_artifact_from_first_supported_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = ""

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create calculator",
                            "path": "calculator.py",
                            "content": "print('ready')\n",
                            "command": "",
                        }
                    ),
                ):
                    should_continue = agent.perform_iteration("create a calculator program in python")

                self.assertFalse(should_continue)
                self.assertEqual(agent.state.primary_artifact_path, "calculator.py")

    def test_perform_iteration_replaces_missing_guessed_artifact_with_real_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "solution.py"

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create actual artifact",
                            "path": "calculator.py",
                            "content": "print('2')\n",
                            "command": "",
                        }
                    ),
                ):
                    should_continue = agent.perform_iteration("create a calculator program in python")

                self.assertFalse(should_continue)
                self.assertEqual(agent.state.primary_artifact_path, "calculator.py")

    def test_perform_iteration_recovers_empty_write_path_from_primary_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "hello.py"

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create hello world script",
                            "path": "",
                            "content": "def main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
                            "command": "",
                        }
                    ),
                ):
                    validation = ActionOutcome(
                        summary="exit=0\nHello, World!\n",
                        progress=True,
                        fingerprint="RUN_TESTS:hello",
                        test_exit_code=0,
                    )
                    with patch.object(agent, "run_builtin_validation", return_value=validation):
                        should_continue = agent.perform_iteration("create a hello world program in python")

                self.assertFalse(should_continue)
                self.assertTrue(Path("hello.py").exists())

    def test_syntax_only_python_validation_does_not_auto_complete_runnable_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a parser program in python",
                max_iterations=3,
            )
            agent = DuckAgent(config)

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create parser program",
                            "path": "parser.py",
                            "content": "def main():\n    return 0\n\nif __name__ == '__main__':\n    main()\n",
                            "command": "",
                        }
                    ),
                ):
                    completed = MagicMock()
                    completed.returncode = 0
                    completed.stdout = ""
                    completed.stderr = ""
                    with patch.object(agent, "run_command", return_value=completed):
                        should_continue = agent.perform_iteration("create a parser program in python")

                self.assertTrue(should_continue)
                self.assertEqual(agent.state.last_test_fingerprint, "VALIDATE:python:syntax:parser.py")

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
                    should_continue = agent.perform_iteration("create a parser program in python")

                self.assertTrue(should_continue)
                self.assertIn("syntax-only validation", agent.state.last_action_summary)

    def test_syntax_only_validation_guard_applies_to_spec_file_program_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "req.md"
            spec_path.write_text("Create a Python command-line program called text_stats.py", encoding="utf-8")
            config = AgentConfig(
                spec_path=spec_path,
                logs_dir=Path(tmp_dir) / "agent_logs",
                max_iterations=3,
            )
            agent = DuckAgent(config)

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create program",
                            "path": "text_stats.py",
                            "content": "def main():\n    return 0\n\nif __name__ == '__main__':\n    main()\n",
                            "command": "",
                        }
                    ),
                ):
                    completed = MagicMock()
                    completed.returncode = 0
                    completed.stdout = ""
                    completed.stderr = ""
                    with patch.object(agent, "run_command", return_value=completed):
                        should_continue = agent.perform_iteration(agent.load_task_description())

                self.assertTrue(should_continue)
                self.assertEqual(agent.state.last_test_fingerprint, "VALIDATE:python:syntax:text_stats.py")

    def test_run_public_tests_uses_builtin_validation_for_python_primary_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a calculator program in python",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "calculator.py"

            with temporary_cwd(Path(tmp_dir)):
                Path("calculator.py").write_text(
                    "def main():\n    print('ready')\n\nif __name__ == '__main__':\n    main()\n",
                    encoding="utf-8",
                )
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = "ready\n"
                completed.stderr = ""

                with patch.object(agent, "run_command", return_value=completed) as run_command:
                    result = agent.run_public_tests()

            self.assertEqual(result.test_exit_code, 0)
            self.assertIn("exit=0", result.summary)
            self.assertGreaterEqual(run_command.call_count, 1)

    def test_run_builtin_validation_checks_hello_world_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a hello world program in python",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "hello.py"

            with temporary_cwd(Path(tmp_dir)):
                Path("hello.py").write_text(
                    "def main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
                    encoding="utf-8",
                )
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = "Hello, World!\n"
                completed.stderr = ""

                with patch.object(agent, "run_command", return_value=completed):
                    result = agent.run_builtin_validation()

            self.assertTrue(result.progress)

    def test_run_command_converts_timeout_to_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with patch("agent.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="python3 demo.py", timeout=5)):
                result = agent.run_command("python3 demo.py", timeout=5)

            self.assertEqual(result.returncode, 124)
            self.assertIn("timed out after 5s", result.stderr)

    def test_perform_iteration_recovers_from_invalid_model_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with patch.object(agent, "call_model", return_value="not-json"):
                should_continue = agent.perform_iteration("create a calculator in python")

            self.assertTrue(should_continue)
            self.assertIn("Invalid model response", agent.state.last_action_summary)

    def test_perform_iteration_recovers_invalid_model_json_with_second_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)

            with temporary_cwd(Path(tmp_dir)):
                recovery_response = json.dumps(
                    {
                        "action": "WRITE_FILE",
                        "reason": "create hello script",
                        "path": "hello.py",
                        "content": "def main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
                        "command": "",
                    }
                )
                validation = ActionOutcome(
                    summary="exit=0\nHello, World!\n",
                    progress=True,
                    fingerprint="RUN_TESTS:hello",
                    test_exit_code=0,
                    validation_passes=["syntax"],
                )
                with patch.object(agent, "call_model", side_effect=["not-json", recovery_response]):
                    with patch.object(agent, "run_builtin_validation", return_value=validation):
                        should_continue = agent.perform_iteration("create a hello world program in python")

                self.assertFalse(should_continue)
                self.assertTrue(Path("hello.py").exists())
                self.assertEqual(agent.state.invalid_response_streak, 0)

    def test_write_file_blocks_destructive_rewrite_after_partial_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.primary_artifact_path = "text_stats.py"
            agent.state.best_validation_score = 2
            agent.state.last_validation_failures = ["top_n"]

            with temporary_cwd(Path(tmp_dir)):
                Path("text_stats.py").write_text(
                    "def main():\n    print('stable implementation')\n\nif __name__ == '__main__':\n    main()\n",
                    encoding="utf-8",
                )
                outcome = agent.write_file(
                    "text_stats.py",
                    "print('totally different rewrite')\n",
                )

            self.assertFalse(outcome.progress)
            self.assertIn("destructive rewrite after partial validation success", outcome.summary)

    def test_perform_iteration_auto_validates_primary_artifact_without_external_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a hello world program in python",
            )
            agent = DuckAgent(config)

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(
                    agent,
                    "call_model",
                    return_value=json.dumps(
                        {
                            "action": "WRITE_FILE",
                            "reason": "create hello world script",
                            "path": "hello.py",
                            "content": "def main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
                            "command": "",
                        }
                    ),
                ):
                    validation = ActionOutcome(
                        summary="exit=0\nHello, World!\n",
                        progress=True,
                        fingerprint="RUN_TESTS:hello",
                        test_exit_code=0,
                    )
                    with patch.object(agent, "run_builtin_validation", return_value=validation) as run_validation:
                        should_continue = agent.perform_iteration("create a hello world program in python")

                self.assertFalse(should_continue)
                self.assertEqual(agent.state.last_test_exit_code, 0)
                run_validation.assert_called_once()

    def test_run_builtin_validation_rejects_python_program_without_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a calculator program in python",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "calculator.py"

            with temporary_cwd(Path(tmp_dir)):
                Path("calculator.py").write_text(
                    "def add(a, b):\n    return a + b\n",
                    encoding="utf-8",
                )
                outcome = agent.run_builtin_validation()

            self.assertFalse(outcome.progress)
            self.assertIn("missing a runnable entrypoint", outcome.summary)

    def test_run_builtin_validation_executes_sample_input_for_file_processing_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "req.md"
            spec_path.write_text(
                "Create a Python command-line program called text_stats.py. It must read a UTF-8 text file and print exactly one JSON object to stdout. The JSON must contain these keys: line_count, word_count, char_count, top_words. Add optional support for --top N. If the input file does not exist, print a JSON error object and exit with code 1.",
                encoding="utf-8",
            )
            config = AgentConfig(
                spec_path=spec_path,
                logs_dir=Path(tmp_dir) / "agent_logs",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "text_stats.py"

            with temporary_cwd(Path(tmp_dir)):
                Path("text_stats.py").write_text(
                    "def main():\n    return 0\n\nif __name__ == '__main__':\n    main()\n",
                    encoding="utf-8",
                )
                sample_completed = MagicMock()
                sample_completed.returncode = 0
                sample_completed.stdout = '{"line_count": 2, "word_count": 9, "char_count": 49, "top_words": [{"word": "hello", "count": 2}, {"word": "world", "count": 1}, {"word": "agent", "count": 1}, {"word": "this", "count": 1}, {"word": "is", "count": 1}]}'
                sample_completed.stderr = ""
                top_completed = MagicMock()
                top_completed.returncode = 0
                top_completed.stdout = '{"line_count": 2, "word_count": 9, "char_count": 49, "top_words": [{"word": "hello", "count": 2}, {"word": "world", "count": 1}, {"word": "agent", "count": 1}]}'
                top_completed.stderr = ""
                missing_completed = MagicMock()
                missing_completed.returncode = 1
                missing_completed.stdout = '{"error": "Input file not found"}'
                missing_completed.stderr = ""

                with patch.object(agent, "run_command", side_effect=[sample_completed, top_completed, missing_completed]) as run_command:
                    outcome = agent.run_builtin_validation()

            self.assertTrue(outcome.progress)
            self.assertEqual(outcome.fingerprint, "VALIDATE:python:sample-io:text_stats.py")
            self.assertIn(".agent_validation_input.txt", run_command.call_args_list[0].args[0])

    def test_run_builtin_validation_rejects_missing_file_json_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "req.md"
            spec_path.write_text(
                "Create a Python command-line program called text_stats.py. It must read a UTF-8 text file and print exactly one JSON object to stdout. The JSON must contain these keys: line_count, word_count, char_count, top_words. Add optional support for --top N. If the input file does not exist, print a JSON error object and exit with code 1.",
                encoding="utf-8",
            )
            config = AgentConfig(
                spec_path=spec_path,
                logs_dir=Path(tmp_dir) / "agent_logs",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "text_stats.py"

            with temporary_cwd(Path(tmp_dir)):
                Path("text_stats.py").write_text(
                    "def main():\n    return 0\n\nif __name__ == '__main__':\n    main()\n",
                    encoding="utf-8",
                )
                sample_completed = MagicMock()
                sample_completed.returncode = 0
                sample_completed.stdout = '{"line_count": 2, "word_count": 9, "char_count": 49, "top_words": [{"word": "hello", "count": 2}, {"word": "world", "count": 1}, {"word": "agent", "count": 1}, {"word": "this", "count": 1}, {"word": "is", "count": 1}]}'
                sample_completed.stderr = ""
                top_completed = MagicMock()
                top_completed.returncode = 0
                top_completed.stdout = '{"line_count": 2, "word_count": 9, "char_count": 49, "top_words": [{"word": "hello", "count": 2}, {"word": "world", "count": 1}, {"word": "agent", "count": 1}]}'
                top_completed.stderr = ""
                missing_completed = MagicMock()
                missing_completed.returncode = 1
                missing_completed.stdout = ""
                missing_completed.stderr = '{"error": "Input file not found"}'

                with patch.object(agent, "run_command", side_effect=[sample_completed, top_completed, missing_completed]):
                    outcome = agent.run_builtin_validation()

            self.assertFalse(outcome.progress)
            self.assertEqual(outcome.test_exit_code, 1)
            self.assertIn("Validation expected missing-file output to be a JSON object on stdout.", outcome.summary)

    def test_run_builtin_validation_rejects_wrong_text_stats_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "req.md"
            spec_path.write_text(
                "Create a Python command-line program called text_stats.py. It must read a UTF-8 text file and print exactly one JSON object to stdout. The JSON must contain these keys: line_count, word_count, char_count, top_words.",
                encoding="utf-8",
            )
            config = AgentConfig(
                spec_path=spec_path,
                logs_dir=Path(tmp_dir) / "agent_logs",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "text_stats.py"

            with temporary_cwd(Path(tmp_dir)):
                Path("text_stats.py").write_text(
                    "def main():\n    return 0\n\nif __name__ == '__main__':\n    main()\n",
                    encoding="utf-8",
                )
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = '{"line_count": 0, "word_count": 9, "char_count": 42, "top_words": []}'
                completed.stderr = ""

                with patch.object(agent, "run_command", return_value=completed):
                    outcome = agent.run_builtin_validation()

            self.assertFalse(outcome.progress)
            self.assertEqual(outcome.test_exit_code, 1)
            self.assertIn("Validation expected line_count=2, word_count=9, char_count=49.", outcome.summary)

    def test_write_file_blocks_runtime_managed_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.external_log_path = "iamthelog"

            outcome = agent.write_file("iamthelog", "anything")

            self.assertFalse(outcome.progress)
            self.assertIn("runtime-managed external log file", outcome.summary)

    def test_print_progress_appends_to_runtime_managed_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.state.external_log_path = "iamthelog"

            with temporary_cwd(Path(tmp_dir)):
                agent.print_progress("WRITE_FILE iamthelog -> changed")
                contents = Path("iamthelog").read_text(encoding="utf-8")

            self.assertIn("WRITE_FILE iamthelog -> changed", contents)

    def test_run_completes_logging_only_task_without_model_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="give me all the log of everything that you do in a new file called iamthelog2",
            )
            agent = DuckAgent(config)

            with temporary_cwd(Path(tmp_dir)):
                with patch.object(agent, "call_model", side_effect=AssertionError("model should not be called")):
                    exit_code = agent.run()
                contents = Path("iamthelog2").read_text(encoding="utf-8")

            self.assertEqual(exit_code, 0)
            self.assertIn("Starting agent loop", contents)
            self.assertIn("Logging-only task completed", contents)

    def test_evaluate_primary_artifact_completion_accepts_document_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="Generate an introduction message toward Vincent, a student at 42. I want to sell softwares products",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "introduction.txt"

            with temporary_cwd(Path(tmp_dir)):
                Path("introduction.txt").write_text(
                    "Hello Vincent, as a student at 42 you can build and sell software products with confidence.",
                    encoding="utf-8",
                )
                outcome = agent.evaluate_primary_artifact_completion()

            self.assertIsNotNone(outcome)
            self.assertTrue(outcome.stop)

    def test_evaluate_primary_artifact_completion_accepts_validated_c_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a hello world program in C",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "hello.c"
            agent.state.last_test_exit_code = 0

            with temporary_cwd(Path(tmp_dir)):
                Path("hello.c").write_text('#include <stdio.h>\nint main(){printf("Hello, World!\\n");return 0;}\n', encoding="utf-8")
                outcome = agent.evaluate_primary_artifact_completion()

            self.assertIsNotNone(outcome)
            self.assertTrue(outcome.stop)

    def test_evaluate_primary_artifact_completion_waits_for_remaining_spec_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = AgentConfig(
                spec_path=Path(tmp_dir) / "secret_spec" / "SECRET_SPEC.md",
                logs_dir=Path(tmp_dir) / "agent_logs",
                task="create a hello world program in python",
            )
            agent = DuckAgent(config)
            agent.state.primary_artifact_path = "hello.py"
            agent.state.last_test_exit_code = 0
            agent.state.spec_pages = ["page one", "page two"]
            agent.state.current_spec_page = 0

            with temporary_cwd(Path(tmp_dir)):
                Path("hello.py").write_text("print('hello')\n", encoding="utf-8")
                outcome = agent.evaluate_primary_artifact_completion()

            self.assertIsNone(outcome)

    def test_run_returns_zero_when_model_requests_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text("spec body", encoding="utf-8")

            with patch.object(agent, "perform_iteration", return_value=False):
                exit_code = agent.run()

            self.assertEqual(exit_code, 0)

    def test_run_returns_nonzero_when_agent_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.spec_path.parent.mkdir(parents=True, exist_ok=True)
            agent.config.spec_path.write_text("spec body", encoding="utf-8")
            agent.state.last_terminal_kind = "stall"

            with patch.object(agent, "perform_iteration", return_value=False):
                exit_code = agent.run()

            self.assertEqual(exit_code, 1)

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

    def test_call_model_uses_grok_provider_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.provider = "groq"
            agent.config.groq_url = "https://api.groq.com/openai/v1"
            agent.config.groq_model = "mixtral-8x7b-32768"
            agent.config.groq_api_key = "secret-key"

            captured_headers = {}

            def fake_urlopen(http_request, timeout=120):
                captured_headers.update(dict(http_request.header_items()))
                fake_response = MagicMock()
                fake_response.read.return_value = b'{"choices":[{"message":{"content":"groq-ok"}}]}'
                fake_response.__enter__.return_value = fake_response
                fake_response.__exit__.return_value = False
                return fake_response

            with patch("agent.request.urlopen", side_effect=fake_urlopen):
                result = agent.call_model("hello groq")

            self.assertEqual(result, "groq-ok")
            self.assertEqual(captured_headers.get("Authorization"), "Bearer secret-key")

    def test_call_model_requires_api_key_for_grok_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = self.make_agent(tmp_dir)
            agent.config.provider = "groq"
            agent.config.groq_api_key = ""

            with self.assertRaises(RuntimeError) as exc_info:
                agent.call_model("hello groq")

            self.assertIn("requires GROQ_API_KEY", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
