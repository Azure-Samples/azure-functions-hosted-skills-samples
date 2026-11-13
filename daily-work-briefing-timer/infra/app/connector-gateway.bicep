// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// Optional (enableOffice365=true): an Office 365 Outlook connection behind a Connector Gateway,
// exposed to the hosted skill as a read-only MCP server with two operations:
// GetEventsCalendarViewV3 (today's meetings) and GetEmailsV3 (unread mail).
// Someone must sign in to the connection once after deployment (see README).
param connectorGatewayName string
param connectionName string = 'office365-outlook'
param mcpServerConfigName string = 'Office-365-Outlook-briefing-read-only'
param location string = resourceGroup().location
param tags object = {}
param managedIdentityPrincipalId string
param deployerPrincipalId string
param tenantId string

resource connectorGateway 'Microsoft.Web/connectorGateways@2026-05-01-preview' = {
  name: connectorGatewayName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource office365Connection 'Microsoft.Web/connectorGateways/connections@2026-05-01-preview' = {
  parent: connectorGateway
  name: connectionName
  properties: {
    connectorName: 'office365'
    displayName: 'Office 365 Outlook Connection'
  }
}

// The function app's managed identity calls the MCP server.
resource office365ConnectionAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = {
  parent: office365Connection
  name: managedIdentityPrincipalId
  properties: {
    principal: {
      type: 'ActiveDirectory'
      identity: {
        objectId: managedIdentityPrincipalId
        tenantId: tenantId
      }
    }
  }
}

// The deployer can call it too, for local runs.
resource office365ConnectionDeployerAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = if (!empty(deployerPrincipalId)) {
  parent: office365Connection
  name: deployerPrincipalId
  properties: {
    principal: {
      type: 'ActiveDirectory'
      identity: {
        objectId: deployerPrincipalId
        tenantId: tenantId
      }
    }
  }
}

resource office365McpServerConfig 'Microsoft.Web/connectorGateways/mcpserverconfigs@2026-05-01-preview' = {
  parent: connectorGateway
  name: mcpServerConfigName
  properties: {
    state: 'Enabled'
    description: 'Read-only Office 365 Outlook calendar and mail for the daily work briefing sample.'
    connectors: [
      {
        name: 'office365'
        connectionName: office365Connection.name
        displayName: 'Office 365 Outlook'
        description: ''
        operations: [
          {
            name: 'GetEventsCalendarViewV3'
            displayName: 'Get calendar view of events (V3)'
            description: 'Gets all events, including instances of recurring events, in a calendar between two UTC times.'
            userParameters: []
            agentParameters: [
              {
                name: 'calendarId'
                schema: {
                  type: 'string'
                  description: 'Calendar to read. Use Calendar for the default calendar.'
                }
              }
              {
                name: 'startDateTimeUtc'
                schema: {
                  type: 'string'
                  description: 'Start time in UTC, for example 2026-01-01T05:00:00.'
                }
              }
              {
                name: 'endDateTimeUtc'
                schema: {
                  type: 'string'
                  description: 'End time in UTC, for example 2026-01-02T05:00:00.'
                }
              }
            ]
          }
          {
            name: 'GetEmailsV3'
            displayName: 'Get emails (V3)'
            description: 'Gets emails from a mail folder.'
            userParameters: []
            agentParameters: [
              {
                name: 'folderPath'
                schema: {
                  type: 'string'
                  description: 'Mail folder to read. Default Inbox.'
                }
              }
              {
                name: 'fetchOnlyUnread'
                schema: {
                  type: 'boolean'
                  description: 'Return only unread email.'
                }
              }
              {
                name: 'importance'
                schema: {
                  type: 'string'
                  description: 'Any, High, Normal, or Low.'
                }
              }
              {
                name: 'top'
                schema: {
                  type: 'integer'
                  description: 'Maximum number of emails to return (25 is plenty for a briefing).'
                }
              }
            ]
          }
        ]
      }
    ]
    policies: []
    settings: {
      textOnlyContent: true
    }
  }
}

output connectorGatewayName string = connectorGateway.name
output connectionId string = office365Connection.id
output mcpEndpointUrl string = office365McpServerConfig.properties.mcpEndpointUrl
