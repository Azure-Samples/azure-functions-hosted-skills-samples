# Expense policy routing (queue-triggered hosted skill)

Expense and purchase requests land on a Storage queue in any format: a sentence, an email, `key: value` lines, or JSON. A hosted skill on Azure Functions reads each request, finds the policy document for its category in Blob Storage, applies the policy's rules, and routes the decision to one of three queues: **approved**, **review**, or **flagged**.

The policies are plain Markdown files in Blob Storage. Change a document and the next request follows the new rules, with no redeploy.

This template implements the [process customer requests from messages](https://learn.microsoft.com/azure/azure-functions/functions-hosted-skills-scenarios#process-customer-requests-from-messages) scenario on Microsoft Learn. It's a simplified version of [Azure-Samples/hosted-skills-expense-processor](https://github.com/Azure-Samples/hosted-skills-expense-processor), which additionally governs model traffic with an API Management AI Gateway and uses Connector Namespace MCP connectors. Here, three small Python tools replace the connectors, and the app calls Microsoft Foundry directly.

```mermaid
flowchart LR
    R["expense request<br/>(any format)"] --> Q[["Storage queue<br/>expense-requests"]]
    Q --> S["expense_processor.agent.md<br/>hosted skill (Flex Consumption)"]
    S -- list_policies / read_policy --> P[("Blob: policies/<br/>travel, meals, equipment, general")]
    S -- route_expense_decision --> D{"rules checked<br/>in code"}
    D --> A[["expense-approved"]]
    D --> V[["expense-review"]]
    D --> F[["expense-flagged"]]
    D -. once per expenseId .-> M[("Blob: decisions/{expenseId}.json")]
```

## How a request is decided

1. **Extract.** The skill reads amount, currency, vendor, category, and expense ID from the message. If the message has no expense ID, it uses `EXP-` plus the first 8 characters of the queue message ID.
2. **Pick a policy.** `list_policies` returns each document and its `**Applies to:**` line. The skill picks the one that matches the category, or falls back to `general-expense-policy.md`.
3. **Apply rules top to bottom.** `read_policy` returns the document, and the first rule that matches decides.
4. **Route once.** `route_expense_decision` checks the decision and sends a compact JSON message to exactly one output queue.

The same 450 USD gets a different outcome depending on what it was for:

| Sample | Request | Policy | Decision |
|---|---|---|---|
| `travel.txt` | Free text: flight to Denver, "four hundred and fifty dollars" | `travel-policy.md` rule 5 (≤ $1,000) | **expense-approved** |
| `client-dinner.txt` | Free text: client dinner at Nobu, $450 | `meals-entertainment-policy.md` rule 2 (client entertainment) | **expense-review** |
| `equipment.txt` | `key: value`: Dell 4K monitor, USD 450.00 | `equipment-software-policy.md` rule 4 (≤ $500) | **expense-approved** |
| `missing-amount.txt` | "Please approve my expenses from the client trip", no amount | Completeness rule | **expense-flagged** |
| `foreign-currency.txt` | Berlin hotel, €480 | `travel-policy.md` rule 4 (foreign currency) | **expense-review** |
| `cash-advance.txt` | 50 USD petty cash advance | `general-expense-policy.md` (cash advances) | **expense-flagged** |
| `travel.txt` after `set-policy strict` | Same 450 USD flight | `samples/strict-travel-policy.md` ($250–$2,500 → review) | **expense-review** |

### What's real and what's simulated

| Part | In this template |
|---|---|
| Queue trigger, output queues, Blob policies, decision markers | **Real** Azure Storage (or Azurite locally), accessed with managed identity |
| Model and reasoning | **Real** Microsoft Foundry model deployment (`gpt-5.4` by default) |
| Policy documents | **Sample** Markdown in `policies/`, uploaded to Blob Storage. Replace them with your own. |
| Downstream systems | **Not included.** Decisions wait in the three output queues for your ERP, approval flow, or a reviewer. |

## Prerequisites

- An Azure subscription with access to a Microsoft Foundry model deployment (the template deploys one).
- [Azure Developer CLI](https://aka.ms/azd) (`azd`) 1.10 or later, and [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`).
- Python 3.13.
- For local runs: [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) 4.x and [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite).

## Deploy to Azure

```bash
git clone https://github.com/Azure-Samples/azure-functions-hosted-skills-samples
cd azure-functions-hosted-skills-samples/expense-policy-queue
azd auth login
az login
azd up
```

`azd up` creates a resource group with:

- A Flex Consumption function app running the hosted skill, with a user-assigned managed identity.
- A storage account (no shared key access) with the queues `expense-requests` (and `expense-requests-poison`), `expense-approved`, `expense-review`, and `expense-flagged`, and the containers `policies` and `decisions`.
- A Microsoft Foundry project with a model deployment.
- Application Insights and Log Analytics.

The app's identity gets Storage Blob Data Owner, Storage Queue Data Contributor, and Storage Table Data Contributor on the storage account, plus Cognitive Services User on Foundry. Your signed-in account gets Storage Blob Data Contributor and Storage Queue Data Contributor so you can run the demo script.

After deployment, a `postdeploy` hook (`scripts/postdeploy.py`) uploads `policies/*.md` with `az storage blob upload --auth-mode login`. It only uploads documents that aren't already in the container, so edits you make in Blob Storage survive later deployments.

## Try it

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install --pre -r requirements-dev.txt

python scripts/demo.py send        # travel, client-dinner, equipment, missing-amount
python scripts/demo.py results     # after a minute or two, peek the three output queues
```

Send other samples or your own text. Use single quotes in PowerShell and bash so `$` amounts aren't expanded:

```bash
python scripts/demo.py send --sample foreign-currency
python scripts/demo.py send --file samples/cash-advance.txt
python scripts/demo.py send --text 'Team lunch at Joe'"'"'s Diner, $45, please reimburse.'
```

### Change a policy without redeploying

`samples/strict-travel-policy.md` lowers travel auto-approval from $1,000 to $250. Upload it over `travel-policy.md`, send the travel sample again, and the same 450 USD flight goes to review. Meals and equipment requests don't change, because they use other documents.

```bash
python scripts/demo.py set-policy strict
python scripts/demo.py send --sample travel
python scripts/demo.py results
python scripts/demo.py set-policy default   # restore
```

You can also edit the document directly in the Azure portal (**Storage account > Containers > policies**).

### Example output

Real output from `python scripts/demo.py e2e` against a deployed app (`gpt-5.4`). The table columns are `queue | expenseId | amount | currency | category | policyApplied | reason`:

```text
--- default policies ---
expense-approved | EXP-TRV-Dbb850d | 450.0 | USD | travel | travel-policy.md | USD 450 airfare is standard travel with a clear USD amount, so under Travel Policy rule 5 ($1,000 or less) it is approved.
expense-approved | EXP-EQP-Dbb850d | 450.0 | USD | equipment | equipment-software-policy.md | USD 450.00 for a one-time hardware purchase falls under Rule 4 of the equipment & software policy, where $500 or less is approved.
expense-review | EXP-DIN-Dbb850d | 450.0 | USD | meals | meals-entertainment-policy.md | USD 450 client dinner at Nobu matches Meals & Entertainment rule 2, so client entertainment requires review regardless of amount.
expense-flagged | EXP-MIS-Dbb850d |  | USD | travel | travel-policy.md | Amount is missing and the request is too vague to price, so Travel Policy rule 1 (Completeness) flags it for clarification.

--- strict travel policy (no redeploy) ---
policies/travel-policy.md <- samples/strict-travel-policy.md
expense-approved | EXP-EQP-Sbb850d | 450.0 | USD | equipment | equipment-software-policy.md | USD 450.00 for a one-time monitor purchase falls under the equipment & software policy amount threshold of $500 or less, so it is approved.
expense-review | EXP-TRV-Sbb850d | 450.0 | USD | travel | travel-policy.md | USD 450 travel airfare matches Travel Policy rule 5 amount thresholds: over $250 up to $2,500 routes to review.
policies/travel-policy.md <- policies/travel-policy.md
e2e passed
```

The foreign-currency and cash-advance samples, sent without an expense ID:

```text
expense-review | EXP-9c2fa484 | 480.0 | EUR | travel | travel-policy.md | €480 lodging expense is in foreign currency, so Travel Policy rule 4 routes it for FX verification.
expense-flagged | EXP-931ab86b | 50.0 | USD | general | general-expense-policy.md | USD 50 cash advance for petty cash is flagged under the Cash advances rule, which always requires manager sign-off regardless of amount.
```

Each output message is compact JSON:

```json
{"expenseId":"EXP-TRV-Dbb850d","decision":"approve","routedTo":"expense-approved","amount":450.0,"currency":"USD","vendor":"","category":"travel","policyApplied":"travel-policy.md","reason":"USD 450 airfare is standard travel with a clear USD amount, so under Travel Policy rule 5 ($1,000 or less) it is approved.","decidedAt":"2026-11-13T06:48:34+00:00"}
```

`python scripts/demo.py e2e` runs the whole story and checks it. It clears the output queues, re-seeds the default policies, and sends the four samples with fresh IDs. It expects travel and equipment to be approved, the dinner to go to review, and the missing amount to be flagged. Then it switches to the strict travel policy and checks that travel now goes to review while equipment is still approved. Finally, it restores the default policy.

Watch runs in Application Insights, or stream logs with `func azure functionapp logstream <app-name>`.

## Run locally

Storage runs on Azurite; the model is still a Microsoft Foundry deployment.

1. Start Azurite (`azurite`, or `docker run -p 10000-10002:10000-10002 mcr.microsoft.com/azure-storage/azurite`).
2. Create the settings file and point it at a Foundry project. After `azd up`, `azd env get-value FOUNDRY_PROJECT_ENDPOINT` prints the endpoint.

   ```bash
   cp src/local.settings.json.sample src/local.settings.json
   # set FOUNDRY_PROJECT_ENDPOINT in src/local.settings.json
   ```

3. Start the app from `src` with the virtual environment active, then use the demo script with `--local` from another terminal:

   ```bash
   cd src && func start
   python scripts/demo.py seed --local
   python scripts/demo.py send --local
   python scripts/demo.py results --local
   python scripts/demo.py e2e --local
   ```

Locally the app uses your `az login` identity for Foundry, and the tools create the `policies` and `decisions` containers in Azurite on first use.

## Guardrails built in

- **Routing rules enforced in code.** `route_expense_decision` rejects a decision when:
  - the queue doesn't match the decision (`approve` → `expense-approved`, `review` → `expense-review`, `flag` → `expense-flagged`);
  - a non-USD amount is approved;
  - an `approve` has no positive amount;
  - the reason is missing;
  - the policy name or expense ID is invalid.

  The skill gets the errors back and can correct itself.
- **Exactly one decision per expense.** Before sending, the tool writes `decisions/<expenseId>.json` with `overwrite=False`. A retried queue message or a second call returns `duplicate` and sends nothing. If the send fails, the marker is removed so a retry can succeed.
- **Only known documents.** `read_policy` accepts only `*.md` names that exist in the `policies` container. Paths like `../x.md` or `a/b.md` are rejected.
- **Message text is data.** The skill instructions treat the request as content to evaluate, not as instructions to follow.
- **No extra reach.** `web_request` and MCP are disabled; the skill can only call its three tools.
- **Poison messages.** `host.json` sets `messageEncoding: none` so plain-text messages work. After 5 failed attempts a message moves to `expense-requests-poison`.

## Test

From the repository root:

```bash
python scripts/test.py --template expense-policy-queue               # Bicep build, skill/tool loading, unit tests
python scripts/test.py --template expense-policy-queue --level local # + func start against Azurite
python scripts/test.py --template expense-policy-queue --level deploy --down  # + azd up, demo.py e2e, azd down
```

The unit tests (`tests/`) run offline with fakes for the Storage clients. They cover policy name validation, routing rules, idempotency, and the e2e checker.

## Make it yours

| To change | Edit |
|---|---|
| Spending rules | The Markdown in the `policies` container. Start from `policies/*.md`, then edit in Blob Storage or run `python scripts/demo.py seed --force` to re-upload. |
| Add a category | Add a document with an `**Applies to:**` line; `list_policies` picks it up automatically. |
| Extraction, decision steps, tone of reasons | `src/expense_processor.agent.md` |
| Hard limits the model can't override | `src/tools/route_expense_decision.py` |
| Where decisions go (ERP, approvals, Teams) | Consume the three output queues, or change `send_to_queue` in `src/tools/_storage.py` |
| Model | `azd env set FOUNDRY_MODEL ...` (and the deployment in `infra/app/foundry.bicep`) |

## Troubleshooting

- **`azd up` fails while uploading the package with `403 ... not authorized to perform this operation`.** The app's role assignments were just created and haven't reached Storage yet. Wait a minute and run `azd deploy`.
- **`postdeploy` waits for Blob access.** Your account's Storage Blob Data Contributor role is still propagating; the hook retries for up to 3 minutes. Run `azd hooks run postdeploy` if it gives up.
- **Nothing reaches the output queues.** Check the function's logs in Application Insights. Messages that fail 5 times move to `expense-requests-poison`.

## Clean up

```bash
azd down --purge
```
