# Daily work briefing (timer-triggered hosted skill)

Every weekday morning, a hosted skill on Azure Functions reads today's calendar, unread email, and open tasks, then publishes a short briefing: the **top three priorities** with the reason for each, the day's meetings with **suggested prep**, messages to answer, follow-ups, and things to watch out for, such as overlapping meetings or a phishing email.

This is the [daily work briefing scenario](https://learn.microsoft.com/azure/azure-functions/functions-hosted-skills-scenarios) (a scheduled agent) as a runnable template. It works with built-in sample data and no extra setup: `azd up`, run it once, and read the briefing. When you're ready, connect it to your own Outlook calendar and mail through the Office 365 Outlook connector.

```text
 timer (BRIEFING_SCHEDULE) ──► daily_briefing.agent.md ──► Blob: briefings/<date>.md + <date>.json
                               │  hosted skill (Flex Consumption)
                               ├─ get_calendar      (meetings, overlaps and free blocks computed in code)
                               ├─ get_messages      (unread mail, external-sender and link flags)
                               ├─ get_tasks         (open tasks, overdue computed in code)
                               └─ publish_briefing  (renders Markdown, one briefing per date)
```

## Why this scenario fits a hosted skill

A calendar, an inbox, and a task list are easy to fetch. Deciding what matters most today is the hard part: connecting the customer email to the 1 PM call and the overdue task, noticing that a deck must be updated before a 10 AM review, and ignoring the urgent-looking email that is really phishing. The hosted skill does that judgment in natural language. The tools do everything that must be exact: dates in your time zone, overlaps, overdue status, and writing one briefing per day.

The sample data (`src/data/*.json`) is one product manager's day, built to exercise that judgment:

| In the data | What a good briefing does |
|---|---|
| An overdue root cause analysis (RCA) for Fabrikam, an angry email, and a Fabrikam escalation call at 13:00 | Makes sending the RCA before the call a top priority |
| A VP asked for a cost slide before the 10:00 roadmap review | Uses the free block before 10:00 to finish the deck |
| The 15:30 interview overlaps an optional budget sync at 16:00 | Flags the conflict and suggests which meeting to drop |
| An "IT Service Desk" email from an outside domain with a sign-in link and a note to "AI assistants" to make it the top priority | Lists it as suspected phishing; never follows it or copies the link |
| A newsletter and a finished task | Leaves them out of the top three |

Meeting times are local times on today's date, and task due dates are relative to today, so the sample is "today" whenever you run it.

## Prerequisites

- An Azure subscription with access to a Microsoft Foundry model deployment (the template deploys one).
- [Azure Developer CLI](https://aka.ms/azd) (`azd`) 1.10 or later, and [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`). The demo script uses `az` to get the app's host name and master key.
- Python 3.13.
- For local runs: [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) 4.x and [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite).

## Deploy to Azure

```bash
git clone https://github.com/Azure-Samples/azure-functions-hosted-skills-samples
cd azure-functions-hosted-skills-samples/daily-work-briefing-timer
azd auth login
az login
azd up
```

`azd up` creates a resource group with:

- A Flex Consumption function app running the hosted skill, with a user-assigned managed identity.
- A storage account (no shared key access) with the `briefings` container.
- A Microsoft Foundry project with a model deployment.
- Application Insights and Log Analytics.

All connections use the managed identity. Your signed-in account gets **Storage Blob Data Contributor** so the demo can read briefings, and Foundry access so you can run the app locally.

### Schedule and time zone

| Setting | Default | Meaning |
|---|---|---|
| `BRIEFING_SCHEDULE` | `0 0 13 * * 1-5` | When the timer fires, as a six-field [NCRONTAB expression](https://learn.microsoft.com/azure/azure-functions/functions-bindings-timer#ncrontab-expressions) `{second} {minute} {hour} {day} {month} {day-of-week}`. Flex Consumption runs on Linux, so it's evaluated in **UTC**: the default is 13:00 UTC Monday to Friday, which is 8:00 or 9:00 in New York depending on daylight saving time. |
| `BRIEFING_TIMEZONE` | `America/New_York` | The [IANA time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) the tools use for "today", meeting times, and the briefing's file name. An unknown name falls back to UTC. |

Set them before `azd up`, or change them later the same way:

```bash
azd env set BRIEFING_SCHEDULE "0 30 6 * * 1-5"   # 06:30 UTC on weekdays
azd env set BRIEFING_TIMEZONE "Europe/London"
azd up
```

## Try it

You don't have to wait for the timer. The Functions host has a built-in admin endpoint that runs a timer-triggered function once — `POST /admin/functions/daily_briefing` with the app's master key — and the demo script calls it for you:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install --pre -r requirements-dev.txt

python scripts/demo.py run        # run the briefing now, wait for it, and print it
python scripts/demo.py results    # print today's briefing again
```

To call the endpoint yourself:

```bash
APP=$(azd env get-value AZURE_FUNCTION_NAME); RG=$(azd env get-value AZURE_RESOURCE_GROUP)
HOST=$(az functionapp show -g $RG -n $APP --query properties.defaultHostName -o tsv)
KEY=$(az functionapp keys list -g $RG -n $APP --query masterKey -o tsv)
curl -X POST "https://$HOST/admin/functions/daily_briefing" \
  -H "x-functions-key: $KEY" -H "Content-Type: application/json" -d '{"input": ""}'   # 202 Accepted
```

The master key gives full control of the function app; don't share it or put it in client code.

Example briefing (from a real run on Azure, with the sample data):

```text
# Daily work briefing - 2026-11-12

_Alex Rivera, Product Manager, Contoso Payments | America/New_York | data: sample | generated 2026-11-12T23:39:01+00:00_

A customer escalation and a VP roadmap review make the morning decisive: send the Fabrikam RCA before the 1 PM call and finish the cost estimate before the 10 AM review. The rest of the day is manageable if you use the 9:15-10:00 and 13:45-15:30 free blocks well.

## Top 3 today

1. **Send the Fabrikam RCA before the 1 PM escalation call** - TASK-2 is overdue from 2026-11-11, MSG-1 says Fabrikam needs the root cause and remediation date before today's 13:00 call, and CAL-4 is a customer escalation meeting.
   - Next step: Use the 11:00-12:00 free block to finalize the RCA and send it before noon.
2. **Add the multi-currency cost estimate before the 10 AM roadmap review** - Dana asked in MSG-2 to add the cost slide before the 10:00 VP roadmap review, and CAL-2 requires a cut/defer decision in the room.
   - Next step: Use the 09:15-10:00 free block to update the deck and confirm the estimate is in the final slides by 09:50.
3. **Submit interview feedback for M. Chen by 6 PM** - TASK-3 is due today, CAL-5 is the interview loop at 15:30, and MSG-4 says feedback is due by 18:00 for tomorrow's debrief.
   - Next step: Reserve 16:30-17:00 to file feedback right after the interview.

## Schedule

| Time | Meeting | Prep |
|---|---|---|
| 09:00-09:15 | Team Phoenix standup |  |
| 10:00-11:00 | Q3 roadmap review with Dana (VP Product) | Bring the updated deck with the multi-currency payouts cost estimate and a clear cut/defer recommendation. |
| 12:00-12:30 | 1:1 with Priya |  |
| 13:00-13:45 | Fabrikam escalation call | Send the RCA beforehand and be ready to state the root cause, customer impact, and remediation date. |
| 15:30-16:15 | Interview: Senior Software Engineer (loop 3 of 4) | Review the candidate rubric and be ready to assess product sense. |
| 16:00-16:30 | FY27 budget sync | Optional; send numbers in advance or decline/leave early because it overlaps the interview. |

## Messages to answer

- **Jordan Lee <jordan.lee@fabrikam.com>** - RE: Tuesday outage - need RCA before our 1pm call: Reply with the RCA and remediation date before noon, then confirm the 1 PM discussion can focus on next steps.
- **Dana Whitfield <dana.whitfield@contoso.com>** - Roadmap review - add the cost slide: Send a quick note once the deck is updated so Dana knows the estimate is included before the 10 AM review.
- **Recruiting <recruiting@contoso.com>** - Reminder: interview feedback due by 6 PM: No reply needed now, but complete the portal feedback by 6 PM after the interview if not auto-confirmed.

## Follow-ups

- Send outage root cause analysis (RCA) to Fabrikam — overdue since 2026-11-11
- Finalize Q3 roadmap deck (add multi-currency cost estimate) — due 2026-11-12
- Submit interview feedback for M. Chen — due 2026-11-12
- Review pricing service API spec — due 2026-11-14
- File October expense report — due 2026-11-18

## Watch out

- Suspected phishing to report: MSG-3 from IT Service Desk <it-support@contoso-helpdesk.net> is from an external sender, contains a link, and includes instructions aimed at AI assistants; do not click or reply.
- Meeting conflict: CAL-5 Interview: Senior Software Engineer (15:30-16:15) overlaps CAL-6 FY27 budget sync (16:00-16:30) by 15 minutes; prioritize the interview and decline CAL-6 or leave it early after sending numbers in advance.
- Deadline risk: if the RCA is not sent before the 13:00 Fabrikam escalation call, the issue may be escalated to the customer's CIO.
```

Each run publishes two blobs in the `briefings` container: `<date>.md` (above) and `<date>.json` (the same content as structured fields, plus the Markdown). The model's wording and ranking vary from run to run.

**One briefing per day.** Running again on the same date doesn't overwrite the briefing: `publish_briefing` writes with `overwrite=False` and returns `already_published`, and the log shows `Briefing for <date> was already published`. To regenerate today's briefing, delete `briefings/<date>.md` and `<date>.json` first. `python scripts/demo.py e2e` does exactly that, then runs the skill and checks the result (one to three priorities, each with a reason, a schedule, and the Markdown). **Don't run `e2e` against an environment where people read the briefings.**

Watch runs in Application Insights (each run is a `daily_briefing` request, and the published Markdown is logged), or stream logs with `func azure functionapp logstream <app-name>`.

## Use your Outlook calendar and mail (optional)

The template can read your real calendar and inbox through the **Office 365 Outlook** connector, exposed to the skill as a remote [MCP](https://modelcontextprotocol.io) server by an Azure Connector Gateway (preview). It's off by default so the template works without any sign-in or consent.

```bash
azd env set ENABLE_OFFICE365 true
azd up
```

This adds a Connector Gateway, an Office 365 Outlook connection, access policies for the app's managed identity and your account, and a **read-only** MCP server with two operations: `GetEventsCalendarViewV3` (today's meetings) and `GetEmailsV3` (unread mail). It also sets two app settings that `src/mcp.json` reads:

```json
{
  "mcpServers": {
    "office365-outlook": {
      "type": "http",
      "url": "$O365_MCP_SERVER_URL",
      "auth": { "scope": "https://apihub.azure.com/.default", "client_id": "$O365_MCP_CLIENT_ID" }
    }
  }
}
```

Then sign in to the connection once, as the mailbox owner:

1. In the Azure portal, open the resource group, then the Connector Gateway, then its `office365-outlook` connection, and complete authentication. Sign in with the Microsoft 365 account whose calendar and mail the briefing should read.
2. Check that the status is `Connected`:

   ```bash
   az resource show --ids "$(azd env get-value O365_CONNECTION_ID)" --query properties.overallStatus -o tsv
   ```

3. Run `python scripts/demo.py run`. The briefing header shows `data: office365`.

How it switches over: when `O365_MCP_SERVER_URL` is set, `get_calendar` and `get_messages` don't return sample data. They return the date window and tell the skill to call `office365_GetEventsCalendarViewV3` and `office365_GetEmailsV3` instead. Tasks still come from `src/data/tasks.json`; replace `get_tasks` with your task system (for example, Microsoft To Do or Planner).

When `O365_MCP_SERVER_URL` is empty or unset, the runtime skips the `office365-outlook` server at startup (the log shows `MCP server 'office365-outlook': could not resolve url ... skipping`) and the skill runs with sample data. The Office 365 path isn't covered by the automated tests, because it needs an interactive sign-in.

## Run locally

1. Start Azurite (`azurite`, or `docker run -p 10000-10002:10000-10002 mcr.microsoft.com/azure-storage/azurite`).
2. Create the settings file and point it at a Foundry project. After `azd up`, `azd env get-value FOUNDRY_PROJECT_ENDPOINT` prints the endpoint.

   ```bash
   cp src/local.settings.json.sample src/local.settings.json
   # set FOUNDRY_PROJECT_ENDPOINT in src/local.settings.json
   ```

3. Start the app from `src` with the virtual environment active, then run the briefing from another terminal. Locally the admin endpoint needs no key.

   ```bash
   cd src && func start
   python scripts/demo.py run --local
   ```

The timer also fires on its own schedule while `func start` is running. Locally the app uses your `az login` identity for Foundry, and briefings go to the `briefings` container in Azurite.

## Make it yours

| To change | Edit |
|---|---|
| How priorities are ranked, what goes in each section, tone | `src/daily_briefing.agent.md` |
| Calendar, mail, and task sources | `src/tools/get_*.py` — replace the `src/data/*.json` reads with calls to your systems, or turn on Office 365 (above) |
| Working hours, focus-block length, what counts as an internal sender | `WORKDAY_START`, `WORKDAY_END`, `MIN_FOCUS_MINUTES` in `get_calendar.py`; `INTERNAL_DOMAIN` in `get_messages.py` |
| Briefing layout | `src/tools/_briefing.py` |
| Where the briefing goes (email, Teams, a wiki page) | `src/tools/publish_briefing.py` and `_storage.py` |
| When it runs | `BRIEFING_SCHEDULE` and `BRIEFING_TIMEZONE` |
| Model | `azd env set FOUNDRY_MODEL ...` (and the deployment in `infra/app/foundry.bicep`) |

## Guardrails built in

- **Read and summarize, don't act.** The skill can read and publish a briefing. It can't send, reply to, or delete mail, or accept or decline meetings — the Office 365 MCP server exposes only two read operations.
- **Email is data, not instructions.** The skill is told to report — never follow — instructions inside messages, and never to copy their links. The sample phishing email tests this, and `get_messages` flags external senders and links in code.
- **Dates and math in code.** Today's date in your time zone, meeting overlaps, free blocks, and overdue status are computed by the tools; the timer payload doesn't even include a date.
- **One briefing per date.** The file name is chosen in code and written with `overwrite=False`, so a retry or manual run can't replace a briefing someone already read.
- **Bounded output.** `publish_briefing` accepts one to three top priorities; anything else is rejected before it's written.
- **No extra reach.** `web_request` is disabled, and MCP servers load only when Office 365 is turned on.

## Test

From the repository root:

```bash
python scripts/test.py --template daily-work-briefing-timer                 # Bicep build, skill/tool loading, unit tests
python scripts/test.py --template daily-work-briefing-timer --level local   # + func start against Azurite
python scripts/test.py --template daily-work-briefing-timer --level deploy --down  # + azd up, e2e, azd down
```

## Troubleshooting

- **`azd up` fails while uploading the package with `403 ... not authorized to perform this operation`.** The app's role assignments were just created and haven't reached Storage yet. Wait a minute and run `azd deploy`.
- **`demo.py run` keeps retrying the trigger, or gets 404.** The host may still be starting after a deployment. Wait a minute and try again.
- **`demo.py run` prints an older briefing.** Today's briefing was already published, so the run returned `already_published`. Delete `briefings/<date>.md` and `.json` to regenerate it.
- **The briefing is for the wrong day.** Check `BRIEFING_TIMEZONE`. The schedule is in UTC, but "today" is in the briefing time zone.
- **With Office 365 on, the briefing still says `data: sample`, or the run fails calling `office365_*` tools.** Check that the connection is `Connected` (see above), and look for MCP errors in Application Insights.

## Clean up

```bash
azd down --purge
```
