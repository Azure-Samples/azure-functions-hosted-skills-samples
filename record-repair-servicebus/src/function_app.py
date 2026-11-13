"""Function app: the existing ``ingest`` function plus the ``record_repair`` hosted skill.

``create_function_app()`` registers every ``*.agent.md`` hosted skill in this folder. ``ingest``
below is the pipeline you already had: ordinary Azure Functions code with no model calls. It
rejects records that don't match ``record-schema.json``; after a few failed deliveries Service
Bus moves them to the dead-letter queue, where the hosted skill picks them up.
"""

import json
import logging

import azure.functions as func
from azure_functions_agents import create_function_app

from ingestion import records, storage

app = create_function_app()


@app.function_name("ingest")
@app.service_bus_queue_trigger(arg_name="message", queue_name="%INGEST_QUEUE%", connection="ServiceBusConnection")
def ingest(message: func.ServiceBusMessage) -> None:
    try:
        record = json.loads(message.get_body())
    except ValueError as exc:
        raise ValueError(f"message {message.message_id} is not JSON") from exc
    errors = records.validation_errors(record)
    if errors:
        # Raising abandons the message. After maxDeliveryCount attempts it is dead-lettered.
        raise ValueError(f"message {message.message_id} failed validation: {'; '.join(errors)}")

    properties = message.application_properties or {}
    blob_name, document = records.build_output(
        record,
        message.enqueued_time_utc,
        repaired=records.is_repaired(properties),
        repair_changes=str(properties.get("repair_changes", "")),
    )
    if not storage.write_once(storage.processed_container_name(), blob_name, document):
        logging.info("ingest duplicate record_id=%s blob=%s", record["record_id"], blob_name)
        return
    logging.info("ingest completed record_id=%s repaired=%s blob=%s", record["record_id"], document["repaired"], blob_name)
