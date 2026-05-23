#!/usr/bin/env python3
"""
Duck Agent Dashboard Server

Live dashboard for real-time monitoring and post-mortem analysis.
"""

import argparse
import asyncio
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    from websockets.asyncio.server import serve
except ImportError:
    print("ERROR: websockets not installed")
    print("  Install with: pip install websockets")
    raise SystemExit(1)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AgentLogParser:
    """Parse agent log files and derive dashboard analytics."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_files = {
            "decisions": log_dir / "decisions.log",
            "prompts": log_dir / "prompts.log",
            "test_runs": log_dir / "test_runs.log",
            "errors": log_dir / "errors.log",
            "commands": log_dir / "commands.log",
            "human_interventions": log_dir / "human_interventions.log",
        }
        self.file_positions = {name: 0 for name in self.log_files}
        self.initialized = False

    def _read_text(self, category: str) -> str:
        path = self.log_files.get(category)
        if not path or not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.error("Error reading %s: %s", category, exc)
            return ""

    def _read_lines(self, category: str) -> List[str]:
        text = self._read_text(category)
        return text.splitlines()

    def _parse_timestamp(self, value: str) -> Optional[datetime]:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _timestamped_entries(self, category: str) -> List[Dict]:
        entries: List[Dict] = []
        for line in self._read_lines(category):
            match = re.match(r"\[([^\]]+)\]\s+(.*)", line)
            if not match:
                continue
            timestamp, message = match.groups()
            entries.append(
                {
                    "timestamp": timestamp,
                    "dt": self._parse_timestamp(timestamp),
                    "message": message,
                    "category": category,
                }
            )
        return entries

    def _all_entries(self) -> List[Dict]:
        entries: List[Dict] = []
        for category in self.log_files:
            entries.extend(self._timestamped_entries(category))
        entries.sort(key=lambda item: item["dt"] or datetime.min)
        return entries

    def _latest_entry(self, category: str) -> Optional[Dict]:
        entries = self._timestamped_entries(category)
        return entries[-1] if entries else None

    def _latest_event(self) -> Optional[Dict]:
        entries = self._all_entries()
        return entries[-1] if entries else None

    def _seconds_since(self, value: Optional[datetime]) -> Optional[int]:
        if value is None:
            return None
        return max(0, int((datetime.now() - value).total_seconds()))

    def get_new_logs(self, category: str, limit: int = 100) -> List[Dict]:
        """Get recent log entries on first read, then incremental updates."""
        path = self.log_files.get(category)
        if not path or not path.exists():
            return []

        entries: List[Dict] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                if self.initialized:
                    handle.seek(self.file_positions[category])
                else:
                    handle.seek(0)
                for line in handle:
                    match = re.match(r"\[([^\]]+)\]\s+(.*)", line)
                    if not match:
                        continue
                    timestamp, message = match.groups()
                    entries.append(
                        {
                            "timestamp": timestamp,
                            "message": message[:500],
                            "category": category,
                        }
                    )
                self.file_positions[category] = handle.tell()
        except Exception as exc:
            logger.error("Error reading %s: %s", category, exc)

        return entries[-limit:]

    def parse_iteration_status(self) -> Dict:
        """Extract current iteration and runtime state."""
        decision_entries = self._timestamped_entries("decisions")
        error_entries = self._timestamped_entries("errors")
        latest_event = self._latest_event()

        if not decision_entries and not error_entries:
            return {
                "iteration": 0,
                "status": "waiting",
                "current_action": "waiting",
                "reason": "No agent activity yet.",
                "last_event_time": None,
                "seconds_since_event": None,
            }

        iteration = 0
        action = "waiting"
        reason = "No action recorded yet."
        session_start = None

        for entry in decision_entries:
            message = entry["message"]
            if "Starting agent loop" in message:
                session_start = entry
            iter_match = re.search(r"iteration\s+(\d+)", message, re.IGNORECASE)
            if iter_match:
                iteration = max(iteration, int(iter_match.group(1)))
            action_match = re.search(r"ACTION\s+([A-Z_]+):\s*(.*)", message)
            if action_match:
                action = action_match.group(1)
                reason = action_match.group(2)[:180] or reason

        for entry in error_entries:
            iter_match = re.search(r"Iteration\s+(\d+)", entry["message"])
            if iter_match:
                iteration = max(iteration, int(iter_match.group(1)))

        latest_message = latest_event["message"] if latest_event else ""
        seconds_since = self._seconds_since(latest_event["dt"] if latest_event else None)

        if latest_event is None:
            status = "waiting"
        elif latest_event["category"] == "errors":
            if "Reached maximum iteration count" in latest_message:
                status = "stopped"
            else:
                status = "error"
        elif "Stopping at iteration" in latest_message or "Stopping after" in latest_message:
            status = "stopped"
        elif seconds_since is not None and seconds_since <= 15:
            status = "running"
        elif session_start is not None:
            status = "idle"
        else:
            status = "waiting"

        return {
            "iteration": iteration,
            "status": status,
            "current_action": action,
            "reason": reason,
            "last_event_time": latest_event["timestamp"] if latest_event else None,
            "seconds_since_event": seconds_since,
        }

    def parse_test_status(self) -> Dict:
        """Extract latest test scoreboard and trend."""
        content = self._read_text("test_runs")
        if not content:
            return {
                "passed": 0,
                "failed": 0,
                "total": 0,
                "percentage": 0,
                "status": "not_run",
                "history": [],
                "last_command": None,
                "last_exit_code": None,
            }

        scoreboards = list(re.finditer(r"Overall \[.*?\] (\d+)/(\d+) passed", content))
        history = []
        for match in scoreboards[-8:]:
            passed = int(match.group(1))
            total = int(match.group(2))
            history.append(
                {
                    "passed": passed,
                    "failed": total - passed,
                    "total": total,
                    "percentage": round((passed / total * 100), 1) if total else 0,
                }
            )

        latest_command = None
        last_exit_code = None
        for line in self._read_lines("test_runs"):
            command_match = re.match(r"\[[^\]]+\]\s+COMMAND\s+(.*)", line)
            if command_match:
                latest_command = command_match.group(1)
            exit_match = re.match(r"\[[^\]]+\]\s+exit=(\d+)", line)
            if exit_match:
                last_exit_code = int(exit_match.group(1))

        if history:
            latest = history[-1]
            status = "completed" if latest["total"] else "pending"
            return {
                **latest,
                "status": status,
                "history": history,
                "last_command": latest_command,
                "last_exit_code": last_exit_code,
            }

        return {
            "passed": 0,
            "failed": 0,
            "total": 0,
            "percentage": 0,
            "status": "pending",
            "history": history,
            "last_command": latest_command,
            "last_exit_code": last_exit_code,
        }

    def parse_file_metrics(self) -> Dict:
        """Count actions, file hotspots, and command exits."""
        decision_entries = self._timestamped_entries("decisions")
        action_counter: Counter = Counter()
        file_counter: Counter = Counter()

        for entry in decision_entries:
            message = entry["message"]
            action_match = re.search(r"ACTION\s+([A-Z_]+):", message)
            if action_match:
                action_counter[action_match.group(1)] += 1
            file_match = re.search(r"WRITE_FILE\s+(.+)$", message)
            if file_match:
                file_counter[file_match.group(1).strip()] += 1

        command_exit_counter: Counter = Counter()
        commands_seen = 0
        for category in ("commands", "test_runs"):
            for line in self._read_lines(category):
                if re.match(r"\[[^\]]+\]\s+COMMAND\s+", line):
                    commands_seen += 1
                exit_match = re.match(r"\[[^\]]+\]\s+exit=(\d+)", line)
                if exit_match:
                    command_exit_counter[exit_match.group(1)] += 1

        return {
            "files_written": sum(file_counter.values()),
            "unique_files": len(file_counter),
            "commands_run": commands_seen,
            "actions_total": sum(action_counter.values()),
            "action_breakdown": dict(action_counter),
            "top_files": [
                {"path": path, "count": count}
                for path, count in file_counter.most_common(6)
            ],
            "command_exit_breakdown": dict(command_exit_counter),
        }

    def _categorize_error(self, message: str) -> str:
        lowered = message.lower()
        categories = [
            ("timeout", ["timed out", "timeout"]),
            ("invalid_json", ["not valid json", "invalid model response json", "invalid json"]),
            ("connection_refused", ["connection refused"]),
            ("missing_resource", ["not found", "can't open file", "missing"]),
            ("bad_action", ["requires a non-empty path"]),
            ("max_iterations", ["maximum iteration", "stopping after"]),
            ("model_runtime", ["model call failed", "openai-compatible response"]),
        ]
        for label, needles in categories:
            if any(needle in lowered for needle in needles):
                return label
        return "other"

    def parse_error_analysis(self) -> Dict:
        """Summarize errors and incident patterns."""
        entries = self._timestamped_entries("errors")
        category_counter: Counter = Counter()
        recurring_counter: Counter = Counter()
        latest_error = entries[-1] if entries else None

        for entry in entries:
            message = entry["message"]
            category_counter[self._categorize_error(message)] += 1
            normalized = re.sub(r"Iteration\s+\d+", "Iteration N", message)
            recurring_counter[normalized[:160]] += 1

        return {
            "total": len(entries),
            "latest": latest_error["message"] if latest_error else None,
            "latest_time": latest_error["timestamp"] if latest_error else None,
            "categories": [
                {"label": label, "count": count}
                for label, count in category_counter.most_common(6)
            ],
            "recurring": [
                {"label": label, "count": count}
                for label, count in recurring_counter.most_common(5)
            ],
        }

    def parse_command_analysis(self) -> Dict:
        """Summarize command and test execution signals."""
        latest_command = None
        latest_exit_code = None
        command_counter = 0
        successful = 0
        failed = 0

        for category in ("commands", "test_runs"):
            for line in self._read_lines(category):
                command_match = re.match(r"\[[^\]]+\]\s+COMMAND\s+(.*)", line)
                if command_match:
                    latest_command = command_match.group(1)
                    command_counter += 1
                exit_match = re.match(r"\[[^\]]+\]\s+exit=(\d+)", line)
                if exit_match:
                    latest_exit_code = int(exit_match.group(1))
                    if latest_exit_code == 0:
                        successful += 1
                    else:
                        failed += 1

        success_rate = round((successful / (successful + failed) * 100), 1) if (successful + failed) else 0
        return {
            "latest_command": latest_command,
            "latest_exit_code": latest_exit_code,
            "total": command_counter,
            "successful": successful,
            "failed": failed,
            "success_rate": success_rate,
        }

    def parse_session_analysis(self) -> Dict:
        """Build post-mortem analysis across agent sessions."""
        decisions = self._timestamped_entries("decisions")
        if not decisions:
            return {
                "session_count": 0,
                "latest_session": None,
                "recent_sessions": [],
                "recommendations": [],
            }

        sessions: List[Dict] = []
        current: Optional[Dict] = None

        for entry in decisions:
            message = entry["message"]
            if "Starting agent loop" in message:
                if current is not None:
                    sessions.append(current)
                current = {
                    "started_at": entry["timestamp"],
                    "started_dt": entry["dt"],
                    "ended_at": entry["timestamp"],
                    "ended_dt": entry["dt"],
                    "actions": 0,
                    "writes": 0,
                    "commands": 0,
                    "tests": 0,
                    "iterations": 0,
                    "stop_reason": "Incomplete",
                }
                continue

            if current is None:
                continue

            current["ended_at"] = entry["timestamp"]
            current["ended_dt"] = entry["dt"]
            iter_match = re.search(r"iteration\s+(\d+)", message, re.IGNORECASE)
            if iter_match:
                current["iterations"] = max(current["iterations"], int(iter_match.group(1)))
            action_match = re.search(r"ACTION\s+([A-Z_]+):", message)
            if action_match:
                current["actions"] += 1
                action = action_match.group(1)
                if action == "WRITE_FILE":
                    current["writes"] += 1
                elif action == "RUN_COMMAND":
                    current["commands"] += 1
                elif action == "RUN_TESTS":
                    current["tests"] += 1
            if "Stopping at iteration" in message or "Stopping after" in message:
                current["stop_reason"] = message

        if current is not None:
            sessions.append(current)

        latest = sessions[-1]
        recent_sessions = []
        for session in sessions[-5:]:
            duration_seconds = None
            if session["started_dt"] and session["ended_dt"]:
                duration_seconds = int((session["ended_dt"] - session["started_dt"]).total_seconds())
            recent_sessions.append(
                {
                    "started_at": session["started_at"],
                    "ended_at": session["ended_at"],
                    "duration_seconds": duration_seconds,
                    "actions": session["actions"],
                    "iterations": session["iterations"],
                    "stop_reason": session["stop_reason"],
                }
            )

        test_status = self.parse_test_status()
        error_analysis = self.parse_error_analysis()
        recommendations: List[str] = []
        if error_analysis["categories"]:
            top_error = error_analysis["categories"][0]["label"] if isinstance(error_analysis["categories"][0], tuple) else error_analysis["categories"][0]["label"]
            if top_error == "timeout":
                recommendations.append("Model or test execution is timing out frequently. Tighten prompts or reduce long-running commands.")
            elif top_error == "invalid_json":
                recommendations.append("Model JSON formatting is unstable. Add stronger response constraints or a repair pass before execution.")
            elif top_error == "missing_resource":
                recommendations.append("Missing files or paths dominate failures. Validate required inputs before the agent loop begins.")
        if test_status.get("total", 0) and test_status.get("percentage", 0) == 0:
            recommendations.append("Tests are still at 0%. Prioritize bootstrapping a minimal runnable artifact before deeper iterations.")
        if latest["writes"] > latest["commands"] * 3 and latest["commands"] <= 1:
            recommendations.append("Latest session is write-heavy with little verification. Force more frequent test or command checkpoints.")
        if not recommendations:
            recommendations.append("Agent signals look balanced. Focus on preserving current verification cadence.")

        latest_duration = None
        if latest["started_dt"] and latest["ended_dt"]:
            latest_duration = int((latest["ended_dt"] - latest["started_dt"]).total_seconds())

        return {
            "session_count": len(sessions),
            "latest_session": {
                "started_at": latest["started_at"],
                "ended_at": latest["ended_at"],
                "duration_seconds": latest_duration,
                "actions": latest["actions"],
                "writes": latest["writes"],
                "commands": latest["commands"],
                "tests": latest["tests"],
                "iterations": latest["iterations"],
                "stop_reason": latest["stop_reason"],
            },
            "recent_sessions": recent_sessions,
            "recommendations": recommendations[:4],
        }

    def parse_prompt_metrics(self) -> Dict:
        """Summarize prompt volume."""
        path = self.log_files["prompts"]
        if not path.exists():
            return {"entries": 0, "bytes": 0, "avg_bytes": 0}
        size = path.stat().st_size
        entries = len(self._timestamped_entries("prompts"))
        avg_bytes = int(size / entries) if entries else 0
        return {"entries": entries, "bytes": size, "avg_bytes": avg_bytes}

    def parse_performance_metrics(self) -> Dict:
        """Build chart-friendly performance series."""
        tests = self.parse_test_status()
        sessions = self.parse_session_analysis()
        commands = self.parse_command_analysis()
        all_entries = self._all_entries()

        test_progress = [
            {
                "label": f"run {index + 1}",
                "value": point["percentage"],
                "passed": point["passed"],
                "total": point["total"],
            }
            for index, point in enumerate(tests.get("history", [])[-10:])
        ]

        session_velocity = [
            {
                "label": session["started_at"][11:16] if session.get("started_at") else f"s{index + 1}",
                "value": session.get("actions", 0),
                "iterations": session.get("iterations", 0),
                "duration_seconds": session.get("duration_seconds"),
            }
            for index, session in enumerate(sessions.get("recent_sessions", [])[-8:])
        ]

        hour_buckets: Counter = Counter()
        for entry in all_entries:
            if entry.get("dt") is None:
                continue
            label = entry["dt"].strftime("%m-%d %H:00")
            hour_buckets[label] += 1
        activity_heat = [
            {"label": label, "value": value}
            for label, value in sorted(hour_buckets.items())[-12:]
        ]

        command_outcomes = [
            {"label": "success", "value": commands.get("successful", 0)},
            {"label": "failed", "value": commands.get("failed", 0)},
        ]

        return {
            "test_progress": test_progress,
            "session_velocity": session_velocity,
            "activity_heat": activity_heat,
            "command_outcomes": command_outcomes,
            "headline": {
                "test_best": max((point["value"] for point in test_progress), default=0),
                "session_peak": max((point["value"] for point in session_velocity), default=0),
                "activity_peak": max((point["value"] for point in activity_heat), default=0),
                "command_success_rate": commands.get("success_rate", 0),
            },
        }

    def parse_human_summary(
        self,
        iteration: Dict,
        tests: Dict,
        metrics: Dict,
        errors: Dict,
        commands: Dict,
        sessions: Dict,
        performance: Dict,
    ) -> Dict:
        """Translate raw telemetry into human-readable takeaways."""
        status = iteration.get("status", "waiting")
        latest_session = sessions.get("latest_session") or {}
        error_categories = errors.get("categories") or []
        dominant_error = error_categories[0]["label"] if error_categories else None

        if status == "running":
            headline = f"The agent is currently active on iteration {iteration.get('iteration', 0)}."
        elif status == "error":
            headline = "The agent is stalled by an error and likely needs intervention."
        elif status == "stopped":
            headline = "The latest run has stopped. Use the post-mortem panels to understand why."
        elif status == "idle":
            headline = "The agent is quiet right now. No fresh activity has appeared recently."
        else:
            headline = "The dashboard is waiting for meaningful agent activity."

        if tests.get("total", 0):
            quality = f"Test coverage snapshot: {tests.get('passed', 0)} of {tests.get('total', 0)} passing ({tests.get('percentage', 0)}%)."
        else:
            quality = "No structured test scoreboard has been captured yet."

        if dominant_error == "timeout":
            bottleneck = "Main bottleneck: timeouts are dominating, so the agent may be spending too long waiting on model or command execution."
        elif dominant_error == "invalid_json":
            bottleneck = "Main bottleneck: malformed model output is breaking the action loop before work can be applied cleanly."
        elif dominant_error == "missing_resource":
            bottleneck = "Main bottleneck: missing files or bad paths are causing the agent to fail before verification can help."
        elif dominant_error == "connection_refused":
            bottleneck = "Main bottleneck: the model backend has been unreachable, so the agent cannot think or continue."
        elif errors.get("total", 0):
            bottleneck = "Main bottleneck: repeated runtime errors are slowing progress and reducing useful iterations."
        else:
            bottleneck = "No dominant failure mode detected right now."

        next_step = (sessions.get("recommendations") or ["Keep watching the latest session for new signals."])[0]

        test_progress = performance.get("test_progress") or []
        if len(test_progress) >= 2:
            delta = round(test_progress[-1]["value"] - test_progress[0]["value"], 1)
            if delta > 0:
                chart_takeaway = f"Performance trend: test results improved by {delta} points across the recent visible runs."
            elif delta < 0:
                chart_takeaway = f"Performance trend: test results dropped by {abs(delta)} points across the recent visible runs."
            else:
                chart_takeaway = "Performance trend: test results are flat across the recent visible runs."
        else:
            chart_takeaway = "Performance trend: not enough repeated test runs yet to measure momentum."

        session_peak = performance.get("headline", {}).get("session_peak", 0)
        activity_peak = performance.get("headline", {}).get("activity_peak", 0)
        command_success_rate = performance.get("headline", {}).get("command_success_rate", 0)

        facts = [
            f"Latest action: {iteration.get('current_action', 'waiting')}",
            f"Files touched so far: {metrics.get('unique_files', 0)} unique files",
            f"Peak session throughput: {session_peak} actions in one visible session",
            f"Busiest hour: {activity_peak} logged events",
            f"Command success rate: {command_success_rate}%",
        ]

        if latest_session:
            facts.append(
                f"Latest session executed {latest_session.get('actions', 0)} actions over {latest_session.get('iterations', 0)} iterations"
            )

        return {
            "headline": headline,
            "quality": quality,
            "bottleneck": bottleneck,
            "next_step": next_step,
            "chart_takeaway": chart_takeaway,
            "facts": facts,
        }

    def parse_health_and_stuck(
        self,
        iteration: Dict,
        tests: Dict,
        errors: Dict,
        commands: Dict,
        sessions: Dict,
        performance: Dict,
    ) -> Dict:
        """Score overall health and explain likely stuck states."""
        score = 100
        reasons: List[str] = []
        warnings: List[str] = []
        status = iteration.get("status", "waiting")
        seconds_since = iteration.get("seconds_since_event")
        latest_session = sessions.get("latest_session") or {}
        top_error = (errors.get("categories") or [{}])[0].get("label")
        test_pct = tests.get("percentage", 0)
        command_success = commands.get("success_rate", 0)

        if status == "error":
            score -= 45
            reasons.append("The latest visible state is an error.")
        elif status == "stopped":
            score -= 30
            reasons.append("The latest run has already stopped.")
        elif status == "idle":
            score -= 15
            reasons.append("The agent is idle rather than actively progressing.")

        if seconds_since is not None and seconds_since > 300:
            score -= 20
            warnings.append(f"No fresh event for {seconds_since} seconds.")
        elif seconds_since is not None and seconds_since > 60:
            score -= 10
            warnings.append(f"Activity has slowed down for {seconds_since} seconds.")

        if top_error == "timeout":
            score -= 15
            reasons.append("Timeouts are frequent.")
        elif top_error == "invalid_json":
            score -= 15
            reasons.append("The model is producing malformed JSON responses.")
        elif top_error == "missing_resource":
            score -= 12
            reasons.append("Missing files or paths are blocking execution.")
        elif top_error == "connection_refused":
            score -= 20
            reasons.append("The model backend appears unreachable.")

        if tests.get("total", 0) and test_pct == 0:
            score -= 10
            warnings.append("Tests exist but nothing is passing yet.")
        elif test_pct >= 80:
            score += 5

        if command_success < 35 and (commands.get("successful", 0) + commands.get("failed", 0)) > 0:
            score -= 12
            warnings.append("Commands fail more often than they succeed.")

        if latest_session.get("writes", 0) > max(1, latest_session.get("commands", 0)) * 4:
            score -= 8
            warnings.append("The latest session is write-heavy with little verification.")

        score = max(0, min(100, int(score)))
        if score >= 75:
            color = "green"
            label = "Healthy"
        elif score >= 45:
            color = "yellow"
            label = "At Risk"
        else:
            color = "red"
            label = "Unhealthy"

        stuck = False
        stuck_title = "No strong stuck signal detected"
        stuck_reason = "The agent still shows enough movement to avoid a hard stuck diagnosis."
        stuck_evidence: List[str] = []

        if status == "error":
            stuck = True
            stuck_title = "Agent is stuck on an error"
            stuck_reason = errors.get("latest") or "The latest event is an error."
        elif status == "stopped":
            stuck = True
            stuck_title = "Agent run stopped"
            stuck_reason = latest_session.get("stop_reason") or "The latest run ended early."
        elif seconds_since is not None and seconds_since > 300:
            stuck = True
            stuck_title = "Agent appears frozen"
            stuck_reason = f"No new event has appeared for {seconds_since} seconds."
        elif latest_session.get("writes", 0) >= 4 and latest_session.get("commands", 0) == 0 and latest_session.get("tests", 0) == 0:
            stuck = True
            stuck_title = "Agent is looping on edits without verification"
            stuck_reason = "The latest session keeps writing files without command or test feedback."
        elif top_error == "invalid_json":
            stuck = True
            stuck_title = "Agent is stuck on malformed model output"
            stuck_reason = "Invalid JSON responses keep preventing clean action execution."
        elif top_error == "timeout":
            stuck = True
            stuck_title = "Agent is stuck waiting on slow operations"
            stuck_reason = "Timeouts dominate the visible failures, so work may be blocked on the model or commands."

        if stuck:
            if top_error:
                stuck_evidence.append(f"Top failure mode: {top_error}")
            if seconds_since is not None:
                stuck_evidence.append(f"Last event age: {seconds_since}s")
            if latest_session:
                stuck_evidence.append(
                    f"Latest session mix: {latest_session.get('writes', 0)} writes, {latest_session.get('commands', 0)} commands, {latest_session.get('tests', 0)} tests"
                )
        else:
            stuck_evidence.append("Recent events still show some useful movement.")

        next_action = (sessions.get("recommendations") or ["Monitor the next few events before intervening."])[0]

        return {
            "score": score,
            "color": color,
            "label": label,
            "reasons": reasons[:4],
            "warnings": warnings[:4],
            "stuck": stuck,
            "stuck_title": stuck_title,
            "stuck_reason": stuck_reason,
            "stuck_evidence": stuck_evidence[:4],
            "next_action": next_action,
        }

    def build_state(self) -> Dict:
        """Build the complete dashboard state."""
        iteration = self.parse_iteration_status()
        tests = self.parse_test_status()
        metrics = self.parse_file_metrics()
        errors = self.parse_error_analysis()
        commands = self.parse_command_analysis()
        sessions = self.parse_session_analysis()
        prompts = self.parse_prompt_metrics()
        performance = self.parse_performance_metrics()
        human = self.parse_human_summary(iteration, tests, metrics, errors, commands, sessions, performance)
        health = self.parse_health_and_stuck(iteration, tests, errors, commands, sessions, performance)
        latest_event = self._latest_event()
        latest_command_entry = self._latest_entry("commands")
        latest_test_entry = self._latest_entry("test_runs")

        combined_logs: List[Dict] = []
        for category, amount in (("decisions", 20), ("errors", 10), ("test_runs", 10), ("commands", 10)):
            entries = self.get_new_logs(category, amount)
            for entry in entries:
                combined_logs.append(
                    {
                        **entry,
                        "type": "test" if category == "test_runs" else category.rstrip("s"),
                    }
                )
        combined_logs.sort(key=lambda item: item["timestamp"])

        live_window_entries = [
            entry for entry in self._all_entries() if self._seconds_since(entry["dt"]) is not None and self._seconds_since(entry["dt"]) <= 3600
        ]

        return {
            "timestamp": datetime.now().isoformat(),
            "overview": {
                "status": iteration["status"],
                "last_event_time": latest_event["timestamp"] if latest_event else None,
                "seconds_since_event": iteration["seconds_since_event"],
                "log_dir": str(self.log_dir),
            },
            "iteration": iteration,
            "tests": tests,
            "metrics": metrics,
            "errors": errors,
            "commands": commands,
            "sessions": sessions,
            "prompts": prompts,
            "performance": performance,
            "human": human,
            "health": health,
            "realtime": {
                "events_last_hour": len(live_window_entries),
                "latest_command": latest_command_entry["message"] if latest_command_entry else None,
                "latest_test": latest_test_entry["message"] if latest_test_entry else None,
                "latest_error": errors["latest"],
            },
            "recent_logs": {
                "combined": combined_logs[-30:],
                "decisions": self.get_new_logs("decisions", 20),
                "errors": self.get_new_logs("errors", 10),
                "tests": self.get_new_logs("test_runs", 10),
                "commands": self.get_new_logs("commands", 10),
            },
        }


class DashboardServer:
    """WebSocket server for dashboard updates."""

    def __init__(self, log_dir: Path, host: str = "localhost", port: int = 8080):
        self.log_dir = log_dir
        self.host = host
        self.port = port
        self.parser = AgentLogParser(log_dir)
        self.clients: Set = set()
        self.poll_interval = 2

    async def broadcast_update(self):
        """Broadcast current state to all connected clients."""
        first_broadcast = True
        while True:
            try:
                state = self.parser.build_state()
                if first_broadcast:
                    self.parser.initialized = True
                    first_broadcast = False
                message = json.dumps(state)
                if self.clients:
                    await asyncio.gather(
                        *[client.send(message) for client in self.clients.copy()],
                        return_exceptions=True,
                    )
                await asyncio.sleep(self.poll_interval)
            except Exception as exc:
                logger.error("Broadcast error: %s", exc)
                await asyncio.sleep(1)

    async def handler(self, websocket):
        """Handle WebSocket connections."""
        self.clients.add(websocket)
        logger.info("Client connected. Total: %s", len(self.clients))
        try:
            async for _ in websocket:
                pass
        except Exception as exc:
            logger.error("Handler error: %s", exc)
        finally:
            self.clients.discard(websocket)
            logger.info("Client disconnected. Total: %s", len(self.clients))

    async def start(self):
        """Start the server."""
        logger.info("Dashboard starting on ws://%s:%s", self.host, self.port)
        broadcast_task = asyncio.create_task(self.broadcast_update())
        async with await serve(self.handler, self.host, self.port):
            await broadcast_task


def get_html_page(ws_url: Optional[str] = None) -> str:
    """Generate the HTML dashboard page."""
    if ws_url is None:
        ws_url = "window.location.host"

    html = """
