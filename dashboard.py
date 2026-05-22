#!/usr/bin/env python3
"""
Duck Agent Dashboard Server

Live WebSocket server that streams agent logs and metrics in real-time.
Provides a web UI for monitoring agent execution.

Usage:
    python dashboard.py --port 8080 --log-dir agent_logs

Then open http://localhost:8080 in your browser.
"""

import asyncio
import json
import os
import re
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set
import logging

try:
    import websockets
    from websockets.asyncio.server import serve
except ImportError:
    print("ERROR: websockets not installed")
    print("  Install with: pip install websockets")
    exit(1)

try:
    from http.server import HTTPServer, SimpleHTTPRequestHandler
except ImportError:
    print("ERROR: http.server not available")
    exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AgentLogParser:
    """Parse agent log files and extract metrics."""
    
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_files = {
            "decisions": log_dir / "decisions.log",
            "prompts": log_dir / "prompts.log",
            "test_runs": log_dir / "test_runs.log",
            "errors": log_dir / "errors.log",
            "commands": log_dir / "commands.log",
        }
        self.file_positions = {name: 0 for name in self.log_files}
    
    def get_new_logs(self, category: str, limit: int = 100) -> List[Dict]:
        """Get new log entries since last read."""
        log_file = self.log_files.get(category)
        if not log_file or not log_file.exists():
            return []
        
        entries = []
        try:
            with open(log_file, 'r') as f:
                f.seek(self.file_positions[category])
                for line in f:
                    if line.strip():
                        # Parse timestamp: [2026-05-22 23:07:15]
                        match = re.match(r'\[([^\]]+)\]\s+(.*)', line)
                        if match:
                            timestamp, message = match.groups()
                            entries.append({
                                "timestamp": timestamp,
                                "message": message[:500],  # Truncate long messages
                                "category": category
                            })
                self.file_positions[category] = f.tell()
        except Exception as e:
            logger.error(f"Error reading {category}: {e}")
        
        return entries[-limit:]
    
    def parse_iteration_status(self) -> Dict:
        """Extract current iteration and status from decision log."""
        if not self.log_files["decisions"].exists():
            return {"iteration": 0, "status": "waiting"}
        
        try:
            with open(self.log_files["decisions"], 'r') as f:
                lines = f.readlines()
            
            iteration = 0
            action = "unknown"
            reason = ""
            
            for line in reversed(lines[-50:]):  # Check last 50 lines
                if "Starting agent loop" in line:
                    break
                if "Iteration" in line and "failed" in line:
                    match = re.search(r'Iteration (\d+)', line)
                    if match:
                        iteration = int(match.group(1))
                if "ACTION" in line:
                    parts = line.split("ACTION", 1)
                    if len(parts) > 1:
                        action_part = parts[1].strip()
                        if ":" in action_part:
                            action, reason = action_part.split(":", 1)
                            action = action.strip()
                            reason = reason.strip()[:100]
            
            return {
                "iteration": iteration,
                "status": "running",
                "current_action": action,
                "reason": reason
            }
        except Exception as e:
            logger.error(f"Error parsing iteration: {e}")
            return {"iteration": 0, "status": "error"}
    
    def parse_test_status(self) -> Dict:
        """Extract test pass/fail counts from test_runs log."""
        if not self.log_files["test_runs"].exists():
            return {"passed": 0, "failed": 0, "status": "not_run"}
        
        try:
            with open(self.log_files["test_runs"], 'r') as f:
                content = f.read()
            
            # Look for scoreboard pattern
            match = re.search(r'Overall \[.*?\] (\d+)/(\d+) passed', content)
            if match:
                passed = int(match.group(1))
                total = int(match.group(2))
                failed = total - passed
                return {
                    "passed": passed,
                    "failed": failed,
                    "total": total,
                    "percentage": (passed / total * 100) if total > 0 else 0,
                    "status": "completed"
                }
            
            return {"passed": 0, "failed": 0, "status": "pending"}
        except Exception as e:
            logger.error(f"Error parsing tests: {e}")
            return {"status": "error"}
    
    def parse_file_metrics(self) -> Dict:
        """Count written files and other artifacts."""
        try:
            write_count = 0
            run_count = 0
            
            if self.log_files["decisions"].exists():
                with open(self.log_files["decisions"], 'r') as f:
                    content = f.read()
                    write_count = len(re.findall(r'WRITE_FILE', content))
                    run_count = len(re.findall(r'RUN_COMMAND', content))
            
            return {
                "files_written": write_count,
                "commands_run": run_count,
                "actions_total": write_count + run_count
            }
        except Exception as e:
            logger.error(f"Error parsing metrics: {e}")
            return {}


