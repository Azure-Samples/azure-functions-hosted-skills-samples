# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Blob and Service Bus SDK boundaries are context-managed offline mocks."""

import json
from unittest.mock import MagicMock, Mock

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

import supplier_store as store


@pytest.fixture
def container(monkeypatch):
    client = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = client
    factory = Mock(return_value=context)
    monkeypatch.setattr(store, "container_client", factory)
    return client


@pytest.fixture
def review_record():
    return {
        "change_id": "SC-test", "po_id": "PO-4101", "supplier_id": "SUP-100",
        "decision": "buyer_review", "requires_human_approval": True,
        "summary": "Private model prose", "reasons": ["Private supplier explanation"],
        "questions": ["Sensitive question"], "context": {"private": "purchasing data"},
    }


def test_read_returns_decoded_record(container, review_record):
    container.download_blob.return_value.readall.return_value = json.dumps(review_record).encode("utf-8")
    assert store.read("SC-test") == review_record
    container.download_blob.assert_called_once_with("SC-test.json")


def test_read_missing_returns_none(container):
    container.download_blob.side_effect = ResourceNotFoundError("missing")
    assert store.read("SC-test") is None


def test_read_missing_during_download_returns_none(container):
    container.download_blob.return_value.readall.side_effect = ResourceNotFoundError("deleted")
    assert store.read("SC-test") is None


@pytest.mark.parametrize("operation", ["read", "save"])
def test_invalid_id_never_opens_storage(monkeypatch, operation):
    factory = Mock()
    monkeypatch.setattr(store, "container_client", factory)
    with pytest.raises(ValueError, match="change_id"):
        if operation == "read":
            store.read("../escape")
        else:
            store.save_once({"change_id": "../escape"})
    factory.assert_not_called()


def test_save_is_conditional_json_and_returns_original_record(container, review_record):
    assert store.save_once(review_record) is review_record
    container.upload_blob.assert_called_once()
    args, kwargs = container.upload_blob.call_args
    assert args[0] == "SC-test.json"
    assert json.loads(args[1]) == review_record
    assert set(kwargs) == {"overwrite", "content_settings"}
    assert kwargs["overwrite"] is False
    assert kwargs["content_settings"].content_type == "application/json"
    container.download_blob.assert_not_called()


def test_write_race_reads_and_returns_authoritative_winner(container, review_record):
    winner = {**review_record, "decision": "request_clarification", "summary": "Already committed."}
    container.upload_blob.side_effect = ResourceExistsError("race")
    container.download_blob.return_value.readall.return_value = json.dumps(winner).encode("utf-8")
    assert store.save_once(review_record) == winner
    assert container.upload_blob.call_args.kwargs["overwrite"] is False
    container.download_blob.assert_called_once_with("SC-test.json")
    container.upload_blob.assert_called_once()


def test_write_race_missing_winner_propagates_instead_of_fabricating_success(container, review_record):
    container.upload_blob.side_effect = ResourceExistsError("race")
    error = ResourceNotFoundError("winner disappeared")
    container.download_blob.side_effect = error
    with pytest.raises(ResourceNotFoundError) as raised:
        store.save_once(review_record)
    assert raised.value is error
    container.upload_blob.assert_called_once()


@pytest.mark.parametrize("operation", ["read", "save"])
def test_storage_errors_are_not_swallowed(container, review_record, operation):
    error = RuntimeError("storage failed")
    if operation == "read":
        container.download_blob.side_effect = error
    else:
        container.upload_blob.side_effect = error
    with pytest.raises(RuntimeError) as raised:
        store.read("SC-test") if operation == "read" else store.save_once(review_record)
    assert raised.value is error


def test_corrupt_saved_json_is_not_treated_as_missing(container):
    container.download_blob.return_value.readall.return_value = b"not JSON"
    with pytest.raises(json.JSONDecodeError):
        store.read("SC-test")


@pytest.mark.parametrize("exists", [False, True], ids=["new", "existing"])
def test_connection_string_container_context(monkeypatch, exists):
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setenv("RECOMMENDATIONS_CONTAINER", "test-recommendations")
    factory = MagicMock()
    container = factory.from_connection_string.return_value.__enter__.return_value
    if exists:
        container.create_container.side_effect = ResourceExistsError("already exists")
    credential = Mock(side_effect=AssertionError("emulator must not request credentials"))
    monkeypatch.setattr(store, "ContainerClient", factory)
    monkeypatch.setattr(store, "DefaultAzureCredential", credential)
    with store.container_client() as actual:
        assert actual is container
    factory.from_connection_string.assert_called_once_with("UseDevelopmentStorage=true", "test-recommendations")
    container.create_container.assert_called_once_with()
    factory.from_connection_string.return_value.__exit__.assert_called_once_with(None, None, None)
    credential.assert_not_called()


