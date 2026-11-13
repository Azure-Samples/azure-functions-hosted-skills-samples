"""Storage for the router: dedupe fingerprints and decisions (Table), work queues (Queue).

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite), or the
``AzureWebJobsStorage__tableServiceUri`` / ``__queueServiceUri`` settings with the app's
managed identity in Azure.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import Any, Literal

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError

Claim = Literal["new", "replay", "duplicate"]


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))


def _connection_string() -> str:
    return os.environ.get("AzureWebJobsStorage", "")


class AzureAlertStore:
    """Real store. ``FakeAlertStore`` in the tests implements the same three methods."""

    def __init__(self) -> None:
        self.decisions_table = os.environ.get("DECISIONS_TABLE", "alertdecisions")
        self.fingerprints_table = os.environ.get("FINGERPRINTS_TABLE", "alertfingerprints")

    @cache
    def _table(self, name: str):
        from azure.data.tables import TableServiceClient

        if _connection_string():
            service = TableServiceClient.from_connection_string(_connection_string())
            # Local development (Azurite): create on first use. Azure tables come from Bicep.
            service.create_table_if_not_exists(name)
        else:
            service = TableServiceClient(os.environ["AzureWebJobsStorage__tableServiceUri"], credential=_credential())
        return service.get_table_client(name)

    @cache
    def _queue(self, name: str):
        from azure.storage.queue import QueueClient

        if _connection_string():
            queue = QueueClient.from_connection_string(_connection_string(), name)
            try:
                queue.create_queue()
            except ResourceExistsError:
                pass
            return queue
        return QueueClient(os.environ["AzureWebJobsStorage__queueServiceUri"], name, credential=_credential())

    def claim_fingerprint(self, fingerprint: str, bucket: int, alert: dict[str, Any]) -> Claim:
        """Record the first alert with this fingerprint in the window.

        Returns ``new`` for the first alert, ``replay`` when the same alert is delivered
        again (Event Hubs is at-least-once), and ``duplicate`` for a different alert with
        the same fingerprint, whose count is added to the first one.
        """
        table = self._table(self.fingerprints_table)
        entity = {
            "PartitionKey": fingerprint,
            "RowKey": str(bucket),
            "FirstAlertId": alert["alert_id"],
            "RuleId": alert["rule_id"],
            "Host": alert["host"],
            "User": alert["user"],
            "DuplicateCount": 0,
        }
        try:
            table.create_entity(entity)
            return "new"
        except ResourceExistsError:
            pass
        for _ in range(5):
            existing = table.get_entity(fingerprint, str(bucket))
            if existing.get("FirstAlertId") == alert["alert_id"]:
                return "replay"
            existing["DuplicateCount"] = int(existing.get("DuplicateCount", 0)) + 1
            existing["LastDuplicateAlertId"] = alert["alert_id"]
            try:
                table.update_entity(
                    existing,
                    etag=existing.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return "duplicate"
            except HttpResponseError as exc:
                if exc.status_code != 412:
                    raise
        return "duplicate"

    def record_decision(self, record: dict[str, Any]) -> None:
        entity = {
            "PartitionKey": record["routed_at"][:10],
            "RowKey": record["group_id"],
            **{k: v for k, v in record.items() if isinstance(v, str | int | float | bool)},
            # Lists and dicts are stored as JSON strings.
            "alert_ids": json.dumps(record["alert_ids"]),
            "probabilities": json.dumps(record.get("probabilities") or {}),
        }
        self._table(self.decisions_table).upsert_entity(entity)

    def enqueue(self, queue_name: str, message: dict[str, Any]) -> None:
        self._queue(queue_name).send_message(json.dumps(message))