class DashboardServer:
    """WebSocket server for dashboard updates."""
    
    def __init__(self, log_dir: Path, host: str = "localhost", port: int = 8080):
        self.log_dir = log_dir
        self.host = host
        self.port = port
        self.parser = AgentLogParser(log_dir)
        self.clients: Set = set()
        self.poll_interval = 2  # seconds
    
    async def broadcast_update(self):
        """Broadcast current state to all connected clients."""
        while True:
            try:
                state = {
                    "timestamp": datetime.now().isoformat(),
                    "iteration": self.parser.parse_iteration_status(),
                    "tests": self.parser.parse_test_status(),
                    "metrics": self.parser.parse_file_metrics(),
                    "recent_logs": {
                        "decisions": self.parser.get_new_logs("decisions", 20),
                        "errors": self.parser.get_new_logs("errors", 10),
                        "tests": self.parser.get_new_logs("test_runs", 10),
                    }
                }
                
                message = json.dumps(state)
                
                if self.clients:
                    # Broadcast to all connected clients
                    await asyncio.gather(
                        *[client.send(message) for client in self.clients.copy()],
                        return_exceptions=True
                    )
                
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"Broadcast error: {e}")
                await asyncio.sleep(1)
    
    async def handler(self, websocket, path):
        """Handle WebSocket connections."""
        self.clients.add(websocket)
        logger.info(f"Client connected. Total: {len(self.clients)}")
        
        try:
            async for message in websocket:
                # Echo back any received messages (keep-alive)
                pass
        except Exception as e:
            logger.error(f"Handler error: {e}")
        finally:
            self.clients.discard(websocket)
            logger.info(f"Client disconnected. Total: {len(self.clients)}")
    
    async def start(self):
        """Start the server."""
        logger.info(f"Dashboard starting on ws://{self.host}:{self.port}")
        
        # Start broadcast task
        broadcast_task = asyncio.create_task(self.broadcast_update())
        
        # Start WebSocket server
        async with await serve(self.handler, self.host, self.port):
            await broadcast_task


