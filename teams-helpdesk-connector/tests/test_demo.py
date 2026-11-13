# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json

import demo
from postdeploy import trigger_config_body


def test_validate_checks_records_and_admin_rights(monkeypatch):
    records = {
        ("replies", "m1"): {"outcome": "answered"},
        ("replies", "m2"): {"outcome": "ticket_created"},
        ("tickets", "m2"): {"access_level": "standard", "admin_rights_granted": True},
        ("replies", "m3"): {"outcome": "escalated"},
    }
    monkeypatch.setattr(demo, "read_record", lambda target, prefix, mid: records.get((prefix, mid)))
    answers = {
        "m1": {"outcome": "answered", "reply": "Use the VPN client."},
        "m2": {"outcome": "ticket_created", "reply": "Ticket SR-1."},
        "m3": {"outcome": "escalated", "reply": "A person will help."},
        "m4": {"error": "HTTP 500"},
    }
    errors = demo.validate(None, answers, {})
    assert errors == [
        "m2: ticket grants elevated access",
        "m3: outcome escalated but no escalation recorded",
        "m4: HTTP 500",
    ]


def test_sample_questions_cover_every_outcome():
    outcomes = {o for q in demo.load_questions() for o in q["expected"]}
    assert outcomes == demo.OUTCOMES


def test_trigger_config_targets_connector_webhook():
    values = {
        "function": "teams_helpdesk", "connection": "teams", "team": "team-guid", "channel": "19:abc@thread.tacv2",
        "app": "func-x", "resource_group": "rg", "subscription": "sub",
    }
    body = trigger_config_body(values, "func-x.azurewebsites.net", "secret")["properties"]
    assert body["operationName"] == "OnNewChannelMessage"
    assert body["notificationDetails"]["callbackUrl"] == (
        "https://func-x.azurewebsites.net/runtime/webhooks/connector?functionName=teams_helpdesk"
    )
    assert body["notificationDetails"]["authentication"]["value"] == "secret"
    assert {p["name"]: p["value"] for p in body["parameters"]} == {"groupId": "team-guid", "channelId": "19:abc@thread.tacv2"}
    json.dumps(body)
