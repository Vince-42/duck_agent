# Duck Agent Dashboard

Real-time web-based monitoring interface for autonomous agent execution.

## Quick Start

### Install Dependencies
```bash
pip install websockets
```

### Start Dashboard
```bash
# Terminal 1: Start the dashboard server
cd /Users/vince/opencode/transfert/duck_agent
python3 dashboard.py --port 8080 --ws-port 8081

# Then open in browser:
# http://localhost:8080
```

### Run Agent (while dashboard is running)
```bash
# Terminal 2: Run agent normally
python3 agent.py --spec-path ../hackathonMay/QUICK_START_AGENT_BUILD.md
# or
python3 agent.py --spec-path ../hackathonMay/QUICK_START_AGENT_BUILD.md --single-agent --max-iterations 10
```

The dashboard will automatically detect new logs and update in real-time.

---

## What the Dashboard Shows

### Top Metrics
- **Iteration**: Current iteration number
- **Test Status**: Passed / Total tests with progress bar
- **Actions**: Total files written + commands run

### Current Action Card
- **Type**: WRITE_FILE, RUN_COMMAND, RUN_TESTS, or STOP
- **Reason**: Why the agent chose this action

### Recent Activity Log
- Live feed of decisions, test results, and errors
- Color-coded by log type:
  - 🔵 Blue = decisions
  - 🔴 Red = errors
  - 🔷 Cyan = test results

---

## Features

✅ **Real-time streaming** - WebSocket-based live updates (no polling lag)  
✅ **Log aggregation** - All 5 log types displayed in one feed  
✅ **Test tracking** - See pass/fail rate update as tests run  
✅ **Progress visibility** - Know exactly which iteration you're on  
✅ **Zero setup** - Reads existing agent logs, no code changes needed  
✅ **Lightweight** - Single Python file, minimal dependencies  

---

## Advanced Usage

### Change Poll Interval
Edit `dashboard.py` line 181:
```python
self.poll_interval = 2  # Change to 1 for faster updates (uses more CPU)
```

### Monitor Remote Agent
```bash
# If agent is on another machine, use:
python3 dashboard.py --port 0.0.0.0:8080 --log-dir /path/to/remote/agent_logs
```

### Run Multiple Agents
The dashboard can monitor only ONE agent at a time (since it reads from one `agent_logs/` directory).
For multiple agents, run multiple dashboard instances:
```bash
python3 dashboard.py --port 8080 --log-dir ./agent_1/logs
python3 dashboard.py --port 8081 --log-dir ./agent_2/logs
```

---

## Troubleshooting

### Dashboard shows "Connecting to agent logs..." but nothing updates
- **Check**: Is agent running? `ps aux | grep agent.py`
- **Check**: Are logs being written? `tail -f agent_logs/decisions.log`
- **Check**: Is log directory accessible? `ls -la agent_logs/`

### Port already in use
```bash
# Find process using port 8080
lsof -i :8080

# Kill it
kill -9 <PID>

# Or use a different port
python3 dashboard.py --port 9000
```

### WebSocket connection refused
- Make sure you're accessing `http://localhost:8080`, not `https://`
- Check firewall settings
- Try restarting the dashboard server

---

## Architecture

```
dashboard.py
├── AgentLogParser
│   ├── get_new_logs(category) → parse log files incrementally
│   ├── parse_iteration_status() → extract current iteration
│   ├── parse_test_status() → extract pass/fail counts
│   └── parse_file_metrics() → count actions
├── DashboardServer
│   ├── broadcast_update() → continuously poll logs
│   ├── handler() → WebSocket client management
│   └── start() → launch server
└── HTML/JavaScript Frontend
    └── Real-time DOM updates via WebSocket
```

**Why WebSocket instead of polling from browser?**
- Server controls update rate (no browser hammering)
- File seeking is more efficient server-side
- Lower latency (2s broadcast interval vs client polling)
- Works without browser storage permissions

---

## Future Enhancements

- [ ] Manual controls: Pause/Resume agent
- [ ] Send messages to agent (inject prompts)
- [ ] Specification progress tracking
- [ ] Orchestrator role status (see which roles are active)
- [ ] Token usage tracking
- [ ] Session history (compare runs)
- [ ] Export to CSV/JSON

---

## Metrics Explained

| Metric | Meaning |
|--------|---------|
| Iteration | How many times has the agent looped? |
| Test Status | Out of all tests, how many pass? |
| Actions | Total file writes + shell commands executed |
| Current Action | What is the agent doing RIGHT NOW? |
| Recent Activity | Last 15 log entries across all categories |

---

## Tips for Debugging with Dashboard

### Detecting early failure (like iteration count stuck at 1)
1. Watch "Iteration" metric - should increment every ~30-60 seconds
2. If stuck, check "Current Action" - what is it doing?
3. Look at "Recent Activity" for errors

### Detecting test regression
1. Watch "Test Status" bar - should trend upward (green)
2. If it stalls, agent may be in a loop
3. Check "Reason" for latest action - why did it stop fixing?

### Detecting infinite loops
1. Watch "Actions" count - should keep increasing
2. If the same action appears repeatedly, agent may be stuck
3. Dashboard will show repeated WRITE_FILE/RUN_COMMAND actions

---

## Example Session

**Time 14:02:00** - Agent starts
- Iteration: 0
- Test Status: 0/150
- Recent: "Starting agent loop"

**Time 14:05:15** - First file written
- Iteration: 1
- Test Status: 0/150
- Recent: "WRITE_FILE solution.py (234 lines)"

**Time 14:05:45** - Test run
- Iteration: 2
- Test Status: 5/150 ✓ (progress!)
- Recent: "RUN_TESTS: 5 passed, 145 failed"

**Time 14:06:30** - Spec comprehension phase
- Iteration: 3
- Test Status: 5/150
- Recent: "WRITE_FILE: Added knitting pattern parser"

... agent continues until tests pass or max iterations ...

---

## License

Same as duck_agent. MIT or whatever the parent project uses.