def get_html_page() -> str:
    """Generate the HTML dashboard page."""
    return """
<!DOCTYPE html>
<html>
<head>
    <title>🦆 Duck Agent Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #0f1419;
            color: #e0e6ed;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 {
            font-size: 28px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .status-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }
        .status-badge.running { background: #10b981; color: #fff; }
        .status-badge.error { background: #ef4444; color: #fff; }
        .status-badge.waiting { background: #6b7280; color: #fff; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: #1f2937;
            border: 1px solid #374151;
            border-radius: 8px;
            padding: 20px;
        }
        .card h3 {
            font-size: 14px;
            font-weight: 600;
            color: #9ca3af;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .metric {
            font-size: 32px;
            font-weight: 700;
            margin-bottom: 4px;
        }
        .metric-label {
            font-size: 12px;
            color: #9ca3af;
        }
        .progress-bar {
            width: 100%;
            height: 6px;
            background: #374151;
            border-radius: 3px;
            overflow: hidden;
            margin-top: 8px;
        }
        .progress-fill {
            height: 100%;
            background: #10b981;
            transition: width 0.3s;
        }
        .logs-container {
            background: #1f2937;
            border: 1px solid #374151;
            border-radius: 8px;
            padding: 20px;
            max-height: 400px;
            overflow-y: auto;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 12px;
            line-height: 1.6;
        }
        .log-entry {
            padding: 4px 0;
            border-bottom: 1px solid #374151;
        }
        .log-entry:last-child { border-bottom: none; }
        .log-time {
            color: #6b7280;
            min-width: 120px;
            display: inline-block;
        }
        .log-decision { color: #93c5fd; }
        .log-error { color: #fca5a5; }
        .log-test { color: #a5f3fc; }
        .action-info {
            background: #374151;
            border-left: 4px solid #10b981;
            padding: 12px;
            border-radius: 4px;
            margin-top: 12px;
        }
        .action-info .label {
            font-size: 12px;
            color: #9ca3af;
            margin-bottom: 4px;
        }
        .action-info .value {
            font-size: 14px;
            color: #e0e6ed;
        }
        .row { display: flex; gap: 20px; }
        .col { flex: 1; }
        @media (max-width: 768px) {
            .row { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            🦆 Duck Agent Dashboard
            <span class="status-badge running" id="status">Running</span>
        </h1>
        
        <div class="row">
            <div class="col">
                <div class="grid">
                    <div class="card">
                        <h3>Iteration</h3>
                        <div class="metric" id="iteration">0</div>
                        <div class="metric-label">Current / Total</div>
                    </div>
                    <div class="card">
                        <h3>Test Status</h3>
                        <div class="metric" id="test-passed">0</div>
                        <div class="metric-label">Passed / Total</div>
                        <div class="progress-bar">
                            <div class="progress-fill" id="test-progress" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="card">
                        <h3>Actions</h3>
                        <div class="metric" id="actions">0</div>
                        <div class="metric-label">Files + Commands</div>
                    </div>
                </div>
                
                <div class="card">
                    <h3>Current Action</h3>
                    <div class="action-info">
                        <div class="label">Type</div>
                        <div class="value" id="action-type">Waiting...</div>
                        <div class="label" style="margin-top: 8px;">Reason</div>
                        <div class="value" id="action-reason">-</div>
                    </div>
                </div>
            </div>
            
            <div class="col">
                <div class="card">
                    <h3>Recent Activity</h3>
                    <div class="logs-container" id="logs">
                        <div class="log-entry">
                            <span class="log-time">--:--:--</span>
                            <span>Connecting to agent logs...</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const ws = new WebSocket('ws://' + window.location.host);
        
        ws.onopen = () => {
            console.log('Connected to dashboard server');
            document.getElementById('status').textContent = 'Live';
            document.getElementById('status').style.background = '#10b981';
        };
        
        ws.onmessage = (event) => {
            try {
                const state = JSON.parse(event.data);
                updateDashboard(state);
            } catch (e) {
                console.error('Failed to parse message:', e);
            }
        };
        
        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            document.getElementById('status').textContent = 'Disconnected';
            document.getElementById('status').style.background = '#ef4444';
        };
        
        function updateDashboard(state) {
            // Update iteration
            if (state.iteration) {
                document.getElementById('iteration').textContent = state.iteration.iteration;
                document.getElementById('action-type').textContent = state.iteration.current_action;
                document.getElementById('action-reason').textContent = state.iteration.reason || '-';
            }
            
            // Update test status
            if (state.tests && state.tests.total) {
                const percentage = state.tests.percentage || 0;
                document.getElementById('test-passed').textContent = 
                    state.tests.passed + ' / ' + state.tests.total;
                document.getElementById('test-progress').style.width = percentage + '%';
            }
            
            // Update metrics
            if (state.metrics) {
                document.getElementById('actions').textContent = state.metrics.actions_total || 0;
            }
            
            // Update logs
            const allLogs = [];
            if (state.recent_logs) {
                if (state.recent_logs.decisions) allLogs.push(...state.recent_logs.decisions.map(l => ({...l, type: 'decision'})));
                if (state.recent_logs.tests) allLogs.push(...state.recent_logs.tests.map(l => ({...l, type: 'test'})));
                if (state.recent_logs.errors) allLogs.push(...state.recent_logs.errors.map(l => ({...l, type: 'error'})));
            }
            
            // Sort by timestamp and show latest
            allLogs.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
            const logsHtml = allLogs.slice(-15).map(log => {
                const cls = 'log-' + log.type;
                return `<div class="log-entry">
                    <span class="log-time">${log.timestamp.split(' ')[1] || '--:--:--'}</span>
                    <span class="${cls}">${escapeHtml(log.message)}</span>
                </div>`;
            }).join('');
            
            document.getElementById('logs').innerHTML = logsHtml;
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Auto-scroll logs to bottom
        const logsContainer = document.getElementById('logs');
        setInterval(() => {
            logsContainer.scrollTop = logsContainer.scrollHeight;
        }, 500);
    </script>
</body>
</html>
"""


class HTTPHandler(SimpleHTTPRequestHandler):
    """Simple HTTP handler that serves the dashboard HTML."""
    
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(get_html_page().encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        logger.info(format % args)


async def run_servers(http_port: int, ws_port: int, log_dir: Path):
    """Run both HTTP and WebSocket servers."""
    
    # HTTP server in a thread
    http_server = HTTPServer(('0.0.0.0', http_port), HTTPHandler)
    logger.info(f"HTTP server started on http://localhost:{http_port}")
    
    def run_http():
        try:
            http_server.handle_request()
        except KeyboardInterrupt:
            pass
    
    # Run HTTP server in thread
    import threading
    http_thread = threading.Thread(target=lambda: None)  # Placeholder
    
    # Run WebSocket server
    dashboard = DashboardServer(log_dir, host='0.0.0.0', port=ws_port)
    await dashboard.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Duck Agent Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--ws-port", type=int, default=8081, help="WebSocket port (default: 8081)")
    parser.add_argument("--log-dir", type=str, default="agent_logs", help="Agent logs directory")
    
    args = parser.parse_args()
    log_dir = Path(args.log_dir)
    
    if not log_dir.exists():
        logger.error(f"Log directory not found: {log_dir}")
        exit(1)
    
    try:
        # Run WebSocket server
        dashboard = DashboardServer(log_dir, host='0.0.0.0', port=args.ws_port)
        logger.info(f"Dashboard UI: http://localhost:{args.port}")
        logger.info(f"WebSocket: ws://localhost:{args.ws_port}")
        asyncio.run(dashboard.start())
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")
