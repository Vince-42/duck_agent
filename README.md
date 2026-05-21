# duck_agent

Team repository for the 42 Berlin x Needle Agent Hackathon.

This repository is structured for the **team submission**, not the public event-materials repo. It contains a reusable local coding agent plus the hackathon submission files and logs.

## Team members

- Vince Jonas Manoa Hossam

## Current status

Reusable local agent setup in progress.

- Agent scaffold: present
- Submission scaffold: present
- Hidden-task solution: not implemented yet
- Public-test score: not available yet

## Final command

The final command for the hackathon solution depends on the hidden specification released under `secret_spec/`.

```bash
python3 solution.py <args-from-hidden-spec>
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Python/runtime version

```text
Python 3.9+
```

## Agent overview

`agent.py` provides a simple autonomous coding loop that can be used for general repository tasks or the hackathon workflow:

1. read a direct task, a task file, or `secret_spec/SECRET_SPEC.md`
2. ask a local Ollama model for the next action
3. write files or run commands
4. run a configured test command or the public tests when available
5. log prompts, decisions, commands, test runs, and errors
6. repeat for a bounded number of iterations

The runtime now includes an orchestrator-driven **16-role multi-agent system** layered on top of the existing execution shell:

- orchestrator: `orchestrator.py`
- role registry: `roles.py`
- structured cognition config: `agent.json`
- execution shell + safeguards: `agent.py`

This is intentionally **one structured-cognition engine**, not 16 independent worker processes. The orchestrator activates role subsets by phase:

- **Understand**: Scout, Parser, Miner, Critic, Planner
- **Build**: Architect, Builder, Minimalist, Tactician
- **Verify**: Tester, Debugger, Repairer, Validator
- **Govern**: Logger, Coordinator, Guardrail, Validator

The final action contract stays unchanged: the orchestrator synthesizes internal role guidance into one JSON action that `DuckAgent` executes through the existing safe write / command / test flow.

`agent.json` is the source of truth for:

- shared rules
- phase definitions
- decision rules
- memory/logging policy
- cognitive mode prompt templates

The implementation is intentionally simple and easy to inspect and can be reused whenever you want to point it at a repository task.

## Model setup

Primary model: `qwen2.5-coder:7b`

Provider/runtime: Ollama

Additional models: none configured yet

See `agent_manifest.json` for disclosure fields.

## Running the agent

Start Ollama separately, then run:

```bash
python3 agent.py
```

To disable the multi-role layer and fall back to the base single-agent prompt:

```bash
python3 agent.py --single-agent
```

To point the runtime at a different structured-cognition config:

```bash
python3 agent.py --orchestrator-config /path/to/agent.json
```

For any ad-hoc task:

```bash
python3 agent.py --task "Create a small CLI in this repository and run the tests after each change."
```

For a task written in a file:

```bash
python3 agent.py --task-file examples/demo_spec.md
```

For a task with an explicit test command:

```bash
python3 agent.py --task-file examples/demo_spec.md --test-command "python3 -m unittest discover -s tests -v"
```

If you prefer short terminal commands:

```bash
make doctor
make test
make run-agent
```

Useful direct CLI examples:

```bash
python3 agent.py --help
python3 agent.py --doctor
python3 agent.py --spec-path examples/demo_spec.md
python3 agent.py --spec-path secret_spec/SECRET_SPEC.md --max-iterations 10
```

Recommended first steps when debugging terminal setup:

1. run `make doctor`
2. confirm Ollama is running
3. confirm your task input exists where you expect
4. run `make test`
5. run `make run-agent`

Hackathon mode still works with the default `secret_spec/SECRET_SPEC.md` path after reveal.

Optional environment variables:

```bash
export OLLAMA_MODEL=qwen2.5-coder:7b
export OLLAMA_URL=http://localhost:11434/api/generate
export AGENT_MAX_ITERATIONS=20
export AGENT_SOLUTION_COMMAND="python3 solution.py"
```

## Public test run

When the reveal materials are present:

```bash
python3 secret_spec/test_runner/run_tests.py --program "python3 solution.py" --suite public
```

## Agent test run

Run the scaffold tests from the terminal with:

```bash
python3 -m unittest discover -s tests -v
```

or:

```bash
make test
```

These tests validate the agent scaffold itself: parsing actions, safe file writes, startup behavior, logging, and public-test runner integration points.

## Known limitations

- Ollama must be running locally for live agent execution.
- The hidden specification is not in this repository yet.
- `solution.py` is intentionally not implemented before reveal.
- The system uses one orchestrator-driven multi-role loop, not parallel worker agents.
- Ollama latency still constrains how many deliberation turns are practical.

## Repository structure

```text
agent.py                 # autonomous execution shell + CLI
orchestrator.py          # structured cognition mode switching
roles.py                 # fallback 16 role definitions
agent.json               # runtime structured-cognition config
agent_manifest.json      # required model/tool disclosure
agent_logs/              # required hackathon logs
examples/                # example reusable task inputs
secret_spec/             # hidden spec and public tests will live here
requirements.txt         # minimal Python dependencies
```

## 19:45 checkpoint

Target checkpoint format:

```bash
git commit -m "Agent readiness checkpoint"
git tag agent-readiness-1945
git push --follow-tags
```
