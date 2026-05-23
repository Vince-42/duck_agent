# 🦆 Dashboard Quick Start

## 30-Second Setup

```bash
# Terminal 1: Dashboard
python3 dashboard.py

# Terminal 2: Agent (in parallel)
python3 agent.py --spec-path examples/demo_spec.md
```

Then open **http://localhost:8080** in browser.

## What You'll See

| Component | Shows |
|-----------|-------|
| **Status Badge** | 🟢 Green = Connected, 🔴 Red = Disconnected |
| **Iteration** | Current loop count |
| **Test Status** | Passed/Total with progress bar |
| **Actions** | Number of files written + commands run |
| **Current Action** | What the agent is doing RIGHT NOW |
| **Recent Activity** | Live colored log feed |

## Log Colors

- 🔵 **Blue** = Decisions (what the agent decided to do)
- 🔴 **Red** = Errors (something went wrong)
- 🔷 **Cyan** = Tests (test results and validation)

## Customization

```bash
# Custom ports
python3 dashboard.py --port 9000 --ws-port 9001

# Remote agent logs
python3 dashboard.py --log-dir /path/to/other/agent_logs

# Combined
python3 dashboard.py --port 9000 --log-dir agent_logs
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Connecting..." but nothing updates | Check if agent is running: `ps aux \| grep agent.py` |
| Port already in use | Use different ports: `--port 9000 --ws-port 9001` |
| WebSocket disconnects | Page auto-reconnects. Check logs: `tail agent_logs/*.log` |
| No logs showing | Agent might not be writing yet. Wait a few seconds. |

## What the Dashboard Reads

From `agent_logs/`:
- `decisions.log` - Agent choices and actions
- `errors.log` - Errors and failures
- `test_runs.log` - Test execution results
- `commands.log` - Shell commands run
- `prompts.log` - LLM prompts (not displayed)

## Behind the Scenes

1. Dashboard polls log files every **2 seconds**
2. Reads new entries since last check (incremental)
3. Broadcasts JSON state to connected browsers via WebSocket
4. Browser renders metrics and updates log list
5. Total latency: ~2-3 seconds from agent action to dashboard display

## Advanced

### Multiple Agents
Each dashboard monitors ONE agent. For 2 agents, run 2 dashboards:

```bash
# Agent 1
python3 dashboard.py --port 8080 --log-dir agent_logs_1

# Agent 2  
python3 dashboard.py --port 8081 --log-dir agent_logs_2
```

Then:
- Agent 1: http://localhost:8080
- Agent 2: http://localhost:8081

### Change Poll Interval
Edit `dashboard.py` line 189:
```python
self.poll_interval = 1  # faster (uses more CPU)
self.poll_interval = 5  # slower (saves CPU)
```

### Debugging
Enable debug logs:
```bash
python3 -u dashboard.py 2>&1 | tee dashboard.log
```

---

**Questions?** See `DASHBOARD_README.md` for detailed documentation.
