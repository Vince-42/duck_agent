from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib import error, request
from urllib.parse import urlparse

from improved_orchestrator import ImprovedOrchestrator
from orchestration_state import StopReason
from requirement_extractor import RequirementExtractor
from requirement_graph import RequirementGraph, RequirementNode, RequirementStatus
from validator import ResultValidator


@dataclass
class AgentConfig:
    spec_path: Path = Path("secret_spec/SECRET_SPEC.md")
    logs_dir: Path = Path("agent_logs")
    task: str = ""
    task_file: Path | None = None
    provider: str = os.getenv("AGENT_PROVIDER", "ollama")
    model: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:latest")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    groq_model: str = os.getenv("GROQ_MODEL", "mixtral-8x7b-32768")
    groq_url: str = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    max_iterations: int = int(os.getenv("AGENT_MAX_ITERATIONS", "1000"))
    solution_command: str = os.getenv("AGENT_SOLUTION_COMMAND", "python3 solution.py")
    test_command: str = os.getenv("AGENT_TEST_COMMAND", "")
    enable_multirole: bool = os.getenv("AGENT_MULTI_ROLE", "1").lower() not in {"0", "false", "no"}
    max_prompt_size: int = int(os.getenv("AGENT_MAX_PROMPT_SIZE", "4096"))
    orchestrator_config: Path | None = Path(os.getenv("AGENT_ORCHESTRATOR_CONFIG")) if os.getenv("AGENT_ORCHESTRATOR_CONFIG") else Path("agent.json")


@dataclass
class RuntimeState:
    iteration: int = 0
    completed_actions: int = 0
    material_changes: int = 0
    consecutive_no_progress: int = 0
    repeated_action_count: int = 0
    dirty_since_test: bool = False
    last_test_exit_code: int | None = None
    last_test_fingerprint: str = ""
    last_test_output: str = "No tests run yet."
    last_validation_passes: list[str] = field(default_factory=list)
    last_validation_failures: list[str] = field(default_factory=list)
    last_action_summary: str = "No actions executed yet."
    last_action_fingerprint: str = ""
    last_action_progress: bool = False
    external_log_path: str = ""
    primary_artifact_path: str = ""
    rewrite_count_for_primary_artifact: int = 0
    current_artifact_hash: str = ""
    last_completed_artifact_hash: str = ""
    best_validation_score: int = 0
    best_validation_artifact_hash: str = ""
    rewrite_stagnation_count: int = 0
    invalid_response_streak: int = 0
    spec_pages: list[str] = field(default_factory=list)
    current_spec_page: int = 0
    requirements_summary: str = ""
    implementation_summary: str = ""
    failure_class: str = ""
    next_repair_objective: str = ""
    last_terminal_kind: str = ""
    chunk_extraction_status: dict[str, Any] = field(default_factory=dict)
    current_chunk_extraction_complete: bool = False
    chunks_with_pending_extraction: set[str] = field(default_factory=set)
    execution_steps: list[ExecutionStep] = field(default_factory=list)
    current_execution_step_index: int = 0
    step_to_requirements: dict[str, list[str]] = field(default_factory=dict)
    requirement_satisfaction_map: dict[str, bool] = field(default_factory=dict)
    requirement_coverage_summary: str = ""
    implemented_requirement_count: int = 0
    validated_requirement_count: int = 0
    pending_requirement_count: int = 0
    ambiguous_requirement_count: int = 0
    semantic_validation_passes: list[str] = field(default_factory=list)
    semantic_validation_failures: list[str] = field(default_factory=list)
    unmet_high_priority_requirement_ids: list[str] = field(default_factory=list)
    current_repair_strategy: RepairStrategy | None = None
    repair_strategy_history: list[RepairStrategy] = field(default_factory=list)
    repair_strategy_summary: str = ""
    validation_stage: str = "none"
    last_quick_validation_artifact_hash: str = ""
    quick_validation_ready_for_full_test: bool = False


@dataclass
class ActionOutcome:
    summary: str
    progress: bool
    fingerprint: str
    stop: bool = False
    test_exit_code: int | None = None
    path: str = ""
    artifact_hash: str = ""
    validation_passes: list[str] = field(default_factory=list)
    validation_failures: list[str] = field(default_factory=list)
    terminal_kind: str = ""


@dataclass
class TaskContract:
    cli_invocation: str = ""
    required_json_keys: list[str] = field(default_factory=list)
    optional_flags: list[str] = field(default_factory=list)
    must_rules: list[str] = field(default_factory=list)
    input_hints: list[str] = field(default_factory=list)
    error_hints: list[str] = field(default_factory=list)


@dataclass
class ExecutionStep:
    step_id: str
    description: str
    requirement_ids: list[str] = field(default_factory=list)
    related_node_kinds: list[str] = field(default_factory=list)
    completed: bool = False
    execution_evidence: str = ""


@dataclass
class SemanticValidationResult:
    passes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    unmet_requirement_ids: list[str] = field(default_factory=list)


