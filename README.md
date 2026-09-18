# Foundry Hosted Activity Model

A standalone Python sample that publishes a model-backed Microsoft Foundry hosted agent to Microsoft Teams using the native Activity protocol. It includes streamed replies, multi-turn history, safe correlated errors, local debugging, deployment helpers, and a reproducible CI workflow.

## How It Works

`ActivityAgentServerHost` provides the Activity 2.0 endpoint, Teams delivery, authentication, state, health probes, and OpenTelemetry integration. For each Teams conversation, the handler stores one `modelConversationId` in M365 conversation state. The corresponding project-scoped OpenAI Responses conversation stores the actual user and assistant transcript.

The model calls use `azure.ai.projects.aio.AIProjectClient` and managed identity. The sample does not contain API keys, connection strings, or a custom conversation database.

### How model streaming reaches Teams

There are three distinct layers in [main.py](src/activity-model/main.py):

1. `openai.responses.create(..., stream=True)` returns model events. The sample consumes only `response.output_text.delta` events.
2. `context.streaming_response` is the M365 Agents SDK `StreamingResponse` helper for the current Activity turn. `queue_text_chunk()` accepts each incremental model delta and coalesces them into the cumulative updates required by Teams.
3. `end_stream()` drains pending updates and finalizes the Teams message exactly once. On a channel that does not support streaming, the same helper sends one completed Activity instead.

The bridge is deliberately visible in the message handler:

```python
streaming_response = context.streaming_response
await respond_with_history(
	prompt,
	state,
	streaming_response.queue_text_chunk,
)
await streaming_response.end_stream()
```

`stream=True` alone does not stream to Teams. It only exposes model events to the application. The code above connects those upstream events to the Activity stream; `ActivityAgentServerHost` and `StreamingResponse` own the channel-specific Activity details.

Teams renders the progressive updates when it delivers a normal Activity turn. The Activity test mode `deliveryMode: expectReplies` buffers replies into the HTTP response, so use Teams to verify visible progressive rendering.

## Prerequisites

- An Azure subscription
- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) 1.27.1 or later
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- Permission to create the Foundry resources and role assignments used by hosted-agent deployment
- A unique agent name before deployment because the generated Azure Bot name is global

## Option 1: Start Without a Foundry Project

Install the Foundry extension and authenticate:

```powershell
azd extension install azure.ai.agents
az login
azd auth login
```

The manifest requires `azure.ai.agents` 1.0.0-beta.16 or later. Beta.16 includes the model-deployment-name persistence fix used by this initialization flow.

Initialize a portable copy directly from this repository:

```powershell
mkdir my-activity-agent
cd my-activity-agent
azd ai agent init `
	-m https://raw.githubusercontent.com/eamonoreilly/foundry-hosted-agents-activity-model/main/azure.yaml `
	--deploy-mode code `
	--runtime python_3_13 `
	--entry-point main.py `
	--agent-name my-unique-activity-agent
cd my-unique-activity-agent
```

`--agent-name` writes the deployed Foundry agent name through the normal azd initialization flow. Choose a unique name to prevent a global Azure Bot name collision.

Set your subscription, region, and new project name:

```powershell
azd env set AZURE_SUBSCRIPTION_ID <subscription-id>
azd env set AZURE_LOCATION northcentralus
azd env set AZURE_AI_PROJECT_NAME ai-project-my-activity-agent
```

The Foundry project name must be 32 characters or fewer. The model deployment defaults to `gpt-5.4-mini`; override it before provisioning when needed:

```powershell
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME <deployment-name>
```

Provision and deploy in separate, observable steps:

```powershell
azd provision --no-state --no-prompt
azd deploy --no-prompt
```

`azd provision` creates the resource group, Foundry account, Foundry project, and `gpt-5.4-mini` deployment. `azd deploy` uploads the Python code, creates the hosted agent and its instance identity, configures its platform-managed project access, exposes Activity 2.0, and creates the Azure Bot and Teams channel. Deploy also makes a best-effort attempt to generate the Teams app package, but it does not install the app in Teams.

### Test a newly deployed version

A Teams chat can keep using an existing hosted session after `azd deploy`. That session is pinned to the agent version that created it, so deploying new code does not move the chat to the new version automatically.

After deployment, stop only sessions pinned to an older version:

```powershell
.\scripts\Stop-StaleAgentSessions.ps1
```

The helper compares the latest version from `azd ai agent show` with the version attached to every session returned by `azd ai agent sessions list`. It stops only non-stopped sessions on older versions; current-version sessions are left running. `stop` preserves the session and its filesystem volume, unlike `delete`. The next message in Teams starts or resumes compute on the latest deployed version.

Preview what would be stopped without changing anything:

```powershell
.\scripts\Stop-StaleAgentSessions.ps1 -WhatIf
```

For a project with multiple agent services or a non-default azd environment:

```powershell
.\scripts\Stop-StaleAgentSessions.ps1 `
	-AgentName my-unique-activity-agent `
	-Environment dev
```

The CLI reports a session waiting between messages as `active`, even when it is colloquially described as idle. Version mismatch, rather than the status label, is what identifies the stale test session.

To run both stages with one command after setting the environment values:

```powershell
azd up --no-prompt
```

After deployment, explicitly generate the personal Teams package so a packaging failure is reported:

```powershell
azd ai agent pack
```

This writes `src/activity-model/appPackage.zip`. In Teams, open **Apps** > **Manage your apps** > **Upload an app** > **Upload a custom app**, select the ZIP, and add it for yourself. Open the installed app to start a personal chat with the hosted agent. Tenant policy must allow custom app uploads; otherwise a Teams administrator must enable sideloading or publish the package for you. See the generated `src/activity-model/TEAMS_APP_SETUP.md` for tenant-specific guidance.

