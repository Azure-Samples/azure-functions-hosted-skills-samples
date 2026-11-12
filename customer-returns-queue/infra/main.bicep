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

@description('Message source that starts the hosted skill: Azure Storage queue (storage) or Azure Service Bus queue (servicebus).')
@allowed([
  'storage'
  'servicebus'
])
param queueProvider string = 'storage'

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
var functionAppName = '${abbrs.webSitesFunctions}returns-${resourceToken}'
var foundryAccountName = 'cog-${resourceToken}'
var foundryProjectName = '${foundryAccountName}-proj'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'
var returnsQueueName = 'customer-returns'
var recommendationsContainerName = 'return-recommendations'
var useServiceBus = queueProvider == 'servicebus'
var serviceBusNamespaceName = '${abbrs.serviceBusNamespaces}${resourceToken}'

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
    name: '${abbrs.managedIdentityUserAssignedIdentities}returns-${resourceToken}'
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
        { name: recommendationsContainerName }
      ]
    }
    queueServices: {
      queues: useServiceBus ? [] : [
        { name: returnsQueueName }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

module serviceBus './app/servicebus.bicep' = if (useServiceBus) {
  name: 'serviceBus'
  scope: rg
  params: {
    namespaceName: serviceBusNamespaceName
    queueName: returnsQueueName
    location: location
    tags: tags
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: principalId
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

// The hosted skill reads its trigger type from RETURNS_TRIGGER_TYPE and its connection
// from the RETURNS_QUEUE_CONNECTION__* setting collection, so the same agent file works
// with either message source.
var storageTriggerSettings = {
  RETURNS_TRIGGER_TYPE: 'queue_trigger'
  RETURNS_QUEUE_CONNECTION__queueServiceUri: 'https://${storage.outputs.name}.queue.${environment().suffixes.storage}'
  RETURNS_QUEUE_CONNECTION__credential: 'managedidentity'
  RETURNS_QUEUE_CONNECTION__clientId: identityClientId
}

var serviceBusTriggerSettings = {
  RETURNS_TRIGGER_TYPE: 'service_bus_queue_trigger'
  RETURNS_QUEUE_CONNECTION__fullyQualifiedNamespace: '${serviceBusNamespaceName}.servicebus.windows.net'
  RETURNS_QUEUE_CONNECTION__credential: 'managedidentity'
  RETURNS_QUEUE_CONNECTION__clientId: identityClientId
}

module api './app/api.bicep' = {
  name: 'api'
  scope: rg
  // Create role assignments before the app so they have time to propagate before
  // the host starts and azd uploads the deployment package with the managed identity.
  dependsOn: [
    serviceBus
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
    appSettings: union(
      {
        AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry'
        FOUNDRY_PROJECT_ENDPOINT: foundry.outputs.projectEndpoint
        FOUNDRY_MODEL: foundry.outputs.modelDeploymentName
        AZURE_CLIENT_ID: identityClientId
        RECOMMENDATIONS_CONTAINER: recommendationsContainerName
        ENABLE_MULTIPLATFORM_BUILD: 'true'
        PYTHON_ENABLE_INIT_INDEXING: '1'
      },
      useServiceBus ? serviceBusTriggerSettings : storageTriggerSettings
    )
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
output QUEUE_PROVIDER string = queueProvider
output RETURNS_QUEUE_NAME string = returnsQueueName
output SERVICEBUS_FULLY_QUALIFIED_NAMESPACE string = useServiceBus ? '${serviceBusNamespaceName}.servicebus.windows.net' : ''
output RECOMMENDATIONS_CONTAINER string = recommendationsContainerName
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL string = foundry.outputs.modelDeploymentName
