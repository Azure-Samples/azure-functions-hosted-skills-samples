param name string
param taskHubName string
param location string = resourceGroup().location
param tags object = {}
param managedIdentityPrincipalId string
@description('Optional user principal (the deployer) that can view workflow runs in the dashboard and run the app locally against this scheduler.')
param userPrincipalId string = ''

var durableTaskDataContributorRoleId = '0ad04412-c4d5-4796-b79c-f76d14c8d402'

resource scheduler 'Microsoft.DurableTask/schedulers@2025-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    ipAllowlist: [
      '0.0.0.0/0'
    ]
    sku: {
      name: 'Consumption'
    }
  }
}

// The scheduler can report Succeeded while it is still finishing provisioning, and a task
// hub created in that window fails with OperationPending. Creating the role assignments
// first gives the scheduler time to settle.
resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2025-11-01' = {
  parent: scheduler
  name: taskHubName
  dependsOn: [
    appRole
    userRole
  ]
}

resource appRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(scheduler.id, managedIdentityPrincipalId, durableTaskDataContributorRoleId)
  scope: scheduler
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', durableTaskDataContributorRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource userRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(userPrincipalId)) {
  name: guid(scheduler.id, userPrincipalId, durableTaskDataContributorRoleId)
  scope: scheduler
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', durableTaskDataContributorRoleId)
    principalId: userPrincipalId
    principalType: 'User'
  }
}

output name string = scheduler.name
output endpoint string = scheduler.properties.endpoint
output taskHubName string = taskHub.name
output dashboardUrl string = taskHub.properties.dashboardUrl