> [!NOTE]
> Model availability and quota vary by subscription and region. If `gpt-5.4-mini` cannot be provisioned in your selected region, choose a supported region or update the deployment in `azure.yaml` before provisioning.

## Option 2: Use an Existing Foundry Project

Pass the existing project's full ARM resource ID during initialization:

```powershell
azd ai agent init `
	-m https://raw.githubusercontent.com/eamonoreilly/foundry-hosted-agents-activity-model/main/azure.yaml `
	--project-id <foundry-project-resource-id> `
	--model-deployment gpt-5.4-mini `
	--deploy-mode code `
	--runtime python_3_13 `
	--entry-point main.py `
	--agent-name my-unique-activity-agent
```

Enter the generated project folder, then run `azd deploy --no-prompt`. Provisioning is unnecessary when the project and model already exist.

## Option 3: VS Code

Set up the runtime:

```powershell
cd src/activity-model
uv sync --frozen
Copy-Item .env.example .env
```

Set `FOUNDRY_PROJECT_ENDPOINT` and `AZURE_AI_MODEL_DEPLOYMENT_NAME` in `.env`, then sign in with `az login`. Open the repository root and press **F5** to debug the Activity host on `http://localhost:8088`.

Activity agents use Teams or an Activity-compatible client rather than the Responses Agent Inspector. Use [local.http](local.http) for the two-turn protocol probe, or use M365 Agents Playground for interactive channel testing.

The debug task uses `uv run --frozen --project src/activity-model`, so it selects the sample's virtual environment consistently on Windows, macOS, and Linux.

## Develop and Test

Install and test only from the committed lockfiles:

```powershell
uv sync --frozen --project src/activity-model
uv run --frozen --project src/activity-model python -m unittest discover -s src/activity-model/tests -v
```

Runtime dependencies are declared in `src/activity-model/pyproject.toml`; local debugger support is in its `dev` dependency group. When changing dependencies, run `uv lock --project src/activity-model` and commit its `uv.lock`. GitHub Actions repeats the frozen install, Python compilation, tests, configuration parsing, and generated-artifact checks.

## Verify Multi-Turn History

Send these messages in the same Teams chat:

```text
Remember that my verification code is CEDAR-842. Reply only SAVED.
What is my verification code? Reply only with the code.
```

The expected replies are `SAVED` and `CEDAR-842`. A new Teams conversation creates a separate project Responses conversation.

## State and Security

- Activity/M365 state stores only `modelConversationId`.
- The Foundry project Responses service stores the transcript.
- The hosted agent uses its dedicated managed identity.
- The runtime identity accesses the same Foundry project named by `FOUNDRY_PROJECT_ENDPOINT`.
- Model prompts and outputs are not enabled as sensitive telemetry.

## Observability and Error Diagnostics

`ENABLE_INSTRUMENTATION=true` enables the hosting runtime's OpenTelemetry instrumentation. It does not, by itself, create an Application Insights resource or configure an exporter.

At the time this sample was validated, `microsoft.foundry` source-code deployment did not provision or link Application Insights for the bicepless path. Use `azd ai agent monitor` and the hosted-agent logs for diagnostics unless your project has a verified telemetry destination. The Application Insights queries below apply only when telemetry export is separately configured and the operation is present there.

When a turn fails, the agent returns a channel-safe message with an opaque reference:

```text
Sorry, something went wrong. Please try again.

Reference: 4962ec560fbbcb05a468b4fef411d301
```

Ask the user to send support the reference and approximate failure time. When an OpenTelemetry request span is active, the reference is its trace ID and maps to Application Insights `operation_Id`. If no valid span is available, the agent generates a fallback reference and writes it into the error log message.

Search by trace ID:

```kusto
union requests, dependencies, exceptions, traces
| where operation_Id == "4962ec560fbbcb05a468b4fef411d301"
| order by timestamp asc
```

For a fallback reference, search the logged message:

```kusto
union exceptions, traces
| where message contains "<reference>"
| order by timestamp asc
```

The server log includes the full traceback, reference, and inbound Activity ID. The Teams response never includes exception messages, stack traces, filesystem paths, session IDs, or user identifiers. Keep that boundary during development as well: use local console output, hosted logs, or a verified telemetry destination for diagnostic detail.

If a failure occurs after model text has started streaming, the sample marks the response as interrupted and finalizes the same Activity stream. This avoids leaving an unfinished streaming message in Teams.

## Troubleshooting

- `403 Forbidden` from `/openai/v1/conversations`: first rerun `azd deploy` with `azure.ai.agents` beta.16 or later. Current deployment handles the agent instance identity's project access. If the failure persists, retrieve the identity and project resource ID and grant the least-scope fallback explicitly:

```powershell
$agent = azd ai agent show --output json | ConvertFrom-Json
$principalId = $agent.instance_identity.principal_id
$projectId = azd env get-value AZURE_AI_PROJECT_ID
az role assignment create `
	--assignee-object-id $principalId `
	--assignee-principal-type ServicePrincipal `
	--role 53ca6127-db72-4b80-b1b0-d745d6d5456d `
	--scope $projectId
```

  The role ID is `Foundry User`. RBAC can take several minutes to propagate; stop the stale hosted session before retrying so the next message uses the latest version.
- `404 Not Found` for a stored conversation: the sample replaces the stale project conversation once and updates the Activity state mapping.
- Teams shows only a generic error: inspect hosted logs for the underlying project API status.

## License

This sample is available under the [MIT License](LICENSE).

## References

- [Foundry hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
- [Official Python hosted-agent samples](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents)
- [Activity protocol echo sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity/echo)