def test_container_creation_failure_is_not_swallowed(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    factory = MagicMock()
    factory.from_connection_string.return_value.__enter__.return_value.create_container.side_effect = RuntimeError("denied")
    monkeypatch.setattr(store, "ContainerClient", factory)
    with pytest.raises(RuntimeError, match="denied"), store.container_client():
        pytest.fail("failed container creation must not yield")
    factory.from_connection_string.return_value.__exit__.assert_called_once()


def test_identity_container_uses_credential_context_and_default_name(monkeypatch):
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("RECOMMENDATIONS_CONTAINER", raising=False)
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://test.blob.core.windows.net")
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-identity")
    credential = MagicMock()
    factory = MagicMock()
    monkeypatch.setattr(store, "DefaultAzureCredential", credential)
    monkeypatch.setattr(store, "ContainerClient", factory)
    with store.container_client() as actual:
        assert actual is factory.return_value.__enter__.return_value
    identity = credential.return_value.__enter__.return_value
    credential.assert_called_once_with(managed_identity_client_id="test-identity")
    factory.assert_called_once_with(
        "https://test.blob.core.windows.net", "supplier-change-recommendations", credential=identity,
    )
    actual.create_container.assert_not_called()
    factory.return_value.__exit__.assert_called_once_with(None, None, None)
    credential.return_value.__exit__.assert_called_once_with(None, None, None)


@pytest.fixture
def notification_clients(monkeypatch):
    monkeypatch.setenv("SUPPLIER_SERVICEBUS_NAMESPACE", "test.servicebus.windows.net")
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-identity")
    credential = MagicMock()
    factory = MagicMock()
    monkeypatch.setattr(store, "DefaultAzureCredential", credential)
    monkeypatch.setattr(store, "ServiceBusClient", factory)
    client = factory.return_value.__enter__.return_value
    sender = client.get_queue_sender.return_value.__enter__.return_value
    return credential, factory, client, sender


def test_notifications_are_reference_only_with_stable_message_id(notification_clients, review_record):
    credential, factory, client, sender = notification_clients
    store.notify_buyer(review_record)
    store.notify_buyer(review_record)
    credential.assert_called_with(managed_identity_client_id="test-identity")
    factory.assert_called_with("test.servicebus.windows.net", credential.return_value.__enter__.return_value)
    client.get_queue_sender.assert_called_with("supplier-reviews")
    assert sender.send_messages.call_count == 2
    for call in sender.send_messages.call_args_list:
        message = call.args[0]
        payload = json.loads(b"".join(message.body).decode("utf-8"))
        assert payload == {
            "change_id": "SC-test", "po_id": "PO-4101", "decision": "buyer_review",
            "recommendation_blob": "SC-test.json",
        }
        assert message.message_id == "SC-test"
        assert message.content_type == "application/json"
    assert client.get_queue_sender.return_value.__exit__.call_count == 2
    assert factory.return_value.__exit__.call_count == 2
    assert credential.return_value.__exit__.call_count == 2


def test_accepted_record_opens_no_credential_or_bus_client(notification_clients, review_record, monkeypatch):
    credential, factory, _, sender = notification_clients
    monkeypatch.delenv("SUPPLIER_SERVICEBUS_NAMESPACE", raising=False)
    store.notify_buyer({**review_record, "decision": "accept_update", "requires_human_approval": False})
    credential.assert_not_called()
    factory.assert_not_called()
    sender.send_messages.assert_not_called()


def test_send_failure_propagates_and_closes_all_contexts(notification_clients, review_record):
    credential, factory, client, sender = notification_clients
    error = RuntimeError("send failed")
    sender.send_messages.side_effect = error
    with pytest.raises(RuntimeError) as raised:
        store.notify_buyer(review_record)
    assert raised.value is error
    for context in (client.get_queue_sender.return_value, factory.return_value, credential.return_value):
        context.__exit__.assert_called_once()
        assert context.__exit__.call_args.args[1] is error