#!/usr/bin/env python3
"""
compliance_checker.py — 42 Berlin x Needle Hackathon Requirement Compliance Checker

Usage:
    python3 compliance_checker.py /path/to/team/repository

Checks a team's solution repository against the submission requirements
defined in SUBMISSION.md, AGENT_SPEC.md, and JUDGING_CRITERIA.md.

Returns exit code 0 if all CRITICAL checks pass, 1 otherwise.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ─── ANSI colors ────────────────────────────────────────────────────────────

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ─── Check categories ───────────────────────────────────────────────────────

class CheckResult:
    def __init__(self, name, category, critical=False):
        self.name = name
        self.category = category
        self.critical = critical
        self.passed = None
        self.message = ""

    def ok(self, msg=""):
        self.passed = True
        self.message = msg
        return self

    def fail(self, msg):
        self.passed = False
        self.message = msg
        return self

    def warn(self, msg):
        self.passed = None
        self.message = msg
        return self

    def __repr__(self):
        if self.passed is True:
            icon = f"{GREEN}[PASS]{RESET}"
        elif self.passed is False:
            icon = f"{RED}[FAIL]{RESET}"
        else:
            icon = f"{YELLOW}[WARN]{RESET}"
        critical_tag = f" {BOLD}(CRITICAL){RESET}" if self.critical else ""
        return f"  {icon}{critical_tag} {self.name} — {self.message}"


# ─── Main checker ───────────────────────────────────────────────────────────

class ComplianceChecker:
    def __init__(self, repo_path: str):
        self.repo = Path(repo_path).resolve()
        self.results: list[CheckResult] = []
        self.checks_run = 0

    def run(self):
        banner = f"""
{BOLD}══════════════════════════════════════════════════════════════{RESET}
{BOLD}  42 Berlin x Needle Hackathon — Requirement Compliance Checker{RESET}
{BOLD}══════════════════════════════════════════════════════════════{RESET}
{BOLD}  Target:{RESET} {self.repo}
{BOLD}  Time:  {RESET} {datetime.now().strftime('%Y-%m-%d %H:%M')}
{BOLD}══════════════════════════════════════════════════════════════{RESET}
"""
        print(banner)

        self._check_repo_access()
        self._check_file_structure()
        self._check_readme()
        self._check_agent_manifest()
        self._check_log_files()
        self._check_final_report()
        self._check_git_history()
        self._check_secrets()
        self._check_agent_capabilities()

        self._print_summary()
        return self._should_exit_with_error()

    def _record(self, name, category, critical=False):
        result = CheckResult(name, category, critical)
        self.results.append(result)
        self.checks_run += 1
        return result

    # ─── 1. Repository access ────────────────────────────────────────────────

    def _check_repo_access(self):
        cat = "Repository Access"

        r = self._record("Repository exists and is accessible", cat, critical=True)
        if not self.repo.is_dir():
            r.fail(f"Path does not exist or is not a directory: {self.repo}")
            return
        r.ok()

        if self._is_git_repo():
            r = self._record("Repository is a Git repository", cat)
            r.ok()
        else:
            r = self._record("Repository is a Git repository", cat)
            r.warn("Not a git repo (ok if using zip/snapshot)")

        r = self._record("Repository is not empty", cat, critical=True)
        if any(self.repo.iterdir()):
            r.ok(f"{sum(1 for _ in self.repo.iterdir())} entries")
        else:
            r.fail("Repository directory is empty")

    # ─── 2. Required file structure ──────────────────────────────────────────

    def _check_file_structure(self):
        cat = "File Structure"

        checks = [
            ("README.md", True),
            ("agent_manifest.json", True),
            ("agent_logs", True),
            ("agent_logs/prompts.log", True),
            ("agent_logs/decisions.log", True),
            ("agent_logs/commands.log", True),
            ("agent_logs/test_runs.log", True),
            ("agent_logs/errors.log", True),
            ("agent_logs/human_interventions.log", True),
            ("agent_logs/final_report.md", True),
        ]

        for path, critical in checks:
            full = self.repo / path
            r = self._record(f"File exists: {path}", cat, critical=critical)
            if full.exists():
                r.ok()
            else:
                r.fail("MISSING")

        for req_name in ("requirements.txt", "pyproject.toml", "environment.yml", "package.json"):
            full = self.repo / req_name
            if full.exists():
                r = self._record(f"Dependency file: {req_name}", cat)
                r.ok()
                break
        else:
            r = self._record("Dependency file (requirements.txt / pyproject.toml / etc.)", cat)
            r.warn("No dependency file found — add if dependencies exist")

        agent_dirs = ["agent", "agents", "src/agent"]
        found_agent = False
        for d in agent_dirs:
            if (self.repo / d).is_dir():
                r = self._record("Agent code directory present", cat)
                r.ok(f"Found: {d}/")
                found_agent = True
                break
        if not found_agent:
            r = self._record("Agent code directory present", cat)
            r.warn("No 'agent/' directory — ok if agent is documented elsewhere")

    # ─── 3. README.md content checks ─────────────────────────────────────────

    def _check_readme(self):
        cat = "README.md Content"
        readme = self.repo / "README.md"
        if not readme.exists():
            return

        content = readme.read_text(encoding="utf-8", errors="replace")

        checks = [
            ("Team name mentioned", r"(?i)#\s*team\s*name|#\s*\w+\s*team"),
            ("Team members listed", r"(?i)members?\s*:|##\s*members?"),
            ("Run command documented", r"(?i)(```bash|```sh|```shell)\s*\n.*python"),
            ("Python/runtime version", r"(?i)python\s*[\d.]+|version|runtime"),
            ("Dependency installation instructions", r"(?i)pip\s+install|venv|poetry|conda"),
            ("Agent architecture overview", r"(?i)agent\s*(architecture|overview|setup|loop)"),
            ("Checkpoint reference (commit/tag)", r"(?i)(checkpoint|agent-readiness|19:?45|19.45)"),
            ("Public test instructions", r"(?i)(public\s*test|test\s*run|run_tests)"),
            ("Known limitations section", r"(?i)known\s*limit|limitations?|known\s*issues?"),
        ]

        for label, pattern in checks:
            r = self._record(f"README.md: {label}", cat)
            if re.search(pattern, content):
                r.ok()
            else:
                r.warn(f"Could not find pattern: {pattern}")

    # ─── 4. agent_manifest.json ──────────────────────────────────────────────

    def _check_agent_manifest(self):
        cat = "agent_manifest.json"
        path = self.repo / "agent_manifest.json"
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            r = self._record("Valid JSON", cat, critical=True)
            r.fail(f"Invalid JSON: {e}")
            return

        r = self._record("Valid JSON", cat, critical=True)
        r.ok()

        required_fields = [
            "team_name",
            "primary_model",
            "provider",
            "paid_frontier_models_used_after_spec_release",
            "copilot_or_paid_ide_assistant_used_after_spec_release",
            "institutional_or_work_model_quota_used_after_spec_release",
        ]

        for field in required_fields:
            r = self._record(f"Manifest field: {field}", cat, critical=True)
            if field in data:
                r.ok(f"= {data[field]}")
            else:
                r.fail("MISSING")

        if "additional_models" in data:
            r = self._record("Manifest field: additional_models", cat)
            r.ok(f"{len(data['additional_models'])} model(s)")
        else:
            r = self._record("Manifest field: additional_models", cat)
            r.warn("Not present (ok if only one model used)")

        r = self._record("Manifest field: notes", cat)
        if "notes" in data and data["notes"]:
            r.ok()
        elif "notes" in data:
            r.warn("Empty notes field")
        else:
            r.warn("MISSING")

        boolean_fields = [
            "paid_frontier_models_used_after_spec_release",
            "copilot_or_paid_ide_assistant_used_after_spec_release",
            "institutional_or_work_model_quota_used_after_spec_release",
        ]
        for field in boolean_fields:
            if field in data and not isinstance(data[field], bool):
                r = self._record(f"Manifest field {field} is boolean", cat)
                r.warn(f"Expected boolean, got {type(data[field]).__name__}")

    # ─── 5. Log file checks ──────────────────────────────────────────────────

    def _check_log_files(self):
        cat = "Agent Logs"

        log_dir = self.repo / "agent_logs"
        if not log_dir.is_dir():
            return

        required_logs = [
            "prompts.log",
            "decisions.log",
            "commands.log",
            "test_runs.log",
            "errors.log",
            "human_interventions.log",
        ]

        for name in required_logs:
            path = log_dir / name
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").strip()

            r = self._record(f"agent_logs/{name}: not empty", cat, critical=True)
            if content:
                r.ok(f"{len(content)} chars")
            else:
                r.fail("EMPTY FILE")

            if name == "human_interventions.log":
                if content and content != "No human interventions after hidden task release.":
                    has_timestamps = bool(re.search(r"\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\]", content))
                    r = self._record(f"agent_logs/{name}: has timestamped entries", cat)
                    if has_timestamps:
                        r.ok()
                    else:
                        r.warn("No timestamped entries found")

            has_timestamps = bool(re.search(r"\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\]", content))
            r = self._record(f"agent_logs/{name}: contains timestamps", cat)
            if has_timestamps:
                r.ok()
            else:
                r.warn("No timestamps found (format: [YYYY-MM-DD HH:MM])")

        r = self._record("agent_logs/human_interventions.log: present and non-empty or explicit-empty", cat, critical=True)
        hi_path = log_dir / "human_interventions.log"
        if hi_path.exists():
            content = hi_path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                r.ok()
            else:
                r.fail("EMPTY — must state 'No human interventions' or list them")
        else:
            r.fail("MISSING")

    # ─── 6. Final report check ───────────────────────────────────────────────

    def _check_final_report(self):
        cat = "Final Report"
        path = self.repo / "agent_logs" / "final_report.md"
        if not path.exists():
            return

        content = path.read_text(encoding="utf-8", errors="replace")

        sections = [
            ("Team name / members", r"(?i)#\s*team|##\s*team"),
            ("Models used", r"(?i)#\s*models?\s*used|##\s*models?\s*used"),
            ("Agent architecture", r"(?i)#\s*agent\s*arch|##\s*agent\s*arch"),
            ("Tools available", r"(?i)#\s*tools?\s*avail|##\s*tools?\s*avail"),
            ("Prompting strategy", r"(?i)#\s*prompt.*strat|##\s*prompt.*strat"),
            ("Test strategy / score progression", r"(?i)#\s*test\s*strat|##\s*test\s*strat|score\s*progression"),
            ("What worked", r"(?i)#\s*what\s*worked|##\s*what\s*worked"),
            ("What failed", r"(?i)#\s*what\s*failed|##\s*what\s*failed"),
            ("What you would improve", r"(?i)#\s*what.*(improve|change)|##\s*what.*(improve|change)"),
            ("Human interventions summary", r"(?i)#\s*human\s*interv|##\s*human\s*interv"),
        ]

        for label, pattern in sections:
            r = self._record(f"final_report.md: {label}", cat)
            if re.search(pattern, content):
                r.ok()
            else:
                r.warn("Section not found")

    # ─── 7. Git history checks ───────────────────────────────────────────────

    def _check_git_history(self):
        cat = "Git History"
        if not self._is_git_repo():
            r = self._record("Git history checks", cat)
            r.warn("Skipped — not a git repository")
            return

        try:
            log = subprocess.run(
                ["git", "log", "--oneline", "-30"],
                capture_output=True, text=True, cwd=self.repo
            )
            if log.returncode != 0:
                r = self._record("Git log accessible", cat)
                r.fail(log.stderr.strip())
                return
            commits = [line for line in log.stdout.strip().split("\n") if line]
            r = self._record("Commit history accessible", cat)
            r.ok(f"{len(commits)} recent commits visible")

            tags = subprocess.run(
                ["git", "tag", "--list"],
                capture_output=True, text=True, cwd=self.repo
            )
            tags_out = tags.stdout.strip()

            r = self._record("19:45 checkpoint tag/commit exists", cat)
            if "agent-readiness" in tags_out or "19-45" in tags_out or "checkpoint" in tags_out.lower():
                r.ok(f"Found tag: {[t for t in tags_out.split() if 'agent' in t or '19' in t or 'checkpoint' in t.lower()]}")
            else:
                tags_list = tags_out.split("\n") if tags_out else []
                if tags_list:
                    r.warn(f"No 'agent-readiness' tag found. Existing tags: {tags_list}")
                else:
                    r.warn("No git tags found")

            if len(commits) < 3:
                r = self._record("Multiple commits (not a single giant commit)", cat)
                r.warn(f"Only {len(commits)} commits — consider more granular history")
            elif len(commits) >= 10:
                r = self._record("Granular commit history", cat)
                r.ok(f"{len(commits)} commits")
            else:
                r = self._record("Multiple commits (not a single giant commit)", cat)
                r.ok(f"{len(commits)} commits")

        except FileNotFoundError:
            r = self._record("Git history checks", cat)
            r.warn("Git not installed — cannot check")

    # ─── 8. Secrets and dangerous files ──────────────────────────────────────

    def _check_secrets(self):
        cat = "Security / Housekeeping"

        # Check for .venv
        r = self._record("No .venv committed", cat)
        if (self.repo / ".venv").is_dir():
            r.fail(".venv/ found — should not be committed")
        else:
            r.ok()

        # Check for __pycache__
        r = self._record("No __pycache__ directories", cat)
        pycache = list(self.repo.rglob("__pycache__"))
        if pycache:
            r.fail(f"Found {len(pycache)} __pycache__ directories")
        else:
            r.ok()

        # Check model files (gguf, etc.)
        r = self._record("No large model files", cat)
        model_extensions = [".gguf", ".bin", ".safetensors", ".pt", ".pth"]
        found_models = []
        for ext in model_extensions:
            found_models.extend(self.repo.rglob(f"*{ext}"))
        if found_models:
            r.fail(f"Found {len(found_models)} model files: {[f.name for f in found_models[:5]]}")
        else:
            r.ok()

        # Check for API keys / secrets
        r = self._record("No exposed API keys or tokens", cat)
        suspicious = []
        secret_patterns = [
            r"sk-[a-zA-Z0-9]{20,}",
            r"api[-_]?key\s*[:=]\s*['\"][a-zA-Z0-9_]{16,}",
            r"token\s*[:=]\s*['\"][a-zA-Z0-9_\-\.]{16,}",
            r"ghp_[a-zA-Z0-9]{36}",
            r"gho_[a-zA-Z0-9]{36}",
        ]
        for pattern in secret_patterns:
            try:
                result = subprocess.run(
                    ["git", "grep", "-n", pattern],
                    capture_output=True, text=True, cwd=self.repo, timeout=30
                )
                if result.stdout.strip():
                    suspicious.extend(result.stdout.strip().split("\n"))
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        if suspicious:
            r.fail(f"Found {len(suspicious)} potential secrets — check and remove")
        else:
            r.ok()

        # Check for .gitignore
        r = self._record(".gitignore present", cat)
        if (self.repo / ".gitignore").exists():
            content = (self.repo / ".gitignore").read_text()
            patterns = [".venv", "__pycache__", ".env", "*.gguf"]
            missing = [p for p in patterns if p not in content]
            if missing:
                r.warn(f".gitignore exists but missing patterns: {missing}")
            else:
                r.ok()
        else:
            r.warn("No .gitignore — recommend adding one")

    # ─── 9. Agent capability checks (observational) ─────────────────────────

    def _check_agent_capabilities(self):
        cat = "Agent Capabilities"

        agent_code = None
        for d in ["agent", "agents"]:
            candidate = self.repo / d
            if candidate.is_dir():
                agent_code = candidate
                break

        if agent_code:
            all_py_files = list(agent_code.rglob("*.py"))
            all_code = " ".join(p.read_text(encoding="utf-8", errors="replace") for p in all_py_files)

            capability_checks = [
                ("Read specification", r"(read|open|load).*spec|SECRET_SPEC|\.md"),
                ("File editing / writing", r"(write|edit|open\(.*['\"]w|save_file|create_file|write_file)"),
                ("Run shell commands", r"(subprocess|os\.system|sh\.run|run_shell|execute|command)"),
                ("Run tests", r"(test|run_test|pytest|test_runner|run_tests)"),
                ("Capture stdout/stderr", r"(stdout|stderr|capture_output|text=True|\.communicate)"),
                ("Logging / timestamp", r"(logging|log\.|timestamp|datetime|strftime|logger)"),
                ("Iteration loop", r"(while|for.*iteration|MAX_ITER|loop|state\[.iteration.])"),
            ]

            for label, pattern in capability_checks:
                r = self._record(f"Agent code: {label}", cat)
                if re.search(pattern, all_code):
                    r.ok()
                else:
                    r.warn(f"No evidence found (pattern: {pattern})")

            r = self._record("Agent code: has main entrypoint or runner", cat)
            if any("__main__" in p.read_text() for p in all_py_files) or \
               any("def main" in p.read_text() for p in all_py_files) or \
               (self.repo / "run_agent.py").exists():
                r.ok()
            else:
                r.warn("No clear entrypoint — maybe in non-Python tooling")
        else:
            r = self._record("Agent code analysis", cat)
            r.warn("No agent/ or agents/ directory — cannot analyze agent capabilities")

    # ─── Summary ─────────────────────────────────────────────────────────────

    def _print_summary(self):
        passed = sum(1 for r in self.results if r.passed is True)
        failed = sum(1 for r in self.results if r.passed is False)
        warns = sum(1 for r in self.results if r.passed is None)

        critical_failed = sum(
            1 for r in self.results
            if r.passed is False and r.critical
        )

        print(f"\n{BOLD}══════════════════════════════════════════════════════════════{RESET}")
        print(f"{BOLD}  SUMMARY{RESET}")
        print(f"{BOLD}══════════════════════════════════════════════════════════════{RESET}")
        print(f"  {GREEN}PASS:{RESET}  {passed}")
        print(f"  {RED}FAIL:{RESET}  {failed}  ({RED}{critical_failed} critical{RESET})")
        print(f"  {YELLOW}WARN:{RESET}  {warns}")
        print(f"  Total:  {self.checks_run}")
        print(f"{BOLD}══════════════════════════════════════════════════════════════{RESET}")

        if failed > 0:
            print(f"\n{BOLD}Failed checks:{RESET}")
            for r in self.results:
                if r.passed is False:
                    critical = f" {RED}(CRITICAL){RESET}" if r.critical else ""
                    print(f"  [{r.category}] {r.name}{critical}")
                    print(f"    {RED}{r.message}{RESET}")

        if warns > 0:
            print(f"\n{BOLD}Warnings:{RESET}")
            for r in self.results:
                if r.passed is None:
                    print(f"  [{r.category}] {r.name}")
                    print(f"    {YELLOW}{r.message}{RESET}")

        if critical_failed > 0:
            print(f"\n{RED}{BOLD}  ✗ {critical_failed} critical check(s) failed — submission may be incomplete{RESET}")
        else:
            print(f"\n{GREEN}{BOLD}  ✓ No critical failures{RESET}")

    def _should_exit_with_error(self):
        critical_failed = sum(
            1 for r in self.results
            if r.passed is False and r.critical
        )
        return critical_failed > 0

    def _is_git_repo(self):
        return (self.repo / ".git").is_dir()


# ─── Entrypoint ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} /path/to/team/repository", file=sys.stderr)
        sys.exit(1)

    repo_path = sys.argv[1]
    if not os.path.isdir(repo_path):
        print(f"Error: path does not exist or is not a directory: {repo_path}", file=sys.stderr)
        sys.exit(1)

    checker = ComplianceChecker(repo_path)
    has_errors = checker.run()
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
