# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""azd postdeploy hook: connect the Teams "new channel message" trigger to the function.

The connector gateway, Teams connection, and reply MCP server are deployed by Bicep.
The trigger config needs the function app's ``connector_extension`` system key, which
exists only after the code is deployed and the host has started, so it is created here.

The hook skips (and exits 0) until everything it needs is in place:

1. The Teams connector is enabled (``ENABLE_TEAMS_CONNECTOR`` is not ``false``).
2. ``TEAMS_TEAM_ID`` and ``TEAMS_CHANNEL_ID`` are set (``azd env set`` + ``azd provision``).
3. The Teams connection is authenticated (status ``Connected``). Run
   ``python scripts/postdeploy.py --consent-link`` to get the sign-in link.

Running it again updates the same trigger config.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

API_VERSION = "2026-05-01-preview"
TRIGGER_OPERATION = "OnNewChannelMessage"
CONSENT_REDIRECT = "https://connectors.azure.com"


def az(*args: str) -> str:
    executable = shutil.which("az")
    if executable is None:
        raise SystemExit("Azure CLI (az) is required for the postdeploy hook: https://aka.ms/azure-cli")
    # Keep '&' and other shell characters out of the arguments: on Windows az is a .cmd
    # script that cmd.exe parses. Request bodies go in a file instead.
    result = subprocess.run(
        [executable, *args, "--only-show-errors"], capture_output=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"az exited with {result.returncode}")
    return result.stdout.strip()


def az_rest(method: str, uri: str, body: dict | None = None) -> dict:
    args = ["rest", "--method", method, "--uri", uri]
    body_path = ""
    if body is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(body, f)
            body_path = f.name
        args += ["--body", f"@{body_path}"]
    try:
        out = az(*args)
    finally:
        if body_path:
            os.unlink(body_path)
    return json.loads(out) if out else {}