<!DOCTYPE html>
<html>
<head>
    <title>🦆 Duck Agent Observatory</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            color-scheme: dark;
            --bg: #09111c;
            --panel: rgba(17, 24, 39, 0.88);
            --panel-2: rgba(31, 41, 55, 0.92);
            --border: rgba(148, 163, 184, 0.18);
            --text: #e5eef9;
            --muted: #93a4b8;
            --green: #22c55e;
            --red: #f87171;
            --amber: #fbbf24;
            --blue: #60a5fa;
            --cyan: #22d3ee;
            --violet: #a78bfa;
            --pink: #f472b6;
        }
        body {
            min-height: 100vh;
            font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background:
                radial-gradient(circle at top left, rgba(34, 211, 238, 0.16), transparent 32%),
                radial-gradient(circle at top right, rgba(167, 139, 250, 0.16), transparent 28%),
                linear-gradient(180deg, #071019 0%, #0b1220 100%);
            color: var(--text);
            padding: 24px;
        }
        .container { max-width: 1500px; margin: 0 auto; }
        .hero {
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: flex-start;
            margin-bottom: 20px;
        }
        .hero h1 { font-size: 34px; line-height: 1.1; margin-bottom: 8px; }
        .hero p { color: var(--muted); max-width: 760px; }
        .badges { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
        .badge {
            padding: 7px 12px;
            border-radius: 999px;
            border: 1px solid var(--border);
            background: rgba(15, 23, 42, 0.8);
            color: var(--text);
            font-size: 12px;
            letter-spacing: 0.03em;
        }
        .status-badge { font-weight: 700; }
        .status-running { background: rgba(34, 197, 94, 0.16); color: #b7f7cb; }
        .status-idle, .status-waiting { background: rgba(251, 191, 36, 0.16); color: #fde68a; }
        .status-error { background: rgba(248, 113, 113, 0.16); color: #fecaca; }
        .status-stopped { background: rgba(148, 163, 184, 0.16); color: #cbd5e1; }
        .health-green { color: #b7f7cb; }
        .health-yellow { color: #fde68a; }
        .health-red { color: #fecaca; }
        .metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 14px;
            margin-bottom: 20px;
        }
        .card {
            background: var(--panel);
            backdrop-filter: blur(14px);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.16);
        }
        .metric-card h3, .section-title {
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 10px;
        }
        .metric { font-size: 34px; font-weight: 750; }
        .metric-sub { margin-top: 6px; color: var(--muted); font-size: 13px; }
        .layout {
            display: grid;
            grid-template-columns: 1.3fr 1fr;
            gap: 16px;
            margin-bottom: 16px;
        }
        .stack { display: grid; gap: 16px; }
        .split {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
        }
        .progress {
            width: 100%;
            height: 8px;
            background: rgba(148, 163, 184, 0.14);
            border-radius: 999px;
            overflow: hidden;
            margin-top: 10px;
        }
        .progress > div {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, var(--green), var(--cyan));
            transition: width 0.3s ease;
        }
        .eyebrow { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
        .value { font-size: 16px; line-height: 1.5; }
        .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
        .list { display: grid; gap: 10px; }
        .item {
            border: 1px solid rgba(148, 163, 184, 0.1);
            background: rgba(15, 23, 42, 0.55);
            border-radius: 14px;
            padding: 12px 14px;
        }
        .item-row {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: baseline;
        }
        .item-title { font-weight: 650; }
        .item-meta { color: var(--muted); font-size: 12px; }
        .timeline {
            max-height: 560px;
            overflow-y: auto;
            display: grid;
            gap: 10px;
            padding-right: 4px;
        }
        .timeline-entry {
            border-left: 3px solid rgba(148, 163, 184, 0.25);
            padding: 10px 12px;
            background: rgba(15, 23, 42, 0.48);
            border-radius: 0 14px 14px 0;
        }
        .timeline-entry.decision { border-left-color: var(--blue); }
        .timeline-entry.error { border-left-color: var(--red); }
        .timeline-entry.test { border-left-color: var(--cyan); }
        .timeline-entry.command { border-left-color: var(--violet); }
        .timeline-head {
            display: flex;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 6px;
            font-size: 12px;
            color: var(--muted);
        }
        .pill {
            padding: 2px 8px;
            border-radius: 999px;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            background: rgba(148, 163, 184, 0.12);
        }
        .recommendation {
            border-left: 3px solid var(--pink);
            background: rgba(244, 114, 182, 0.08);
            padding: 12px 14px;
            border-radius: 0 14px 14px 0;
        }
        .chart-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 16px;
        }
        .chart-card {
            display: grid;
            gap: 14px;
        }
        .chart-header {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: baseline;
        }
        .chart-stat {
            font-size: 28px;
            font-weight: 750;
        }
        .chart-shell {
            border-radius: 14px;
            border: 1px solid rgba(148, 163, 184, 0.1);
            background: rgba(15, 23, 42, 0.5);
            padding: 10px;
        }
        .chart-shell svg {
            width: 100%;
            height: 180px;
            display: block;
        }
        .chart-empty {
            min-height: 180px;
            display: grid;
            place-items: center;
            color: var(--muted);
            font-size: 13px;
        }
        .chart-legend {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            color: var(--muted);
            font-size: 12px;
        }
        .summary-grid {
            display: grid;
            grid-template-columns: 1.2fr 0.8fr;
            gap: 16px;
            margin-bottom: 16px;
        }
        .summary-box {
            display: grid;
            gap: 12px;
        }
        .health-grid {
            display: grid;
            grid-template-columns: 0.8fr 1.2fr;
            gap: 16px;
            margin-bottom: 16px;
        }
        .health-score {
            display: grid;
            gap: 10px;
            align-content: start;
        }
        .health-number {
            font-size: 64px;
            font-weight: 800;
            line-height: 0.95;
        }
        .health-label {
            font-size: 16px;
            font-weight: 700;
        }
        .health-bar {
            height: 12px;
            border-radius: 999px;
            background: rgba(148, 163, 184, 0.14);
            overflow: hidden;
        }
        .health-bar-fill {
            height: 100%;
            width: 0%;
            transition: width 0.3s ease;
        }
        .stuck-box {
            display: grid;
            gap: 12px;
        }
        .stuck-title {
            font-size: 20px;
            font-weight: 760;
        }
        .stuck-state {
            display: inline-flex;
            width: fit-content;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 12px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            border: 1px solid rgba(148, 163, 184, 0.14);
        }
        .stuck-yes { background: rgba(248, 113, 113, 0.14); color: #fecaca; }
        .stuck-no { background: rgba(34, 197, 94, 0.14); color: #b7f7cb; }
        .summary-callout {
            padding: 14px 16px;
            border-radius: 14px;
            border: 1px solid rgba(148, 163, 184, 0.12);
            background: rgba(15, 23, 42, 0.55);
        }
        .summary-callout strong {
            display: block;
            margin-bottom: 6px;
            font-size: 12px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .fact-list {
            display: grid;
            gap: 8px;
        }
        .fact-pill {
            padding: 10px 12px;
            border-radius: 12px;
            background: rgba(96, 165, 250, 0.08);
            border: 1px solid rgba(96, 165, 250, 0.12);
            color: var(--text);
            font-size: 13px;
        }
        .chart-note {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
        }
        .legend-dot {
            width: 10px;
            height: 10px;
            border-radius: 999px;
            display: inline-block;
            margin-right: 6px;
        }
        .footer-note { color: var(--muted); font-size: 12px; margin-top: 12px; }
        @media (max-width: 1100px) {
            .layout, .split, .chart-grid, .summary-grid, .health-grid { grid-template-columns: 1fr; }
            .hero { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="hero">
            <div>
                <h1>🦆 Duck Agent Observatory</h1>
                <p>Real-time execution telemetry, debugging signals, and post-mortem analysis for the local duck agent.</p>
                <div class="badges">
                    <span class="badge status-badge" id="status-badge">Connecting</span>
                    <span class="badge" id="last-event-badge">No events yet</span>
                    <span class="badge" id="log-dir-badge">agent_logs</span>
                </div>
            </div>
            <div class="card" style="min-width: 280px;">
                <div class="section-title">Realtime Pulse</div>
                <div class="value"><span id="pulse-events">0</span> events in the last hour</div>
                <div class="metric-sub" id="pulse-command">Latest command: -</div>
                <div class="metric-sub" id="pulse-test">Latest test: -</div>
                <div class="metric-sub" id="pulse-error">Latest error: none</div>
            </div>
        </div>

        <div class="metrics">
            <div class="card metric-card">
                <h3>Status</h3>
                <div class="metric" id="metric-status">-</div>
                <div class="metric-sub" id="metric-status-sub">Awaiting data</div>
            </div>
            <div class="card metric-card">
                <h3>Iteration</h3>
                <div class="metric" id="metric-iteration">0</div>
                <div class="metric-sub" id="metric-action">waiting</div>
            </div>
            <div class="card metric-card">
                <h3>Tests</h3>
                <div class="metric" id="metric-tests">0 / 0</div>
                <div class="metric-sub" id="metric-tests-sub">No runs yet</div>
                <div class="progress"><div id="tests-progress"></div></div>
            </div>
            <div class="card metric-card">
                <h3>Actions</h3>
                <div class="metric" id="metric-actions">0</div>
                <div class="metric-sub" id="metric-actions-sub">0 files · 0 commands</div>
            </div>
            <div class="card metric-card">
                <h3>Sessions</h3>
                <div class="metric" id="metric-sessions">0</div>
                <div class="metric-sub" id="metric-session-sub">No session data</div>
            </div>
            <div class="card metric-card">
                <h3>Errors</h3>
                <div class="metric" id="metric-errors">0</div>
                <div class="metric-sub" id="metric-errors-sub">No incidents</div>
            </div>
        </div>

        <div class="health-grid">
            <div class="card health-score">
                <div class="section-title">Agent health score</div>
                <div class="health-number" id="health-score">0</div>
                <div class="health-label" id="health-label">Unknown</div>
                <div class="health-bar"><div class="health-bar-fill" id="health-bar-fill"></div></div>
                <div class="metric-sub" id="health-next-action">No recommendation yet.</div>
                <div class="list" id="health-reasons"></div>
            </div>
            <div class="card stuck-box">
                <div class="section-title">Why the agent is stuck</div>
                <span class="stuck-state stuck-no" id="stuck-state">Not stuck</span>
                <div class="stuck-title" id="stuck-title">No strong stuck signal detected</div>
                <div class="value" id="stuck-reason">The dashboard has not found a hard stuck pattern yet.</div>
                <div class="list" id="stuck-evidence"></div>
            </div>
        </div>

        <div class="summary-grid">
            <div class="card summary-box">
                <div class="section-title">What a human should know right now</div>
                <div class="summary-callout">
                    <strong>Headline</strong>
                    <div class="value" id="human-headline">Waiting for interpretation.</div>
                </div>
                <div class="summary-callout">
                    <strong>Quality snapshot</strong>
                    <div class="value" id="human-quality">No quality signal yet.</div>
                </div>
                <div class="summary-callout">
                    <strong>Main bottleneck</strong>
                    <div class="value" id="human-bottleneck">No bottleneck detected yet.</div>
                </div>
                <div class="summary-callout">
                    <strong>Best next step</strong>
                    <div class="value" id="human-next-step">Watch for new activity.</div>
                </div>
            </div>
            <div class="card summary-box">
                <div class="section-title">Quick facts</div>
                <div class="fact-list" id="human-facts"></div>
                <div class="summary-callout">
                    <strong>Chart takeaway</strong>
                    <div class="value" id="human-chart-takeaway">No chart trend yet.</div>
                </div>
            </div>
        </div>

        <div class="chart-grid">
            <div class="card chart-card">
                <div class="chart-header">
                    <div>
                        <div class="section-title">Test progression</div>
                        <div class="metric-sub">Pass rate across recent scoreboard runs</div>
                    </div>
                    <div class="chart-stat" id="chart-test-stat">0%</div>
                </div>
                <div class="chart-shell" id="chart-test-progress"></div>
                <div class="chart-legend"><span><span class="legend-dot" style="background: var(--cyan);"></span>test pass percentage</span></div>
                <div class="chart-note" id="chart-test-note">This graph shows whether recent test runs are improving or staying flat.</div>
            </div>
            <div class="card chart-card">
                <div class="chart-header">
                    <div>
                        <div class="section-title">Session throughput</div>
                        <div class="metric-sub">Actions completed per recent session</div>
                    </div>
                    <div class="chart-stat" id="chart-session-stat">0</div>
                </div>
                <div class="chart-shell" id="chart-session-velocity"></div>
                <div class="chart-legend"><span><span class="legend-dot" style="background: var(--violet);"></span>actions per session</span></div>
                <div class="chart-note" id="chart-session-note">This graph shows how much useful work each recent session produced.</div>
            </div>
            <div class="card chart-card">
                <div class="chart-header">
                    <div>
                        <div class="section-title">Hourly activity</div>
                        <div class="metric-sub">How much the agent produced over time</div>
                    </div>
                    <div class="chart-stat" id="chart-activity-stat">0</div>
                </div>
                <div class="chart-shell" id="chart-activity-heat"></div>
                <div class="chart-legend"><span><span class="legend-dot" style="background: var(--blue);"></span>events per hour</span></div>
                <div class="chart-note" id="chart-activity-note">This graph shows when the agent was busiest.</div>
            </div>
            <div class="card chart-card">
                <div class="chart-header">
                    <div>
                        <div class="section-title">Command outcomes</div>
                        <div class="metric-sub">Verification health across command and test execution</div>
                    </div>
                    <div class="chart-stat" id="chart-command-stat">0%</div>
                </div>
                <div class="chart-shell" id="chart-command-outcomes"></div>
                <div class="chart-legend"><span><span class="legend-dot" style="background: var(--green);"></span>success</span><span><span class="legend-dot" style="background: var(--red);"></span>failure</span></div>
                <div class="chart-note" id="chart-command-note">This graph shows how often verification-style commands succeed versus fail.</div>
            </div>
        </div>

        <div class="layout">
            <div class="stack">
                <div class="card">
                    <div class="section-title">Current action</div>
                    <div class="eyebrow">Reasoning</div>
                    <div class="value" id="current-reason">Waiting for agent activity.</div>
                    <div class="footer-note mono" id="current-meta">action=waiting</div>
                </div>

                <div class="card">
                    <div class="section-title">Live timeline</div>
                    <div class="timeline" id="timeline"></div>
                </div>
            </div>

            <div class="stack">
                <div class="card">
                    <div class="section-title">Post-mortem recommendations</div>
                    <div class="list" id="recommendations"></div>
                </div>

                <div class="card">
                    <div class="section-title">Latest session</div>
                    <div class="list" id="latest-session"></div>
                </div>

                <div class="card">
                    <div class="section-title">Action breakdown</div>
                    <div class="list" id="action-breakdown"></div>
                </div>
            </div>
        </div>

        <div class="split">
            <div class="card">
                <div class="section-title">File hotspots</div>
                <div class="list" id="top-files"></div>
            </div>
            <div class="card">
                <div class="section-title">Error clusters</div>
                <div class="list" id="error-clusters"></div>
            </div>
        </div>

        <div class="split" style="margin-top: 16px;">
            <div class="card">
                <div class="section-title">Recent sessions</div>
                <div class="list" id="recent-sessions"></div>
            </div>
            <div class="card">
                <div class="section-title">Prompt pressure</div>
                <div class="list" id="prompt-metrics"></div>
            </div>
        </div>
    </div>

    <script>
        const ws = new WebSocket('ws://' + __WS_URL__);

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text == null ? '' : String(text);
            return div.innerHTML;
        }

        function formatDuration(seconds) {
            if (seconds == null) return '-';
            if (seconds < 60) return `${seconds}s`;
            const minutes = Math.floor(seconds / 60);
            const remainder = seconds % 60;
            if (minutes < 60) return `${minutes}m ${remainder}s`;
            const hours = Math.floor(minutes / 60);
            return `${hours}h ${minutes % 60}m`;
        }

        function statusClass(status) {
            return `status-${status || 'waiting'}`;
        }

        function setStatus(status, subtitle) {
            const badge = document.getElementById('status-badge');
            badge.className = `badge status-badge ${statusClass(status)}`;
            badge.textContent = (status || 'waiting').toUpperCase();
            document.getElementById('metric-status').textContent = status || '-';
            document.getElementById('metric-status-sub').textContent = subtitle;
        }

        function renderKeyValueList(id, items, emptyText, formatter) {
            const root = document.getElementById(id);
            if (!items || items.length === 0) {
                root.innerHTML = `<div class="item"><div class="item-meta">${escapeHtml(emptyText)}</div></div>`;
                return;
            }
            root.innerHTML = items.map((item, index) => formatter(item, index)).join('');
        }

        function renderLineChart(id, points, color, formatter) {
            const root = document.getElementById(id);
            if (!points || points.length === 0) {
                root.innerHTML = '<div class="chart-empty">No chart data yet.</div>';
                return;
            }
            const width = 640;
            const height = 180;
            const padding = 18;
            const values = points.map(point => Number(point.value || 0));
            const maxValue = Math.max(...values, 1);
            const xStep = points.length === 1 ? 0 : (width - padding * 2) / (points.length - 1);
            const coords = points.map((point, index) => {
                const x = padding + index * xStep;
                const y = height - padding - ((Number(point.value || 0) / maxValue) * (height - padding * 2));
                return { x, y, point };
            });
            const polyline = coords.map(coord => `${coord.x},${coord.y}`).join(' ');
            const area = [`${padding},${height - padding}`, ...coords.map(coord => `${coord.x},${coord.y}`), `${coords[coords.length - 1].x},${height - padding}`].join(' ');
            root.innerHTML = `
                <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
                    <defs>
                        <linearGradient id="${id}-fill" x1="0" x2="0" y1="0" y2="1">
                            <stop offset="0%" stop-color="${color}" stop-opacity="0.35"></stop>
                            <stop offset="100%" stop-color="${color}" stop-opacity="0.02"></stop>
                        </linearGradient>
                    </defs>
                    <polygon points="${area}" fill="url(#${id}-fill)"></polygon>
                    <polyline points="${polyline}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></polyline>
                    ${coords.map(coord => `<circle cx="${coord.x}" cy="${coord.y}" r="4" fill="${color}"><title>${escapeHtml(formatter(coord.point))}</title></circle>`).join('')}
                </svg>
            `;
        }

        function renderBarChart(id, points, colorForPoint, formatter) {
            const root = document.getElementById(id);
            if (!points || points.length === 0) {
                root.innerHTML = '<div class="chart-empty">No chart data yet.</div>';
                return;
            }
            const width = 640;
            const height = 180;
            const padding = 18;
            const gap = 10;
            const values = points.map(point => Number(point.value || 0));
            const maxValue = Math.max(...values, 1);
            const barWidth = Math.max(18, ((width - padding * 2) - (gap * (points.length - 1))) / points.length);
            const bars = points.map((point, index) => {
                const value = Number(point.value || 0);
                const barHeight = (value / maxValue) * (height - padding * 2);
                const x = padding + index * (barWidth + gap);
                const y = height - padding - barHeight;
                const color = colorForPoint(point, index);
                return `<g><rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="8" fill="${color}"><title>${escapeHtml(formatter(point))}</title></rect></g>`;
            }).join('');
            root.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${bars}</svg>`;
        }

        function updateDashboard(state) {
            const overview = state.overview || {};
            const iteration = state.iteration || {};
            const tests = state.tests || {};
            const metrics = state.metrics || {};
            const errors = state.errors || {};
            const sessions = state.sessions || {};
            const realtime = state.realtime || {};
            const prompts = state.prompts || {};
            const commands = state.commands || {};
            const performance = state.performance || {};
            const human = state.human || {};
            const health = state.health || {};

            const since = overview.seconds_since_event == null
                ? 'No events yet'
                : `Last event ${formatDuration(overview.seconds_since_event)} ago`;

            setStatus(overview.status, since);
            document.getElementById('last-event-badge').textContent = since;
            document.getElementById('log-dir-badge').textContent = overview.log_dir || 'agent_logs';

            document.getElementById('metric-iteration').textContent = iteration.iteration ?? 0;
            document.getElementById('metric-action').textContent = iteration.current_action || 'waiting';
            document.getElementById('current-reason').textContent = iteration.reason || 'No action reason available.';
            document.getElementById('current-meta').textContent = `action=${iteration.current_action || 'waiting'} · last=${iteration.last_event_time || '-'}`;

            const testTotal = tests.total || 0;
            const testPassed = tests.passed || 0;
            document.getElementById('metric-tests').textContent = `${testPassed} / ${testTotal}`;
            document.getElementById('metric-tests-sub').textContent = tests.last_command
                ? `exit ${tests.last_exit_code ?? '-'} · ${tests.last_command}`
                : (tests.status || 'pending');
            document.getElementById('tests-progress').style.width = `${tests.percentage || 0}%`;

            document.getElementById('metric-actions').textContent = metrics.actions_total || 0;
            document.getElementById('metric-actions-sub').textContent = `${metrics.files_written || 0} files · ${metrics.commands_run || 0} commands`;

            document.getElementById('metric-sessions').textContent = sessions.session_count || 0;
            document.getElementById('metric-session-sub').textContent = sessions.latest_session
                ? `Latest lasted ${formatDuration(sessions.latest_session.duration_seconds)}`
                : 'No session data';

            document.getElementById('metric-errors').textContent = errors.total || 0;
            document.getElementById('metric-errors-sub').textContent = errors.latest || 'No incidents';

            document.getElementById('pulse-events').textContent = realtime.events_last_hour || 0;
            document.getElementById('pulse-command').textContent = `Latest command: ${realtime.latest_command || '-'}`;
            document.getElementById('pulse-test').textContent = `Latest test: ${realtime.latest_test || '-'}`;
            document.getElementById('pulse-error').textContent = `Latest error: ${realtime.latest_error || 'none'}`;

            const healthScore = health.score ?? 0;
            const healthColor = health.color || 'yellow';
            document.getElementById('health-score').textContent = healthScore;
            document.getElementById('health-score').className = `health-number health-${healthColor}`;
            document.getElementById('health-label').textContent = health.label || 'Unknown';
            document.getElementById('health-label').className = `health-label health-${healthColor}`;
            document.getElementById('health-bar-fill').style.width = `${healthScore}%`;
            document.getElementById('health-bar-fill').style.background = healthColor === 'green'
                ? 'linear-gradient(90deg, #22c55e, #86efac)'
                : healthColor === 'yellow'
                    ? 'linear-gradient(90deg, #f59e0b, #fde68a)'
                    : 'linear-gradient(90deg, #ef4444, #fca5a5)';
            document.getElementById('health-next-action').textContent = health.next_action || 'No recommendation yet.';

            renderKeyValueList('health-reasons', [...(health.reasons || []), ...(health.warnings || [])], 'No health issues detected.', (item) => `
                <div class="item"><div class="item-meta">${escapeHtml(item)}</div></div>
            `);

            const stuckState = document.getElementById('stuck-state');
            stuckState.textContent = health.stuck ? 'Stuck' : 'Not stuck';
            stuckState.className = `stuck-state ${health.stuck ? 'stuck-yes' : 'stuck-no'}`;
            document.getElementById('stuck-title').textContent = health.stuck_title || 'No strong stuck signal detected';
            document.getElementById('stuck-reason').textContent = health.stuck_reason || 'No stuck reason available.';
            renderKeyValueList('stuck-evidence', health.stuck_evidence || [], 'No evidence yet.', (item) => `
                <div class="item"><div class="item-meta">${escapeHtml(item)}</div></div>
            `);

            document.getElementById('human-headline').textContent = human.headline || 'No summary available yet.';
            document.getElementById('human-quality').textContent = human.quality || 'No quality signal yet.';
            document.getElementById('human-bottleneck').textContent = human.bottleneck || 'No bottleneck detected yet.';
            document.getElementById('human-next-step').textContent = human.next_step || 'Keep watching the dashboard.';
            document.getElementById('human-chart-takeaway').textContent = human.chart_takeaway || 'No chart trend yet.';

            renderKeyValueList('human-facts', human.facts || [], 'No facts yet.', (item) => `
                <div class="fact-pill">${escapeHtml(item)}</div>
            `);

            document.getElementById('chart-test-stat').textContent = `${performance.headline?.test_best || 0}%`;
            document.getElementById('chart-session-stat').textContent = `${performance.headline?.session_peak || 0}`;
            document.getElementById('chart-activity-stat').textContent = `${performance.headline?.activity_peak || 0}`;
            document.getElementById('chart-command-stat').textContent = `${performance.headline?.command_success_rate || 0}%`;

            const testTrend = performance.test_progress || [];
            const sessionTrend = performance.session_velocity || [];
            const activityTrend = performance.activity_heat || [];
            const commandTrend = performance.command_outcomes || [];

            document.getElementById('chart-test-note').textContent = testTrend.length >= 2
                ? `Recent visible run trend: ${testTrend[0].value}% → ${testTrend[testTrend.length - 1].value}%.`
                : 'Need at least two scored test runs before a trend is visible.';
            document.getElementById('chart-session-note').textContent = sessionTrend.length
                ? `Best recent session produced ${Math.max(...sessionTrend.map(point => point.value || 0))} actions.`
                : 'Session throughput will appear once multiple sessions have been logged.';
            document.getElementById('chart-activity-note').textContent = activityTrend.length
                ? `Busiest visible hour: ${activityTrend.reduce((best, point) => (point.value > best.value ? point : best), activityTrend[0]).label}.`
                : 'Hourly activity will appear once logs accumulate over time.';
            document.getElementById('chart-command-note').textContent = commandTrend.length
                ? `Commands currently succeed ${performance.headline?.command_success_rate || 0}% of the time.`
                : 'Command health will appear once command logs are available.';

            renderLineChart(
                'chart-test-progress',
                performance.test_progress || [],
                '#22d3ee',
                point => `${point.label}: ${point.value}% (${point.passed}/${point.total})`
            );
            renderBarChart(
                'chart-session-velocity',
                performance.session_velocity || [],
                () => '#a78bfa',
                point => `${point.label}: ${point.value} actions, ${point.iterations} iterations, ${formatDuration(point.duration_seconds)}`
            );
            renderLineChart(
                'chart-activity-heat',
                performance.activity_heat || [],
                '#60a5fa',
                point => `${point.label}: ${point.value} events`
            );
            renderBarChart(
                'chart-command-outcomes',
                performance.command_outcomes || [],
                point => point.label === 'success' ? '#22c55e' : '#f87171',
                point => `${point.label}: ${point.value}`
            );

            renderKeyValueList('recommendations', sessions.recommendations, 'No recommendations yet.', (item) => `
                <div class="recommendation">${escapeHtml(item)}</div>
            `);

            const latestSession = sessions.latest_session ? [sessions.latest_session] : [];
            renderKeyValueList('latest-session', latestSession, 'No completed session yet.', (item) => `
                <div class="item">
                    <div class="item-row"><div class="item-title">Started ${escapeHtml(item.started_at || '-')}</div><div class="item-meta">${formatDuration(item.duration_seconds)}</div></div>
                    <div class="item-meta">iterations=${escapeHtml(item.iterations || 0)} · actions=${escapeHtml(item.actions || 0)} · writes=${escapeHtml(item.writes || 0)} · commands=${escapeHtml(item.commands || 0)} · tests=${escapeHtml(item.tests || 0)}</div>
                    <div class="value" style="margin-top:8px; font-size:14px;">${escapeHtml(item.stop_reason || 'No stop reason')}</div>
                </div>
            `);

            renderKeyValueList('action-breakdown', Object.entries(metrics.action_breakdown || {}), 'No action data yet.', ([label, count]) => `
                <div class="item"><div class="item-row"><div class="item-title mono">${escapeHtml(label)}</div><div>${escapeHtml(count)}</div></div></div>
            `);

            renderKeyValueList('top-files', metrics.top_files, 'No files written yet.', (item) => `
                <div class="item"><div class="item-row"><div class="item-title mono">${escapeHtml(item.path)}</div><div>${escapeHtml(item.count)}</div></div></div>
            `);

            renderKeyValueList('error-clusters', errors.categories, 'No clustered errors.', (item) => `
                <div class="item">
                    <div class="item-row"><div class="item-title">${escapeHtml(item.label)}</div><div>${escapeHtml(item.count)}</div></div>
                </div>
            `);

            renderKeyValueList('recent-sessions', sessions.recent_sessions, 'No prior sessions.', (item) => `
                <div class="item">
                    <div class="item-row"><div class="item-title">${escapeHtml(item.started_at || '-')}</div><div class="item-meta">${formatDuration(item.duration_seconds)}</div></div>
                    <div class="item-meta">iterations=${escapeHtml(item.iterations || 0)} · actions=${escapeHtml(item.actions || 0)}</div>
                    <div class="value" style="margin-top:8px; font-size:14px;">${escapeHtml(item.stop_reason || 'No stop reason')}</div>
                </div>
            `);

            renderKeyValueList('prompt-metrics', [
                { label: 'prompt entries', value: prompts.entries || 0 },
                { label: 'prompt bytes', value: prompts.bytes || 0 },
                { label: 'avg bytes / prompt', value: prompts.avg_bytes || 0 },
                { label: 'command success', value: `${commands.success_rate || 0}%` },
            ], 'No prompt metrics.', (item) => `
                <div class="item"><div class="item-row"><div class="item-title">${escapeHtml(item.label)}</div><div>${escapeHtml(item.value)}</div></div></div>
            `);

            const timelineItems = (state.recent_logs && state.recent_logs.combined) || [];
            const timeline = document.getElementById('timeline');
            if (!timelineItems.length) {
                timeline.innerHTML = '<div class="item"><div class="item-meta">No activity yet.</div></div>';
            } else {
                timeline.innerHTML = timelineItems.slice(-30).reverse().map((item) => `
                    <div class="timeline-entry ${escapeHtml(item.type || 'decision')}">
                        <div class="timeline-head">
                            <span>${escapeHtml(item.timestamp)}</span>
                            <span class="pill">${escapeHtml(item.type || item.category || 'event')}</span>
                        </div>
                        <div class="value mono" style="font-size:13px;">${escapeHtml(item.message)}</div>
                    </div>
                `).join('');
            }
        }

        ws.onopen = () => {
            setStatus('running', 'Connected to dashboard stream');
        };

        ws.onmessage = (event) => {
            try {
                updateDashboard(JSON.parse(event.data));
            } catch (error) {
                console.error('Failed to parse dashboard event:', error);
            }
        };

        ws.onerror = () => {
            setStatus('error', 'WebSocket connection error');
        };

        ws.onclose = () => {
            setStatus('stopped', 'WebSocket disconnected');
        };
    </script>
</body>
</html>
"""

    return html.replace("__WS_URL__", ws_url)


class HTTPHandler(SimpleHTTPRequestHandler):
    """Serve the dashboard page."""

    ws_port = 8081

    def do_GET(self):
        if self.path in {"/", "/index.html"}:
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = get_html_page(f"window.location.hostname + ':{self.ws_port}'")
            self.wfile.write(html.encode())
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(format % args)


async def run_servers(http_port: int, ws_port: int, log_dir: Path):
    """Run both HTTP and WebSocket servers."""
    HTTPHandler.ws_port = ws_port
    try:
        http_server = HTTPServer(("0.0.0.0", http_port), HTTPHandler)
    except OSError as exc:
        raise OSError(f"HTTP port {http_port} is unavailable. Choose another with --port.") from exc
    logger.info("HTTP server started on http://localhost:%s", http_port)

    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()

    dashboard = DashboardServer(log_dir, host="0.0.0.0", port=ws_port)
    try:
        await dashboard.start()
    finally:
        http_server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Duck Agent Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--ws-port", type=int, default=8081, help="WebSocket port (default: 8081)")
    parser.add_argument("--log-dir", type=str, default="agent_logs", help="Agent logs directory")

    args = parser.parse_args()
    log_dir = Path(args.log_dir)

    if not log_dir.exists():
        logger.error("Log directory not found: %s", log_dir)
        raise SystemExit(1)

    try:
        logger.info("Open dashboard in browser: http://localhost:%s", args.port)
        logger.info("Internal WebSocket endpoint: ws://localhost:%s", args.ws_port)
        asyncio.run(run_servers(args.port, args.ws_port, log_dir))
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")
    except OSError as exc:
        logger.error(str(exc))
