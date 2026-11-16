<!--
---
name: "Azure Functions hosted skills: Hosted Skill binding starter"
description: Triage support messages from an Azure Storage queue with a Python function that explicitly invokes a Markdown-defined hosted skill.
page_type: sample
languages:
- python
- bicep
- azdeveloper
products:
- azure
- azure-functions
urlFragment: ai-hosted-skills-starter-hosted-skill-binding-python
---
-->

# Starter: Hosted Skill binding in a queue-triggered function

Add an AI step to an ordinary **Azure Storage queue-triggered function**. Python
validates a support message, explicitly calls a Markdown-defined skill, and handles
its structured result. The skill categorizes the message and drafts a first reply;
it does not own the trigger or send anything to the customer.

This uses the same support-message scenario as [starter-queue](../starter-queue/README.md).
The difference is who owns execution: that starter declares the queue trigger in
Markdown; this starter declares it in Python and injects a `HostedSkill` with
`@app.hosted_skill`. There are no custom tools, public HTTP endpoints, remote MCP
servers, code interpreter, or conversation-continuation logic.

```text
 support message ──► Storage queue 'support-requests'
   (JSON)                    │
                             ▼
                  Python: triage_support_message
                    validate and prepare prompt
                             │
                       await skill.run()
                             ▼
                    support_triage.agent.md
                    (no trigger or endpoints)
                             │ validated JSON
                             ▼
                  Python: handle and log result ──► Application Insights
```

## The binding

The application constructs `HostedSkillFunctionApp()`. In
[src/function_app.py](src/function_app.py), the queue-trigger decorator is above
`@app.hosted_skill(arg_name="skill", agent_name="support_triage")`, immediately
followed by an `async def` handler with a `skill: HostedSkill` parameter.

`agent_name` selects the agent by filename, not its display name. The runtime
injects the facade and hides that parameter from Functions worker indexing. This
is a runtime-owned Python decorator, not a new Functions host binding or separate
extension package. The call reuses the agent's model, timeout, response contract,
and telemetry configuration.

The handler owns three steps:

1. **Before AI:** reject malformed JSON, unsafe ticket IDs, and missing, blank, or
   oversized subject/message fields. Only subject and message go to the model,
   not the separate customer address or queue metadata.
2. **AI step:** call `await skill.run(prompt)`. The runtime validates the response
   against the agent's JSON Schema before returning.
3. **After AI:** parse the result, attach the original ticket ID in code, and log
   classification metadata. Replace this step with your own downstream processing.

## What gets deployed

`azd up` creates:

- A Flex Consumption function app (Python 3.13) with a user-assigned managed identity.
- A storage account with shared key access disabled, app storage, and the
  `support-requests` queue.
- A Microsoft Foundry project with a `gpt-5.4` deployment, configurable through the
  same model environment settings as the other starters.
- Application Insights and Log Analytics.

Azure connections use managed identity. The deployer receives data-plane roles for
local development and the demo. No dynamic session pool or connector is provisioned:
this triage task needs neither.

## Prerequisites

