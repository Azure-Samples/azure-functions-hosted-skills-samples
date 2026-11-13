# Invoice review (blob-triggered hosted skill)

A supplier invoice PDF is uploaded to Blob Storage. A hosted skill on Azure Functions reads it, matches it against the purchase order, goods receipt, and vendor contract, then records a review — **approve for payment**, **hold for review**, or **reject** — with every discrepancy, the amount in dispute, and a message draft for the vendor.

This is the [invoice review scenario](https://learn.microsoft.com/azure/azure-functions/functions-hosted-skills-scenarios#process-uploaded-invoices) as a runnable template: `azd up`, upload the sample invoices, and read the reviews.

![Diagram of a Blob Storage invoice upload starting a hosted skill, which calls document and business tools and creates an invoice review report.](media/architecture.png)

*Scenario overview from [Process uploaded invoices](https://learn.microsoft.com/azure/azure-functions/functions-hosted-skills-scenarios#process-uploaded-invoices) on Microsoft Learn. The diagram below shows how this template implements it.*

```text
 invoice PDF ──► Blob: invoices/{name} ──► Event Grid ──► invoice_review.agent.md ──► Blob: invoice-reviews/{name}.json
                                                           │  hosted skill (Flex Consumption)
                                                           ├─ extract_invoice           (Document Intelligence or PDF text)
                                                           ├─ get_purchase_order        (ordered + received quantities)
                                                           ├─ get_contract_terms        (price tolerance, freight, remit-to)
                                                           ├─ check_invoice_against_po  (three-way match, computed in code)
                                                           └─ record_invoice_review     (idempotent, approval rule enforced in code)
```

## Why this scenario fits a hosted skill

Invoices arrive in every layout, and the hard part isn't the arithmetic — it's reading the document, finding the right PO, and explaining what's wrong in terms AP and the vendor both understand. The hosted skill reads and explains; the tools do the matching math and keep the side effects deterministic. Anything that isn't a clean match goes to a person.

The three samples show the range:

| Sample | Situation | What a reviewer would expect |
|---|---|---|
| `INV-FAB-1001.pdf` | Matches PO-4001 exactly; freight within the contract limit | `approve_for_payment` |
| `INV-NW-2002.pdf` | Bills 500 boxes when 400 were received, tape 7% above the contract price, and freight the contract doesn't allow — 522.00 in dispute | `hold_for_review`, needs approval |
| `INV-TS-3003.pdf` | Unknown vendor and PO, and asks to pay a new bank account today | `reject`, needs approval |

## Prerequisites

- An Azure subscription with access to a Microsoft Foundry model deployment (the template deploys one).
- [Azure Developer CLI](https://aka.ms/azd) (`azd`) 1.10 or later, and [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`). The postdeploy hook uses `az`.
- Python 3.13. On Windows, the hook runs in [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows) (`pwsh`).
- For local runs: [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) 4.x and [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite).

## Deploy to Azure

```bash
git clone https://github.com/Azure-Samples/azure-functions-hosted-skills-samples
cd azure-functions-hosted-skills-samples/invoice-review-blob
azd auth login
az login
azd up
```

`azd up` creates a resource group with:

- A Flex Consumption function app running the hosted skill, with a user-assigned managed identity.
- A storage account (no shared key access) with the `invoices` and `invoice-reviews` containers.
- An Event Grid system topic on the storage account. Flex Consumption runs blob triggers through Event Grid; after the code is deployed, `scripts/postdeploy.py` subscribes the topic to the function's blob webhook for `BlobCreated` events in `invoices/`.
- A Microsoft Foundry project with a model deployment.
- Application Insights and Log Analytics.

All connections use the managed identity. Your signed-in account also gets data-plane roles so you can run the demo and the app locally.

### Read invoices with Azure AI Document Intelligence

By default, `extract_invoice` reads the PDF's text with pypdf and the model finds the fields. That works for the samples and other text-based PDFs. For scanned invoices, or to get structured fields such as `InvoiceId`, `PurchaseOrder`, and `Items`, turn on the [prebuilt invoice model](https://learn.microsoft.com/azure/ai-services/document-intelligence/prebuilt/invoice):

```bash
azd env set USE_DOCUMENT_INTELLIGENCE true
azd up
```

This adds a Document Intelligence resource (S0, Entra ID only) and sets `DOCUMENT_INTELLIGENCE_ENDPOINT` on the app.

## Try it

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install --pre -r requirements-dev.txt

python scripts/demo.py upload               # upload all three sample invoices
python scripts/demo.py results --wait 300   # wait for and print the reviews
```

Example output (from a real run):

```text
INV-NW-2002: hold_for_review (needs approval) total 2882.0 / in dispute 522.0
  ! BOX-241812 billed quantity exceeds received quantity: billed 500, received 400.
  ! TAPE-48 unit price exceeds PO/contract tolerance: billed 57.90 vs agreed 54.00.
  ! Freight of 220.00 is billed even though the contract does not allow freight charges.
  - Contract MSA-NWP-2026 states prices are delivered FOB destination and freight is not billable.

INV-TS-3003: reject (needs approval) total 3968.0 / in dispute 3968.0
  ! PO PO-4099 does not exist.
  ! Invoice requests payment to a new bank account ending 7781 and says to ignore bank details on file.
```

Each review is a JSON document in the `invoice-reviews` container, named after the invoice file. Upload one sample with `--sample INV-TS-3003`, or your own file with `--file my-invoice.pdf` (add its PO to `src/data` first). `python scripts/demo.py e2e` uploads uniquely named copies and checks that each one was reviewed with reasons and the approval rule.

Watch the run in Application Insights, or stream logs with `func azure functionapp logstream <app-name>`.

## Run locally

Azurite has no Event Grid, so locally the blob trigger polls the container instead (`INVOICE_BLOB_SOURCE=LogsAndContainerScan` in the settings sample). Polling can take a few seconds to notice a new blob.

1. Start Azurite (`azurite`, or `docker run -p 10000-10002:10000-10002 mcr.microsoft.com/azure-storage/azurite`).
2. Create the settings file and point it at a Foundry project. After `azd up`, `azd env get-value FOUNDRY_PROJECT_ENDPOINT` prints the endpoint.

   ```bash
   cp src/local.settings.json.sample src/local.settings.json
   # set FOUNDRY_PROJECT_ENDPOINT in src/local.settings.json
   ```

3. Start the app from `src` with the virtual environment active, then upload invoices to Azurite from another terminal:

   ```bash
   cd src && func start
   python scripts/demo.py upload --local
   python scripts/demo.py results --local --wait 300
   ```

Locally the app uses your `az login` identity for Foundry (and Document Intelligence, if you set `DOCUMENT_INTELLIGENCE_ENDPOINT`).

## Make it yours

| To change | Edit |
|---|---|
| Decision rules, fraud signals, tone of the vendor message | `src/invoice_review.agent.md` |
| PO, goods receipt, and contract lookups | `src/tools/get_*.py` — replace the `src/data/*.json` reads with calls to your ERP |
| Matching rules (tolerances, freight, partial receipts) | `src/tools/check_invoice_against_po.py` |
| Where reviews go (ERP hold, AP queue, Teams) | `src/tools/record_invoice_review.py` and `_storage.py` |
| Sample invoices | `scripts/make_samples.py`, then `python scripts/make_samples.py` |
| Model | `azd env set FOUNDRY_MODEL ...` (and the deployment in `infra/app/foundry.bicep`) |

## Guardrails built in

- **Recommend, don't act.** The skill records a review. Payments and holds stay in your ERP behind its own approval flow.
- **Approval enforced in code.** `record_invoice_review` forces `requires_human_approval` for every decision other than `approve_for_payment`, and whenever discrepancies or an amount in dispute are recorded — whatever the model says.
- **Math in code.** `check_invoice_against_po` does the three-way match and every calculation with `Decimal`; the model only explains the results.
- **Invoice content is data, not instructions.** The skill is told to report — never follow — text such as "pay a new bank account today", and the third sample tests it.
- **Idempotent.** Blob events can be delivered more than once. The review is written with `overwrite=False`; a retry returns `duplicate` instead of a second write.
- **Bounded input.** `extract_invoice` rejects files over 10 MB and paths outside the `invoices` container.
- **No extra reach.** `web_request` and MCP are disabled for this skill; it can only call its five tools.

## Test

From the repository root:

```bash
python scripts/test.py --template invoice-review-blob               # Bicep build, skill/tool loading, unit tests
python scripts/test.py --template invoice-review-blob --level local # + func start against Azurite
python scripts/test.py --template invoice-review-blob --level deploy --down  # + azd up, e2e, azd down
```

## Troubleshooting

- **`azd up` fails while uploading the package with `403 ... not authorized to perform this operation`.** The app's role assignments were just created and haven't reached Storage yet. Wait a minute and run `azd deploy`.
- **The postdeploy hook gives up waiting for the `blobs_extension` key or the Event Grid subscription.** The host may still be starting. Run `azd hooks run postdeploy` to try again.
- **Uploads don't produce reviews in Azure.** Check that the `invoices-blob-created` subscription exists on the Event Grid system topic (`az eventgrid system-topic event-subscription list -g <rg> --system-topic-name <topic>`), then check the function's logs in Application Insights.

## Clean up

```bash
azd down --purge
```
