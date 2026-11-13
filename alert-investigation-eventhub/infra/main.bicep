targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources. Must support Azure Functions Flex Consumption and Global Standard deployments of gpt-5.4 and Microsoft-Decision-1 in Microsoft Foundry.')
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

@description('Microsoft Foundry model deployment name for the investigation hosted skill.')
param foundryModel string = 'gpt-5.4'

@description('Microsoft Foundry model name.')
param foundryModelName string = 'gpt-5.4'

@description('Microsoft Foundry model version.')
param foundryModelVersion string = '2026-03-05'

@description('Microsoft Foundry deployment capacity.')
param foundryDeploymentCapacity int = 50

@description('Deployment name for Microsoft-Decision-1 used by the Event Hubs router. Deployment names cannot contain the reserved word Microsoft.')
param decisionDeploymentName string = 'decision-1'

@description('Microsoft-Decision-1 deployment capacity. Each unit allows 1 request and 1,000 tokens per minute; usage is billed per call, not per unit. Set with azd env set DECISION_CAPACITY.')
param decisionDeploymentCapacity int = 50

@description('Principal ID of the user running azd. Used to grant access for local runs and the demo script.')
param principalId string = deployer().objectId

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = '${abbrs.webSitesFunctions}alerts-${resourceToken}'
var foundryAccountName = 'cog-${resourceToken}'
var foundryProjectName = '${foundryAccountName}-proj'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'
var eventHubNamespaceName = '${abbrs.eventHubNamespaces}${resourceToken}'
var eventHubName = 'alerts'
var consumerGroupName = 'alert-router'
var localConsumerGroupName = 'local-dev'
var investigateQueueName = 'alerts-investigate'
var reviewQueueName = 'alerts-review'
var investigationsContainerName = 'investigations'
var decisionsTableName = 'alertdecisions'
var fingerprintsTableName = 'alertfingerprints'

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
    name: '${abbrs.managedIdentityUserAssignedIdentities}alerts-${resourceToken}'
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
    decisionDeploymentName: decisionDeploymentName
    decisionDeploymentCapacity: decisionDeploymentCapacity
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: principalId
  }
}

module eventHubs './app/eventhubs.bicep' = {
  name: 'eventHubs'
  scope: rg
  params: {
    namespaceName: eventHubNamespaceName
    eventHubName: eventHubName
    consumerGroupName: consumerGroupName
    localConsumerGroupName: localConsumerGroupName
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
        { name: investigationsContainerName }
      ]
    }
    queueServices: {
      queues: [
        { name: investigateQueueName }
        { name: reviewQueueName }
      ]
    }
    tableServices: {
      tables: [
        { name: decisionsTableName }
        { name: fingerprintsTableName }
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
      ENABLE_MULTIPLATFORM_BUILD: 'true'
      PYTHON_ENABLE_INIT_INDEXING: '1'
      // Stage 1: Event Hubs router (code) with Microsoft-Decision-1.
      EVENTHUB__fullyQualifiedNamespace: eventHubs.outputs.fullyQualifiedNamespace
      EVENTHUB__credential: 'managedidentity'
      EVENTHUB__clientId: identityClientId
      EVENTHUB_NAME: eventHubs.outputs.eventHubName
      EVENTHUB_CONSUMER_GROUP: eventHubs.outputs.consumerGroupName
      DECISION_ENDPOINT: foundry.outputs.accountEndpoint
      DECISION_DEPLOYMENT: foundry.outputs.decisionDeploymentName
      DECISION_MIN_CONFIDENCE: '0.5'
      DEDUPE_WINDOW_MINUTES: '60'
      INVESTIGATE_QUEUE: investigateQueueName
      REVIEW_QUEUE: reviewQueueName
      DECISIONS_TABLE: decisionsTableName
      FINGERPRINTS_TABLE: fingerprintsTableName
      // Stage 2: investigation hosted skill.
      INVESTIGATIONS_CONTAINER: investigationsContainerName
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
output EVENTHUB_FULLY_QUALIFIED_NAMESPACE string = eventHubs.outputs.fullyQualifiedNamespace
output EVENTHUB_NAME string = eventHubs.outputs.eventHubName
output EVENTHUB_CONSUMER_GROUP string = eventHubs.outputs.consumerGroupName
output EVENTHUB_LOCAL_CONSUMER_GROUP string = eventHubs.outputs.localConsumerGroupName
output INVESTIGATE_QUEUE string = investigateQueueName
output REVIEW_QUEUE string = reviewQueueName
output INVESTIGATIONS_CONTAINER string = investigationsContainerName
output DECISIONS_TABLE string = decisionsTableName
output FINGERPRINTS_TABLE string = fingerprintsTableName
output DECISION_ENDPOINT string = foundry.outputs.accountEndpoint
output DECISION_DEPLOYMENT string = foundry.outputs.decisionDeploymentName
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL string = foundry.outputs.modelDeploymentName
