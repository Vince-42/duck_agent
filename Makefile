PYTHON ?= python3

.PHONY: test doctor run-agent run-single-agent help

help:
	@printf '%s\n' \
	  'make test       - run the unit test suite' \
	  'make doctor     - check local readiness (spec + Ollama)' \
	  'make run-agent  - run the agent scaffold' \
	  'make run-single-agent - run without the multi-role orchestrator' \
	  'make demo-task  - run the agent against the example task file'

test:
	$(PYTHON) -m unittest discover -s tests -v

doctor:
	$(PYTHON) agent.py --doctor

run-agent:
	$(PYTHON) agent.py

run-single-agent:
	$(PYTHON) agent.py --single-agent

demo-task:
	$(PYTHON) agent.py --task-file examples/demo_spec.md
