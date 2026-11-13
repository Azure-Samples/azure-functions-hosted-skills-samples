import json

import _storage
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from create_software_request import SoftwareRequest, create_software_request
from escalate_to_human import Escalation, escalate_to_human
from get_password_reset_guidance import PasswordHelpRequest, get_password_reset_guidance
from post_reply import Reply, post_reply
from search_knowledge_base import KnowledgeBaseQuery, search_knowledge_base


class FakeBlob:
    def __init__(self, blobs, name):
        self.blobs, self.name = blobs, name

    def upload_blob(self, data, overwrite=False, **_):
        if self.name in self.blobs and not overwrite:
            raise ResourceExistsError("exists")
        self.blobs[self.name] = data


class FakeDownload:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data.encode()


class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, str] = {}

    def get_blob_client(self, name):
        return FakeBlob(self.blobs, name)

    def download_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        return FakeDownload(self.blobs[name])


@pytest.fixture
def container(monkeypatch):
    fake = FakeContainer()
    monkeypatch.setattr(_storage, "_container", lambda: fake)
    return fake


def test_knowledge_base_finds_vpn_article():
    result = search_knowledge_base(KnowledgeBaseQuery(query="VPN says gateway unreachable at the hotel"))
    assert result["articles"][0]["id"] == "KB-1001"
    assert result["confident_match"] is True


def test_knowledge_base_is_not_confident_about_unknown_issues():
    result = search_knowledge_base(KnowledgeBaseQuery(query="coffee spilled, screen flickers green"))
    assert result["confident_match"] is False


def test_password_guidance_never_handles_passwords():
    result = get_password_reset_guidance(PasswordHelpRequest(situation="forgot"))
    assert result["self_service_url"] == "https://aka.ms/sspr"
    assert result["needs_human"] is False
    assert get_password_reset_guidance(PasswordHelpRequest(situation="no_registered_method"))["needs_human"] is True


def _request(software, text="", message_id="msg-1"):
    return SoftwareRequest(message_id=message_id, requester="Sam", software=software, original_text=text)


def test_software_ticket_is_standard_access_and_idempotent(container):
    first = create_software_request(_request("Visio", "Can I get Visio for diagrams?"))
    assert first["created"] is True
    assert first["ticket_id"].startswith("SR-")
    assert first["access_level"] == "standard" and first["admin_rights_granted"] is False

    again = create_software_request(_request("Visio", "Can I get Visio for diagrams?"))
    assert again["created"] is False and again["reason"] == "duplicate"
    assert again["ticket_id"] == first["ticket_id"]
    assert list(container.blobs) == ["tickets/msg-1.json"]


@pytest.mark.parametrize("software,text", [
    ("Docker Desktop", "I need local admin rights to install Docker"),
    ("local administrator", ""),
    ("Visio", "please run as administrator and elevate my account"),
])
def test_admin_rights_are_never_granted(container, software, text):
    result = create_software_request(_request(software, text))
    assert result["created"] is False
    assert result["reason"] == "admin_rights_not_granted"
    assert result["admin_rights_granted"] is False and result["escalate"] is True
    assert container.blobs == {}


def test_catalog_rules(container):
    assert create_software_request(_request("VS Code"))["reason"] == "self_service"
    assert create_software_request(_request("WinRAR"))["reason"] == "not_approved"
    unknown = create_software_request(_request("Some Unknown App"))
    assert unknown["reason"] == "not_in_catalog" and unknown["escalate"] is True
    assert container.blobs == {}


def test_escalation_is_idempotent(container):
    escalation = Escalation(message_id="msg-2", requester="Taylor", summary="Screen flickers", reason="hardware")
    first = escalate_to_human(escalation)
    second = escalate_to_human(escalation)
    assert first["created"] is True and first["queue"] == "it-service-desk"
    assert second["created"] is False and second["escalation_id"] == first["escalation_id"]


def test_post_reply_records_once_and_decides_delivery(container, monkeypatch):
    monkeypatch.setenv("TEAMS_MCP_SERVER_URL", "https://example/mcp")
    reply = Reply(message_id="msg-3", channel="teams", outcome="answered", reply="Use https://aka.ms/sspr")
    assert post_reply(reply) == {"status": "recorded", "deliver_via_teams": True, "blob": "replies/msg-3.json"}
    duplicate = post_reply(reply)
    assert duplicate["status"] == "duplicate" and duplicate["deliver_via_teams"] is False
    assert json.loads(container.blobs["replies/msg-3.json"])["outcome"] == "answered"

    http_reply = Reply(message_id="msg-4", channel="http", outcome="answered", reply="Hi")
    assert post_reply(http_reply)["deliver_via_teams"] is False


def test_post_reply_without_teams_connection(container, monkeypatch):
    monkeypatch.setenv("TEAMS_MCP_SERVER_URL", "")
    reply = Reply(message_id="msg-5", channel="teams", outcome="escalated", reply="A person will follow up.")
    assert post_reply(reply)["deliver_via_teams"] is False


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "x" * 129])
def test_message_id_is_validated(bad):
    with pytest.raises(ValueError):
        _storage.check_message_id(bad)
