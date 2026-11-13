// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources. Must support Azure Functions Flex Consumption and the default Microsoft Foundry gpt-5.4 Global Standard deployment.')
@allowed([
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'centralus'
  'eastus'
  'eastus2'
  'northcentralus'
  'southcentralus'
  'westus'
  'westus3'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Deploy the connector gateway, the Microsoft Teams connection, and the Teams reply MCP server. Creating them does not need consent; the connection must still be authorized once before Teams messages flow.')
param enableTeamsConnector bool = true

@description('Connector gateway location. Defaults to westcentralus, where connector trigger configs are available during the preview.')
param connectorGatewayLocation string = 'westcentralus'

@description('Team (Microsoft 365 group) ID of the helpdesk channel. Leave empty until the connection is authorized.')
param teamsTeamId string = ''

@description('Channel ID of the helpdesk channel, for example 19:...@thread.tacv2. Leave empty until the connection is authorized.')
param teamsChannelId string = ''

@description('Microsoft Foundry model deployment name.')
param foundryModel string = 'gpt-5.4'

@description('Microsoft Foundry model name.')
param foundryModelName string = 'gpt-5.4'

@description('Microsoft Foundry model version.')
param foundryModelVersion string = '2026-03-05'

@description('Microsoft Foundry deployment capacity.')
param foundryDeploymentCapacity int = 50

@description('Principal ID of the user running azd. Used to grant access for local runs and the demo script.')
param principalId string = deployer().objectId

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = '${abbrs.webSitesFunctions}helpdesk-${resourceToken}'
var foundryAccountName = 'cog-${resourceToken}'
var foundryProjectName = '${foundryAccountName}-proj'
var connectorGatewayName = 'cg-${resourceToken}'
var teamsConnectionName = 'teams'
var teamsMcpServerConfigName = 'teams-helpdesk-reply'
var connectorTriggerFunctionName = 'teams_helpdesk'
var connectorTriggerConfigName = 'teams-helpdesk-new-channel-message'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'
var helpdeskContainerName = 'helpdesk'
var teamsChannelConfigured = enableTeamsConnector && !empty(teamsTeamId) && !empty(teamsChannelId)

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module apiUserAssignedIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'apiUserAssignedIdentity'
  scope: rg
  params: {
    location: location
    tags: tags
    name: '${abbrs.managedIdentityUserAssignedIdentities}helpdesk-${resourceToken}'
  }
}

module foundry './app/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    accountName: foundryAccountName
    projectName: foundryProjectName
    location: location
    tags: tags
    modelDeploymentName: foundryModel
    modelName: foundryModelName
    modelVersion: foundryModelVersion
    deploymentCapacity: foundryDeploymentCapacity
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: principalId
  }
}

module teamsConnector './app/connector-gateway.bicep' = if (enableTeamsConnector) {
  name: 'teamsConnector'
  scope: rg
  params: {
    connectorGatewayName: connectorGatewayName
    connectionName: teamsConnectionName
    mcpServerConfigName: teamsMcpServerConfigName
    location: connectorGatewayLocation
    tags: tags
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    deployerPrincipalId: principalId
    tenantId: tenant().tenantId
    teamId: teamsTeamId
    channelId: teamsChannelId
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'storage'
  scope: rg
  params: {
    name: '${abbrs.storageStorageAccounts}${resourceToken}'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [
        { name: deploymentStorageContainerName }
        { name: helpdeskContainerName }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

module appServicePlan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: 'appserviceplan'
  scope: rg
  params: {
    name: '${abbrs.webServerFarms}${resourceToken}'
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
    reserved: true
    location: location
    tags: tags
  }
}

var identityClientId = apiUserAssignedIdentity.outputs.clientId

module api './app/api.bicep' = {
  name: 'api'
  scope: rg
  // Create role assignments before the app so they have time to propagate before
  // the host starts and azd uploads the deployment package with the managed identity.
  dependsOn: [
    rbac
  ]
  params: {
    name: functionAppName
    location: location
    tags: tags
    applicationInsightsName: monitoring.outputs.name
    appServicePlanId: appServicePlan.outputs.resourceId
    runtimeName: 'python'
    runtimeVersion: '3.13'
    storageAccountName: storage.outputs.name
    deploymentStorageContainerName: deploymentStorageContainerName
    identityId: apiUserAssignedIdentity.outputs.resourceId
    identityClientId: identityClientId
    appSettings: {
      AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry'
      FOUNDRY_PROJECT_ENDPOINT: foundry.outputs.projectEndpoint
      FOUNDRY_MODEL: foundry.outputs.modelDeploymentName
      AZURE_CLIENT_ID: identityClientId
      HELPDESK_CONTAINER: helpdeskContainerName
      // Empty until a channel is configured: the skill then records replies in Blob
      // Storage only, and mcp.json skips the unresolved Teams server.
      TEAMS_MCP_SERVER_URL: teamsChannelConfigured ? teamsConnector!.outputs.mcpEndpointUrl : ''
      TEAMS_MCP_CLIENT_ID: identityClientId
      TEAMS_TEAM_ID: teamsTeamId
      TEAMS_CHANNEL_ID: teamsChannelId
      ENABLE_MULTIPLATFORM_BUILD: 'true'
      PYTHON_ENABLE_INIT_INDEXING: '1'
    }
  }
}

module rbac './app/rbac.bicep' = {
  name: 'rbacAssignments'
  scope: rg
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: principalId
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.7.0' = {
  name: '${uniqueString(deployment().name, location)}-loganalytics'
  scope: rg
  params: {
    name: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    location: location
    tags: tags
    dataRetention: 30
  }
}

module monitoring 'br/public:avm/res/insights/component:0.4.1' = {
  name: '${uniqueString(deployment().name, location)}-appinsights'
  scope: rg
  params: {
    name: '${abbrs.insightsComponents}${resourceToken}'
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
    disableLocalAuth: true
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.name
output HELPDESK_CONTAINER string = helpdeskContainerName
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL string = foundry.outputs.modelDeploymentName
output TEAMS_CONNECTOR_ENABLED bool = enableTeamsConnector
output TEAMS_CONNECTOR_GATEWAY_NAME string = enableTeamsConnector ? teamsConnector!.outputs.connectorGatewayName : ''
output TEAMS_CONNECTION_NAME string = enableTeamsConnector ? teamsConnector!.outputs.connectionName : ''
output TEAMS_CONNECTION_ID string = enableTeamsConnector ? teamsConnector!.outputs.connectionId : ''
output TEAMS_MCP_SERVER_CONFIG_NAME string = enableTeamsConnector ? teamsConnector!.outputs.mcpServerConfigName : ''
output TEAMS_MCP_SERVER_URL string = teamsChannelConfigured ? teamsConnector!.outputs.mcpEndpointUrl : ''
output CONNECTOR_TRIGGER_FUNCTION_NAME string = connectorTriggerFunctionName
output CONNECTOR_TRIGGER_CONFIG_NAME string = connectorTriggerConfigName
