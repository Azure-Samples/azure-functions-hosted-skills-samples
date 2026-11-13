# Azure Functions Hosted Skills — scenario samples

Ready-to-run templates for [Azure Functions hosted skills](https://learn.microsoft.com/azure/azure-functions/functions-hosted-skills-scenarios). Each folder is one business scenario: a hosted skill written in Markdown, the tools it calls, sample data, and Bicep infrastructure you deploy with one `azd up`.

Pick a scenario, deploy it, send the sample events, and read what the skill decided. Then replace the sample data with your systems.

## Scenarios

| Template | Scenario | Trigger | Status |
|---|---|---|---|
| [`customer-returns-queue`](customer-returns-queue) | Triage return requests that don't fit the policy and recommend approve / exception / decline / escalate | Storage queue or Service Bus | ✅ Available |
| [`invoice-review-blob`](invoice-review-blob) | Check uploaded invoice PDFs against purchase orders, receipts, and contract terms, and hold or reject the ones that don't match | Blob (Event Grid) | ✅ Available |
| [`contract-review-durable`](contract-review-durable) | Add an AI clause review step to an existing Durable Functions workflow | HTTP from Durable orchestration | ✅ Available |
| [`daily-work-briefing-timer`](daily-work-briefing-timer) | Send a daily summary of mail, meetings, and chats | Timer | ✅ Available |
| [`customer-retention-cosmos`](customer-retention-cosmos) | Write a retention report when an account's usage drops | Cosmos DB change feed | ✅ Available |
| [`claims-review-workflow`](claims-review-workflow) | Plan and run parallel checks for an insurance claim | Blob + Dynamic Workflows (Durable Task Scheduler) | ✅ Available |
| [`alert-investigation-eventhub`](alert-investigation-eventhub) | Investigate only the security alerts that rules can't settle | Event Hubs → Microsoft-Decision-1 → queue | ✅ Available |
| [`teams-helpdesk-connector`](teams-helpdesk-connector) | Answer helpdesk questions posted in a Teams channel | Connector (Teams) + HTTP test | ✅ Available |

## Quickstart

```bash
git clone https://github.com/Azure-Samples/azure-functions-hosted-skills-samples
cd azure-functions-hosted-skills-samples/customer-returns-queue
azd up
```

Each template's README has the demo steps, local run instructions, and a "Make it yours" section.

## What every template includes

- `src/*.agent.md` — the hosted skill: trigger, instructions, and tool access in one Markdown file.
- `src/tools/` — Python tools the skill calls. Side effects are idempotent and approval rules are enforced in code.
- `src/data/` and `samples/` — mock business data and sample events that show interesting decisions.
- `infra/` — Bicep for a Flex Consumption app, managed identity everywhere (no keys), Microsoft Foundry model, and monitoring.
- `scripts/demo.py` — send or upload the samples and print the results.
- `tests/` — unit tests for the tools.

## Test

One command runs the checks for every template:

```bash
python scripts/test.py                     # static: Bicep build + skill/tool loading; unit: pytest
python scripts/test.py --level local       # + func start against Azurite (and the DTS emulator for Durable templates)
python scripts/test.py --template claims-review-workflow --level local --port 7072  # pick another host port
python scripts/test.py --template customer-returns-queue --level deploy --down  # + azd up, e2e, azd down
```

Requires Python 3.13 and the Azure CLI. The `local` level also needs Azure Functions Core Tools and Azurite. The `deploy` level creates billable Azure resources.

## Resources

- [Hosted skills scenarios](https://learn.microsoft.com/azure/azure-functions/functions-hosted-skills-scenarios)
- [Daily blog hosted skill template](https://github.com/Azure-Samples/functions-daily-blog-hosted-skills-azd)
- [Azure Developer CLI](https://aka.ms/azd)

## Contributing

This project welcomes contributions and suggestions. See [CONTRIBUTING.md](CONTRIBUTING.md).
