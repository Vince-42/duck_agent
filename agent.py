from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence
from urllib import error, request
from urllib.parse import urlparse


@dataclass
class AgentConfig:
    spec_path: Path = Path("secret_spec/SECRET_SPEC.md")
    logs_dir: Path = Path("agent_logs")
    task: str = ""
    task_file: Path | None = None
    model: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    max_iterations: int = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))
    solution_command: str = os.getenv("AGENT_SOLUTION_COMMAND", "python3 solution.py")
    test_command: str = os.getenv("AGENT_TEST_COMMAND", "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the duck_agent hackathon scaffold.")
    parser.add_argument("--spec-path", default=str(AgentConfig.spec_path), help="Path to the spec markdown file")
    parser.add_argument("--logs-dir", default=str(AgentConfig.logs_dir), help="Directory for agent logs")
    parser.add_argument("--task", default="", help="Direct task text for general-purpose runs")
    parser.add_argument("--task-file", default="", help="Path to a markdown/text file describing the task")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b"), help="Ollama model name")
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
        help="Ollama generate endpoint URL",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=int(os.getenv("AGENT_MAX_ITERATIONS", "20")),
        help="Maximum agent iterations before stopping",
    )
    parser.add_argument(
        "--solution-command",
        default=os.getenv("AGENT_SOLUTION_COMMAND", "python3 solution.py"),
        help="Command passed to the public test runner",
    )
    parser.add_argument(
        "--test-command",
        default=os.getenv("AGENT_TEST_COMMAND", ""),
        help="General test command for non-hackathon tasks",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Check whether the local environment looks ready to run the agent",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> AgentConfig:
    return AgentConfig(
        spec_path=Path(args.spec_path),
        logs_dir=Path(args.logs_dir),
        task=args.task,
        task_file=Path(args.task_file) if args.task_file else None,
        model=args.model,
        ollama_url=args.ollama_url,
        max_iterations=args.max_iterations,
        solution_command=args.solution_command,
        test_command=args.test_command,
    )


def run_doctor(config: AgentConfig) -> int:
    print("duck_agent doctor")
    print(f"- spec path: {config.spec_path}")
    print(f"- task provided: {'yes' if config.task else 'no'}")
    print(f"- task file: {config.task_file if config.task_file else '(none)'}")
    print(f"- logs dir: {config.logs_dir}")
    print(f"- model: {config.model}")
    print(f"- ollama url: {config.ollama_url}")

    spec_exists = config.spec_path.exists()
    task_file_exists = True if config.task_file is None else config.task_file.exists()
    print(f"- hidden spec present: {'yes' if spec_exists else 'no'}")
    print(f"- task file present: {'yes' if task_file_exists else 'no'}")

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    print("- logs dir writable: yes")

    probe_agent = DuckAgent(config)
    try:
        probe_agent.call_model("ping")
        print("- model endpoint reachable: yes")
    except Exception as exc:  # noqa: BLE001
        print(f"- model endpoint reachable: no ({exc})")
        if not spec_exists and not config.task and not task_file_exists:
            print("Doctor result: not ready yet (missing task input and model endpoint not reachable).")
            return 1
        print("Doctor result: partially ready (task input may exist, but the model endpoint is not reachable).")
        return 1

    if not spec_exists and not config.task and not task_file_exists:
        print("Doctor result: partially ready (Ollama reachable, waiting for a task input).")
        return 1

    print("Doctor result: ready to run.")
    return 0


class AgentLogger:
    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.paths = {
            "prompts": logs_dir / "prompts.log",
            "decisions": logs_dir / "decisions.log",
            "commands": logs_dir / "commands.log",
            "test_runs": logs_dir / "test_runs.log",
            "errors": logs_dir / "errors.log",
            "human_interventions": logs_dir / "human_interventions.log",
        }
        for path in self.paths.values():
            path.touch(exist_ok=True)

    def write(self, category: str, message: str) -> None:
        path = self.paths[category]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")


class DuckAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.logger = AgentLogger(config.logs_dir)
        self.iteration = 0
        self.last_test_output = "No tests run yet."
        self.last_action_result = "No actions executed yet."
        self.completed_actions = 0

    def has_test_target(self) -> bool:
        return bool(self.config.test_command) or Path("secret_spec/test_runner/run_tests.py").exists()

    def load_task_description(self) -> str:
        if self.config.task:
            return self.config.task

        if self.config.task_file is not None:
            if not self.config.task_file.exists():
                raise FileNotFoundError(f"Task file not found at {self.config.task_file}.")
            return self.config.task_file.read_text(encoding="utf-8")

        if not self.config.spec_path.exists():
            raise FileNotFoundError(
                f"No task provided. Pass --task, --task-file, or create the spec at {self.config.spec_path}."
            )
        return self.config.spec_path.read_text(encoding="utf-8")

    @staticmethod
    def normalize_model_response(response: str) -> str:
        stripped = response.strip()

        if not stripped.startswith("```"):
            return stripped

        lines = stripped.splitlines()
        if not lines:
            return stripped

        if lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        return "\n".join(lines).strip()

    def model_request_candidates(self, prompt: str) -> list[tuple[str, bytes, str]]:
        parsed_url = urlparse(self.config.ollama_url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.scheme and parsed_url.netloc else self.config.ollama_url
        path = parsed_url.path.rstrip("/")

        ollama_payload = json.dumps(
            {
                "model": self.config.model,
                "prompt": prompt,
                "stream": False,
            }
        ).encode("utf-8")
        chat_payload = json.dumps(
            {
                "model": self.config.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        ).encode("utf-8")

        candidates: list[tuple[str, bytes, str]] = []
        seen_urls: set[str] = set()

        def add_candidate(url: str, payload: bytes, response_type: str) -> None:
            if url not in seen_urls:
                candidates.append((url, payload, response_type))
                seen_urls.add(url)

        if path in {"", "/"}:
            add_candidate(f"{base_url}/api/generate", ollama_payload, "ollama")
            add_candidate(f"{base_url}/v1/chat/completions", chat_payload, "openai")
        else:
            add_candidate(self.config.ollama_url, ollama_payload, "ollama")
            add_candidate(self.config.ollama_url, chat_payload, "openai")
            add_candidate(f"{base_url}/api/generate", ollama_payload, "ollama")
            add_candidate(f"{base_url}/v1/chat/completions", chat_payload, "openai")

        return candidates

    def parse_model_response(self, raw_response: str, response_type: str) -> str:
        parsed = json.loads(raw_response)

        if response_type == "ollama":
            return str(parsed.get("response", "")).strip()

        choices = parsed.get("choices", [])
        if not choices:
            raise ValueError("OpenAI-compatible response did not include choices")

        message = choices[0].get("message", {})
        return str(message.get("content", "")).strip()

    def call_model(self, prompt: str) -> str:
        errors_seen: list[str] = []

        for url, payload, response_type in self.model_request_candidates(prompt):
            http_request = request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            try:
                with request.urlopen(http_request, timeout=120) as response:
                    raw_response = response.read().decode("utf-8")
                return self.parse_model_response(raw_response, response_type)
            except error.HTTPError as exc:
                error_body = ""
                try:
                    error_body = exc.read().decode("utf-8").strip()
                except Exception:  # noqa: BLE001
                    error_body = ""

                if error_body:
                    errors_seen.append(f"{url} -> HTTP {exc.code}: {error_body}")
                else:
                    errors_seen.append(f"{url} -> HTTP {exc.code}")
                continue
            except error.URLError as exc:
                errors_seen.append(f"{url} -> {exc}")
                continue
            except (ValueError, json.JSONDecodeError) as exc:
                errors_seen.append(f"{url} -> invalid response ({exc})")
                continue

        joined_errors = "; ".join(errors_seen) if errors_seen else "no candidates tried"
        raise RuntimeError(f"Model call failed: {joined_errors}")

    def build_prompt(self, task_description: str) -> str:
        return textwrap.dedent(
            f"""
            You are an autonomous coding agent working in a local repository.

            Task description:
            {task_description}

            Current iteration: {self.iteration}

            Test target available: {"yes" if self.has_test_target() else "no"}

            Last public test output:
            {self.last_test_output}

            Last action result:
            {self.last_action_result}

            Return exactly one JSON object with this schema:
            {{
              "action": "WRITE_FILE" | "RUN_TESTS" | "RUN_COMMAND" | "STOP",
              "reason": "short explanation",
              "path": "relative/path/or-empty-string",
              "content": "full file contents or empty string",
              "command": "shell command or empty string"
            }}

            Rules:
            - Prefer small targeted edits.
            - Do not invent requirements beyond the specification.
            - Use RUN_TESTS regularly when a test command or public test runner is available.
            - If no test target is available, do not choose RUN_TESTS.
            - Use prior command output and file-write results as evidence for your next action.
            - Use WRITE_FILE only when you can provide the full file contents.
            - Use STOP only after at least one concrete action has been executed and you have observable evidence.
            - In STOP reasons, mention the evidence that justifies stopping.
            """
        ).strip()

    def parse_action(self, response: str) -> dict[str, str]:
        normalized_response = self.normalize_model_response(response)

        try:
            parsed = json.loads(normalized_response)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model response was not valid JSON: {response}") from exc

        action = str(parsed.get("action", "")).strip().upper()
        if action not in {"WRITE_FILE", "RUN_TESTS", "RUN_COMMAND", "STOP"}:
            raise ValueError(f"Unsupported action: {action!r}")

        return {
            "action": action,
            "reason": str(parsed.get("reason", "")).strip(),
            "path": str(parsed.get("path", "")).strip(),
            "content": str(parsed.get("content", "")),
            "command": str(parsed.get("command", "")).strip(),
        }

    def write_file(self, relative_path: str, content: str) -> str:
        if not relative_path:
            raise ValueError("WRITE_FILE action requires a non-empty path")

        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Refusing to write outside repository: {relative_path}")

        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(content, encoding="utf-8")
        self.logger.write("decisions", f"WRITE_FILE {relative_path}")
        preview = content.strip().replace("\n", " ")[:120]
        return f"Wrote {relative_path} ({len(content)} chars). Preview: {preview}"

    def run_command(self, command: str, *, category: str = "commands") -> subprocess.CompletedProcess[str]:
        if not command:
            raise ValueError("Command cannot be empty")

        self.logger.write(category, f"COMMAND {command}")
        result = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
        output = f"exit={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}".strip()
        self.logger.write(category, output)
        return result

    def run_public_tests(self) -> str:
        if self.config.test_command:
            result = self.run_command(self.config.test_command, category="test_runs")
            return f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()

        runner = Path("secret_spec/test_runner/run_tests.py")
        if not runner.exists():
            message = "No test command configured and public test runner not available yet."
            self.logger.write("test_runs", message)
            return message

        command = (
            f"python3 {shlex.quote(str(runner))} "
            f"--program {shlex.quote(self.config.solution_command)} --suite public"
        )
        result = self.run_command(command, category="test_runs")
        return f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()

    def perform_iteration(self, task_description: str) -> bool:
        prompt = self.build_prompt(task_description)
        self.logger.write("prompts", prompt)
        response = self.call_model(prompt)
        self.logger.write("decisions", f"MODEL_RESPONSE {response}")
        action = self.parse_action(response)
        self.logger.write("decisions", f"ACTION {action['action']}: {action['reason']}")

        if action["action"] == "WRITE_FILE":
            self.last_action_result = self.write_file(action["path"], action["content"])
            self.completed_actions += 1
        elif action["action"] == "RUN_COMMAND":
            result = self.run_command(action["command"])
            self.last_action_result = (
                f"Command `{action['command']}` finished with exit={result.returncode}. "
                f"STDOUT: {result.stdout.strip() or '(empty)'} STDERR: {result.stderr.strip() or '(empty)'}"
            )
            self.completed_actions += 1
        elif action["action"] == "RUN_TESTS":
            self.last_test_output = self.run_public_tests()
            self.last_action_result = self.last_test_output
            self.completed_actions += 1
        elif action["action"] == "STOP":
            if self.completed_actions == 0:
                raise ValueError("Refusing to STOP before executing any concrete action")
            return False

        return True

    def run(self) -> int:
        try:
            task_description = self.load_task_description()
        except FileNotFoundError as exc:
            self.logger.write("errors", str(exc))
            print(exc)
            return 1

        self.logger.write("decisions", "Starting agent loop")

        while self.iteration < self.config.max_iterations:
            try:
                should_continue = self.perform_iteration(task_description)
            except Exception as exc:  # noqa: BLE001
                self.logger.write("errors", f"Iteration {self.iteration}: {exc}")
                print(f"Iteration {self.iteration} failed: {exc}")
                return 1

            if not should_continue:
                self.logger.write("decisions", f"Stopping at iteration {self.iteration}")
                print("Agent requested stop.")
                return 0

            self.iteration += 1

        self.logger.write("errors", "Reached maximum iteration count")
        print("Reached maximum iteration count.")
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)

    if args.doctor:
        return run_doctor(config)

    agent = DuckAgent(config)
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
