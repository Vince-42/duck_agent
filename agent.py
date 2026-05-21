from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    enable_multirole: bool = os.getenv("AGENT_MULTI_ROLE", "1").lower() not in {"0", "false", "no"}
    orchestrator_config: Path | None = Path(os.getenv("AGENT_ORCHESTRATOR_CONFIG")) if os.getenv("AGENT_ORCHESTRATOR_CONFIG") else Path("agent.json")


@dataclass
class RuntimeState:
    iteration: int = 0
    completed_actions: int = 0
    material_changes: int = 0
    consecutive_no_progress: int = 0
    dirty_since_test: bool = False
    last_test_exit_code: int | None = None
    last_test_output: str = "No tests run yet."
    last_action_summary: str = "No actions executed yet."
    last_action_fingerprint: str = ""
    last_action_progress: bool = False
    external_log_path: str = ""
    primary_artifact_path: str = ""
    rewrite_count_for_primary_artifact: int = 0
    current_artifact_hash: str = ""
    last_completed_artifact_hash: str = ""


@dataclass
class ActionOutcome:
    summary: str
    progress: bool
    fingerprint: str
    stop: bool = False
    test_exit_code: int | None = None
    path: str = ""
    artifact_hash: str = ""


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
        "--orchestrator-config",
        default=os.getenv("AGENT_ORCHESTRATOR_CONFIG", "agent.json"),
        help="Path to the structured cognition engine JSON config",
    )
    parser.add_argument(
        "--single-agent",
        action="store_true",
        help="Disable the multi-role orchestrator layer and use the base single-agent prompt only",
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
        enable_multirole=not args.single_agent,
        orchestrator_config=Path(args.orchestrator_config) if args.orchestrator_config else None,
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
        from orchestrator import MultiRoleOrchestrator

        self.config = config
        self.logger = AgentLogger(config.logs_dir)
        self.state = RuntimeState()
        self.state.external_log_path = self.detect_requested_log_output_path()
        self.orchestrator = (
            MultiRoleOrchestrator(orchestrator_config_path=config.orchestrator_config)
            if config.enable_multirole
            else None
        )

    def has_test_target(self) -> bool:
        return self.has_validation_target()

    def has_validation_target(self) -> bool:
        return bool(self.config.test_command) or Path("secret_spec/test_runner/run_tests.py").exists() or self.can_run_builtin_validation()

    def supports_builtin_validation_path(self, relative_path: str) -> bool:
        return Path(relative_path).suffix.lower() in {".py", ".c", ".cpp", ".txt", ".md"}

    def can_run_builtin_validation(self) -> bool:
        path = self.state.primary_artifact_path
        if not path:
            return False
        artifact = Path(path)
        if not artifact.exists():
            return False
        return self.supports_builtin_validation_path(path)

    def primary_task_text(self) -> str:
        if self.config.task:
            return self.config.task.strip()

        if self.config.task_file and self.config.task_file.exists():
            return self.config.task_file.read_text(encoding="utf-8").strip()

        return ""

    def detect_requested_log_output_path(self) -> str:
        task_description = self.primary_task_text()

        lowered = task_description.lower()
        if "log of everything" not in lowered and "all the log" not in lowered and "log everything" not in lowered:
            return ""

        match = re.search(r"file called\s+([A-Za-z0-9_.\-/]+)", task_description, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

        return ""

    def detect_primary_artifact_path(self) -> str:
        task_description = self.primary_task_text()
        if not task_description:
            return ""

        patterns = [
            r"(?:file|program|script|document|message)\s+called\s+([A-Za-z0-9_.\-/]+)",
            r"(?:create|write|generate)\s+(?:a\s+)?([A-Za-z0-9_.\-/]+\.(?:txt|md|py|js|ts|json|html|css|c|cpp))",
        ]
        for pattern in patterns:
            match = re.search(pattern, task_description, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()

        lowered = task_description.lower()
        if "hello world" in lowered and " c" in lowered:
            return "hello.c"
        if "hello world" in lowered and "python" in lowered:
            return "hello.py"
        if "introduction message" in lowered:
            return "introduction.txt"

        inferred_python_program = re.search(
            r"(?:create|write|build|make)\s+(?:a\s+|an\s+)?([A-Za-z0-9_-]+)\s+(?:program|script|cli|calculator|tool)\s+in\s+python",
            task_description,
            flags=re.IGNORECASE,
        )
        if inferred_python_program:
            artifact_stem = inferred_python_program.group(1).strip().replace("-", "_")
            return f"{artifact_stem}.py"

        return ""

    def task_requires_runnable_program(self) -> bool:
        task_description = self.primary_task_text().lower()
        return any(keyword in task_description for keyword in (" program", " script", " cli", " command-line", " calculator"))

    def is_logging_only_task(self) -> bool:
        if not self.state.external_log_path:
            return False

        task_description = self.primary_task_text().lower()
        logging_phrases = ["log of everything", "all the log", "log everything"]
        setup_phrases = ["new file called", "file called", "in a new file"]

        return any(phrase in task_description for phrase in logging_phrases) and any(
            phrase in task_description for phrase in setup_phrases
        )

    def append_external_log(self, message: str) -> None:
        if not self.state.external_log_path:
            return

        path = Path(self.state.external_log_path)
        if path.is_absolute() or ".." in path.parts:
            return

        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")

    @staticmethod
    def hash_text(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

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

    def build_prompt(self, task_description: str, orchestration_context: str = "") -> str:
        orchestration_block = ""
        if orchestration_context:
            orchestration_block = f"\n\nMulti-role orchestration context:\n{orchestration_context.strip()}\n"

        program_expectation_block = ""
        if self.task_requires_runnable_program():
            program_expectation_block = (
                "\nProgram expectation:\n"
                "- The task asks for a runnable program/script/CLI.\n"
                "- Produce an executable entrypoint or main flow, not only helper functions.\n"
                "- If you write Python code, include a main path that can actually run.\n"
            )
            if "calculator" in task_description.lower():
                program_expectation_block += (
                    "- For a calculator task, bare arithmetic helper functions are insufficient; include runnable user-facing behavior.\n"
                )

        primary_artifact_block = ""
        if self.state.primary_artifact_path:
            primary_artifact_block = f"\nPrimary artifact target:\n{self.state.primary_artifact_path}\n"

        return textwrap.dedent(
            f"""
            You are an autonomous coding agent working in a local repository.

            Task description:
            {task_description}

            Current iteration: {self.state.iteration}

            Test target available: {"yes" if self.has_test_target() else "no"}

            Last public test output:
            {self.state.last_test_output}

            Last action result:
            {self.state.last_action_summary}

            Runtime-managed external log file:
            {self.state.external_log_path or '(none)'}
            {primary_artifact_block}
            {program_expectation_block}
            {orchestration_block}

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
            - Use RUN_TESTS whenever validation is available after material changes or before STOP.
            - If no validation target is available yet, do not choose RUN_TESTS.
            - Use prior command output and file-write results as evidence for your next action.
            - Use WRITE_FILE only when you can provide the full file contents.
            - Use STOP only after at least one concrete action has been executed and you have observable evidence.
            - In STOP reasons, mention the evidence that justifies stopping.
            - If a runtime-managed external log file is configured, do not write to that file yourself; the runtime maintains it.
            - Do not copy the 'Last action result' text verbatim into output files.
            - If multi-role orchestration context is present, use it internally and still return only one final JSON action object.
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

    @staticmethod
    def fingerprint_for_write(path: str, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return f"WRITE_FILE:{path}:{digest}"

    @staticmethod
    def fingerprint_for_command(command: str) -> str:
        return f"RUN_COMMAND:{' '.join(command.split())}"

    @staticmethod
    def fingerprint_for_tests(command: str) -> str:
        return f"RUN_TESTS:{' '.join(command.split())}"

    def write_file(self, relative_path: str, content: str) -> ActionOutcome:
        if not relative_path:
            raise ValueError("WRITE_FILE action requires a non-empty path")

        if self.state.external_log_path and relative_path == self.state.external_log_path:
            return ActionOutcome(
                summary=f"WRITE_FILE {relative_path} -> blocked (runtime-managed external log file)",
                progress=False,
                fingerprint=f"WRITE_FILE_BLOCKED:{relative_path}",
            )

        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Refusing to write outside repository: {relative_path}")

        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)

        fingerprint = self.fingerprint_for_write(relative_path, content)
        previous_content = path.read_text(encoding="utf-8") if path.exists() else None
        if previous_content == content:
            self.logger.write("decisions", f"WRITE_FILE_NOOP {relative_path}")
            return ActionOutcome(
                summary=f"WRITE_FILE {relative_path} -> noop (same content)",
                progress=False,
                fingerprint=fingerprint,
            )

        path.write_text(content, encoding="utf-8")
        self.logger.write("decisions", f"WRITE_FILE {relative_path}")
        preview = content.strip().replace("\n", " ")[:120]
        artifact_hash = self.hash_text(content)
        return ActionOutcome(
            summary=f"WRITE_FILE {relative_path} -> changed ({len(content)} chars). Preview: {preview}",
            progress=True,
            fingerprint=fingerprint,
            path=relative_path,
            artifact_hash=artifact_hash,
        )

    def run_builtin_validation(self) -> ActionOutcome:
        path = self.state.primary_artifact_path
        if not path:
            return ActionOutcome(summary="No primary artifact available for built-in validation.", progress=False, fingerprint="VALIDATE:none")

        artifact = Path(path)
        if not artifact.exists():
            return ActionOutcome(summary=f"Primary artifact {path} does not exist yet.", progress=False, fingerprint=f"VALIDATE:missing:{path}")

        suffix = artifact.suffix.lower()
        if suffix == ".py":
            content = artifact.read_text(encoding="utf-8")
            if self.task_requires_runnable_program() and "__main__" not in content and "main(" not in content:
                return ActionOutcome(
                    summary=f"Python artifact {path} is missing a runnable entrypoint.",
                    progress=False,
                    fingerprint=f"VALIDATE:python:missing-entrypoint:{path}",
                )
            if self.task_requires_runnable_program():
                command = f"python3 -m py_compile {shlex.quote(path)} && python3 {shlex.quote(path)}"
            else:
                command = f"python3 -m py_compile {shlex.quote(path)}"
        elif suffix == ".c":
            binary = artifact.with_suffix("")
            command = f"gcc {shlex.quote(path)} -o {shlex.quote(str(binary))} && {shlex.quote(str(binary))}"
        elif suffix == ".cpp":
            binary = artifact.with_suffix("")
            command = f"g++ {shlex.quote(path)} -o {shlex.quote(str(binary))} && {shlex.quote(str(binary))}"
        elif suffix in {".txt", ".md"}:
            content = artifact.read_text(encoding="utf-8").strip()
            if len(content) < 20 or "[your" in content.lower() or "placeholder" in content.lower():
                return ActionOutcome(
                    summary=f"Document {path} is not complete enough for runtime validation.",
                    progress=False,
                    fingerprint=f"VALIDATE:document:incomplete:{path}",
                )

            return ActionOutcome(
                summary=f"Document {path} passed runtime validation.",
                progress=True,
                fingerprint=f"VALIDATE:document:{path}:{self.hash_text(content)}",
                test_exit_code=0,
            )
        else:
            return ActionOutcome(summary=f"No built-in validation available for {path}.", progress=False, fingerprint=f"VALIDATE:unsupported:{path}")

        result = self.run_command(command, category="test_runs")
        output = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()
        return ActionOutcome(
            summary=output,
            progress=result.returncode == 0,
            fingerprint=self.fingerprint_for_tests(command),
            test_exit_code=result.returncode,
        )

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

    def run_public_tests(self) -> ActionOutcome:
        if self.config.test_command:
            result = self.run_command(self.config.test_command, category="test_runs")
            output = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()
            return ActionOutcome(
                summary=output,
                progress=True,
                fingerprint=self.fingerprint_for_tests(self.config.test_command),
                test_exit_code=result.returncode,
            )

        runner = Path("secret_spec/test_runner/run_tests.py")
        if not runner.exists():
            builtin_outcome = self.run_builtin_validation()
            if builtin_outcome.fingerprint != "VALIDATE:none" and not builtin_outcome.fingerprint.startswith("VALIDATE:unsupported"):
                self.logger.write("test_runs", builtin_outcome.summary)
                return builtin_outcome

            message = "No test command configured, public test runner not available, and no built-in validation applied yet."
            self.logger.write("test_runs", message)
            return ActionOutcome(summary=message, progress=False, fingerprint="RUN_TESTS:unavailable")

        command = (
            f"python3 {shlex.quote(str(runner))} "
            f"--program {shlex.quote(self.config.solution_command)} --suite public"
        )
        result = self.run_command(command, category="test_runs")
        output = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()
        return ActionOutcome(
            summary=output,
            progress=True,
            fingerprint=self.fingerprint_for_tests(command),
            test_exit_code=result.returncode,
        )

    def should_block_repeated_action(self, fingerprint: str, progress: bool) -> bool:
        return fingerprint == self.state.last_action_fingerprint and not self.state.last_action_progress and not progress

    def update_state_from_outcome(self, outcome: ActionOutcome) -> None:
        self.state.last_action_summary = outcome.summary
        self.state.last_action_fingerprint = outcome.fingerprint
        self.state.last_action_progress = outcome.progress

        if outcome.progress:
            self.state.completed_actions += 1
            self.state.consecutive_no_progress = 0
        else:
            self.state.consecutive_no_progress += 1

        if outcome.path and self.state.primary_artifact_path and outcome.path == self.state.primary_artifact_path and outcome.progress:
            self.state.rewrite_count_for_primary_artifact += 1
            if outcome.artifact_hash:
                self.state.current_artifact_hash = outcome.artifact_hash

    def evaluate_primary_artifact_completion(self) -> ActionOutcome | None:
        path = self.state.primary_artifact_path
        if not path or path == self.state.external_log_path:
            return None

        artifact = Path(path)
        if not artifact.exists():
            return None

        content = artifact.read_text(encoding="utf-8")
        artifact_hash = self.hash_text(content)
        lowered_task = self.primary_task_text().lower()

        if self.has_test_target() and self.state.dirty_since_test:
            return None

        if artifact_hash == self.state.last_completed_artifact_hash:
            return self.stop_outcome(f"artifact {path} already validated")

        if artifact.suffix.lower() in {".txt", ".md"}:
            if len(content.strip()) < 20 or "[your" in content.lower() or "placeholder" in content.lower():
                return None
            if "introduction" in lowered_task and "vincent" not in content.lower():
                return None

            self.state.last_completed_artifact_hash = artifact_hash
            return self.stop_outcome(f"artifact {path} satisfies runtime completion checks")

        if artifact.suffix.lower() in {".py", ".c", ".cpp"} and self.state.last_test_exit_code == 0:
            self.state.last_completed_artifact_hash = artifact_hash
            return self.stop_outcome(f"artifact {path} passed built-in validation")

        return None

    def print_progress(self, summary: str) -> None:
        print(f"[{self.state.iteration + 1}/{self.config.max_iterations}] {summary}")
        self.append_external_log(summary)

    def stop_outcome(self, reason: str) -> ActionOutcome:
        return ActionOutcome(summary=reason, progress=False, fingerprint=f"STOP:{reason}", stop=True)

    def complete_logging_only_task(self) -> int:
        path = self.state.external_log_path
        if not path:
            return 1

        print("Starting agent loop")
        print(f"Runtime-managed external log file: {path}")
        self.append_external_log("Starting agent loop")
        self.append_external_log(f"Runtime-managed external log file: {path}")
        completion_message = f"Logging-only task completed: runtime is writing activity to {path}"
        print(f"[done] {completion_message}")
        self.append_external_log(completion_message)
        self.logger.write("decisions", "Completed logging-only task without model loop")
        return 0

    def perform_iteration(self, task_description: str) -> bool:
        orchestration_context = ""
        if self.orchestrator is not None:
            turn = self.orchestrator.prepare_turn(self, task_description)
            orchestration_context = turn.orchestration_context
            self.logger.write(
                "decisions",
                f"ORCHESTRATION phase={turn.phase} roles={','.join(turn.active_role_names)}",
            )
            self.append_external_log(
                f"ORCHESTRATION phase={turn.phase} roles={','.join(turn.active_role_names)}"
            )

        prompt = self.build_prompt(task_description, orchestration_context=orchestration_context)
        self.logger.write("prompts", prompt)
        response = self.call_model(prompt)
        self.logger.write("decisions", f"MODEL_RESPONSE {response}")
        self.append_external_log(f"MODEL_RESPONSE {self.normalize_model_response(response)}")
        action = self.parse_action(response)
        self.logger.write("decisions", f"ACTION {action['action']}: {action['reason']}")
        self.append_external_log(f"ACTION {action['action']}: {action['reason']}")

        if action["action"] == "WRITE_FILE":
            outcome = self.write_file(action["path"], action["content"])
            if outcome.progress and outcome.path and not self.state.primary_artifact_path and self.supports_builtin_validation_path(outcome.path):
                self.state.primary_artifact_path = outcome.path
                if outcome.artifact_hash:
                    self.state.current_artifact_hash = outcome.artifact_hash
            if self.should_block_repeated_action(outcome.fingerprint, outcome.progress):
                outcome = self.stop_outcome(f"stalled: repeated equivalent write for {action['path']}")
            elif outcome.progress:
                self.state.material_changes += 1
                self.state.dirty_since_test = True
        elif action["action"] == "RUN_COMMAND":
            result = self.run_command(action["command"])
            summary = (
                f"RUN_COMMAND `{action['command']}` -> exit={result.returncode}. "
                f"STDOUT: {result.stdout.strip() or '(empty)'} STDERR: {result.stderr.strip() or '(empty)'}"
            )
            fingerprint = self.fingerprint_for_command(action["command"])
            progress = bool(result.stdout.strip() or result.stderr.strip() or result.returncode != 0)
            if self.should_block_repeated_action(fingerprint, progress):
                outcome = self.stop_outcome(f"stalled: repeated equivalent command `{action['command']}`")
            else:
                outcome = ActionOutcome(summary=summary, progress=progress, fingerprint=fingerprint)
        elif action["action"] == "RUN_TESTS":
            outcome = self.run_public_tests()
            if not self.has_validation_target() and outcome.fingerprint == "RUN_TESTS:unavailable":
                outcome = self.stop_outcome("stalled: model requested tests, but no test target or built-in validation is available")
            elif self.should_block_repeated_action(outcome.fingerprint, outcome.progress):
                outcome = self.stop_outcome("stalled: repeated equivalent test run")
            else:
                self.state.last_test_output = outcome.summary
                self.state.last_test_exit_code = outcome.test_exit_code
                self.state.dirty_since_test = outcome.test_exit_code not in {0, None}
        elif action["action"] == "STOP":
            if self.state.completed_actions == 0:
                raise ValueError("Refusing to STOP before executing any concrete action")

            if self.state.dirty_since_test and self.has_validation_target():
                raise ValueError("Refusing to STOP while changes have not been validated by a test run")

            outcome = self.stop_outcome(f"STOP accepted: {action['reason']}")

        self.update_state_from_outcome(outcome)
        self.print_progress(outcome.summary)

        completion_outcome = self.evaluate_primary_artifact_completion()
        if completion_outcome is not None:
            self.update_state_from_outcome(completion_outcome)
            self.print_progress(completion_outcome.summary)
            return False

        if self.state.consecutive_no_progress >= 3:
            raise ValueError("Stopping after 3 consecutive no-progress iterations")

        if outcome.stop:
            return False

        return True

    def run(self) -> int:
        try:
            task_description = self.load_task_description()
        except FileNotFoundError as exc:
            self.logger.write("errors", str(exc))
            print(exc)
            return 1

        if self.is_logging_only_task():
            return self.complete_logging_only_task()

        self.state.primary_artifact_path = self.detect_primary_artifact_path()

        self.logger.write("decisions", "Starting agent loop")
        print("Starting agent loop")
        self.append_external_log("Starting agent loop")

        while self.state.iteration < self.config.max_iterations:
            try:
                should_continue = self.perform_iteration(task_description)
            except Exception as exc:  # noqa: BLE001
                self.logger.write("errors", f"Iteration {self.state.iteration}: {exc}")
                print(f"Iteration {self.state.iteration} failed: {exc}")
                return 1

            if not should_continue:
                self.logger.write("decisions", f"Stopping at iteration {self.state.iteration}")
                print("Agent requested stop.")
                return 0

            self.state.iteration += 1

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
