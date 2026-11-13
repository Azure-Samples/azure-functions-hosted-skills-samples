param accountName string
param projectName string
param location string = resourceGroup().location
param tags object = {}
param modelDeploymentName string = 'gpt-5.4'
param modelName string = 'gpt-5.4'
param modelVersion string = '2026-03-05'
param deploymentCapacity int = 50
@description('Deployment name for Microsoft-Decision-1, the routing model the Event Hubs function calls. Cannot contain the reserved word Microsoft.')
param decisionDeploymentName string = 'decision-1'
param decisionModelVersion string = '1'
param decisionDeploymentCapacity int = 50
param managedIdentityPrincipalId string
@description('Optional user principal (the deployer) that can call the models when running the app locally.')
param userPrincipalId string = ''

var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  tags: tags
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: foundryAccount
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'Alert investigation hosted skill sample project'
  }
  // The account accepts one child operation at a time; parallel creation fails with RequestConflict.
  dependsOn: [
    decisionModelDeployment
  ]
}

resource foundryModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundryAccount
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: deploymentCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// Microsoft-Decision-1 returns a typed decision (choice + per-option probabilities) instead of
// generated text. The Event Hubs function uses it to route alerts cheaply at high volume.
resource decisionModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundryAccount
  name: decisionDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: decisionDeploymentCapacity
  }
  properties: {
    model: {
      format: 'Microsoft'
      name: 'Microsoft-Decision-1'
      version: decisionModelVersion
    }
  }
  dependsOn: [
    foundryModelDeployment
  ]
}

resource foundryCognitiveServicesUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, managedIdentityPrincipalId, cognitiveServicesUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryOpenAiUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, managedIdentityPrincipalId, cognitiveServicesOpenAiUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryUserOpenAiUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(userPrincipalId)) {
  name: guid(foundryAccount.id, userPrincipalId, cognitiveServicesOpenAiUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: userPrincipalId
    principalType: 'User'
  }
}

resource foundryUserCognitiveServicesUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(userPrincipalId)) {
  name: guid(foundryAccount.id, userPrincipalId, cognitiveServicesUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: userPrincipalId
    principalType: 'User'
  }
}

output accountName string = foundryAccount.name
output projectName string = foundryProject.name
output projectEndpoint string = '${foundryAccount.properties.endpoints['AI Foundry API']}api/projects/${foundryProject.name}'
output accountEndpoint string = foundryAccount.properties.endpoint
output modelDeploymentName string = foundryModelDeployment.name
output decisionDeploymentName string = decisionModelDeployment.name
