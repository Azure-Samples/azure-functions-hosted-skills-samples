"""Function app: a code Event Hubs router (stage 1) plus the alert-investigator hosted skill (stage 2).

``create_function_app()`` registers every ``*.agent.md`` hosted skill in this folder. The
router below is ordinary Azure Functions code on the same app: deterministic rules first,
then Microsoft-Decision-1 for the alerts the rules can't settle.
"""

import logging
from typing import List  # noqa: UP035 - the Functions worker needs typing.List for cardinality=many

import azure.functions as func
from azure_functions_agents import create_function_app

from alert_router import router, store
from alert_router.decision import DecisionClient

app = create_function_app()

_store: store.AzureAlertStore | None = None
_decider: router.Decider | None = None


def _dependencies() -> tuple[store.AzureAlertStore, router.Decider]:
    global _store, _decider
    if _store is None:
        _store = store.AzureAlertStore()
    if _decider is None:
        _decider = router.decision1_decider(DecisionClient.from_environment())
    return _store, _decider


@app.function_name("alert_router")
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENTHUB_NAME%",
    connection="EVENTHUB",
    consumer_group="%EVENTHUB_CONSUMER_GROUP%",
    cardinality=func.Cardinality.MANY,
)
@app.retry(strategy="exponential_backoff", max_retry_count="3", minimum_interval="00:00:05", maximum_interval="00:01:00")
def alert_router(events: List[func.EventHubEvent]) -> None:
    alert_store, decider = _dependencies()
    records = router.route_batch([event.get_body() for event in events], store=alert_store, decider=decider)
    logging.info("alert_router %s", router.summarize(records, len(events)))
