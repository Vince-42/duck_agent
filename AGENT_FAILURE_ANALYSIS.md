# Agent Failure Analysis & Improvement Plan

## Executive Summary

Your agent was tasked with building a **knitting DSL compiler** (detailed 100+ line spec in `QUICK_START_AGENT_BUILD.md`). It completed in **1 iteration** with just a placeholder `solution.py` file. The real tests show **0/150 passing**.

**Root Cause:** The agent received the spec but prioritized early completion (via built-in validation heuristic) over actually reading and understanding the detailed requirements.

---

## What Went Wrong

### 1. **Spec Comprehension Failure**
- Agent wrote a stub `solution.py` with `if __name__ == '__main__': pass`
- Built-in validation passed (file exists, syntax valid)
- Agent decided task was complete and stopped

**Why:** The prompt didn't force deep spec reading. The agent's heuristic for "completion" was too shallow.

### 2. **Test Output Disconnection**
- Test runner showed **0/150 passing** with clear errors
- Agent never read test feedback to understand it was failing
- After validation passed, agent considered task "done"

**Why:** Agent uses context window to track `last_test_output`, but doesn't strongly incentivize fixing failures.

### 3. **Prompt Engineering Weakness**
The default prompt in `agent.py` line 580-628:
```python
template = textwrap.dedent("""\
    You are an autonomous coding agent working in a local repository.

    Task description:
    {task_description}
    ...
    Rules:
    - Prefer small targeted edits.
    - Do not invent requirements beyond the specification.
    - Use RUN_TESTS whenever validation is available after material changes or before STOP.
    ...
""")
```

**Issues:**
- Spec is chunked if too large (good), but each chunk only shows partial context
- "Prefer small targeted edits" can mean agent over-iterates on syntax instead of implementing logic
- Rules don't explicitly say "Read the ENTIRE spec first before writing code"
- No penalty for incomplete solutions

### 4. **Orchestrator Over-Activation**
- Agent uses multi-role orchestration mode (16 roles)
- Each role adds its own reasoning layer
- Result: More tokens used, sometimes contradictory guidance

**Evidence:**
```
[2026-05-21 18:49:02] ORCHESTRATION phase=Understand roles=Scout,Parser,Miner,Coordinator
[2026-05-21 18:49:02] ORCHESTRATION phase=Build roles=Architect,Builder,Minimalist,Coordinator
[2026-05-21 18:49:02] Stopping at iteration 0
```

Agent wrote 1 file and stopped immediately.

---

## Specific Test Failures

All 150 tests failed with:
```
exit code expected 0, got 2
stderr: "python3: can't open file '/home/vleroy/hackathon_duck/solution.py': [Errno 2] No such file or directory"
```

This is because `solution.py` exists locally but the test runner runs in a different context (mounted directory in their test environment).

The real failures would be JSON parsing, knitting DSL parsing, etc.

---

## Root Cause Hierarchy

| Priority | Issue | Impact | Evidence |
|---|---|---|---|
| **CRITICAL** | Agent doesn't deeply read spec before writing code | 1-iteration completion | Logs show agent wrote placeholder without understanding requirements |
| **CRITICAL** | Early completion heuristic too aggressive | Task marked "done" after built-in validation passes | `evaluate_primary_artifact_completion()` at line 934 |
| **HIGH** | Prompt doesn't explicitly require spec comprehension | Agent skips detailed reading | Default prompt template doesn't mandate requirements analysis |
| **HIGH** | No real-time visibility into what agent is doing | User finds out too late that something went wrong | Logs are files; no live dashboard |
| **MEDIUM** | Orchestrator adds complexity without forcing rigor | Agent may get contradictory guidance from 16 roles | Each role competes for attention |
| **MEDIUM** | Test feedback not strongly weighted in next-iteration decisions | Agent ignores test failures if built-in validation passed | State tracking doesn't penalize failed tests |

---

## Solutions (Priority Order)

### 1. **Fix Prompt to Force Spec Understanding** (Quick Fix)
Add an explicit "analysis phase" to the prompt:

```python
# Before writing ANY code, analyze the spec:
template = textwrap.dedent("""\
    You are an autonomous coding agent.

    CRITICAL: Before writing any code, you MUST analyze the specification.

    If this is your first iteration:
    1. Read the ENTIRE task description carefully
    2. Identify core requirements (what must the program do?)
    3. Identify constraints (size limits, runtime limits, input format)
    4. Identify edge cases mentioned in the spec
    5. Identify examples provided
    
    ONLY after completing this analysis, proceed with implementation.

    Task description:
    {task_description}
    
    Current iteration: {iteration}
    
    If iteration == 0:
        - Action must be "RUN_COMMAND" with a command like "echo 'I have read and analyzed the spec'"
        - Do NOT write files yet
        - Reason must summarize the core requirements in 2-3 sentences
        
    If iteration > 0:
        - Proceed with implementation based on spec analysis
    ...
""")
```

### 2. **Add Real-Time Dashboard** (Medium Effort)
A **web-based dashboard** that:
- ✅ Displays live agent logs as they happen (WebSocket streaming)
- ✅ Shows current iteration, action, reasoning in real-time
- ✅ Displays test status (passed/failed) with color coding
- ✅ Allows manual intervention (pause/resume, inject messages)
- ✅ Visualizes specification pages completed vs remaining
- ✅ Shows token usage and estimated time remaining

**Benefits:**
- You can catch failures early (like after 1 iteration instead of 8+ hours later)
- Easy to see if agent is looping vs making progress
- Debug what the orchestrator roles are thinking

### 3. **Disable Orchestrator for Debug Runs** (Quick Fix)
Run with `--single-agent` flag to use base prompt only:

