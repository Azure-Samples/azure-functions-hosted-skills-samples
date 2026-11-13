// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// Durable Task Scheduler: the managed backend that stores orchestration state for the
// contract review process. The function app reaches it with its user-assigned managed identity.
param schedulerName string
param taskHubName string
param location string = resourceGroup().location
param tags object = {}

@allowed([
  'Consumption'
  'Dedicated'
])
param skuName string = 'Consumption'

param managedIdentityPrincipalId string
param userPrincipalId string = ''

// Durable Task Data Contributor
var durableTaskDataContributorRoleId = '0ad04412-c4d5-4796-b79c-f76d14c8d402'

resource scheduler 'Microsoft.DurableTask/schedulers@2025-11-01' = {
  name: schedulerName
  location: location
  tags: tags
  properties: {
    ipAllowlist: [
      '0.0.0.0/0'
    ]
    sku: skuName == 'Dedicated' ? { name: 'Dedicated', capacity: 1 } : { name: 'Consumption' }
  }
}

resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2025-11-01' = {
  parent: scheduler
  name: taskHubName
}

resource appRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(scheduler.id, managedIdentityPrincipalId, durableTaskDataContributorRoleId)
  scope: scheduler
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', durableTaskDataContributorRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Lets the deployer open the Durable Task Scheduler dashboard to inspect orchestrations.
resource userRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(userPrincipalId)) {
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
