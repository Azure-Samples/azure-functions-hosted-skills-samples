// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources. Must support Azure Functions Flex Consumption, Durable Task Scheduler, and the default Microsoft Foundry gpt-5.4 Global Standard deployment.')
@allowed([
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'centralus'
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

@description('Claims with an estimated payout above this amount are never auto-approved.')
param autoApproveLimit string = '10000'

@description('Microsoft Foundry model deployment name.')
param foundryModel string = 'gpt-5.4'

@description('Microsoft Foundry model name.')
param foundryModelName string = 'gpt-5.4'

@description('Microsoft Foundry model version.')
param foundryModelVersion string = '2026-03-05'

@description('Microsoft Foundry deployment capacity.')
param foundryDeploymentCapacity int = 50

@description('Principal ID of the user running azd. Used to grant access for local runs, the demo script, and the Durable Task Scheduler dashboard.')
param principalId string = deployer().objectId

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = '${abbrs.webSitesFunctions}claims-${resourceToken}'
var foundryAccountName = 'cog-${resourceToken}'
var foundryProjectName = '${foundryAccountName}-proj'
var schedulerName = 'dts-${resourceToken}'
var taskHubName = 'claims'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'
var inboxContainerName = 'claims-inbox'
var attachmentsContainerName = 'claims-attachments'
var decisionsContainerName = 'claims-decisions'
var inboxSystemTopicName = 'evgt-claims-${resourceToken}'

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
    name: '${abbrs.managedIdentityUserAssignedIdentities}claims-${resourceToken}'
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

// Dynamic Workflows run as Durable orchestrations. Durable Task Scheduler stores their
// state and gives a dashboard with per-task status for every claim.
module durableTaskScheduler './app/dts.bicep' = {
  name: 'durableTaskScheduler'
  scope: rg
  params: {
    name: schedulerName
    taskHubName: taskHubName
    location: location
    tags: tags
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: principalId
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
        { name: inboxContainerName }
        { name: attachmentsContainerName }
        { name: decisionsContainerName }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

// Flex Consumption runs blob triggers through Event Grid. The subscription that points
// this topic at the function's blob webhook is created by scripts/postdeploy.py, because
// the webhook key exists only after the app code is deployed.
module eventGrid './app/eventgrid.bicep' = {
  name: 'eventGrid'
  scope: rg
  params: {
    name: inboxSystemTopicName
    location: location
    tags: tags
    storageAccountName: storage.outputs.name
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
      DURABLE_TASK_SCHEDULER_CONNECTION_STRING: 'Endpoint=${durableTaskScheduler.outputs.endpoint};Authentication=ManagedIdentity;ClientID=${identityClientId}'
      TASKHUB_NAME: durableTaskScheduler.outputs.taskHubName
      CLAIMS_BLOB_SOURCE: 'EventGrid'
      CLAIMS_INBOX_CONTAINER: inboxContainerName
      CLAIMS_ATTACHMENTS_CONTAINER: attachmentsContainerName
      CLAIMS_DECISIONS_CONTAINER: decisionsContainerName
      CLAIMS_AUTO_APPROVE_LIMIT: autoApproveLimit
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
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.name
output CLAIMS_INBOX_CONTAINER string = inboxContainerName
output CLAIMS_ATTACHMENTS_CONTAINER string = attachmentsContainerName
output CLAIMS_DECISIONS_CONTAINER string = decisionsContainerName
output CLAIMS_AUTO_APPROVE_LIMIT string = autoApproveLimit
output CLAIMS_SYSTEM_TOPIC_NAME string = eventGrid.outputs.name
output DURABLE_TASK_SCHEDULER_NAME string = durableTaskScheduler.outputs.name
output DURABLE_TASK_SCHEDULER_ENDPOINT string = durableTaskScheduler.outputs.endpoint
output TASKHUB_NAME string = durableTaskScheduler.outputs.taskHubName
output DTS_DASHBOARD_URL string = durableTaskScheduler.outputs.dashboardUrl
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL string = foundry.outputs.modelDeploymentName
