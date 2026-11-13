// Connector gateway with a Microsoft Teams connection and an MCP server that exposes
// exactly one action: reply in a thread of the configured channel.
//
// Creating these resources does not need consent. The connection stays in an
// "Unauthenticated" state until a person signs in to Teams once (see README).

param connectorGatewayName string
param connectionName string = 'teams'
param mcpServerConfigName string = 'teams-helpdesk-reply'
param location string = resourceGroup().location
param tags object = {}
param managedIdentityPrincipalId string
param deployerPrincipalId string
param tenantId string

@description('Team (Microsoft 365 group) ID the helpdesk replies in. The MCP tool can only post to this team.')
param teamId string = ''

@description('Channel ID the helpdesk replies in, for example 19:...@thread.tacv2. The MCP tool can only post to this channel.')
param channelId string = ''

resource connectorGateway 'Microsoft.Web/connectorGateways@2026-05-01-preview' = {
  name: connectorGatewayName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource teamsConnection 'Microsoft.Web/connectorGateways/connections@2026-05-01-preview' = {
  parent: connectorGateway
  name: connectionName
  properties: {
    connectorName: 'teams'
    displayName: 'Microsoft Teams (IT helpdesk)'
  }
}

// The function app's managed identity calls the MCP server with this connection.
resource appAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = {
  parent: teamsConnection
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

// The deployer can check the connection and create the trigger config.
resource deployerAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = if (!empty(deployerPrincipalId)) {
  parent: teamsConnection
  name: empty(deployerPrincipalId) ? 'deployer' : deployerPrincipalId
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

// The gateway polls Teams for new channel messages with its own identity.
resource gatewayAccessPolicy 'Microsoft.Web/connectorGateways/connections/accessPolicies@2026-05-01-preview' = {
  parent: teamsConnection
  name: 'connectorGateway-msi'
  properties: {
    principal: {
      type: 'ActiveDirectory'
      identity: {
        objectId: connectorGateway.identity.principalId
        tenantId: tenantId
      }
    }
  }
}

resource replyMcpServerConfig 'Microsoft.Web/connectorGateways/mcpserverconfigs@2026-05-01-preview' = {
  parent: connectorGateway
  name: mcpServerConfigName
  properties: {
    state: 'Enabled'
    description: 'Replies in a thread of the IT helpdesk Teams channel. Team, channel, and poster are fixed by the deployment.'
    connectors: [
      {
        name: 'teams'
        connectionName: teamsConnection.name
        displayName: 'Microsoft Teams'
        description: ''
        operations: [
          {
            name: 'ReplyWithMessageToConversation'
            displayName: 'Reply in the helpdesk channel thread'
            description: 'Posts an HTML reply under one message in the IT helpdesk channel.'
            // Fixed by the deployment, not chosen by the model: the skill can only reply
            // in the configured channel, as the Flow bot.
            userParameters: [
              {
                name: 'poster'
                value: 'Flow bot'
              }
              {
                name: 'location'
                value: 'Channel'
              }
              {
                name: 'body/recipient'
                value: {
                  groupId: teamId
                  channelId: channelId
                }
              }
            ]
            agentParameters: [
              {
                name: 'body/parentMessageId'
                schema: {
                  type: 'string'
                  description: 'ID of the employee\'s channel message to reply to (the "id" field of the trigger message).'
                }
              }
              {
                name: 'body/messageBody'
                schema: {
                  type: 'string'
                  format: 'html'
                  description: 'HTML reply body. Use <p>, <ul>, <li>, <a>, and <b> only.'
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
output connectorGatewayPrincipalId string = connectorGateway.identity.principalId
output connectionName string = teamsConnection.name
output connectionId string = teamsConnection.id
output mcpServerConfigName string = replyMcpServerConfig.name
output mcpEndpointUrl string = replyMcpServerConfig.properties.mcpEndpointUrl