- An Azure subscription with `gpt-5.4` GlobalStandard quota in the selected region.
- [Azure Developer CLI](https://aka.ms/azd) and
  [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
- Python 3.13.
- For local runs: [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
  4.x and [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite).

## Deploy to Azure

From this sample's folder:

```bash
azd auth login
azd env new starter-binding
azd up
```

Deployment creates billable resources. If package upload initially fails with a
Storage authorization error, allow the new role assignments to propagate and retry
`azd deploy`.

## Try it

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install --pre -r requirements-dev.txt

python scripts/demo.py send
python scripts/demo.py send --sample T-1001
```

| Sample | Situation | Expected classification (illustrative) |
|---|---|---|
| T-1001 | Duplicate charge; customer needs the money back | billing, high urgency, negative sentiment |
| T-1002 | Happy customer asks about another boot size | feedback or other, low urgency, positive sentiment |

Model wording and classification can vary. Successful processing produces an
application log such as:

```text
Support triage completed: {"ticket_id": "T-1001", "category": "billing", "urgency": "high", "sentiment": "negative"}
```

Find it in Application Insights Logs:

```kusto
traces
| where message startswith "Support triage completed: "
| project timestamp, message, operation_Id
| order by timestamp desc
```

For **synthetic data only**, set `ENABLE_SENSITIVE_DATA=true` in the Function App
settings (or local settings) to also log the summary and suggested reply. This
setting also enables sensitive runtime telemetry. Those fields may contain customer
data; review log access and retention and disable it afterwards. Ticket IDs and
classification metadata are always logged, so use opaque, non-sensitive IDs.

```bash
python scripts/demo.py e2e
```

The deployed smoke check sends uniquely identified copies and waits for a completion
log for **every submitted ticket**, rather than counting unrelated invocations.
It checks processing, not the business accuracy of generated wording. Application
Insights ingestion delays or sampling can cause a timeout; increase `--timeout`
and inspect invocation logs before resending.

## Run locally

1. Provision once with `azd up` or use an existing Foundry project. Local processing
   still calls a real model and can incur model charges.
2. Sign in with `az login` and start Azurite with `azurite --skipApiVersionCheck`.
3. Copy the local settings template:

   ```bash
   cp src/local.settings.json.sample src/local.settings.json
   ```

   Set `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` from your project (or the
   outputs of `azd env get-values`). Keep both storage connections pointed at
   Azurite. Leave `ENABLE_SENSITIVE_DATA=false` unless inspecting synthetic output.
4. With the virtual environment active, start the host:

   ```bash
   cd src
   func start
   ```

5. In another terminal, from this sample's folder, send the samples:

   ```bash
   python scripts/demo.py send --local
   ```

Read `Support triage completed:` in the host console. The `e2e` command targets
deployed Azure resources, not the local host.

## Guardrails and retry behavior

- Invalid inputs fail **before** a model call and are never logged verbatim.
- Customer content is untrusted data; the agent has no tools or external actions.
- The skill drafts a reply only; nobody is emailed and no refund is issued.
- Invalid model responses and provider errors propagate to the queue host for retry.
- After five failed attempts, the host moves a message to `support-requests-poison`.
- Queue delivery is at least once. Redelivery can repeat model calls and logs.
  Before adding external writes, add idempotency keyed to the ticket or message ID;
  this starter does not promise exactly-once processing.
- Each invocation uses a fresh conversation. No caller-supplied sessions or public
  agent endpoints are exposed.

## Make it yours

| To change | Edit |
|---|---|
| Validation, prompt preparation, conditional invocation, or result handling | [src/function_app.py](src/function_app.py) |
| Triage instructions and structured result | [src/support_triage.agent.md](src/support_triage.agent.md) |
| Queue name or connection | Python queue-trigger decorator, [infra/main.bicep](infra/main.bicep), and [scripts/demo.py](scripts/demo.py) |
| Retry count or batch size | [src/host.json](src/host.json) |
| Model or timeout | [src/agents.config.yaml](src/agents.config.yaml), and model deployment settings |

Keep `@app.hosted_skill` innermost and the injected parameter annotated as
`HostedSkill`. Do not add a queue trigger to the Markdown agent unless you
intentionally want a second independently registered consumer.

## Test

Unit tests use the real app/decorator and mock `HostedSkill.run`; they need no Azure
credentials, model calls, or running storage emulator.

```bash
python -m pytest tests -q
```

From the repository root:

```bash
python scripts/test.py --template starter-hosted-skill-binding
python scripts/test.py --template starter-hosted-skill-binding --level local
python scripts/test.py --template starter-hosted-skill-binding --level deploy --down
```

The levels add Bicep/config loading, unit tests, host startup, and deployed smoke
testing respectively. Local startup requires valid Foundry settings; the deploy
level creates billable resources.

## Files

```text
starter-hosted-skill-binding/
├── azure.yaml
├── infra/                       Function app, storage + queue, Foundry, monitoring
├── samples/                     Two support messages
├── scripts/demo.py              Send messages and check correlated completions
├── tests/                       Binding/indexing, validation, response and demo tests
└── src/
    ├── function_app.py          Python trigger + explicit skill.run()
    ├── support_triage.agent.md  Endpoint-less instructions + response schema
    ├── agents.config.yaml       Model and timeout
    ├── local.settings.json.sample
    ├── requirements.txt
    └── host.json
```

## Clean up

```bash
azd down --purge
```