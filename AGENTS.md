# Coding Agent Instructions

This repository is a Microsoft Foundry hosted Activity agent for Microsoft Teams. The Activity SDK owns the channel contract, M365 conversation state maps each channel conversation to a project Responses conversation, and the Foundry project stores the model transcript.

## Key Files

- `azure.yaml` defines the Foundry project, model deployment, hosted agent, Activity 2.0 endpoint, and direct code deployment.
- `src/activity-model/main.py` contains the Activity handler and native async Foundry SDK calls.
- `src/activity-model/pyproject.toml` and `uv.lock` are the authoritative Python dependency artifacts.
- `scripts/Stop-StaleAgentSessions.ps1` stops sessions pinned to older agent versions after deployment. No repository script runs during provision or deploy; the `azure.ai.agents` extension owns the hosted identity's platform-managed project access.
- `local.http` contains a deterministic two-turn Activity probe.
- `.github/workflows/ci.yml` is the release gate for locks, compilation, tests, configuration syntax, and deployment exclusions.

## Development

Use Python 3.13 and the pinned uv workflow:

```powershell
cd src/activity-model
uv sync --frozen
uv run python -m unittest discover -s tests -v
cd ../..
```

Keep runtime packages in `[project].dependencies` and development-only tools in `[dependency-groups].dev`. After dependency changes, update and commit the owning `uv.lock`; CI rejects stale locks.

Use the standard `logging` module for long-running runtime diagnostics. Keep Activity state limited to the project conversation ID; do not duplicate model transcripts in channel state. Do not claim Application Insights is configured merely because `ENABLE_INSTRUMENTATION` is enabled; the bicepless source-deployment path needs a separately verified export destination.

This project was built with the microsoft-foundry skill. Before working on or answering questions about Foundry agents, read the microsoft-foundry skill first. If you are in VS Code, read the vscode-microsoft-foundry skill first.