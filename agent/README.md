# Agent Directory

This directory exists to document the repository's agent runtime layout for compliance tooling.

The executable runtime remains at the repository root:

- `agent.py` — main execution shell
- `orchestrator.py` — structured cognition mode switching
- `agent.json` — structured cognition configuration

This repository intentionally uses a single-agent runtime loop with orchestrated cognitive modes rather than a nested Python package under `agent/`.