```bash
python3 agent.py --spec-path ../hackathonMay/QUICK_START_AGENT_BUILD.md --single-agent --max-iterations 5
```

This removes the 16-role complexity and lets you see clearer decision-making.

### 4. **Adjust Completion Heuristic** (Code Change)
In `agent.py` line 934, make `evaluate_primary_artifact_completion()` stricter:

```python
def evaluate_primary_artifact_completion(self) -> ActionOutcome | None:
    path = self.state.primary_artifact_path
    if not path:
        return None
    
    # NEW: Require minimum iterations before accepting completion
    if self.state.iteration < 3:  # Require at least 3 iterations
        return None
    
    artifact = Path(path)
    if not artifact.exists():
        return None
    
    # NEW: Require test pass, not just syntax check
    if self.state.last_test_exit_code not in {0}:  # Must pass tests
        return None
    ...
```

### 5. **Improve Context Window Usage** (Advanced)
- Keep full spec in context for all iterations (not chunked)
- Reduce other noise (orchestration reasoning) to make room
- Use semantic compression: summarize spec into bullet points

---

## Recommended Next Steps

### **Immediate (15 min):**
1. Run agent with `--single-agent` flag to disable orchestrator
2. Add explicit prompt section requiring spec analysis before code
3. Commit changes

### **Short-term (1-2 hours):**
4. Build dashboard prototype (WebSocket server + simple HTML UI)
5. Test with dashboard on next agent run

### **Medium-term (before next challenge):**
6. Implement spec comprehension test (agent must output summary before coding)
7. Add test result weighting to completion heuristic
8. Document agent failure patterns

---

## Dashboard Design Sketch

```
┌────────────────────────────────────────────────────────────────┐
│ 🦆 Duck Agent Dashboard                      [Live] [Pause] [Stop] │
├────────────────────────────────────────────────────────────────┤
│ Status: RUNNING                    Iteration: 2/20              │
├────────────────────────────────────────────────────────────────┤
│                                                                  │
│  CURRENT ACTION:                                                │
│  ├─ Type: WRITE_FILE                                            │
│  ├─ Path: knit_compiler.py                                      │
│  ├─ Reason: Implement main parsing logic...                     │
│  └─ Status: ✅ Complete (45 seconds)                            │
│                                                                  │
│  SPECIFICATION PROGRESS:                                        │
│  ├─ Pages: [████████░░░░░░░░░░░░░░░░░░░░] 2/8                   │
│  └─ Analysis: 87% complete (core parsing logic identified)      │
│                                                                  │
│  TEST STATUS:                                                   │
│  ├─ Last Run: 15 passed, 135 failed                             │
│  ├─ Trending: ↗ (improving)                                     │
│  └─ Next: Run tests after artifact update                       │
│                                                                  │
│  RECENT LOGS:                                                   │
│  ├─ 14:02:15 | WRITE_FILE solution.py (234 lines)               │
│  ├─ 14:02:45 | AUTO_VALIDATION: Syntax OK, 0 runtime errors     │
│  ├─ 14:03:10 | RUN_TESTS: 2/150 passing                         │
│  ├─ 14:03:45 | WRITE_FILE: Updated stitch parser                │
│  └─ [View Full Logs] [Export CSV]                               │
│                                                                  │
│  ORCHESTRATOR ROLES (this iteration):                           │
│  ├─ Scout: ✅ Identified remaining test failures                │
│  ├─ Parser: 🔄 Formalizing repeat syntax                        │
│  ├─ Builder: ⏳ Waiting for direction                            │
│  └─ Tester: 📋 Queued for next iteration                        │
│                                                                  │
└────────────────────────────────────────────────────────────────┘
```

**Features:**
- Live iteration counter and time estimate
- Spec progress visualization
- Test results trending (passing vs failing over time)
- Role state visibility (which orchestrator roles active now)
- Manual controls (pause/resume, send message to agent)
- One-click log export

---

## Testing Your Fix

### Test 1: Spec Reading Phase
```bash
cd /Users/vince/opencode/transfert/duck_agent
python3 agent.py --spec-path ../hackathonMay/QUICK_START_AGENT_BUILD.md --single-agent --max-iterations 1
# Should see: Agent outputs summary of knitting DSL requirements, no code yet
```

### Test 2: Single-Agent Baseline
```bash
python3 agent.py --spec-path ../hackathonMay/QUICK_START_AGENT_BUILD.md --single-agent --max-iterations 10
# Should iterate more times, not stop at 1
# Check: Does test pass rate increase?
```

### Test 3: Dashboard Live Monitoring
```bash
python3 agent.py ... &  # Run in background
# In another terminal:
open http://localhost:8080  # Dashboard should show live logs
```

---

## Metrics to Track

| Metric | Current | Target | Status |
|---|---|---|---|
| Iterations before completion | 1 | 5-10 | ❌ |
| Test pass rate (public) | 0/150 | >50/150 | ❌ |
| Spec comprehension (manual check) | None | Can summarize requirements | ❌ |
| Time to first failure detection | 8h+ (manual) | <5 min (dashboard) | ❌ |
| Agent self-correction rate | ~0% | >30% | ❌ |

---

## Implementation Priority Matrix

```
Impact  │  Quick Fix (Prompt)          │  Dashboard
        │  [x] 15 min setup          │  [ ] 2-3 hours
        │  [✓] Immediate results     │  [✓] Game-changer
        │                             │
────────┼─────────────────────────────┼─────────────────
Effort  │  Single-Agent Mode          │  Orchestrator Tune
        │  [x] 5 min                  │  [ ] 1 hour
        │  [✓] Helps debug            │  [?] May hurt quality
```

**Recommendation:** Do prompt + single-agent mode TODAY, dashboard THIS WEEK.