def retry(action, what: str, attempts: int = 12, delay: int = 15):
    error = ""
    for attempt in range(1, attempts + 1):
        try:
            value = action()
            if value:
                return value
            error = "not available yet"
        except RuntimeError as exc:
            error = str(exc).splitlines()[-1]
        if attempt < attempts:
            print(f"  {what}: {error}; retrying in {delay}s [{attempt}/{attempts}]")
            time.sleep(delay)
    raise SystemExit(f"Gave up waiting for {what}: {error}")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _load_azd_env() -> None:
    """When run directly (not as an azd hook), read the values from the azd environment."""
    if "AZURE_RESOURCE_GROUP" in os.environ or shutil.which("azd") is None:
        return
    result = subprocess.run(
        ["azd", "env", "get-values"], capture_output=True, text=True, shell=os.name == "nt",
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _gateway_uri(subscription_id: str, resource_group: str, gateway: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Web/connectorGateways/{gateway}"
    )


def connection_status(connection_id: str) -> str:
    connection = az_rest("get", f"https://management.azure.com{connection_id}?api-version={API_VERSION}")
    return connection.get("properties", {}).get("overallStatus", "Unknown")


def _signed_in_user() -> tuple[str, str]:
    tenant_id = _env("AZURE_TENANT_ID") or az("account", "show", "--query", "tenantId", "-o", "tsv")
    object_id = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    return tenant_id, object_id


def confirm_consent_code(connection_id: str, code: str) -> None:
    tenant_id, object_id = _signed_in_user()
    az_rest(
        "post", f"https://management.azure.com{connection_id}/confirmConsentCode?api-version={API_VERSION}",
        {"code": code, "objectId": object_id, "tenantId": tenant_id},
    )


def consent_link(connection_id: str) -> str:
    tenant_id, object_id = _signed_in_user()
    body = {
        "parameters": [{
            "parameterName": "token",
            "redirectUrl": CONSENT_REDIRECT,
            "objectId": object_id,
            "tenantId": tenant_id,
        }]
    }
    result = az_rest(
        "post", f"https://management.azure.com{connection_id}/listConsentLinks?api-version={API_VERSION}", body
    )
    return result["value"][0]["link"]


def trigger_config_body(values: dict[str, str], host: str, key: str) -> dict:
    function_name = values["function"]
    return {
        "properties": {
            "state": "Enabled",
            "description": "New messages in the IT help channel start the Teams IT helpdesk hosted skill.",
            "connectionDetails": {"connectorName": "teams", "connectionName": values["connection"]},
            "operationName": TRIGGER_OPERATION,
            "parameters": [
                {"name": "groupId", "value": values["team"]},
                {"name": "channelId", "value": values["channel"]},
            ],
            "notificationDetails": {
                "callbackUrl": f"https://{host}/runtime/webhooks/connector?functionName={function_name}",
                "httpMethod": "POST",
                "authentication": {"type": "QueryString", "name": "code", "value": key},
            },
            "metadata": {
                "destinationType": "functionApp",
                "functionAppName": values["app"],
                "functionAppResourceGroup": values["resource_group"],
                "functionAppSubscriptionId": values["subscription"],
                "functionName": function_name,
                "recurrenceFrequency": "Minute",
                "recurrenceInterval": "1",
            },
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--consent-link", action="store_true", help="Print the Teams connection sign-in link and exit.")
    parser.add_argument(
        "--confirm-code", metavar="CODE",
        help="Confirm consent with the 'code' query value from the page you were redirected to after signing in.",
    )
    args = parser.parse_args(argv)
    _load_azd_env()

    if _env("TEAMS_CONNECTOR_ENABLED", "true").lower() != "true":
        print("Teams connector is disabled (ENABLE_TEAMS_CONNECTOR=false). Skipping the Teams trigger setup.")
        return 0

    try:
        values = {
            "subscription": os.environ["AZURE_SUBSCRIPTION_ID"],
            "resource_group": os.environ["AZURE_RESOURCE_GROUP"],
            "app": os.environ["AZURE_FUNCTION_NAME"],
            "gateway": os.environ["TEAMS_CONNECTOR_GATEWAY_NAME"],
            "connection": os.environ["TEAMS_CONNECTION_NAME"],
            "connection_id": os.environ["TEAMS_CONNECTION_ID"],
            "function": os.environ.get("CONNECTOR_TRIGGER_FUNCTION_NAME", "teams_helpdesk"),
            "trigger": os.environ.get("CONNECTOR_TRIGGER_CONFIG_NAME", "teams-helpdesk-new-channel-message"),
            "team": _env("TEAMS_TEAM_ID"),
            "channel": _env("TEAMS_CHANNEL_ID"),
        }
    except KeyError as exc:
        raise SystemExit(f"Missing azd environment value {exc}. Run 'azd provision' first.") from exc

    if args.consent_link:
        print(consent_link(values["connection_id"]))
        return 0
    if args.confirm_code:
        confirm_consent_code(values["connection_id"], args.confirm_code)
        print(f"Consent confirmed. Connection status: {connection_status(values['connection_id'])}")
        return 0

    status = connection_status(values["connection_id"])
    if status != "Connected":
        print(
            f"Teams connection '{values['connection']}' is not authenticated yet (status: {status}). "
            "Skipping the Teams trigger setup; the HTTP test endpoint works without it.\n"
            "  To connect Teams: run 'python scripts/postdeploy.py --consent-link', open the link and sign in, "
            "then run 'azd hooks run postdeploy'."
        )
        return 0

    if not values["team"] or not values["channel"]:
        print(
            "TEAMS_TEAM_ID and TEAMS_CHANNEL_ID are not set. Skipping the Teams trigger setup.\n"
            "  Run 'azd env set TEAMS_TEAM_ID <team id>', 'azd env set TEAMS_CHANNEL_ID <channel id>', "
            "then 'azd provision' and 'azd hooks run postdeploy'."
        )
        return 0

    print(f"Connecting Teams channel messages to {values['app']}/{values['function']}...")
    host = az(
        "functionapp", "show", "-g", values["resource_group"], "-n", values["app"],
        "--query", "properties.defaultHostName", "-o", "tsv",
    )
    key = retry(
        lambda: az(
            "functionapp", "keys", "list", "-g", values["resource_group"], "-n", values["app"],
            "--query", "systemKeys.connector_extension", "-o", "tsv",
        ),
        "connector_extension key",
    )
    uri = (
        f"{_gateway_uri(values['subscription'], values['resource_group'], values['gateway'])}"
        f"/triggerconfigs/{values['trigger']}?api-version={API_VERSION}"
    )
    retry(
        lambda: az_rest("put", uri, trigger_config_body(values, host, key)),
        "Teams trigger config", attempts=6, delay=20,
    )
    state = az_rest("get", uri).get("properties", {}).get("provisioningState", "unknown")
    print(f"Teams trigger config '{values['trigger']}' is {state}. Post a question in the channel to try it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