@dataclass
class RepairStrategy:
    strategy_id: str
    failure_class: str
    objective: str
    target_requirement_ids: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    attempt_count: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the duck_agent hackathon scaffold.")
    parser.add_argument("requirement_file", nargs="?", help="Requirement/spec file to run, equivalent to --spec-path")
    parser.add_argument("--spec-path", default=str(AgentConfig.spec_path), help="Path to the spec markdown file")
    parser.add_argument("--logs-dir", default=str(AgentConfig.logs_dir), help="Directory for agent logs")
    parser.add_argument("--task", default="", help="Direct task text for general-purpose runs")
    parser.add_argument("--task-file", default="", help="Path to a markdown/text file describing the task")
    parser.add_argument(
        "--provider",
        default=os.getenv("AGENT_PROVIDER", "ollama"),
        help="Model provider to prefer: ollama, groq, or auto",
    )
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen2.5-coder:latest"), help="Ollama model name")
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
        help="Ollama generate endpoint URL",
    )
    parser.add_argument(
        "--groq-url",
        default=os.getenv("GROQ_URL", "https://api.groq.com/openai/v1"),
        help="Groq Cloud base URL",
    )
    parser.add_argument(
        "--groq-model",
        default=os.getenv("GROQ_MODEL", "mixtral-8x7b-32768"),
        help="Groq Cloud model name",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=int(os.getenv("AGENT_MAX_ITERATIONS", "1000")),
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
    parser.add_argument(
        "--max-prompt-size",
        type=int,
        default=int(os.getenv("AGENT_MAX_PROMPT_SIZE", "4096")),
        help="Maximum characters for the prompt sent to the AI model (default: 4096)",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> AgentConfig:
    spec_path = Path(args.requirement_file or args.spec_path)
    if spec_path.is_dir():
        spec_path = spec_path / "SECRET_SPEC.md"
    return AgentConfig(
        spec_path=spec_path,
        logs_dir=Path(args.logs_dir),
        task=args.task,
        task_file=Path(args.task_file) if args.task_file else None,
        provider=args.provider,
        model=args.model,
        ollama_url=args.ollama_url,
        groq_model=args.groq_model,
        groq_url=args.groq_url,
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        max_iterations=args.max_iterations,
        solution_command=args.solution_command,
        test_command=args.test_command,
        enable_multirole=not args.single_agent,
        max_prompt_size=args.max_prompt_size,
        orchestrator_config=Path(args.orchestrator_config) if args.orchestrator_config else None,
    )


def run_doctor(config: AgentConfig) -> int:
    print("duck_agent doctor")
    print(f"- spec path: {config.spec_path}")
    print(f"- task provided: {'yes' if config.task else 'no'}")
    print(f"- task file: {config.task_file if config.task_file else '(none)'}")
    print(f"- logs dir: {config.logs_dir}")
    print(f"- provider: {config.provider}")
    print(f"- model: {config.model}")
    print(f"- ollama url: {config.ollama_url}")
    print(f"- groq url: {config.groq_url}")

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
        self.improved_orchestrator = ImprovedOrchestrator(logs_dir=config.logs_dir, max_iterations=config.max_iterations)
        self.improved_orchestrator.state.task_id = self.hash_text(self.primary_task_text() or "default-task")[:12]
        self.improved_orchestrator.state.user_goal = self.primary_task_text().strip()
        self.improved_orchestrator.working_memory.objective = self.primary_task_text().strip()
        self.requirement_extractor = RequirementExtractor()
        self.requirement_graph: RequirementGraph | None = None
        self.orchestrator = (
            MultiRoleOrchestrator(orchestrator_config_path=config.orchestrator_config)
            if config.enable_multirole
            else None
        )

    def summarize_chunk_for_prompt(self, chunk: str) -> str:
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            return ""
        priority_lines = [
            line for line in lines
            if any(marker in line.lower() for marker in ("must", "exactly", "json", "error", "stdout", "stderr", "command", "input", "valid", "invalid"))
        ]
        selected = priority_lines[:6] if priority_lines else lines[:3] + lines[-2:]
        head = " ".join(selected[:3])
        tail = " ".join(selected[3:5]) if len(selected) > 3 else ""
        summary = head if not tail else f"{head} ... {tail}"
        return self.prompt_excerpt(summary, max_chars=320)

    def prepare_task_description_for_prompt(self, task_description: str) -> str:
        if len(task_description) <= self.config.max_prompt_size:
            return task_description
        processed = self.improved_orchestrator.process_long_task_description(
            task_description,
            summarize_fn=self.summarize_chunk_for_prompt,
        )
        self.improved_orchestrator.working_memory.current_status = (
            f"Condensed long task from {len(task_description)} chars into {len(processed)} chars"
        )
        return processed

    def sync_improved_orchestrator(self, action_type: str, outcome: ActionOutcome) -> None:
        improved_state = self.improved_orchestrator.state
        improved_state.iteration_count = self.state.iteration
        improved_state.actions_executed = self.state.completed_actions
        improved_state.files_written = self.state.material_changes
        improved_state.consecutive_no_progress_iterations = self.state.consecutive_no_progress
        improved_state.context_summary = self.state.requirements_summary
        improved_state.assumptions["primary_artifact_path"] = self.state.primary_artifact_path or ""
        improved_state.decisions_made["next_repair_objective"] = self.state.next_repair_objective

        validation_result = None
        if action_type == "WRITE_FILE":
            new_size = 0
            if outcome.path:
                artifact_path = Path(outcome.path)
                if artifact_path.exists():
                    new_size = len(artifact_path.read_text(encoding="utf-8"))
            validation_result = ResultValidator.validate_file_write(outcome.path, 0, new_size)
        elif action_type == "RUN_COMMAND":
            validation_result = ResultValidator.validate_command_output(
                0 if outcome.progress else 1,
                outcome.summary if outcome.progress else "",
                "" if outcome.progress else outcome.summary,
            )
        elif action_type == "RUN_TESTS" and outcome.test_exit_code is not None:
            validation_result = ResultValidator.validate_test_result(
                outcome.test_exit_code,
                outcome.summary,
                improved_state.validation_score,
            )

        if validation_result is not None:
            improved_state.validation_score = max(improved_state.validation_score, validation_result.score)
            self.improved_orchestrator.update_working_memory_from_result(action_type, validation_result, outcome.summary)
            self.improved_orchestrator.logger.log_action_result(
                action_type,
                outcome.progress,
                validation_result.score,
                outcome.summary,
            )

        if outcome.progress:
            improved_state.record_step_execution(
                improved_state.current_step_description(),
                action_type,
                outcome.summary,
                True,
                outcome.fingerprint,
            )
        else:
            improved_state.record_failure(action_type.lower(), outcome.summary, outcome.summary)

        if self.state.last_validation_failures:
            improved_state.tests_failed += 1
        elif self.state.last_test_exit_code == 0:
            improved_state.tests_passed += 1

    def has_test_target(self) -> bool:
        return self.has_validation_target()

    def should_use_public_test_runner(self) -> bool:
        return self.config.task_file is None and not self.config.task and self.config.spec_path.name == "SECRET_SPEC.md"

    def public_test_runner_path(self) -> Path | None:
        if not self.should_use_public_test_runner():
            return None
        candidates = [self.config.spec_path.parent / "test_runner" / "run_tests.py", Path("secret_spec/test_runner/run_tests.py")]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def has_validation_target(self) -> bool:
        return bool(self.config.test_command) or self.public_test_runner_path() is not None or self.can_run_builtin_validation()

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

    def resolved_write_path(self, requested_path: str) -> str:
        if requested_path:
            return requested_path
        if self.state.primary_artifact_path:
            return self.state.primary_artifact_path
        return self.detect_primary_artifact_path()

    def primary_task_text(self) -> str:
        if self.config.task:
            return self.config.task.strip()

        if self.config.task_file and self.config.task_file.exists():
            return self.config.task_file.read_text(encoding="utf-8").strip()

        if self.config.spec_path.exists():
            return self.config.spec_path.read_text(encoding="utf-8").strip()

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

        cli_script_path = self.extract_cli_script_path()
        if cli_script_path:
            return cli_script_path

        if self.config.task_file is None and not self.config.task:
            default_script_path = self.extract_script_path_from_command(self.config.solution_command)
            if default_script_path:
                return default_script_path

        patterns = [
            r"(?:file|program|script|document|message)\s+called\s+([A-Za-z0-9_.\-/]+)",
            r"(?:create|write|generate)\s+(?:a\s+)?([A-Za-z0-9_.\-/]+\.(?:txt|md|py|js|ts|json|html|css|c|cpp))",
        ]
        for pattern in patterns:
            match = re.search(pattern, task_description, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()

        inferred_program = re.search(
            r"(?:create|write|build|make)\s+(?:a\s+|an\s+)?([A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+)*)\s+(?:program|script|cli|tool)\s+in\s+(python|c\+\+|cpp|c)",
            task_description,
            flags=re.IGNORECASE,
        )
        if inferred_program:
            artifact_stem = re.sub(r"\s+", "_", inferred_program.group(1).strip().replace("-", "_")).lower()
            language = inferred_program.group(2).lower()
            if language == "python":
                return f"{artifact_stem}.py"
            if language in {"c++", "cpp"}:
                return f"{artifact_stem}.cpp"
            return f"{artifact_stem}.c"

        lowered = task_description.lower()
        if any(marker in lowered for marker in ("document", "message", "introduction")):
            return "output.txt"

        return ""

    @staticmethod
    def extract_script_path_from_command(command: str) -> str:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return ""
        if len(tokens) >= 2 and tokens[0].startswith("python") and "." in tokens[1]:
            return tokens[1]
        return ""

    def task_requires_runnable_program(self) -> bool:
        task_description = self.primary_task_text().lower()
        return any(keyword in task_description for keyword in (" program", " script", " cli", " command-line", " calculator"))

    def task_requests_json_stdout(self) -> bool:
        task_description = self.primary_task_text().lower()
        return "json object" in task_description or "json" in task_description

    def task_is_complex(self) -> bool:
        task_description = self.primary_task_text()
        if not task_description:
            return False
        return len(task_description) > 1200 or len([line for line in task_description.splitlines() if line.strip()]) > 40

    def extract_cli_invocation(self) -> str:
        task_description = self.primary_task_text()
        for raw_line in task_description.splitlines():
            line = raw_line.strip().strip("`")
            if line.startswith("$"):
                line = line[1:].strip()
            if line.startswith("python ") or line.startswith("python3 "):
                return line
        return ""

    def extract_cli_script_path(self) -> str:
        invocation = self.extract_cli_invocation()
        if not invocation:
            return ""
        try:
            tokens = shlex.split(invocation)
        except ValueError:
            return ""
        if len(tokens) < 2 or not tokens[0].startswith("python"):
            return ""
        script_candidate = tokens[1].strip()
        return script_candidate if "." in script_candidate else ""

    @staticmethod
    def normalize_direct_python_command(command: str) -> str:
        stripped = command.strip()
        if not stripped.startswith("python "):
            return command
        if shutil.which("python") is not None or shutil.which("python3") is None:
            return command
        return re.sub(r"^python\b", "python3", command, count=1)

    def declared_validation_command(self) -> str:
        invocation = self.extract_cli_invocation()
        if invocation:
            return self.normalize_direct_python_command(invocation)
        return self.normalize_direct_python_command(self.config.solution_command)

    def build_debug_cli_command(self, sample_input_name: str = "sample_input.txt") -> str:
        command = self.declared_validation_command()
        command = re.sub(r"\[[^\]]+\]", "", command)
        command = re.sub(r"<[^>]*input[^>]*>", sample_input_name, command, flags=re.IGNORECASE)
        command = re.sub(r"<[^>]+>", sample_input_name, command)
        return self.normalize_direct_python_command(re.sub(r"\s+", " ", command).strip())

    def extract_task_contract(self) -> TaskContract:
        graph = self.extract_requirement_graph()
        graph_contract = self.task_contract_from_requirement_graph(graph)
        task_text = self.primary_task_text()
        lines = [line.rstrip() for line in task_text.splitlines()]
        contract = TaskContract(cli_invocation=graph_contract.cli_invocation or self.extract_cli_invocation())
        contract.required_json_keys.extend(graph_contract.required_json_keys)
        contract.optional_flags.extend(graph_contract.optional_flags)
        contract.must_rules.extend(graph_contract.must_rules)
        contract.input_hints.extend(graph_contract.input_hints)
        contract.error_hints.extend(graph_contract.error_hints)

        if contract.cli_invocation:
            contract.optional_flags.extend(re.findall(r"(\-\-[A-Za-z0-9_-]+)", contract.cli_invocation))

        current_section = ""
        collecting_json_keys = False
        for raw_line in lines:
            stripped = raw_line.strip()
            lowered = stripped.lower()

            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip().lower()
                current_section = heading
                collecting_json_keys = "json" in heading and "key" in heading
                continue

            if not stripped:
                if collecting_json_keys:
                    collecting_json_keys = False
                continue

            if "json must contain" in lowered or "required json keys" in lowered:
                collecting_json_keys = True

            if collecting_json_keys and (stripped.startswith("-") or stripped.startswith("*")):
                key_match = re.search(r"`([^`]+)`", stripped)
                if key_match:
                    contract.required_json_keys.append(key_match.group(1).strip())
                else:
                    key = stripped.lstrip("-* ").split()[0].strip(":,.")
                    if key:
                        contract.required_json_keys.append(key)
                continue

            if "must" in lowered or "exactly" in lowered or lowered.startswith("do not"):
                contract.must_rules.append(stripped)

            if current_section.startswith("input") or "input format" in lowered or "input file" in lowered:
                if stripped.startswith("-") or stripped.startswith("*") or "input" in lowered:
                    contract.input_hints.append(stripped)

            if "error" in lowered or "stderr" in lowered or "exit with code" in lowered or "does not exist" in lowered:
                contract.error_hints.append(stripped)

            contract.optional_flags.extend(re.findall(r"(\-\-[A-Za-z0-9_-]+)", stripped))

        contract.required_json_keys = list(dict.fromkeys(contract.required_json_keys))
        contract.optional_flags = list(dict.fromkeys(contract.optional_flags))
        contract.must_rules = contract.must_rules[:8]
        contract.input_hints = contract.input_hints[:6]
        contract.error_hints = contract.error_hints[:6]
        return contract

    def extract_requirement_graph(self) -> RequirementGraph:
        if self.requirement_graph is not None:
            return self.requirement_graph

        self.requirement_graph = self.requirement_extractor.build_graph(
            self.primary_task_text(),
            chunk_id="full-task",
        )
        summary = self.requirement_graph.summary()
        self.improved_orchestrator.state.requirement_graph_summary = (
            f"{summary['nodes']} nodes, {summary['edges']} edges, {summary['ambiguities']} ambiguities"
        )
        self.improved_orchestrator.state.requirement_graph_node_count = summary["nodes"]
        self.improved_orchestrator.state.open_ambiguity_count = summary["ambiguities"]
        return self.requirement_graph

    def task_contract_from_requirement_graph(self, graph: RequirementGraph) -> TaskContract:
        contract = TaskContract()
        for node in graph.nodes.values():
            text = node.normalized_text
            if node.kind == "cli_contract" and not contract.cli_invocation:
                contract.cli_invocation = text
                contract.optional_flags.extend(re.findall(r"(\-\-[A-Za-z0-9_-]+)", text))
            elif node.kind == "json_schema_rule":
                contract.required_json_keys.extend(re.findall(r"`([^`]+)`", text))
                if text not in contract.must_rules:
                    contract.must_rules.append(text)
            elif node.kind == "input_rule":
                contract.input_hints.append(text)
            elif node.kind == "error_rule":
                contract.error_hints.append(text)
            elif node.kind in {"output_rule", "invariant", "forbidden_behavior", "completion_criterion"}:
                contract.must_rules.append(text)
            contract.optional_flags.extend(re.findall(r"(\-\-[A-Za-z0-9_-]+)", text))

        contract.required_json_keys = list(dict.fromkeys(contract.required_json_keys))
        contract.optional_flags = list(dict.fromkeys(contract.optional_flags))
        contract.must_rules = list(dict.fromkeys(contract.must_rules))[:8]
        contract.input_hints = list(dict.fromkeys(contract.input_hints))[:6]
        contract.error_hints = list(dict.fromkeys(contract.error_hints))[:6]
        return contract

    def derive_execution_steps_from_requirement_graph(self, graph: RequirementGraph) -> list[ExecutionStep]:
        grouped_nodes = self._group_requirement_nodes(graph)
        execution_steps = [
            ExecutionStep(
                step_id="step-understand",
                description="Understand the user task and constraints",
                requirement_ids=[],
                related_node_kinds=sorted({node.kind for node in graph.nodes.values()}),
            )
        ]

        if grouped_nodes["cli_contract"]:
            execution_steps.append(
                ExecutionStep(
                    step_id="step-cli-contract",
                    description="Implement the declared CLI contract",
                    requirement_ids=[node.stable_id for node in grouped_nodes["cli_contract"]],
                    related_node_kinds=["cli_contract"],
                )
            )
        if grouped_nodes["input_rule"]:
            execution_steps.append(
                ExecutionStep(
                    step_id="step-input-rules",
                    description="Implement the declared input format and parsing behavior",
                    requirement_ids=[node.stable_id for node in grouped_nodes["input_rule"]],
                    related_node_kinds=["input_rule"],
                )
            )
        output_requirement_ids = [
            node.stable_id for node in grouped_nodes["json_schema_rule"] + grouped_nodes["output_rule"]
        ]
        if output_requirement_ids:
            execution_steps.append(
                ExecutionStep(
                    step_id="step-output-rules",
                    description="Produce the required JSON output structure",
                    requirement_ids=output_requirement_ids,
                    related_node_kinds=[kind for kind in ["json_schema_rule", "output_rule"] if grouped_nodes[kind]],
                )
            )
        error_requirement_ids = [
            node.stable_id for node in grouped_nodes["error_rule"] + grouped_nodes["forbidden_behavior"]
        ]
        if error_requirement_ids:
            execution_steps.append(
                ExecutionStep(
                    step_id="step-error-rules",
                    description="Implement the required error handling and forbidden-behavior constraints",
                    requirement_ids=error_requirement_ids,
                    related_node_kinds=[kind for kind in ["error_rule", "forbidden_behavior"] if grouped_nodes[kind]],
                )
            )
        validation_requirement_ids = [
            node.stable_id for node in grouped_nodes["completion_criterion"] + grouped_nodes["invariant"]
        ]
        if validation_requirement_ids:
            execution_steps.append(
                ExecutionStep(
                    step_id="step-validation-rules",
                    description="Validate high-priority completion and invariant requirements",
                    requirement_ids=validation_requirement_ids,
                    related_node_kinds=[kind for kind in ["completion_criterion", "invariant"] if grouped_nodes[kind]],
                )
            )

        covered_requirement_ids = {
            requirement_id
            for step in execution_steps
            for requirement_id in step.requirement_ids
        }
        uncovered_requirement_ids = [
            node_id for node_id in sorted(graph.nodes.keys()) if node_id not in covered_requirement_ids
        ]
        if uncovered_requirement_ids:
            execution_steps.append(
                ExecutionStep(
                    step_id="step-generic-requirements",
                    description="Implement uncovered extracted requirements",
                    requirement_ids=uncovered_requirement_ids,
                    related_node_kinds=sorted({graph.nodes[node_id].kind for node_id in uncovered_requirement_ids}),
                )
            )

        execution_steps.extend(
            [
                ExecutionStep(
                    step_id="step-validate-repair",
                    description="Validate the result and repair if necessary",
                    requirement_ids=[],
                    related_node_kinds=["validation"],
                ),
                ExecutionStep(
                    step_id="step-stop",
                    description="Stop with evidence",
                    requirement_ids=[],
                    related_node_kinds=["completion"],
                ),
            ]
        )

        deduped_steps: list[ExecutionStep] = []
        seen_ids: set[str] = set()
        for step in execution_steps:
            if step.step_id in seen_ids:
                continue
            step.requirement_ids = list(dict.fromkeys(step.requirement_ids))
            step.related_node_kinds = list(dict.fromkeys(step.related_node_kinds))
            deduped_steps.append(step)
            seen_ids.add(step.step_id)
        return deduped_steps

    def derive_planned_steps_from_requirement_graph(self, graph: RequirementGraph) -> list[str]:
        return [step.description for step in self.derive_execution_steps_from_requirement_graph(graph)]

    def validate_execution_plan_coverage(self, graph: RequirementGraph, execution_steps: list[ExecutionStep]) -> bool:
        covered_requirement_ids: set[str] = set()
        for step in execution_steps:
            covered_requirement_ids.update(step.requirement_ids)
        return set(graph.nodes.keys()).issubset(covered_requirement_ids)

    def initialize_execution_plan(self, graph: RequirementGraph) -> list[ExecutionStep]:
        execution_steps = self.derive_execution_steps_from_requirement_graph(graph)
        self.state.execution_steps = execution_steps
        self.state.current_execution_step_index = 0
        self.state.step_to_requirements = {step.step_id: list(step.requirement_ids) for step in execution_steps}
        self.state.requirement_satisfaction_map = {node_id: False for node_id in graph.nodes.keys()}
        for step in execution_steps:
            for requirement_id in step.requirement_ids:
                node = graph.nodes.get(requirement_id)
                if node is not None and node.status == RequirementStatus.EXTRACTED:
                    node.status = RequirementStatus.PLANNED
        return execution_steps

    def mark_current_execution_step_complete(self, evidence: str) -> None:
        if not self.state.execution_steps:
            return
        if self.state.current_execution_step_index >= len(self.state.execution_steps):
            return
        step = self.state.execution_steps[self.state.current_execution_step_index]
        step.completed = True
        step.execution_evidence = evidence
        for requirement_id in step.requirement_ids:
            self.state.requirement_satisfaction_map[requirement_id] = True
            if self.requirement_graph is not None:
                node = self.requirement_graph.nodes.get(requirement_id)
                if node is not None and node.status in {RequirementStatus.EXTRACTED, RequirementStatus.PLANNED}:
                    node.status = RequirementStatus.IMPLEMENTED
        self.state.current_execution_step_index += 1
        if self.improved_orchestrator.state.current_step_index < len(self.improved_orchestrator.state.planned_steps):
            self.improved_orchestrator.state.current_step_index += 1

    def compute_requirement_coverage(self) -> dict[str, int | str]:
        if self.requirement_graph is None:
            return {
                "implemented": 0,
                "validated": 0,
                "pending": 0,
                "ambiguous": 0,
                "total": 0,
                "summary": "no requirement graph",
            }

        implemented = 0
        validated = 0
        pending = 0
        ambiguous = len(self.requirement_graph.ambiguities)
        for node in self.requirement_graph.nodes.values():
            if node.status == RequirementStatus.VALIDATED:
                validated += 1
            elif node.status == RequirementStatus.IMPLEMENTED:
                implemented += 1
            elif node.status == RequirementStatus.AMBIGUOUS:
                ambiguous += 1
            else:
                pending += 1
        total = len(self.requirement_graph.nodes)
        summary = f"validated {validated}/{total}, implemented {implemented}, pending {pending}, ambiguities {ambiguous}"
        return {
            "implemented": implemented,
            "validated": validated,
            "pending": pending,
            "ambiguous": ambiguous,
            "total": total,
            "summary": summary,
        }

    def semantic_validate_requirement_graph(self) -> SemanticValidationResult:
        result = SemanticValidationResult()
        if self.requirement_graph is None:
            result.passes.append("no_requirement_graph")
            return result

        for node in self.requirement_graph.nodes.values():
            requires_coverage = node.priority in {"high", "critical"} or node.kind in {
                "cli_contract",
                "json_schema_rule",
                "output_rule",
                "error_rule",
                "completion_criterion",
                "invariant",
            }
            if not requires_coverage:
                continue
            if node.status not in {RequirementStatus.IMPLEMENTED, RequirementStatus.VALIDATED}:
                result.unmet_requirement_ids.append(node.stable_id)
                result.failures.append(f"unmet:{node.kind}:{node.stable_id}")

        if not result.failures:
            result.passes.append("all_high_priority_requirements_covered")
            if self.requirement_graph.nodes:
                result.passes.append("requirement_graph_semantically_consistent")
        return result

    def update_requirement_coverage_state(self) -> None:
        coverage = self.compute_requirement_coverage()
        semantic = self.semantic_validate_requirement_graph()
        self.state.implemented_requirement_count = int(coverage["implemented"])
        self.state.validated_requirement_count = int(coverage["validated"])
        self.state.pending_requirement_count = int(coverage["pending"])
        self.state.ambiguous_requirement_count = int(coverage["ambiguous"])
        self.state.requirement_coverage_summary = str(coverage["summary"])
        self.state.semantic_validation_passes = list(dict.fromkeys(semantic.passes))
        self.state.semantic_validation_failures = list(dict.fromkeys(semantic.failures))
        self.state.unmet_high_priority_requirement_ids = list(dict.fromkeys(semantic.unmet_requirement_ids))
        self.improved_orchestrator.state.validated_requirement_count = self.state.validated_requirement_count
        self.improved_orchestrator.state.implemented_requirement_count = self.state.implemented_requirement_count
        self.improved_orchestrator.state.pending_requirement_count = self.state.pending_requirement_count
        self.improved_orchestrator.state.requirement_coverage_summary = self.state.requirement_coverage_summary
        semantic_summary = ", ".join(self.state.semantic_validation_failures[:3]) if self.state.semantic_validation_failures else "ok"
        self.improved_orchestrator.state.semantic_validation_summary = semantic_summary
        self.improved_orchestrator.working_memory.current_status = self.state.requirement_coverage_summary
        self.improved_orchestrator.working_memory.add_assumption("requirement_coverage", self.state.requirement_coverage_summary)
        if self.state.semantic_validation_failures:
            self.improved_orchestrator.working_memory.set_next_actions([
                f"Address semantic gaps: {self.state.semantic_validation_failures[0]}"
            ])

    def render_requirement_coverage(self) -> str:
        if not self.state.requirement_coverage_summary:
            return ""
        lines = [f"- Coverage: {self.state.requirement_coverage_summary}"]
        if self.state.unmet_high_priority_requirement_ids:
            lines.append(
                f"- Unmet high-priority requirement IDs: {', '.join(self.state.unmet_high_priority_requirement_ids[:4])}"
            )
        if self.state.semantic_validation_failures:
            lines.append(f"- Semantic validation blockers: {' | '.join(self.state.semantic_validation_failures[:3])}")
        elif self.state.semantic_validation_passes:
            lines.append(f"- Semantic validation: {' | '.join(self.state.semantic_validation_passes[:2])}")
        return "\n".join(lines)

    def render_repair_strategy_block(self) -> str:
        return self.render_repair_strategy()

    @staticmethod
    def _group_requirement_nodes(graph: RequirementGraph) -> dict[str, list[RequirementNode]]:
        groups: dict[str, list[RequirementNode]] = {
            "cli_contract": [],
            "input_rule": [],
            "output_rule": [],
            "json_schema_rule": [],
            "error_rule": [],
            "invariant": [],
            "forbidden_behavior": [],
            "completion_criterion": [],
        }
        for node in graph.nodes.values():
            if node.kind in groups:
                groups[node.kind].append(node)
        return groups

    def render_task_contract(self) -> str:
        contract = self.extract_task_contract()
        parts: list[str] = []
        if contract.cli_invocation:
            parts.append(f"- CLI contract: {contract.cli_invocation}")
        if contract.required_json_keys:
            parts.append(f"- Required JSON keys: {', '.join(contract.required_json_keys[:8])}")
        if contract.optional_flags:
            parts.append(f"- Optional flags seen: {', '.join(contract.optional_flags[:6])}")
        if contract.input_hints:
            parts.append(f"- Input requirements: {' | '.join(contract.input_hints[:3])}")
        if contract.error_hints:
            parts.append(f"- Error handling hints: {' | '.join(contract.error_hints[:3])}")
        if contract.must_rules:
            parts.append(f"- High-priority rules: {' | '.join(contract.must_rules[:4])}")
        return "\n".join(parts)

    def build_sample_cli_command(self, artifact_path: str, sample_input_path: Path) -> str:
        invocation = self.extract_cli_invocation()
        if not invocation:
            return f"python3 {shlex.quote(artifact_path)} {shlex.quote(str(sample_input_path))}"

        command = re.sub(r"\[[^\]]+\]", "", invocation)
        command = re.sub(r"<[^>]*input[^>]*>", shlex.quote(str(sample_input_path)), command, flags=re.IGNORECASE)
        command = re.sub(r"<[^>]+>", shlex.quote(str(sample_input_path)), command)
        command = re.sub(r"\s+", " ", command).strip()

        tokens = shlex.split(command)
        if len(tokens) >= 2 and tokens[0].startswith("python"):
            if tokens[0] == "python" and shutil.which("python") is None and shutil.which("python3") is not None:
                tokens[0] = "python3"
            tokens[1] = artifact_path
        if all(str(sample_input_path) not in token for token in tokens) and any("input" in token.lower() for token in invocation.split()):
            tokens.append(str(sample_input_path))
        return self.normalize_direct_python_command(" ".join(shlex.quote(token) for token in tokens))

    def task_looks_like_file_processing_cli(self) -> bool:
        task_description = self.primary_task_text().lower()
        file_markers = ("input file", "input-file", "<input-file>", "read a utf-8 text file", "reads a utf-8 text file", "read a text file", "reads a text file")
        return self.task_requires_runnable_program() and self.task_requests_json_stdout() and any(marker in task_description for marker in file_markers)

    def task_requests_text_stats_fields(self) -> bool:
        task_description = self.primary_task_text().lower()
        required_fields = ("line_count", "word_count", "char_count", "top_words")
        return all(field in task_description for field in required_fields)

    def task_requests_top_n_option(self) -> bool:
        task_description = self.primary_task_text().lower()
        return "--top n" in task_description or "--top" in task_description

    def task_requests_missing_file_json_error(self) -> bool:
        task_description = self.primary_task_text().lower()
        return "input file does not exist" in task_description or "file does not exist" in task_description

    @staticmethod
    def validation_score(passed: list[str], failed: list[str]) -> int:
        return len(set(passed)) - len(set(failed))

    @staticmethod
    def is_destructive_rewrite(previous_content: str, new_content: str) -> bool:
        if not previous_content.strip() or not new_content.strip():
            return False
        similarity = difflib.SequenceMatcher(a=previous_content, b=new_content).ratio()
        length_ratio = len(new_content) / max(len(previous_content), 1)
        return similarity < 0.45 or length_ratio < 0.5 or length_ratio > 2.0

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
        groq_base = self.config.groq_url.rstrip("/")

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
        groq_payload = json.dumps(
            {
                "model": self.config.groq_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        ).encode("utf-8")

        candidates: list[tuple[str, bytes, str]] = []
        seen_urls: set[str] = set()

        def add_candidate(url: str, payload: bytes, response_type: str) -> None:
            key = f"{response_type}:{url}"
            if key not in seen_urls:
                candidates.append((url, payload, response_type))
                seen_urls.add(key)

        provider = self.config.provider.lower().strip()

        def add_ollama_candidates() -> None:
            if path in {"", "/"}:
                add_candidate(f"{base_url}/api/generate", ollama_payload, "ollama")
                add_candidate(f"{base_url}/v1/chat/completions", chat_payload, "openai")
            else:
                add_candidate(self.config.ollama_url, ollama_payload, "ollama")
                add_candidate(self.config.ollama_url, chat_payload, "openai")
                add_candidate(f"{base_url}/api/generate", ollama_payload, "ollama")
                add_candidate(f"{base_url}/v1/chat/completions", chat_payload, "openai")

        def add_groq_candidates() -> None:
            if self.config.groq_url and self.config.groq_api_key:
                add_candidate(f"{groq_base}/chat/completions", groq_payload, "groq")

        if provider == "groq":
            add_groq_candidates()
        elif provider == "auto":
            add_groq_candidates()
            add_ollama_candidates()
        else:
            add_ollama_candidates()

        return candidates

    def parse_model_response(self, raw_response: str, response_type: str) -> str:
        parsed = json.loads(raw_response)

        if response_type == "ollama":
            return str(parsed.get("response", "")).strip()

        if response_type == "groq":
            if parsed.get("response"):
                return str(parsed.get("response", "")).strip()
            if parsed.get("output_text"):
                return str(parsed.get("output_text", "")).strip()
            if parsed.get("output") and isinstance(parsed["output"], list):
                for item in parsed["output"]:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str) and text.strip():
                            return text.strip()

        choices = parsed.get("choices", [])
        if not choices:
            raise ValueError("OpenAI-compatible response did not include choices")

        message = choices[0].get("message", {})
        return str(message.get("content", "")).strip()

    def call_model(self, prompt: str) -> str:
        errors_seen: list[str] = []
        provider = self.config.provider.lower().strip()

        if provider == "groq" and not self.config.groq_api_key:
            raise RuntimeError("Model call failed: provider 'groq' requires GROQ_API_KEY")

        model_timeout = int(os.getenv("AGENT_MODEL_TIMEOUT", "945"))

        for url, payload, response_type in self.model_request_candidates(prompt):
            headers = {"Content-Type": "application/json"}
            if response_type == "groq" and self.config.groq_api_key:
                headers["Authorization"] = f"Bearer {self.config.groq_api_key}"
            http_request = request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )

            try:
                with request.urlopen(http_request, timeout=model_timeout) as response:
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
            except (socket.timeout, TimeoutError) as exc:
                errors_seen.append(f"{url} -> timed out ({exc})")
                continue
            except (ValueError, json.JSONDecodeError) as exc:
                errors_seen.append(f"{url} -> invalid response ({exc})")
                continue

        joined_errors = "; ".join(errors_seen) if errors_seen else "no candidates tried"
        raise RuntimeError(f"Model call failed: {joined_errors}")

    @staticmethod
    def prompt_excerpt(text: str, *, max_chars: int = 600) -> str:
        normalized = text.strip()
        if len(normalized) <= max_chars:
            return normalized
        return f"...{normalized[-max_chars:]}"

    @staticmethod
    def normalize_trivial_content(text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()

    @classmethod
    def is_trivial_rewrite(cls, previous_content: str, new_content: str) -> bool:
        return cls.normalize_trivial_content(previous_content) == cls.normalize_trivial_content(new_content)

    def summarize_requirements(self) -> str:
        task_text = self.primary_task_text().strip()
        if not task_text:
            return "No task summary available."
        lines = [line.strip() for line in task_text.splitlines() if line.strip()]
        summary = " ".join(lines[:3])
        return self.prompt_excerpt(summary, max_chars=220)

    def is_prompt_regurgitation(self, content: str) -> bool:
        normalized = content.strip()
        if not normalized:
            return False

        lowered = normalized.lower()

        prompt_markers = (
            "current iteration:",
            "last test output:",
            "last action result:",
            "persistent iteration memory:",
            "multi-role orchestration context:",
            "return exactly one json object with this schema:",
            "task description:",
            "validation workflow:",
            "repair guidance:",
            "primary artifact target:",
            "write target guidance:",
            "program expectation:",
            "shared rules:",
            "deterministic constraints:",
            "active mode briefs:",
        )
        if any(marker in lowered for marker in prompt_markers):
            return True

        task_text = self.primary_task_text().strip()
        if task_text:
            task_lines = [line.strip() for line in task_text.splitlines() if line.strip()]
            shared_lines = sum(1 for line in task_lines if len(line) > 20 and line.lower() in lowered)
            if shared_lines >= 6:
                return True

        return False

    def summarize_implementation_state(self) -> str:
        path = self.state.primary_artifact_path or "(unset)"
        artifact = Path(path) if path and path != "(unset)" else None
        if artifact and artifact.exists():
            size = len(artifact.read_text(encoding="utf-8"))
            return f"Target {path} exists ({size} chars). Last action: {self.prompt_excerpt(self.state.last_action_summary, max_chars=120)}"
        return f"Target {path} not created yet. Last action: {self.prompt_excerpt(self.state.last_action_summary, max_chars=120)}"

    def current_artifact_excerpt(self, *, max_chars: int = 900) -> str:
        path = self.state.primary_artifact_path
        if not path:
            return ""
        artifact = Path(path)
        if not artifact.exists():
            return ""
        try:
            content = artifact.read_text(encoding="utf-8")
        except OSError:
            return ""
        normalized = content.strip()
        if len(normalized) <= max_chars:
            return normalized
        front = normalized[: max_chars // 2]
        back = normalized[-(max_chars // 3) :]
        return f"{front}\n...\n{back}"

    def extract_validation_focus(self) -> str:
        output = (self.state.last_test_output or "").strip()
        if not output:
            return ""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("Validation expected"):
                return line
        for line in lines:
            if "SyntaxError" in line:
                return line
        for line in lines:
            if "missing a runnable entrypoint" in line:
                return line
        return self.prompt_excerpt(lines[-1], max_chars=180) if lines else ""

    def classify_failure_state(self) -> str:
        summary = self.state.last_action_summary.lower()
        if self.state.last_validation_failures:
            return f"validation:{', '.join(self.state.last_validation_failures)}"
        if self.state.last_test_exit_code not in {None, 0}:
            return "test_failure"
        if "stalled:" in summary:
            return "stall"
        if "invalid model response" in summary:
            return "invalid_model_response"
        if self.state.last_test_exit_code == 0 and self.state.last_test_output != "No tests run yet.":
            return "validation_passing"
        return "none"

    def select_repair_strategy(self) -> RepairStrategy | None:
        failure_class = self.classify_failure_state()
        if failure_class in {"none", "validation_passing"} and not self.state.semantic_validation_failures:
            return None

        evidence: list[str] = []
        if self.state.semantic_validation_failures:
            evidence.extend(self.state.semantic_validation_failures[:4])
        if self.state.last_validation_failures:
            evidence.extend([f"validation:{item}" for item in self.state.last_validation_failures[:4]])
        validation_focus = self.extract_validation_focus()
        if validation_focus:
            evidence.append(validation_focus)

        target_requirement_ids = list(self.state.unmet_high_priority_requirement_ids[:6])
        recommended_actions: list[str] = []
        objective = "Continue with the smallest evidence-backed next step."

        if "Refusing to rerun validation before repairing failing checks" in self.state.last_action_summary:
            objective = "Do not choose RUN_TESTS next. Your next action must be WRITE_FILE to patch the artifact or RUN_COMMAND to gather targeted evidence."
            recommended_actions.append("WRITE_FILE: patch the artifact before any further validation")
            recommended_actions.append("RUN_COMMAND: gather targeted evidence for the failing checks")
        elif self.state.semantic_validation_failures:
            objective = "Implement or validate unmet high-priority requirements before stopping."
            recommended_actions.append("WRITE_FILE: patch the primary artifact to satisfy unmet high-priority requirements")
            if validation_focus:
                recommended_actions.append("RUN_COMMAND: gather targeted evidence for the exact failing semantic gap")
        elif self.state.last_validation_failures:
            objective = f"Repair failing validation checks: {', '.join(self.state.last_validation_failures[:3])}."
            recommended_actions.append("WRITE_FILE: patch only the code paths tied to the failing validation checks")
            if validation_focus:
                recommended_actions.append("RUN_COMMAND: reproduce and inspect the exact failing validation issue")
            recommended_actions.append("RUN_TESTS: only after a targeted patch or new evidence")
        elif self.state.last_test_exit_code not in {None, 0}:
            objective = "Use the latest test evidence to make the smallest repair before rerunning tests."
            recommended_actions.append("RUN_COMMAND: inspect the exact runtime or CLI failure")
            recommended_actions.append("WRITE_FILE: patch the smallest failing path")
        elif self.state.invalid_response_streak > 0:
            objective = "Recover clean structured control after invalid model output."
            recommended_actions.append("RETURN_VALID_JSON: emit one valid JSON action object")
        elif self.state.dirty_since_test and self.has_validation_target():
            objective = "Validate the latest material change before stopping."
            recommended_actions.append("RUN_TESTS: validate the latest changed artifact")

        if not recommended_actions:
            recommended_actions.append("RUN_COMMAND: gather targeted evidence for the next minimal fix")

        strategy_basis = "|".join([failure_class, *target_requirement_ids[:2], *(self.state.last_validation_failures[:2])]) or failure_class
        strategy_id = f"repair-{self.hash_text(strategy_basis)[:10]}"
        attempt_count = 1
        if self.state.current_repair_strategy and self.state.current_repair_strategy.strategy_id == strategy_id:
            attempt_count = self.state.current_repair_strategy.attempt_count + 1
        return RepairStrategy(
            strategy_id=strategy_id,
            failure_class=failure_class,
            objective=objective,
            target_requirement_ids=target_requirement_ids,
            recommended_actions=list(dict.fromkeys(recommended_actions)),
            evidence=list(dict.fromkeys(evidence)),
            attempt_count=attempt_count,
        )

    def update_repair_strategy_state(self) -> None:
        strategy = self.select_repair_strategy()
        previous_strategy = self.state.current_repair_strategy
        self.state.current_repair_strategy = strategy
        if strategy is None:
            self.state.repair_strategy_summary = ""
            self.improved_orchestrator.state.repair_strategy_summary = ""
            self.improved_orchestrator.working_memory.decisions.pop("repair_strategy", None)
            return
        if previous_strategy is None or previous_strategy.strategy_id != strategy.strategy_id:
            self.state.repair_strategy_history.append(strategy)
        self.state.repair_strategy_summary = self.render_repair_strategy(strategy)
        self.improved_orchestrator.state.repair_strategy_summary = strategy.objective
        self.improved_orchestrator.working_memory.add_decision("repair_strategy", strategy.objective)
        if strategy.recommended_actions:
            self.improved_orchestrator.working_memory.set_next_actions(strategy.recommended_actions)

    def render_repair_strategy(self, strategy: RepairStrategy | None = None) -> str:
        strategy = strategy or self.state.current_repair_strategy
        if strategy is None:
            return ""
        parts = [
            f"- Failure class: {strategy.failure_class}",
            f"- Objective: {strategy.objective}",
            f"- Attempt: {strategy.attempt_count}",
        ]
        if strategy.target_requirement_ids:
            parts.append(f"- Target requirements: {', '.join(strategy.target_requirement_ids[:4])}")
        if strategy.recommended_actions:
            parts.append(f"- Recommended next actions: {' | '.join(strategy.recommended_actions[:3])}")
        if strategy.evidence:
            parts.append(f"- Evidence: {' | '.join(strategy.evidence[:3])}")
        return "\n".join(parts)

    def compute_next_repair_objective(self) -> str:
        strategy = self.state.current_repair_strategy
        if strategy is not None:
            return strategy.objective
        if "Refusing to rerun validation before repairing failing checks" in self.state.last_action_summary:
            return "Do not choose RUN_TESTS next. Your next action must be WRITE_FILE to patch the artifact or RUN_COMMAND to gather targeted evidence."
        if self.state.last_validation_failures:
            failing_checks = ", ".join(self.state.last_validation_failures)
            validation_focus = self.extract_validation_focus()
            if validation_focus:
                return f"Fix failing checks: {failing_checks}. Focus on this exact validation issue: {validation_focus}"
            return f"Fix failing checks: {failing_checks}."
        if self.state.last_test_exit_code not in {None, 0}:
            validation_focus = self.extract_validation_focus()
            if validation_focus:
                return f"Use the latest test evidence to make the smallest repair. Focus on this exact validation issue: {validation_focus}"
            return "Use the latest test evidence to make the smallest repair. Do not rerun tests until you have changed the artifact or gathered targeted evidence."
        if self.state.invalid_response_streak > 0:
            return "Return one valid JSON action object."
        if self.state.dirty_since_test and self.has_validation_target():
            return "Run validation before stopping."
        if self.state.primary_artifact_path and not Path(self.state.primary_artifact_path).exists():
            return f"Create the primary artifact at {self.state.primary_artifact_path}."
        return "Continue with the smallest evidence-backed next step."

    def refresh_iteration_memory(self) -> None:
        self.update_requirement_coverage_state()
        self.state.requirements_summary = self.summarize_requirements()
        self.state.implementation_summary = self.summarize_implementation_state()
        self.state.failure_class = self.classify_failure_state()
        self.update_repair_strategy_state()
        self.state.next_repair_objective = self.compute_next_repair_objective()

    def has_external_test_target(self) -> bool:
        return bool(self.config.test_command) or self.public_test_runner_path() is not None

    @staticmethod
    def _split_spec(text: str, chunk_size: int) -> list[str]:
        """Split text at line boundaries, each chunk ≤ chunk_size chars."""
        if chunk_size <= 0:
            return [text]
        lines = text.splitlines(keepends=True)
        chunks: list[str] = []
        current = ""
        for line in lines:
            if current and len(current) + len(line) > chunk_size:
                chunks.append(current)
                current = line
            else:
                current += line
        if current:
            chunks.append(current)
        return chunks if chunks else [text]

    def _generate_chunk_id(self, page_index: int, page_content: str) -> str:
        """Generate a stable chunk ID based on page index and content hash."""
        content_hash = self.hash_text(page_content)
        return f"spec-page-{page_index}-{content_hash[:8]}"

    def _process_spec_chunk_extraction(self, chunk_id: str, chunk_content: str) -> bool:
        """
        Attempt extraction on current spec chunk, mark completion status.
        Returns True if extraction was completed and marked, False otherwise.
        """
        if not self.requirement_extractor:
            return False

        if chunk_id in self.state.chunks_with_pending_extraction:
            try:
                result = self.requirement_extractor.extract_from_text(chunk_content, chunk_id=chunk_id)
                report = result.report
                self.state.chunk_extraction_status[chunk_id] = {
                    "chunk_id": report.chunk_id,
                    "attempted": report.attempted,
                    "completeness_marked": report.completeness_marked,
                    "merged_node_ids": report.merged_node_ids,
                    "ambiguity_ids": report.ambiguity_ids,
                    "duplicate_count": report.duplicate_count,
                }
                self.state.chunks_with_pending_extraction.discard(chunk_id)
                self.state.current_chunk_extraction_complete = report.completeness_marked
                return report.completeness_marked
            except Exception as e:
                self.logger.write("errors", f"Extraction failed for chunk {chunk_id}: {e}")
                self.state.chunks_with_pending_extraction.discard(chunk_id)
                self.state.current_chunk_extraction_complete = True
                return True

        extracted = chunk_id in self.state.chunk_extraction_status
        self.state.current_chunk_extraction_complete = extracted
        return extracted

    def build_prompt(self, task_description: str, orchestration_context: str = "") -> str:
        orchestration_block = ""
        if orchestration_context:
            orchestration_block = f"\n\nMulti-role orchestration context:\n{orchestration_context.strip()}\n"

        self.refresh_iteration_memory()
        improved_context_block = self.improved_orchestrator.get_context_for_model(max_chars=260)
        improved_context_block = f"\n\nStructured orchestration memory:\n{improved_context_block}\n"
        task_contract_block = self.render_task_contract()
        task_contract_block = f"\n\nExtracted task contract:\n{task_contract_block}\n" if task_contract_block else ""
        requirement_coverage_block = self.render_requirement_coverage()
        requirement_coverage_block = f"\n\nRequirement coverage:\n{requirement_coverage_block}\n" if requirement_coverage_block else ""
        repair_strategy_block = self.render_repair_strategy_block()
        repair_strategy_block = f"\n\nRepair strategy:\n{repair_strategy_block}\n" if repair_strategy_block else ""
        last_test_output = self.prompt_excerpt(self.state.last_test_output, max_chars=320)
        last_action_summary = self.prompt_excerpt(self.state.last_action_summary, max_chars=180)

        program_expectation_block = ""
        if self.task_requires_runnable_program():
            program_expectation_block = (
                "\nProgram expectation:\n"
                "- The task asks for a runnable program/script/CLI.\n"
                "- Produce an executable entrypoint or main flow, not only helper functions.\n"
                "- If you write Python code, include a main path that can actually run.\n"
                "- Treat angle-bracket placeholders from the spec (for example <input-file>) as notation, not literal Python identifiers or attribute names.\n"
            )
            cli_invocation = self.extract_cli_invocation()
            if cli_invocation:
                program_expectation_block += (
                    f"- Match this exact external CLI shape unless runtime validation says otherwise: {cli_invocation}\n"
                    "- Do not replace the declared subcommand or optional flags with a different CLI contract.\n"
                )
            if self.task_requests_json_stdout():
                program_expectation_block += (
                    "- When JSON output is required, emit exactly one JSON object on stdout and keep the shape stable across validation runs.\n"
                )

        primary_artifact_block = ""
        if self.state.primary_artifact_path:
            primary_artifact_block = f"\nPrimary artifact target:\n{self.state.primary_artifact_path}\n"

        write_path_guidance_block = ""
        if self.state.primary_artifact_path:
            write_path_guidance_block = (
                "\nWrite target guidance:\n"
                f"- When using WRITE_FILE, set path to {self.state.primary_artifact_path}.\n"
                "- Do not leave WRITE_FILE.path empty.\n"
            )

        repair_guidance_block = ""
        if self.state.primary_artifact_path and self.state.last_test_output != "No tests run yet.":
            repair_guidance_block = (
                "\nRepair guidance:\n"
                f"- Repair {self.state.primary_artifact_path} in place instead of restarting from scratch.\n"
                "- Use the latest validation output to make the smallest correction that addresses the failure.\n"
                "- Do not rewrite the requirements or prompt text into the program file.\n"
            )
            if self.state.last_validation_failures:
                failing_checks = ", ".join(self.state.last_validation_failures)
                repair_guidance_block += (
                    f"- The current failing checks are: {failing_checks}.\n"
                    "- Do not choose RUN_TESTS again until you have changed the artifact or gathered new targeted evidence with RUN_COMMAND.\n"
                )

        convergence_guidance_block = ""
        if self.state.primary_artifact_path and (self.state.last_validation_passes or self.state.last_validation_failures):
            passed = ", ".join(self.state.last_validation_passes) if self.state.last_validation_passes else "none"
            failed = ", ".join(self.state.last_validation_failures) if self.state.last_validation_failures else "none"
            convergence_guidance_block = (
                "\nValidation feedback:\n"
                f"- Passed checks: {passed}.\n"
                f"- Failing checks: {failed}.\n"
                "- Keep the parts that already pass. Change only the failing parts unless a minimal patch is impossible.\n"
            )
            if self.state.rewrite_stagnation_count >= 2:
                convergence_guidance_block += (
                    f"- Convergence warning: {self.state.primary_artifact_path} has been rewritten {self.state.rewrite_stagnation_count} times without full validation success.\n"
                    "- Do not restart from scratch. Patch the current file structure.\n"
                )

        json_recovery_guidance_block = ""
        if self.state.invalid_response_streak > 0:
            json_recovery_guidance_block = (
                "\nResponse format warning:\n"
                "- Your previous response was malformed JSON.\n"
                "- Return one raw JSON object only, with properly escaped newlines and quotes.\n"
                "- Do not use markdown fences.\n"
            )

        persistent_memory_block = (
            "\nPersistent iteration memory:\n"
            f"- Requirements summary: {self.state.requirements_summary}\n"
            f"- Artifact target: {self.state.primary_artifact_path or '(unset)'}\n"
            f"- Implementation summary: {self.state.implementation_summary}\n"
            f"- Failure class: {self.state.failure_class}\n"
            f"- Next repair objective: {self.state.next_repair_objective}\n"
        )

        artifact_excerpt_block = ""
        if self.state.primary_artifact_path and self.state.last_validation_failures:
            artifact_excerpt = self.current_artifact_excerpt()
            if artifact_excerpt:
                artifact_excerpt_block = (
                    "\nCurrent artifact excerpt:\n"
                    f"{artifact_excerpt}\n"
                    "- Patch this artifact directly instead of rewriting from the task text.\n"
                )

        validation_guidance_block = ""
        runner = self.public_test_runner_path()
        if runner and self.state.primary_artifact_path:
            compiler_command = self.declared_validation_command()
            runner_command = f"python3 {shlex.quote(str(runner))} --compiler {shlex.quote(compiler_command)} --suite public --failures 10"
            debug_command = self.build_debug_cli_command("sample_input.txt")
            category_command = f"python3 {shlex.quote(str(runner))} --compiler {shlex.quote(compiler_command)} --category level_0N_XXXX --failures 10"
            expected_dir = runner.parents[1] / "expected_outputs"
            validation_guidance_block = (
                "\nValidation workflow:\n"
                f"- Write your code to {self.state.primary_artifact_path}. The runner executes this declared compiler command: {compiler_command}.\n"
                f"- Use RUN_COMMAND like \"{debug_command}\" to debug individual cases.\n"
                f"- Use RUN_TESTS to run the full public test suite with \"{runner_command}\"\n"
                f"- Debug repeat tests first by running: {category_command}\n"
                f"- If tests report \"stale expected output\", touch the expected files under {expected_dir}.\n"
            )
        if self.task_requires_runnable_program() and self.task_is_complex():
            validation_guidance_block += (
                "- For a complex runnable program, do not jump straight to the full suite from the first large write.\n"
                "- Earn the full-suite run by passing a quick syntax/build checkpoint first, then gather one narrow debug signal if needed.\n"
            )

        comprehension_phase_block = ""
        if self.state.iteration == 0:
            comprehension_phase_block = textwrap.dedent("""\
                ========================================================
                Initial specification-comprehension guidance
                ========================================================
                - Do not waste the first turn on an echo/print command that merely restates the task.
                - If you already have enough evidence from the task, your first action may be WRITE_FILE.
                - If evidence is missing, use RUN_COMMAND only to gather concrete evidence from the repository or runtime.
                - For complex tasks, the first implementation must match the declared CLI/output contract and must not be a placeholder template.
                ========================================================
                """)

        test_target_label = "yes" if self.has_test_target() else "no"
        external_log = self.state.external_log_path or "(none)"

        template = textwrap.dedent("""\
            You are an autonomous coding agent working in a local repository.

            Task description:
            {task_description}

            Current iteration: {iteration}

            Test target available: {test_target}

            Last test output:
            {last_test_output}

            Last action result:
            {last_action_summary}

            {persistent_memory_block}
            {artifact_excerpt_block}

            Runtime-managed external log file:
            {external_log}
            {primary_artifact_block}
            {write_path_guidance_block}
            {repair_guidance_block}
            {convergence_guidance_block}
            {json_recovery_guidance_block}
            {program_expectation_block}
            {task_contract_block}
            {requirement_coverage_block}
            {repair_strategy_block}
            {validation_guidance_block}
            {comprehension_phase_block}
            {orchestration_block}
            {improved_context_block}

            Return exactly one JSON object with this schema:
            {{
              "action": "WRITE_FILE" | "RUN_TESTS" | "RUN_COMMAND" | "STOP",
              "reason": "short explanation",
              "path": "relative/path/or-empty-string",
              "content": "full file contents or empty string",
              "command": "shell command or empty string"
            }}

            Internal decision contract:
            - Role: act as a deterministic repository worker, not a chat assistant.
            - Objective: choose the smallest action that increases verified progress.
            - If evidence is insufficient, prefer RUN_COMMAND to gather evidence over guessing.
            - If the task context is condensed, trust the structured orchestration memory and do not reconstruct missing details.
            - If uncertain, state that uncertainty briefly in reason and choose an evidence-gathering action.
            - Never invent file contents, tests, or requirements that were not observed.

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
            - Do not copy the task description, requirements markdown, or prompt/rules text into output files.
            - If multi-role orchestration context is present, use it internally and still return only one final JSON action object.
        """)

        prompt_without_task = template.format(
            task_description="",
            iteration=self.state.iteration,
            test_target=test_target_label,
            last_test_output=last_test_output,
            last_action_summary=last_action_summary,
            persistent_memory_block=persistent_memory_block,
            artifact_excerpt_block=artifact_excerpt_block,
            external_log=external_log,
            primary_artifact_block=primary_artifact_block,
            write_path_guidance_block=write_path_guidance_block,
            repair_guidance_block=repair_guidance_block,
            convergence_guidance_block=convergence_guidance_block,
            json_recovery_guidance_block=json_recovery_guidance_block,
            program_expectation_block=program_expectation_block,
            validation_guidance_block=validation_guidance_block,
            task_contract_block=task_contract_block,
            requirement_coverage_block=requirement_coverage_block,
            repair_strategy_block=repair_strategy_block,
            comprehension_phase_block=comprehension_phase_block,
            orchestration_block=orchestration_block,
            improved_context_block=improved_context_block,
        ).strip()

        if len(prompt_without_task) > self.config.max_prompt_size - 400:
            improved_context_block = ""
            prompt_without_task = template.format(
                task_description="",
                iteration=self.state.iteration,
                test_target=test_target_label,
                last_test_output=last_test_output,
                last_action_summary=last_action_summary,
                persistent_memory_block=persistent_memory_block,
                artifact_excerpt_block=artifact_excerpt_block,
                external_log=external_log,
                primary_artifact_block=primary_artifact_block,
                write_path_guidance_block=write_path_guidance_block,
                repair_guidance_block=repair_guidance_block,
                convergence_guidance_block=convergence_guidance_block,
                json_recovery_guidance_block=json_recovery_guidance_block,
                program_expectation_block=program_expectation_block,
                validation_guidance_block=validation_guidance_block,
                task_contract_block=task_contract_block,
                requirement_coverage_block=requirement_coverage_block,
                repair_strategy_block=repair_strategy_block,
                comprehension_phase_block=comprehension_phase_block,
                orchestration_block=orchestration_block,
                improved_context_block=improved_context_block,
            ).strip()

        available = self.config.max_prompt_size - len(prompt_without_task)

        if not self.state.spec_pages and available < len(task_description):
            self.state.spec_pages = self._split_spec(task_description, available)
            self.state.current_spec_page = 0
            for idx, page in enumerate(self.state.spec_pages):
                chunk_id = self._generate_chunk_id(idx, page)
                self.state.chunks_with_pending_extraction.add(chunk_id)

        if self.state.spec_pages:
            page_idx = self.state.current_spec_page
            total_pages = len(self.state.spec_pages)
            if page_idx < total_pages:
                page_content = self.state.spec_pages[page_idx]
                chunk_id = self._generate_chunk_id(page_idx, page_content)
                self._process_spec_chunk_extraction(chunk_id, page_content)
                task_description = f"(page {page_idx + 1}/{total_pages})\n{page_content}"
                if page_idx < total_pages - 1:
                    remaining = total_pages - page_idx - 1
                    s = "s" if remaining > 1 else ""
                    task_description += f"\n\n[{remaining} more page{s} remain. Continue working with the next page.]"

        prompt = template.format(
            task_description=task_description,
            iteration=self.state.iteration,
            test_target=test_target_label,
            last_test_output=last_test_output,
            last_action_summary=last_action_summary,
            persistent_memory_block=persistent_memory_block,
            artifact_excerpt_block=artifact_excerpt_block,
            external_log=external_log,
            primary_artifact_block=primary_artifact_block,
            write_path_guidance_block=write_path_guidance_block,
            repair_guidance_block=repair_guidance_block,
            convergence_guidance_block=convergence_guidance_block,
            json_recovery_guidance_block=json_recovery_guidance_block,
            program_expectation_block=program_expectation_block,
            validation_guidance_block=validation_guidance_block,
            task_contract_block=task_contract_block,
            requirement_coverage_block=requirement_coverage_block,
            repair_strategy_block=repair_strategy_block,
            comprehension_phase_block=comprehension_phase_block,
            orchestration_block=orchestration_block,
            improved_context_block=improved_context_block,
        ).strip()

        if len(prompt) > self.config.max_prompt_size:
            remaining = max(120, self.config.max_prompt_size - len(prompt_without_task))
            task_description = self.prompt_excerpt(task_description, max_chars=remaining)
            prompt = template.format(
                task_description=task_description,
                iteration=self.state.iteration,
                test_target=test_target_label,
                last_test_output=last_test_output,
                last_action_summary=last_action_summary,
                persistent_memory_block=persistent_memory_block,
                artifact_excerpt_block=artifact_excerpt_block,
                external_log=external_log,
                primary_artifact_block=primary_artifact_block,
                write_path_guidance_block=write_path_guidance_block,
                repair_guidance_block=repair_guidance_block,
                convergence_guidance_block=convergence_guidance_block,
                json_recovery_guidance_block=json_recovery_guidance_block,
                program_expectation_block=program_expectation_block,
                validation_guidance_block=validation_guidance_block,
                task_contract_block=task_contract_block,
                requirement_coverage_block=requirement_coverage_block,
                repair_strategy_block=repair_strategy_block,
                comprehension_phase_block=comprehension_phase_block,
                orchestration_block=orchestration_block,
                improved_context_block=improved_context_block,
            ).strip()

        return prompt

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

    def recover_action_from_invalid_response(self, invalid_response: str) -> dict[str, str] | None:
        locally_recovered = self.salvage_action_from_text(invalid_response)
        if locally_recovered is not None:
            self.logger.write("decisions", f"MODEL_RESPONSE_LOCAL_RECOVERY {locally_recovered['action']}: {locally_recovered['reason']}")
            return locally_recovered

        recovery_prompt = textwrap.dedent(f"""\
            Reformat the following invalid model response into one valid JSON object only.

            Required schema:
            {{
              "action": "WRITE_FILE" | "RUN_TESTS" | "RUN_COMMAND" | "STOP",
              "reason": "short explanation",
              "path": "relative/path/or-empty-string",
              "content": "full file contents or empty string",
              "command": "shell command or empty string"
            }}

            Rules:
            - Return raw JSON only.
            - No markdown fences.
            - Escape all embedded newlines correctly.
            - Preserve the original intended action as closely as possible.

            Invalid response:
            {invalid_response}
        """)
        recovered_response = self.call_model(recovery_prompt)
        self.logger.write("decisions", f"MODEL_RESPONSE_RECOVERY {recovered_response}")
        return self.parse_action(recovered_response)

    def salvage_action_from_text(self, response: str) -> dict[str, str] | None:
        normalized = self.normalize_model_response(response)
        action_match = re.search(r'"action"\s*:\s*"([A-Z_]+)"', normalized)
        reason_match = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', normalized, flags=re.DOTALL)
        path_match = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', normalized, flags=re.DOTALL)
        command_match = re.search(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', normalized, flags=re.DOTALL)
        content_match = re.search(r'"content"\s*:\s*"(.*)"\s*(?:,\s*"command"\s*:|\}\s*$)', normalized, flags=re.DOTALL)
        if not action_match or not reason_match:
            return None

        action = action_match.group(1).strip().upper()
        if action not in {"WRITE_FILE", "RUN_TESTS", "RUN_COMMAND", "STOP"}:
            return None

        def decode_fragment(value: str) -> str:
            try:
                return bytes(value, "utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                return value

        return {
            "action": action,
            "reason": decode_fragment(reason_match.group(1)).strip(),
            "path": decode_fragment(path_match.group(1)).strip() if path_match else "",
            "content": decode_fragment(content_match.group(1)) if content_match else "",
            "command": decode_fragment(command_match.group(1)).strip() if command_match else "",
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

        if previous_content is not None and self.is_trivial_rewrite(previous_content, content):
            trivial_fingerprint = f"WRITE_FILE_TRIVIAL:{relative_path}:{hashlib.sha256(self.normalize_trivial_content(content).encode('utf-8')).hexdigest()}"
            self.logger.write("decisions", f"WRITE_FILE_TRIVIAL {relative_path}")
            return ActionOutcome(
                summary=f"WRITE_FILE {relative_path} -> trivial rewrite (no meaningful change)",
                progress=False,
                fingerprint=trivial_fingerprint,
            )

        if self.is_prompt_regurgitation(content):
            self.logger.write("decisions", f"WRITE_FILE_REJECTED_PROMPT_LEAK {relative_path}")
            return ActionOutcome(
                summary=f"WRITE_FILE {relative_path} -> blocked (content appears to copy prompt/spec/runtime text)",
                progress=False,
                fingerprint=f"WRITE_FILE_BLOCKED:prompt-leak:{relative_path}",
            )

        if self.is_low_substance_initial_write(relative_path, content):
            self.logger.write("decisions", f"WRITE_FILE_REJECTED_LOW_SUBSTANCE {relative_path}")
            return ActionOutcome(
                summary=(
                    f"WRITE_FILE {relative_path} -> blocked (initial implementation is too shallow for the task). "
                    "Replace placeholders and implement the declared CLI/output contract before writing again."
                ),
                progress=False,
                fingerprint=f"WRITE_FILE_BLOCKED:low-substance:{relative_path}",
            )

        if (
            previous_content is not None
            and relative_path == self.state.primary_artifact_path
            and self.state.best_validation_score > 0
            and self.state.last_validation_failures
            and self.is_destructive_rewrite(previous_content, content)
        ):
            failing_checks = ", ".join(self.state.last_validation_failures)
            return ActionOutcome(
                summary=(
                    f"WRITE_FILE {relative_path} -> blocked (destructive rewrite after partial validation success). "
                    f"Preserve passing behavior and repair only: {failing_checks}."
                ),
                progress=False,
                fingerprint=f"WRITE_FILE_BLOCKED:destructive:{relative_path}",
            )

        path.write_text(content, encoding="utf-8")
        self.logger.write("decisions", f"WRITE_FILE {relative_path}")
        artifact_hash = self.hash_text(content)
        return ActionOutcome(
            summary=f"WRITE_FILE {relative_path} -> changed ({len(content)} chars).",
            progress=True,
            fingerprint=fingerprint,
            path=relative_path,
            artifact_hash=artifact_hash,
        )

    def is_low_substance_initial_write(self, relative_path: str, content: str) -> bool:
        if self.state.iteration > 0:
            return False
        if self.state.completed_actions > 0:
            return False
        if not self.task_is_complex():
            return False
        if Path(relative_path).suffix.lower() != ".py":
            return False

        lowered = content.lower()
        placeholder_markers = ("placeholder", "add your", "todo", "not implemented", "pass\n", "pass\r\n")
        if any(marker in lowered for marker in placeholder_markers):
            return True
        if self.task_requires_runnable_program() and self.extract_cli_invocation():
            has_entry = "__main__" in content or "argparse" in content or "sys.argv" in content
            if not has_entry:
                return True
        if self.extract_cli_invocation() and re.search(r"<[A-Za-z0-9_-]+>", content):
            return True
        if self.task_requests_json_stdout() and "json" not in lowered:
            return True
        if len(content.strip()) < 220:
            return True
        return False

    def current_primary_artifact_hash(self) -> str:
        path = self.state.primary_artifact_path
        if not path:
            return ""
        artifact = Path(path)
        if not artifact.exists():
            return ""
        return self.hash_text(artifact.read_text(encoding="utf-8"))

    def should_defer_full_test_suite(self) -> bool:
        if not self.task_requires_runnable_program() or not self.task_is_complex():
            return False
        artifact_hash = self.current_primary_artifact_hash()
        if not artifact_hash:
            return False
        if self.state.last_quick_validation_artifact_hash != artifact_hash:
            return True
        return not self.state.quick_validation_ready_for_full_test

    def quick_validation_outcome(self, outcome: ActionOutcome) -> ActionOutcome:
        artifact_hash = self.current_primary_artifact_hash()
        self.state.last_quick_validation_artifact_hash = artifact_hash
        self.state.quick_validation_ready_for_full_test = True
        self.state.validation_stage = "quick_validated"
        passes = list(dict.fromkeys([*outcome.validation_passes, "quick_validation"]))
        return ActionOutcome(
            summary=(
                f"{outcome.summary}\n"
                "Quick validation passed. Continue building or gather targeted debug evidence before running the full suite."
            ),
            progress=True,
            fingerprint=f"RUN_TESTS:quick-check:{artifact_hash[:12] or 'none'}",
            test_exit_code=0,
            validation_passes=passes,
        )

    def run_builtin_validation(self) -> ActionOutcome:
        path = self.state.primary_artifact_path
        if not path:
            return ActionOutcome(summary="No primary artifact available for built-in validation.", progress=False, fingerprint="VALIDATE:none")

        artifact = Path(path)
        if not artifact.exists():
            return ActionOutcome(summary=f"Primary artifact {path} does not exist yet.", progress=False, fingerprint=f"VALIDATE:missing:{path}")

        suffix = artifact.suffix.lower()
        command_timeout = 15 if self.task_requires_runnable_program() else 30
        if suffix == ".py":
            content = artifact.read_text(encoding="utf-8")
            if self.task_requires_runnable_program() and "__main__" not in content and "main(" not in content:
                return ActionOutcome(
                    summary=f"Python artifact {path} is missing a runnable entrypoint.",
                    progress=False,
                    fingerprint=f"VALIDATE:python:missing-entrypoint:{path}",
                    validation_failures=["syntax"],
                )
            if self.task_looks_like_file_processing_cli():
                sample_input = Path(".agent_validation_input.txt")
                sample_input.write_text("Hello world! Hello agent.\nThis is a sample file.\n", encoding="utf-8")
                cli_command = self.build_sample_cli_command(path, sample_input)
                command = f"python3 -m py_compile {shlex.quote(path)} && {cli_command}"
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
                validation_passes=["semantic_counts"],
            )
        else:
            return ActionOutcome(summary=f"No built-in validation available for {path}.", progress=False, fingerprint=f"VALIDATE:unsupported:{path}")

        result = self.run_command(command, category="test_runs", timeout=command_timeout)
        output = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()
        if suffix == ".py" and self.task_looks_like_file_processing_cli() and result.returncode == 0:
            validation_passes = ["syntax"]
            try:
                parsed_output = json.loads(result.stdout)
            except json.JSONDecodeError:
                return ActionOutcome(
                    summary=f"{output}\nValidation expected exactly one JSON object on stdout.",
                    progress=False,
                    fingerprint=f"VALIDATE:python:json-output:{path}",
                    test_exit_code=1,
                    validation_passes=validation_passes,
                    validation_failures=["stdout_json"],
                )
            if not isinstance(parsed_output, dict):
                return ActionOutcome(
                    summary=f"{output}\nValidation expected stdout JSON to be an object.",
                    progress=False,
                    fingerprint=f"VALIDATE:python:json-output:{path}",
                    test_exit_code=1,
                    validation_passes=validation_passes,
                    validation_failures=["stdout_json"],
                )
            validation_passes.append("stdout_json")
            return ActionOutcome(
                summary=output,
                progress=True,
                fingerprint=f"VALIDATE:python:sample-io:{path}",
                test_exit_code=result.returncode,
                validation_passes=validation_passes,
            )
        if suffix == ".py" and result.returncode != 0:
            return ActionOutcome(
                summary=output,
                progress=False,
                fingerprint=self.fingerprint_for_tests(command),
                test_exit_code=result.returncode,
                validation_failures=self.infer_python_validation_failures(output),
            )
        fingerprint = self.fingerprint_for_tests(command)
        if suffix == ".py" and self.task_requires_runnable_program():
            fingerprint = f"VALIDATE:python:syntax:{path}"
        if suffix == ".py" and self.task_looks_like_file_processing_cli() and result.returncode == 0:
            fingerprint = f"VALIDATE:python:sample-io:{path}"
        validation_passes = ["syntax"] if suffix == ".py" and result.returncode == 0 else []
        validation_failures = ["syntax"] if suffix == ".py" and result.returncode != 0 else []
        return ActionOutcome(summary=output, progress=result.returncode == 0, fingerprint=fingerprint, test_exit_code=result.returncode, validation_passes=validation_passes, validation_failures=validation_failures)

    def run_command(self, command: str, *, category: str = "commands", timeout: int = 950) -> subprocess.CompletedProcess[str]:
        if not command:
            raise ValueError("Command cannot be empty")

        command = self.normalize_direct_python_command(command)

        self.logger.write(category, f"COMMAND {command}")
        try:
            result = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            result = subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout=str(stdout),
                stderr=f"Command timed out after {timeout}s. Program may be waiting for input.\n{stderr}",
            )
        output = f"exit={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}".strip()
        self.logger.write(category, output)
        return result

    def refresh_expected_outputs(self) -> None:
        runner = self.public_test_runner_path()
        expected_dir = runner.parents[1] / "expected_outputs" if runner else Path("secret_spec/expected_outputs")
        if expected_dir.exists():
            for f in expected_dir.rglob("*.expected.json"):
                f.touch()

    def run_public_tests(self) -> ActionOutcome:
        if self.should_defer_full_test_suite():
            quick_outcome = self.run_builtin_validation()
            if not quick_outcome.progress:
                self.state.validation_stage = "quick_failed"
                return quick_outcome
            return self.quick_validation_outcome(quick_outcome)

        if self.config.test_command:
            result = self.run_command(self.config.test_command, category="test_runs")
            output = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()
            self.state.validation_stage = "full_suite"
            return ActionOutcome(
                summary=output,
                progress=True,
                fingerprint=self.fingerprint_for_tests(self.config.test_command),
                test_exit_code=result.returncode,
            )

        runner = self.public_test_runner_path()
        if runner is None:
            builtin_outcome = self.run_builtin_validation()
            if builtin_outcome.fingerprint != "VALIDATE:none" and not builtin_outcome.fingerprint.startswith("VALIDATE:unsupported"):
                self.logger.write("test_runs", builtin_outcome.summary)
                self.state.validation_stage = "builtin"
                return builtin_outcome

            message = "No test command configured, public test runner not available, and no built-in validation applied yet."
            self.logger.write("test_runs", message)
            return ActionOutcome(summary=message, progress=False, fingerprint="RUN_TESTS:unavailable")

        self.refresh_expected_outputs()
        command = (
            f"python3 {shlex.quote(str(runner))} "
            f"--compiler {shlex.quote(self.config.solution_command)} --suite public --failures 10"
        )
        result = self.run_command(command, category="test_runs")
        output = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}".strip()
        validation_passes, validation_failures = self.extract_validation_signals(output)
        self.state.validation_stage = "full_suite"
        return ActionOutcome(
            summary=output,
            progress=result.returncode == 0,
            fingerprint=self.fingerprint_for_tests(command),
            test_exit_code=result.returncode,
            validation_passes=validation_passes,
            validation_failures=validation_failures,
        )

    def extract_validation_signals(self, output: str) -> tuple[list[str], list[str]]:
        lowered = output.lower()
        passes: list[str] = []
        failures: list[str] = []

        if "150/150 passed" in lowered or "overall [################################]" in lowered:
            passes.append("public_suite")
        if "stdout is not exactly one json document" in lowered or "expected exactly one json object" in lowered:
            failures.append("stdout_json")
        if "usage:" in lowered or "the following arguments are required" in lowered or "invalid choice" in lowered:
            failures.append("cli_contract")
        if "syntaxerror" in lowered:
            failures.append("syntax")
        if "traceback" in lowered or "exception" in lowered:
            failures.append("runtime_error")
        if "stale expected output" in lowered:
            failures.append("stale_expected_output")
        first_failure = re.search(r"-\s+(pub_[^:]+):", output)
        if first_failure:
            failures.append(f"first_failure:{first_failure.group(1)}")
        return list(dict.fromkeys(passes)), list(dict.fromkeys(failures))

    def infer_python_validation_failures(self, output: str) -> list[str]:
        lowered = output.lower()
        failures: list[str] = []
        if "syntaxerror" in output:
            failures.append("syntax")
        if "traceback" in lowered:
            failures.append("runtime_error")
        if "nameerror" in lowered or "attributeerror" in lowered or "typeerror" in lowered:
            failures.append("runtime_error")
        if "jsondecodeerror" in lowered or "expecting value" in lowered:
            failures.append("input_parsing")
        if "the following arguments are required" in lowered or "invalid choice" in lowered:
            failures.append("cli_contract")
        return list(dict.fromkeys(failures or ["syntax"]))

    def should_block_repeated_action(self, fingerprint: str, progress: bool) -> bool:
        if fingerprint.startswith("WRITE_FILE_BLOCKED:") or fingerprint.startswith("ACTION_REJECTED:"):
            return False
        if fingerprint == self.state.last_action_fingerprint:
            if not progress and not self.state.last_action_progress:
                return True
            self.state.repeated_action_count += 1
            return self.state.repeated_action_count >= 3
        self.state.repeated_action_count = 0
        return False

    def update_state_from_outcome(self, outcome: ActionOutcome) -> None:
        self.state.last_action_summary = outcome.summary
        self.state.last_action_fingerprint = outcome.fingerprint
        self.state.last_action_progress = outcome.progress
        if outcome.terminal_kind:
            self.state.last_terminal_kind = outcome.terminal_kind
        if outcome.test_exit_code is not None:
            self.state.last_test_fingerprint = outcome.fingerprint
            self.state.last_validation_passes = list(outcome.validation_passes)
            self.state.last_validation_failures = list(outcome.validation_failures)
            score = self.validation_score(outcome.validation_passes, outcome.validation_failures)
            if outcome.artifact_hash and score > self.state.best_validation_score:
                self.state.best_validation_score = score
                self.state.best_validation_artifact_hash = outcome.artifact_hash
                self.state.rewrite_stagnation_count = 0
            elif outcome.validation_failures and outcome.path == self.state.primary_artifact_path:
                self.state.rewrite_stagnation_count += 1

        if outcome.progress:
            self.state.completed_actions += 1
            self.state.consecutive_no_progress = 0
            self.mark_current_execution_step_complete(outcome.summary)
            if outcome.test_exit_code == 0 or not self.state.semantic_validation_failures:
                self.state.current_repair_strategy = None
                self.state.repair_strategy_summary = ""
        else:
            self.state.consecutive_no_progress += 1

        if outcome.test_exit_code == 0 and self.requirement_graph is not None:
            for requirement_id, satisfied in self.state.requirement_satisfaction_map.items():
                if not satisfied:
                    continue
                node = self.requirement_graph.nodes.get(requirement_id)
                if node is not None and node.status in {RequirementStatus.IMPLEMENTED, RequirementStatus.PLANNED}:
                    node.status = RequirementStatus.VALIDATED

        if outcome.path and self.state.primary_artifact_path and outcome.path == self.state.primary_artifact_path and outcome.progress:
            self.state.rewrite_count_for_primary_artifact += 1
            if outcome.artifact_hash:
                self.state.current_artifact_hash = outcome.artifact_hash
                self.state.quick_validation_ready_for_full_test = False
                self.state.validation_stage = "dirty_after_write"

        self.refresh_iteration_memory()

    def evaluate_primary_artifact_completion(self) -> ActionOutcome | None:
        path = self.state.primary_artifact_path
        if not path or path == self.state.external_log_path:
            return None

        if self.state.spec_pages and self.state.current_spec_page < len(self.state.spec_pages) - 1:
            return None

        if self.state.chunks_with_pending_extraction and not self.state.current_chunk_extraction_complete:
            return None

        if self.state.semantic_validation_failures:
            return None

        artifact = Path(path)
        if not artifact.exists():
            return None

        content = artifact.read_text(encoding="utf-8")
        artifact_hash = self.hash_text(content)
        if self.has_test_target() and self.state.dirty_since_test:
            return None

        if artifact_hash == self.state.last_completed_artifact_hash:
            return self.stop_outcome(f"artifact {path} already validated", terminal_kind="success")

        if artifact.suffix.lower() in {".txt", ".md"}:
            if len(content.strip()) < 20 or "[your" in content.lower() or "placeholder" in content.lower():
                return None

            self.state.last_completed_artifact_hash = artifact_hash
            return self.stop_outcome(f"artifact {path} satisfies runtime completion checks", terminal_kind="success")

        # Tightened completion for code files: require passing tests or 80%+ at iteration >= 3
        if artifact.suffix.lower() in {".py", ".c", ".cpp"}:
            if self.state.last_test_fingerprint.startswith("VALIDATE:python:syntax:"):
                return None
            if self.state.last_test_exit_code == 0:
                self.state.last_completed_artifact_hash = artifact_hash
                return self.stop_outcome(f"artifact {path} passed tests", terminal_kind="success")
            elif self.state.last_test_exit_code is not None and self.state.last_test_exit_code != 0:
                if self.state.iteration >= 3:
                    match = re.search(r'(\d+)/(\d+) passed', self.state.last_test_output or "")
                    if match:
                        passed = int(match.group(1))
                        total = int(match.group(2))
                        if total > 0 and (passed / total >= 0.8):
                            self.state.last_completed_artifact_hash = artifact_hash
                            return self.stop_outcome(f"artifact {path} has 80%+ tests passing ({passed}/{total})", terminal_kind="success")

        return None

    def print_progress(self, summary: str) -> None:
        print(f"[{self.state.iteration + 1}/{self.config.max_iterations}] {summary}")
        self.append_external_log(summary)

    def stop_outcome(self, reason: str, *, terminal_kind: str = "stall") -> ActionOutcome:
        return ActionOutcome(summary=reason, progress=False, fingerprint=f"STOP:{reason}", stop=True, terminal_kind=terminal_kind)

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
        self.improved_orchestrator.state.iteration_count = self.state.iteration
        next_phase = self.improved_orchestrator.decide_next_phase()
        self.improved_orchestrator.state.update_phase(next_phase)
        should_stop, stop_reason, stop_message = self.improved_orchestrator.should_stop()
        if should_stop:
            self.improved_orchestrator.state.should_stop = True
            self.improved_orchestrator.state.stop_reason = stop_reason
            self.improved_orchestrator.state.stop_evidence = stop_message
            self.state.last_action_summary = stop_message
            self.state.last_terminal_kind = "success" if stop_reason == StopReason.SUCCESS else "stall"
            return False

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
        try:
            action = self.parse_action(response)
        except ValueError as exc:
            self.state.invalid_response_streak += 1
            self.logger.write("errors", f"Invalid model response JSON: {self.normalize_model_response(response)} -> {exc}")
            try:
                action = self.recover_action_from_invalid_response(response)
                self.state.invalid_response_streak = 0
                self.logger.write("decisions", f"ACTION_RECOVERED {action['action']}: {action['reason']}")
            except ValueError as recovery_exc:
                outcome = ActionOutcome(
                    summary=f"Invalid model response: {recovery_exc}",
                    progress=False,
                    fingerprint="MODEL_RESPONSE_INVALID",
                )
                self.update_state_from_outcome(outcome)
                self.print_progress(outcome.summary)
                if self.state.consecutive_no_progress >= 3:
                    raise ValueError("Stopping after 3 consecutive no-progress iterations")
                return True
        else:
            self.state.invalid_response_streak = 0
        self.logger.write("decisions", f"ACTION {action['action']}: {action['reason']}")
        self.append_external_log(f"ACTION {action['action']}: {action['reason']}")

        try:
            if action["action"] == "WRITE_FILE":
                action_path = self.resolved_write_path(action["path"])
                outcome = self.write_file(action_path, action["content"])
                if outcome.progress and outcome.path and self.supports_builtin_validation_path(outcome.path):
                    guessed_artifact_missing = bool(self.state.primary_artifact_path) and not Path(self.state.primary_artifact_path).exists()
                    if not self.state.primary_artifact_path or guessed_artifact_missing:
                        self.state.primary_artifact_path = outcome.path
                        if outcome.artifact_hash:
                            self.state.current_artifact_hash = outcome.artifact_hash
                if self.should_block_repeated_action(outcome.fingerprint, outcome.progress):
                    outcome = self.stop_outcome(f"stalled: repeated equivalent write for {action['path']}")
                elif outcome.progress:
                    self.state.material_changes += 1
                    self.state.dirty_since_test = True
                    if not self.has_external_test_target() and self.state.primary_artifact_path and self.can_run_builtin_validation():
                        test_outcome = self.run_builtin_validation()
                        self.state.last_test_output = test_outcome.summary
                        self.state.last_test_exit_code = test_outcome.test_exit_code
                        self.state.last_test_fingerprint = test_outcome.fingerprint
                        self.state.dirty_since_test = test_outcome.test_exit_code not in {0, None}
                        outcome = ActionOutcome(
                            summary=f"{outcome.summary}\nAUTO_VALIDATION: {test_outcome.summary}",
                            progress=outcome.progress or test_outcome.progress,
                            fingerprint=test_outcome.fingerprint,
                            test_exit_code=test_outcome.test_exit_code,
                            path=outcome.path,
                            artifact_hash=outcome.artifact_hash,
                        )
            elif action["action"] == "RUN_COMMAND":
                result = self.run_command(action["command"])
                summary = (
                    f"RUN_COMMAND `{action['command']}` -> exit={result.returncode}. "
                    f"STDOUT: {result.stdout.strip() or '(empty)'} STDERR: {result.stderr.strip() or '(empty)'}"
                )
                fingerprint = self.fingerprint_for_command(action["command"])
                progress = bool(result.stdout.strip()) or result.returncode == 0
                if self.should_block_repeated_action(fingerprint, progress):
                    outcome = self.stop_outcome(f"stalled: repeated equivalent command `{action['command']}`")
                else:
                    outcome = ActionOutcome(summary=summary, progress=progress, fingerprint=fingerprint)
            elif action["action"] == "RUN_TESTS":
                if (
                    (self.state.last_validation_failures or self.state.last_test_exit_code not in {None, 0})
                    and self.state.last_test_fingerprint
                    and self.state.last_action_fingerprint == self.state.last_test_fingerprint
                ):
                    failing_checks = ", ".join(self.state.last_validation_failures) if self.state.last_validation_failures else self.extract_validation_focus() or "the latest failing validation evidence"
                    raise ValueError(
                        f"Refusing to rerun validation before repairing failing checks: {failing_checks}"
                    )
                outcome = self.run_public_tests()
                if not self.has_validation_target() and outcome.fingerprint == "RUN_TESTS:unavailable":
                    outcome = self.stop_outcome("stalled: model requested tests, but no test target or built-in validation is available")
                elif self.should_block_repeated_action(outcome.fingerprint, outcome.progress):
                    outcome = self.stop_outcome("stalled: repeated equivalent test run")
                else:
                    self.state.last_test_output = outcome.summary
                    self.state.last_test_exit_code = outcome.test_exit_code
                    self.state.last_test_fingerprint = outcome.fingerprint
                    self.state.dirty_since_test = outcome.test_exit_code not in {0, None}
            elif action["action"] == "STOP":
                if self.state.completed_actions == 0:
                    raise ValueError("Refusing to STOP before executing any concrete action")

                if self.state.spec_pages and self.state.current_spec_page < len(self.state.spec_pages) - 1:
                    remaining = len(self.state.spec_pages) - self.state.current_spec_page - 1
                    raise ValueError(
                        f"Cannot STOP — spec page {self.state.current_spec_page + 1}/{len(self.state.spec_pages)} "
                        f"is current, {remaining} more page(s) remain."
                    )

                if not self.state.current_chunk_extraction_complete and self.state.chunks_with_pending_extraction:
                    raise ValueError(
                        f"Cannot STOP — extraction not complete for current chunk. Pending chunks: {len(self.state.chunks_with_pending_extraction)}"
                    )

                if self.state.semantic_validation_failures:
                    raise ValueError(
                        "Cannot STOP — semantic validation is incomplete: "
                        + " | ".join(self.state.semantic_validation_failures[:3])
                    )

                if self.state.dirty_since_test and self.has_validation_target():
                    raise ValueError("Refusing to STOP while changes have not been validated by a test run")

                if self.state.last_test_fingerprint.startswith("VALIDATE:python:syntax:") and self.task_requires_runnable_program():
                    raise ValueError("Refusing to STOP after syntax-only validation for a runnable Python program")

                outcome = self.stop_outcome(f"STOP accepted: {action['reason']}", terminal_kind="explicit_stop")
        except ValueError as exc:
            outcome = ActionOutcome(
                summary=f"Action rejected: {exc}",
                progress=False,
                fingerprint=f"ACTION_REJECTED:{action['action']}",
            )

        self.update_state_from_outcome(outcome)
        self.sync_improved_orchestrator(action["action"], outcome)
        self.print_progress(outcome.summary)

        completion_outcome = self.evaluate_primary_artifact_completion()
        if completion_outcome is not None:
            self.update_state_from_outcome(completion_outcome)
            self.print_progress(completion_outcome.summary)
            return False

        if self.state.spec_pages and outcome.progress:
            if self.state.current_chunk_extraction_complete:
                self.state.current_spec_page += 1
                if self.state.current_spec_page >= len(self.state.spec_pages):
                    self.state.spec_pages = []
                    self.state.current_spec_page = 0
            else:
                self.print_progress("Page advancement blocked: waiting for extraction completeness on current chunk")

        if self.state.consecutive_no_progress >= 3:
            raise ValueError("Stopping after 3 consecutive no-progress iterations")

        if outcome.stop:
            return False

        return True

    def run(self) -> int:
        try:
            task_description = self.prepare_task_description_for_prompt(self.load_task_description())
        except FileNotFoundError as exc:
            self.logger.write("errors", str(exc))
            print(exc)
            return 1

        if not self.improved_orchestrator.state.current_plan:
            graph = self.extract_requirement_graph()
            contract = self.task_contract_from_requirement_graph(graph)
            self.improved_orchestrator.state.current_plan = "Understand the task contract, implement the requested artifact, validate against the declared CLI/output behavior, then stop cleanly."
            execution_steps = self.initialize_execution_plan(graph)
            if not self.validate_execution_plan_coverage(graph, execution_steps):
                raise ValueError("Execution plan does not cover all extracted requirements")
            planned_steps = [step.description for step in execution_steps]
            if not contract.cli_invocation and not contract.input_hints and not contract.required_json_keys:
                planned_steps = [
                    "Understand the user task and constraints",
                    "Validate the result and repair if necessary",
                    "Stop with evidence",
                ]
                self.state.execution_steps = [
                    ExecutionStep(step_id="step-understand", description=planned_steps[0]),
                    ExecutionStep(step_id="step-validate-repair", description=planned_steps[1]),
                    ExecutionStep(step_id="step-stop", description=planned_steps[2]),
                ]
                self.state.current_execution_step_index = 0
                self.state.step_to_requirements = {step.step_id: [] for step in self.state.execution_steps}
                self.state.requirement_satisfaction_map = {}
            self.improved_orchestrator.state.planned_steps = planned_steps

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
                if self.state.last_terminal_kind == "stall":
                    print("Agent stalled.")
                    return 1
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
